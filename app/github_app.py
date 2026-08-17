"""GitHub App authentication, PR diff fetching, and comment upsert.

Narrow responsibility: authenticate as the GitHub App installation (JWT ->
short-lived installation token via PyGithub's ``Auth`` API), fetch a PR's
unified diff, and find-or-create-or-edit the bot's single PR comment.

Knows nothing about LLMs, orchestration, or diff annotation/truncation —
those belong to ``orchestrator.py`` / ``diff_utils.py``.
"""

from __future__ import annotations

import base64
import binascii

from github import Auth, Github, GithubException
from github.IssueComment import IssueComment

from app.config import settings

# Hidden marker used to find the bot's own comment across re-reviews so a
# `synchronize` event edits it in place instead of spamming a new one.
COMMENT_MARKER = "<!-- ai-code-review-bot -->"

# Sub-marker delimiting an optional failure footnote appended below a preserved
# good review. Idempotent: a new footnote replaces any prior block, and a later
# successful review's full-body overwrite (via upsert_comment) removes it.
FAIL_NOTE_START = "<!-- ai-review-fail-note -->"
FAIL_NOTE_END = "<!-- /ai-review-fail-note -->"

# Sub-marker delimiting the self-cleaning "re-review scheduled" notice shown
# while a cooldown/rate-limit wait is pending. NOT mutually exclusive with
# FAIL_NOTE_* by ticket-state construction alone -- a ticket that hits the
# failure ceiling (fail note posted) and is then pushed can briefly carry
# both footnote kinds on GitHub at once. What actually guarantees only one
# is ever visible is _strip_existing_footnote recognizing both marker pairs:
# whichever footnote-writing function runs next cleans up the other kind.
SCHEDULE_NOTE_START = "<!-- ai-review-schedule-note -->"
SCHEDULE_NOTE_END = "<!-- /ai-review-schedule-note -->"


def _is_bot_comment(comment: IssueComment) -> bool:
    """True if authored by a GitHub App bot (not a human), so a human quoting
    the marker is never mistaken for the bot's own comment."""
    return getattr(comment.user, "type", None) == "Bot"


def _find_bot_comment(repo, pr, comment_id: int | None) -> IssueComment | None:
    """Locate the bot's own comment: by stored id first (trusted — we created it),
    else an author-filtered marker scan (bot-authored AND our marker). Returns
    None if neither finds one, so the caller creates a fresh marker comment."""
    if comment_id is not None:
        try:
            headers, data = repo.requester.requestJsonAndCheck(
                "GET", f"/repos/{repo.full_name}/issues/comments/{comment_id}"
            )
            return IssueComment(repo.requester, headers, data, completed=True)
        except GithubException:
            pass  # deleted/unknown id -> fall back to the scan
    for comment in pr.get_issue_comments():
        if _is_bot_comment(comment) and COMMENT_MARKER in comment.body:
            return comment
    return None


_FOOTNOTE_MARKERS = (
    (FAIL_NOTE_START, FAIL_NOTE_END),
    (SCHEDULE_NOTE_START, SCHEDULE_NOTE_END),
)


def _strip_existing_footnote(body: str) -> str:
    """Strip whichever known footnote block (failure note or schedule note) is
    present as a well-formed TRAILING block, if any.

    Deliberately NOT a regex-from-first-marker-to-next-marker scan: a
    specialist's finding text could plausibly quote a literal marker string
    (this very file contains both), which would make a "first START to next
    END" match span from that stray marker all the way to the real trailing
    footnote's END, deleting genuine review content in between. Instead: for
    each known marker pair, find the LAST occurrence of its START and only
    treat it as a real footnote to strip if the body actually ends with its
    END -- any earlier/unmatched occurrence is left alone as incidental
    review text. Trying both pairs means whichever footnote-writing function
    runs next cleans up a stale leftover of the OTHER kind too, so the two
    kinds can never both be visible at once even if an earlier cleanup step
    failed.
    """
    stripped = body.rstrip()
    for start, end in _FOOTNOTE_MARKERS:
        idx = stripped.rfind(start)
        if idx != -1 and stripped.endswith(end):
            return stripped[:idx].rstrip()
    return stripped


