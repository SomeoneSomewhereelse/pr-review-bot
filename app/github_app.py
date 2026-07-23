"""GitHub App authentication, PR diff fetching, and comment upsert.

Narrow responsibility: authenticate as the GitHub App installation (JWT ->
short-lived installation token via PyGithub's ``Auth`` API), fetch a PR's
unified diff, and find-or-create-or-edit the bot's single PR comment.

Knows nothing about LLMs, orchestration, or diff annotation/truncation —
those belong to ``orchestrator.py`` / ``diff_utils.py``.
"""

from __future__ import annotations

from pathlib import Path

from github import Auth, Github
from github.IssueComment import IssueComment

from app.config import settings

# Hidden marker used to find the bot's own comment across re-reviews so a
# `synchronize` event edits it in place instead of spamming a new one.
COMMENT_MARKER = "<!-- ai-code-review-bot -->"


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


def upsert_comment(repo_full_name: str, pr_number: int, body: str) -> IssueComment:
    """Find the bot's marker comment on the PR and edit it in place; else create one.

    This is what makes re-reviews on `synchronize` edit rather than spam new
    comments. Returns the resulting IssueComment.
    """
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    marked_body = body if COMMENT_MARKER in body else f"{COMMENT_MARKER}\n{body}"

    for comment in pr.get_issue_comments():
        if COMMENT_MARKER in comment.body:
            comment.edit(marked_body)
            return comment

    return pr.create_issue_comment(marked_body)
