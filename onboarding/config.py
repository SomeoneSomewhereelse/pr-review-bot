"""onboarding/'s own Settings — a separate deployed service from app/, so
this does NOT import app/config.py's Settings (per onboarding/CLAUDE.md's
no-shared-credential-path rule) even though public_base_url is conceptually
similar to app/config.py's own field of the same name."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # No pydantic-required (no-default) field: that would raise the moment
    # anything first imports this module — including pytest collection —
    # before onboarding/main.py's lifespan could report the problem with a
    # clear message. Same reasoning as app/config.py's own public_base_url
    # field. Validated explicitly in the lifespan instead (Task 1 step 5).
    public_base_url: str = ""


settings = Settings()
