"""onboarding/'s own Settings — a separate deployed service from bot/, so
this does NOT import bot/config.py's Settings (per onboarding/CLAUDE.md's
no-shared-credential-path rule)."""
from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# supabase_oauth_client_id is substituted raw into a <script> block by
# onboarding/router.py's index(), on a page that holds a GitHub App private
# key in sessionStorage — any of these characters can break out of the JS
# string literal, or out of the <script> tag itself.
_INJECTION_CHARS_RE = re.compile(r"[\"'<>\\]")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # No public_base_url here, deliberately: the page derives its own base
    # from location.origin, which the browser already knows exactly and which
    # is by definition the origin GitHub and Supabase redirect back to. A
    # hand-set env var was a second source of truth for the same fact, and the
    # two drifted — a difference of one trailing slash broke the Supabase
    # OAuth leg outright (see ISSUES.md). Do not reintroduce it without a use
    # the browser genuinely cannot serve itself.

    # This service's only operator-level secrets: set once by the operator
    # after manually registering an OAuth app in Supabase org settings ->
    # OAuth Apps (Supabase has no self-registration mechanism, unlike
    # GitHub's App Manifest flow). Never visitor-supplied.
    # supabase_oauth_client_id gets a whitespace-strip-and-empty-sentinel plus
    # an injection-character check, since a pasted value with a trailing
    # newline would otherwise pass the lifespan's presence check while booting
    # the service into an unusable state, and it is templated raw into a
    # <script> block (window.SUPABASE_OAUTH_CLIENT_ID). No exact UUID format
    # check — just whitespace + injection-safety, without over-engineering a
    # full format check.
    # supabase_oauth_client_secret gets only the whitespace-strip half: it is
    # never templated into HTML (client_secret stays server-side), so a
    # malformed value still fails visibly at OAuth-authorize time before any
    # credential is created.
    supabase_oauth_client_id: str = ""
    supabase_oauth_client_secret: str = ""

    @field_validator("supabase_oauth_client_id")
    @classmethod
    def _normalize_supabase_oauth_client_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            # The "unset" sentinel onboarding/main.py's lifespan checks for.
            return ""
        if _INJECTION_CHARS_RE.search(value):
            raise ValueError(
                "SUPABASE_OAUTH_CLIENT_ID must not contain \", ', <, >, or \\ "
                "characters"
            )
        return value

    @field_validator("supabase_oauth_client_secret")
    @classmethod
    def _normalize_supabase_oauth_client_secret(cls, value: str) -> str:
        # strip() alone also handles the all-whitespace -> "" sentinel case.
        return value.strip()


settings = Settings()
