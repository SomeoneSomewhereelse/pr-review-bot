"""Where am I in setup, what is missing, and what do I run next.

Read-only and idempotent: doctor never writes a file, starts a process, or
mutates remote state. Writing belongs to scripts/init_env.py and
scripts/create_github_app.py (design spec 2026-08-18 section 4d).

It COMPOSES scripts/deploy.py's checks rather than reimplementing them --
two check implementations that could drift is the thing most worth avoiding
here -- and adds only the backwards-looking probes deploy.py has no reason to
own (is .env populated, does the PEM decode, is LLM_PROVIDER set).
"""

from __future__ import annotations

from typing import NamedTuple

from app.config import settings

TRACKS = ("local", "hosted")


class State(NamedTuple):
    """Observable setup state. Every field is a plain bool: a step is either
    satisfied or it is not, and nothing here can carry a secret."""

    prereqs: bool
    app_credentials: bool
    app_installed: bool
    llm_ready: bool
    database: bool
    public_url: bool
    webhook: bool
    keepalive: bool


class Step(NamedTuple):
    number: int
    title: str
    field: str    # the State field that must be True for this step to be done
    command: str  # the exact next action, verbatim


_SHARED: tuple[Step, ...] = (
    Step(1, "Install prerequisites", "prereqs",
         "uv sync, then install anything the prereqs rows above name"),
    Step(2, "Create the GitHub App", "app_credentials",
         "uv run python -m scripts.create_github_app   (run this yourself -- it writes secrets)"),
    Step(3, "Install the App on your repo(s)", "app_installed",
         "open https://github.com/settings/apps -> your app -> Install App"),
    Step(4, "Configure an LLM provider", "llm_ready",
         "set LLM_PROVIDER in .env.config and its API key via "
         "`uv run python -m scripts.init_env` (run this yourself)"),
)

# Steps 5-8 diverge. 'keepalive' means something different per track: locally
# nothing needs to stay warm, so the running uvicorn process satisfies it;
# hosted, it is the UptimeRobot monitor that stops Render's free tier sleeping.
_LOCAL: tuple[Step, ...] = (
    Step(5, "Get a Postgres", "database",
         "start one (`docker run -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16`) "
         "and set DATABASE_URL"),
    Step(6, "Start a tunnel", "public_url",
         "cloudflared tunnel --url http://localhost:8000, then set PUBLIC_BASE_URL "
         "to the printed https URL"),
    Step(7, "Register the webhook", "webhook",
         "uv run python -m scripts.deploy"),
    Step(8, "Run the service", "keepalive",
         "uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"),
)

_HOSTED: tuple[Step, ...] = (
    Step(5, "Create the Supabase project", "database",
         "create it at https://supabase.com, then set DATABASE_URL to the "
         "Session-mode pooler string (port 5432, NOT 6543)"),
    Step(6, "Create the Render service", "public_url",
         "Render dashboard -> New + -> Blueprint -> render.yaml, then set the four "
         "boot vars (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET, "
         "DATABASE_URL)"),
    Step(7, "Sync config and verify", "webhook",
         "uv run python -m scripts.deploy --sync-env"),
    Step(8, "Add the keep-warm pinger", "keepalive",
         "create an UptimeRobot monitor on <your-service>/healthz at a 5-minute "
         "interval (the URL must match exactly); set UPTIMEROBOT_API_KEY locally "
         "if you want doctor to verify it rather than report SKIPPED"),
)


def steps_for(track: str) -> tuple[Step, ...]:
    if track not in TRACKS:
        raise ValueError(f"unknown track {track!r}; expected one of {TRACKS}")
    return _SHARED + (_LOCAL if track == "local" else _HOSTED)


def current_step(track: str, state: State) -> Step | None:
    """The EARLIEST unsatisfied step, or None when setup is complete.

    Earliest, not most-severe: a later gap is usually a consequence of an
    earlier one, so reporting it first would send an operator down the wrong
    path.
    """
    for step in steps_for(track):
        if not getattr(state, step.field):
            return step
    return None


def resolve_track(explicit: str | None = None) -> str:
    """Which track to grade against. An explicit --track always wins.

    Auto-detection is a documented rule, not a guess: a RENDER_API_KEY or an
    onrender.com base URL means hosted; anything else means local. Both tracks
    share steps 1-4, so a wrong guess early costs nothing.
    """
    if explicit:
        if explicit not in TRACKS:
            raise ValueError(f"unknown track {explicit!r}; expected one of {TRACKS}")
        return explicit
    if settings.render_api_key or "onrender.com" in settings.public_base_url:
        return "hosted"
    return "local"
