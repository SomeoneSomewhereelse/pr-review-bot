"""GitHub App authentication, PR diff fetching, and comment upsert.

Narrow responsibility: authenticate as the GitHub App installation (JWT ->
short-lived installation token via PyGithub's ``Auth`` API), fetch a PR's
unified diff, and find-or-create-or-edit the bot's single PR comment.

Knows nothing about LLMs, orchestration, or diff annotation/truncation —
those belong to ``orchestrator.py`` / ``diff_utils.py``.
"""

from __future__ import annotations

from pathlib import Path

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
    """Prefer the base64 env var (host-portable); fall back to the PEM file for
    local dev. Never logged."""
    b64 = settings.github_app_private_key_b64
    if b64:
        import base64

        return base64.b64decode(b64).decode()
    key_path = Path(settings.github_app_private_key_path)
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    return key_path.read_text()


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


def discover_installation_id(repo_full_name: str) -> int:
    """Return the installation id for the App on `repo_full_name` (App JWT).

    Raises RuntimeError with an actionable message if the App is not installed
    -- GitHub does not permit an App to install itself; a repo admin must
    authorize it once in the GitHub UI. Only a 404 is interpreted as "not
    installed" -- any other status (e.g. a 401 from a malformed
    GITHUB_APP_PRIVATE_KEY_B64, or a transient 5xx) is a genuine auth/API
    error and must not be misdiagnosed as a missing installation.

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
            raise RuntimeError(
                f"GitHub App is not installed on {repo_full_name}: install it once via the "
                f"GitHub UI (repo Settings -> GitHub Apps), then redeploy. ({exc.status})"
            ) from exc
        raise RuntimeError(
            f"GitHub App installation lookup for {repo_full_name} failed with "
            f"{exc.status} ({exc.data}) -- likely a bad GITHUB_APP_ID or "
            f"GITHUB_APP_PRIVATE_KEY_B64, not a missing App installation."
        ) from exc
    return int(data["id"])


def set_webhook_url(url: str) -> None:
    """Idempotently point the App's webhook at `url` (PATCH /app/hook/config, App JWT)."""
    gh = _app_jwt_client()
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})


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
