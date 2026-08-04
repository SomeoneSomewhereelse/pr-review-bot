"""Idempotent deploy-time registration: verify the App is installed on the
target repo and point its webhook at this deployment. Safe to run every deploy.
Uses only the existing App JWT -- no new secrets.
"""

from __future__ import annotations

import os
import sys

from app import github_app
from app.config import settings


def main() -> int:
    repo = settings.github_target_repo
    base = settings.public_base_url or os.environ.get("RENDER_EXTERNAL_URL", "")
    if not repo or not base:
        print(
            "GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
            "are required",
            file=sys.stderr,
        )
        return 2
    installation_id = github_app.discover_installation_id(repo)  # raises if not installed
    github_app.set_webhook_url(f"{base.rstrip('/')}/webhook")
    print(f"registered: installation={installation_id} webhook={base.rstrip('/')}/webhook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