def _read_private_key() -> str:
    """Decode the base64-encoded App private key. Never logged."""
    try:
        return base64.b64decode(settings.github_app_private_key, validate=True).decode()
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "GITHUB_APP_PRIVATE_KEY is not valid base64 -- encode the PEM with: "
            "uv run python -m scripts.encode_credential github-app-private-key.pem"
        ) from exc


def get_installation_auth() -> Auth.AppInstallationAuth:
    """Build the installation-level auth object (JWT -> installation token).

    Uses ``Auth.AppAuth`` (signs a JWT with the App's private key) wrapped in
    ``Auth.AppInstallationAuth``, which PyGithub transparently exchanges for
    a short-lived installation access token and refreshes as needed. This is
    NOT a JWT-only auth and NOT a personal-access-token auth.
    """
    app_auth = Auth.AppAuth(settings.github_app_id, _read_private_key())
    return app_auth.get_installation_auth(settings.github_app_installation_id)


def get_installation_client() -> Github:
    """Build a ``Github`` client authenticated as the App installation."""
    return Github(auth=get_installation_auth())


def _app_jwt_client() -> Github:
    """Client authenticated as the App itself (JWT), for App-level endpoints."""
    return Github(auth=Auth.AppAuth(settings.github_app_id, _read_private_key()))


class AppNotInstalledError(RuntimeError):
    """The App is not installed on the target repo (GitHub returned 404).

    Subclasses RuntimeError so existing callers and tests that catch
    RuntimeError keep working; the distinct type exists so a caller can branch
    on "not installed" without matching on message text.
    """


def discover_installation_id(repo_full_name: str) -> int:
    """Return the installation id for the App on `repo_full_name` (App JWT).

    Raises `AppNotInstalledError` (with an actionable message) if the App is
    not installed -- GitHub does not permit an App to install itself; a repo
    admin must authorize it once in the GitHub UI. Only a 404 is interpreted
    this way -- any other status (e.g. a 401 from a malformed
    GITHUB_APP_PRIVATE_KEY, or a transient 5xx) raises a plain
    RuntimeError instead, so a genuine auth/API error is never misdiagnosed as
    a missing installation. `AppNotInstalledError` subclasses RuntimeError, so
    a caller that only catches RuntimeError still sees both cases, but one
    that wants to branch on "not installed" specifically (as
    check_installation_and_webhook does) can catch the narrower type first.

    Uses the raw requester (``GET /repos/{repo}/installation``) rather than a
    typed PyGithub method: the installed PyGithub version's ``Repository``
    class has no ``get_installation()`` method for this endpoint.
    """
    gh = _app_jwt_client()
    try:
        _, data = gh.requester.requestJsonAndCheck(
            "GET", f"/repos/{repo_full_name}/installation"
        )
    except GithubException as exc:
        if exc.status == 404:
            raise AppNotInstalledError(
                f"GitHub App is not installed on {repo_full_name}: install it once via the "
                f"GitHub UI (repo Settings -> GitHub Apps), then redeploy. ({exc.status})"
            ) from exc
        raise RuntimeError(
            f"GitHub App installation lookup for {repo_full_name} failed with "
            f"{exc.status} ({exc.data}) -- likely a bad GITHUB_APP_ID or "
            f"GITHUB_APP_PRIVATE_KEY, not a missing App installation."
        ) from exc
    return int(data["id"])


