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


def _is_bot_comment(comment: IssueComment) -> bool:
    """True if authored by a GitHub App bot (not a human), so a human quoting
    the marker is never mistaken for the bot's own comment."""
    user_data = comment._rawData.get("user", {})
    return user_data.get("type") == "Bot"


def _find_bot_comment(repo, pr, comment_id: int | None) -> IssueComment | None:
    """Locate the bot's own comment: by stored id first (trusted — we created it),
    else an author-filtered marker scan (bot-authored AND our marker). Returns
    None if neither finds one, so the caller creates a fresh marker comment."""
    if comment_id is not None:
        try:
            headers, data = repo._requester.requestJsonAndCheck(
                "GET", f"/repos/{repo.full_name}/issues/comments/{comment_id}"
            )
            return IssueComment(repo._requester, headers, data, completed=True)
        except GithubException:
            pass  # deleted/unknown id -> fall back to the scan
    for comment in pr.get_issue_comments():
        if _is_bot_comment(comment) and COMMENT_MARKER in comment.body:
            return comment
    return None


def _strip_existing_footnote(body: str) -> str:
    """Strip a well-formed TRAILING footnote block, if one is present.

    Deliberately NOT a regex-from-first-marker-to-next-marker scan: a
    specialist's finding text could plausibly quote the literal
    ``FAIL_NOTE_START`` string (this very file contains it), which would make
    a "first START to next END" match span from that stray marker all the way
    to the real trailing footnote's END, deleting genuine review content in
    between. Instead: find the LAST occurrence of ``FAIL_NOTE_START`` and only
    treat it as a real footnote to strip if the body actually ends with
    ``FAIL_NOTE_END`` -- any earlier/unmatched occurrence is left alone as
    incidental review text.
    """
    stripped = body.rstrip()
    idx = stripped.rfind(FAIL_NOTE_START)
    if idx != -1 and stripped.endswith(FAIL_NOTE_END):
        return stripped[:idx].rstrip()
    return stripped


def _read_private_key() -> str:
    """Read the App's PEM private key from disk.

    Never logged/printed — callers must not do so either.
    """
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
