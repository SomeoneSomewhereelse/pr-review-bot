"""onboarding/'s own Settings — a separate deployed service from app/, so
this does NOT import app/config.py's Settings (per onboarding/CLAUDE.md's
no-shared-credential-path rule) even though public_base_url is conceptually
similar to app/config.py's own field of the same name."""
from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A plain http(s):// base URL and nothing else. The excluded characters are
# each excluded for a concrete reason, not tidiness:
#   " ' < > \  — the value is substituted raw into a <script> block by
#                onboarding/router.py's index(); any of these can break out of
#                the JS string literal (or the <script> tag itself) on a page
#                that holds a GitHub App private key in sessionStorage.
#   ? #        — a query string or fragment here would be silently mangled
#                when index.html appends its own `/?gh_step=...`.
#   whitespace — never valid in a URL, and a sign of a mis-pasted value.
# The first character after "://" may not itself be a "/", so that the
# rstrip("/") below can never eat into the scheme (e.g. "https:///").
_BASE_URL_RE = re.compile(r"^https?://[^\s\"'<>?#\\/]+[^\s\"'<>?#\\]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # No pydantic-required (no-default) field: that would raise the moment
    # anything first imports this module — including pytest collection —
    # before onboarding/main.py's lifespan could report the problem with a
    # clear message. Same reasoning as app/config.py's own public_base_url
    # field. Validated explicitly in the lifespan instead (Task 1 step 5).
    # The validator below is shape-only — it never makes "unset" an error,
    # so the lifespan stays the thing that refuses to boot on a missing value.
    public_base_url: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _normalize_public_base_url(cls, value: str) -> str:
        """Normalize and shape-check the base URL.

        A trailing slash is not cosmetic here: index.html's buildManifest()
        builds `${ONBOARDING_BASE_URL}/?gh_step=manifest`, so a trailing slash
        yields `https://host//?gh_step=manifest`, and Starlette does not match
        a request path of `//` against the `/` route. GitHub's redirect back
        would 404 *after* the visitor has already created a real App on their
        account — and the manifest code/PEM/webhook secret are single-use, so
        that App is orphaned with no way to recover its credentials. Same
        `.rstrip('/')` guard as scripts/create_github_app.py:114,
        scripts/deploy.py:165 and scripts/doctor.py:418.
        """
        value = value.strip()
        if not value:
            # The "unset" sentinel onboarding/main.py's lifespan checks for.
            return ""
        if not _BASE_URL_RE.match(value):
            raise ValueError(
                "PUBLIC_BASE_URL must be a plain http(s):// URL (e.g. "
                "https://onboarding.example.com) with no query string, "
                "fragment, whitespace, or embedded quote/angle-bracket/"
                "backslash characters"
            )
        return value.rstrip("/")


settings = Settings()
