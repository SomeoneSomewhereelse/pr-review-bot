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

    # The wizard's own dedicated Postgres (never bot/'s queue DB, never a
    # visitor's provisioned project) backing session_store.py. See
    # docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md.
    database_url: str = ""

    # A Fernet key encrypting every credential value session_store.py
    # writes. Only whitespace-normalized here, deliberately NOT format-
    # validated via a pydantic field_validator: pydantic's ValidationError
    # embeds the rejected input_value verbatim in its own __str__ output
    # regardless of the validator's own error message (root CLAUDE.md's
    # secret-handling section documents exactly this failure mode) -- for a
    # non-secret field like supabase_oauth_client_id above that's fine, but
    # this field is a secret, so raising a pydantic-level ValidationError
    # here would leak it into whatever prints/logs Settings() construction
    # failures. Format validity is checked instead in main.py's lifespan,
    # which raises a plain RuntimeError with a clean, hand-written message.
    onboarding_session_encryption_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("onboarding_session_encryption_key")
    @classmethod
    def _normalize_session_encryption_key(cls, value: str) -> str:
        return value.strip()


settings = Settings()