def discover_installation_id_for_app() -> int:
    """Return the App's single installation id (GET /app/installations, App JWT).

    This project's scope (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md)
    is one GitHub account/org per App installation -- so, unlike
    discover_installation_id(repo), this needs no repo to seed the lookup and
    works whether or not GITHUB_TARGET_REPO is configured.

    Raises `AppNotInstalledError` if the App has no installations at all.
    Raises a plain `RuntimeError` naming every installation's account login if
    there is more than one -- that's the out-of-scope cross-org case; an
    operator must pin GITHUB_APP_INSTALLATION_ID explicitly rather than have
    one silently chosen for them.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    if not data:
        raise AppNotInstalledError(
            "GitHub App has no installations: install it once via the GitHub UI "
            "(repo or org Settings -> GitHub Apps), then redeploy."
        )
    if len(data) > 1:
        accounts = ", ".join(installation["account"]["login"] for installation in data)
        raise RuntimeError(
            f"GitHub App has multiple installations ({accounts}) -- set "
            "GITHUB_APP_INSTALLATION_ID explicitly to pick one."
        )
    return int(data[0]["id"])


def list_installation_repos() -> list[str]:
    """Full names of repos the installation token can access (GET
    /installation/repositories, first page only).

    Used by scripts/deploy.py's github-app check for display/verification of a
    configured GITHUB_TARGET_REPO allowlist -- not a security boundary. The
    webhook's legitimacy guarantee comes from HMAC verification
    (app/webhook.py), not from this list.
    """
    gh = get_installation_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/installation/repositories")
    return [repo["full_name"] for repo in data.get("repositories", [])]


def set_webhook_url(url: str) -> None:
    """Idempotently point the App's webhook at `url` (PATCH /app/hook/config, App JWT)."""
    gh = _app_jwt_client()
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})


def get_webhook_url() -> str:
    """Return the App's currently configured webhook URL (App JWT).

    Returns "" when the App has no webhook URL set, which is the genuine
    first-deploy state rather than an error. Any API failure propagates as
    GithubException so the caller can decline to write after a failed read.

    Only an absolute http(s) URL counts as configured. PyGithub's
    Requester.__postProcess injects a synthetic ``url`` key -- the literal
    request path -- into any GET response dict that lacks one, so an
    unconfigured webhook arrives here as {"url": "/app/hook/config"} rather
    than {}. A webhook URL is by definition absolute, so requiring that scheme
    rejects the synthetic value without depending on PyGithub's internals or
    hard-coding the path it happens to inject.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/hook/config")
    url = (data or {}).get("url") or ""
    return url if url.startswith(("http://", "https://")) else ""


def fetch_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetch a PR's raw unified diff text.

    Built by concatenating each changed file's patch (as returned by the
    GitHub API), rather than requesting the `.diff` media type directly —
    PyGithub's typed `PullRequest.get_files()` keeps this dependency-free.
    Annotation (file:line) and token-capping happen later, in diff_utils.py.
    """
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    chunks: list[str] = []
    for f in pr.get_files():
        header = f"diff --git a/{f.filename} b/{f.filename}"
        patch = f.patch if f.patch else "(binary file or no textual diff available)"
        chunks.append(f"{header}\n{patch}")
    return "\n".join(chunks)


def upsert_comment(
    repo_full_name: str, pr_number: int, body: str, comment_id: int | None = None
) -> IssueComment:
    """Find the bot's own comment (by id, else author-filtered marker scan) and edit
    it in place; else create one. Returns the resulting IssueComment."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    marked_body = body if COMMENT_MARKER in body else f"{COMMENT_MARKER}\n{body}"
    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        existing.edit(marked_body)
        return existing
    return pr.create_issue_comment(marked_body)


def append_review_footnote(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append a failure footnote below the bot's own comment, preserving the review.
    Finds the comment by id then author-filtered marker scan; creates a
    marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")


def append_schedule_notice(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append/refresh the schedule-wait footnote below the bot's own comment,
    preserving the review. Finds the comment by id then author-filtered marker
    scan; creates a marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")


def clear_schedule_notice(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> IssueComment | None:
    """Strip any existing footnote (schedule note or, defensively, failure
    note) from the bot's comment -- called once a deferred ticket is claimed
    and its wait is over. No-op (no edit call) if the comment has no footnote
    to strip, or if no bot comment exists yet."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is None:
        return None
    stripped = _strip_existing_footnote(existing.body)
    if stripped != existing.body.rstrip():
        existing.edit(stripped)
    return existing
