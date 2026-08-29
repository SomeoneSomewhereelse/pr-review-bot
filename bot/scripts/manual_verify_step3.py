"""Manual live verification for Step 3 (app/github_app.py).

Not part of the pytest suite (CI never runs this) — it depends on live
external GitHub state: a real installation of the configured GitHub App, and a
real PR on the configured test repo (GITHUB_TARGET_REPO).

Run it directly:

    uv run python -m bot.scripts.manual_verify_step3 [owner/repo] [pr_number]

It proves, against real GitHub:
  1. Authentication as the GitHub App installation (not `gh`'s user token).
  2. Fetching a real PR's diff.
  3. Posting a marker comment, then editing it in place on a second call
     (never creating a duplicate).
"""

from __future__ import annotations

import sys

from github import GithubException

from bot import github_app
from bot.config import settings

DEFAULT_REPO = settings.github_target_repo
DEFAULT_PR_NUMBER = 1


def main() -> None:
    repo_full_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    pr_number = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PR_NUMBER

    print(f"Repo: {repo_full_name}  PR: #{pr_number}")
    print("Authenticating as the GitHub App installation ...")
    installation_auth = github_app.get_installation_auth()
    print(
        f"Built Auth.AppInstallationAuth for app_id={installation_auth.app_id} "
        f"installation_id={installation_auth.installation_id}"
    )
    gh = github_app.get_installation_client()

    print("Proving this is an installation token, not gh's personal user token ...")
    try:
        gh.get_user().login
        print(
            "  UNEXPECTED: GET /user succeeded — this looks like a personal token, "
            "not an App installation token."
        )
        raise SystemExit(1)
    except GithubException as e:
        print(
            f"  Confirmed: GET /user was rejected (status={e.status}) — installation "
            "tokens cannot call user-scoped endpoints, personal tokens can."
        )

    print("\nFetching PR diff ...")
    diff = github_app.fetch_pr_diff(repo_full_name, pr_number)
    print(f"Diff length: {len(diff.text)} chars")
    print("First 5 lines of diff:")
    for line in diff.text.splitlines()[:5]:
        print(f"  {line}")

    print("\nPosting first comment (create) ...")
    body_1 = (
        "🤖 Step 3 live verification — GitHub App auth, diff fetch, and "
        "comment upsert all working. (first call — should create)"
    )
    comment_1 = github_app.upsert_comment(repo_full_name, pr_number, body_1)
    print(f"Comment id after first upsert_comment(): {comment_1.id}")

    print("\nPosting second comment (edit-in-place) ...")
    body_2 = (
        "🤖 Step 3 live verification — GitHub App auth, diff fetch, and "
        "comment upsert all working. (second call — should edit the same comment)"
    )
    comment_2 = github_app.upsert_comment(repo_full_name, pr_number, body_2)
    print(f"Comment id after second upsert_comment(): {comment_2.id}")

    assert comment_1.id == comment_2.id, "upsert_comment created a duplicate instead of editing!"

    print("\nVerifying via the API that exactly one marker-bearing comment exists ...")
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    marker_comments = [c for c in pr.get_issue_comments() if github_app.COMMENT_MARKER in c.body]
    print(f"Marker-bearing comments found: {len(marker_comments)}")
    assert len(marker_comments) == 1, f"expected exactly 1, found {len(marker_comments)}"

    print("\nSUCCESS: auth, diff fetch, and idempotent comment upsert all verified live.")


if __name__ == "__main__":
    main()
