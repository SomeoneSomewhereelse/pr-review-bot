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

import argparse
import json
import sys
from typing import NamedTuple

from app import github_app
from app.config import settings
from scripts import _prereqs, _probes, deploy

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


_APP_CREDENTIALS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET")


def check_prereqs(track: str) -> deploy.CheckResult:
    """Python version and the tools that must be on PATH.

    cloudflared is required for the local track only -- it is what makes the
    service reachable by GitHub's webhook delivery (design spec section 4a-i).
    """
    problems: list[str] = []
    if not _prereqs.python_version_ok():
        major, minor = _prereqs.MINIMUM_PYTHON
        problems.append(
            f"Python {major}.{minor}+ required, running "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    tools = list(_prereqs.REQUIRED_TOOLS)
    if track == "local":
        tools.append(_prereqs.TUNNEL_TOOL)
    problems.extend(
        _prereqs.install_hint(tool) for tool in tools if not _prereqs.is_available(tool)
    )
    if problems:
        return deploy.CheckResult("prereqs", "FAIL", "\n".join(problems))
    return deploy.CheckResult("prereqs", "PASS", "")


def check_test_database() -> deploy.CheckResult:
    """Whether `uv run pytest` can get a Postgres: Docker present, or
    DATABASE_URL set. WARN, not FAIL -- it blocks the test suite, never the
    service itself, so it must not stop an operator from deploying."""
    if _prereqs.database_available():
        return deploy.CheckResult("test-db", "PASS", "")
    return deploy.CheckResult(
        "test-db", "WARN",
        "DB-touching tests need Docker running or DATABASE_URL set; "
        f"{_prereqs.install_hint(_prereqs.DOCKER)}",
    )


def check_local_config() -> deploy.CheckResult:
    """The App credentials present locally.

    Reports NAMES and a decode boolean only -- never a value, never a length in
    the failure path. Pasting the PEM verbatim instead of its base64 form is the
    single most common setup mistake, so it gets its own line.
    """
    present = _probes.present_secrets()
    missing = [name for name in _APP_CREDENTIALS if name not in present]
    have = [name for name in _APP_CREDENTIALS if name in present]
    problems = []
    if missing:
        line = f"missing: {', '.join(missing)}"
        if have:
            line += f" (have: {', '.join(have)})"
        problems.append(line)
    if "GITHUB_APP_PRIVATE_KEY" in present and not _probes.private_key_decodes():
        problems.append(
            "GITHUB_APP_PRIVATE_KEY is set but does not base64-decode to a PEM "
            "-- it must be the base64 form, not the file's contents verbatim: "
            "uv run python -m scripts.encode_credential github-app-private-key.pem"
        )
    if problems:
        return deploy.CheckResult("local-config", "FAIL", "\n".join(problems))
    return deploy.CheckResult("local-config", "PASS", "")


def check_llm_provider() -> deploy.CheckResult:
    """LLM_PROVIDER is set and its credential is present -- LOCALLY.

    Deliberately NOT deploy.check_provider, which resolves the DB override and
    therefore SKIPs without DATABASE_URL. Gating step 4 on that would leave an
    operator who has configured a provider but not yet a database stuck on
    step 4 forever, which is precisely the kind of dead end doctor exists to
    prevent. deploy.check_provider still runs as its own row, for the override
    resolution this cannot see.
    """
    provider, has_credential = _probes.llm_provider_state()
    if not provider:
        return deploy.CheckResult(
            "llm-provider", "FAIL",
            "LLM_PROVIDER is unset (there is no default) -- set it in .env.config",
        )
    if not has_credential:
        return deploy.CheckResult(
            "llm-provider", "FAIL", f"LLM_PROVIDER={provider} but its credential is not set"
        )
    return deploy.CheckResult("llm-provider", "PASS", f"provider={provider}")


def check_github_install() -> deploy.CheckResult:
    """Whether the App has exactly one installation. READ-ONLY."""
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult(
            "github-install", "SKIPPED", "needs the App credentials (see local-config)"
        )
    try:
        installation_id = github_app.discover_installation_id_for_app()
    except Exception as exc:  # noqa: BLE001 -- structural report, never the value
        return deploy.CheckResult(
            "github-install", "FAIL",
            f"could not resolve an installation ({type(exc).__name__}); "
            "install the App at https://github.com/settings/apps",
        )
    return deploy.CheckResult("github-install", "PASS", f"installation={installation_id}")


def check_webhook(base: str) -> deploy.CheckResult:
    """Whether the App's webhook points at `base`. READ-ONLY -- deliberately
    does not call deploy.py's equivalent check, which PATCHes the URL when it
    is wrong. Fixing it is `uv run python -m scripts.deploy`; reporting it is
    here."""
    if not base:
        return deploy.CheckResult("webhook", "SKIPPED", "no public base URL yet")
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult("webhook", "SKIPPED", "needs the App credentials")
    wanted = base.rstrip("/") + "/webhook"
    try:
        current = github_app.get_webhook_url()
    except Exception as exc:  # noqa: BLE001
        return deploy.CheckResult("webhook", "FAIL", f"could not read it ({type(exc).__name__})")
    if current == wanted:
        return deploy.CheckResult("webhook", "PASS", "")
    return deploy.CheckResult(
        "webhook", "FAIL",
        f"points at {current or '(unset)'}, wanted {wanted} "
        "-- fix with: uv run python -m scripts.deploy",
    )


def build_state(track: str, base: str) -> tuple[State, list[deploy.CheckResult]]:
    """Probe, staged: local first, then remote only for resources that exist.

    Ordering matters. Render not existing at step 1 is the normal state, so a
    remote probe is SKIPPED rather than failed until its precondition holds.
    """
    results = [
        deploy._safe("prereqs", check_prereqs, track),
        deploy._safe("test-db", check_test_database),
        deploy._safe("local-config", check_local_config),
        deploy._safe("llm-provider", check_llm_provider),
        deploy._safe("config", deploy.check_config),
        deploy._safe("pricing", deploy.check_pricing),
        deploy._safe("github-install", check_github_install),
        deploy._safe("database", deploy.check_database),
        deploy._safe("provider", deploy.check_provider),
    ]
    if track == "local":
        results.append(
            deploy.CheckResult(
                "tunnel", "PASS" if base else "FAIL",
                "" if base else "no PUBLIC_BASE_URL yet -- start a tunnel: "
                "cloudflared tunnel --url http://localhost:8000",
            )
        )
    results.append(deploy._safe("health", deploy.check_health_endpoint, base) if base
                   else deploy.CheckResult("health", "SKIPPED", "no public base URL yet"))
    results.append(deploy._safe("webhook", check_webhook, base))
    if track == "hosted":
        results.extend([
            deploy._safe("boot-creds-live", deploy.check_boot_credentials_live),
            deploy._safe("provider-live", deploy.check_provider_live),
            deploy._safe("api-key-live", deploy.check_api_key_live),
            deploy._safe("render-service", deploy.check_render_service),
            deploy._safe("uptime-pinger", deploy.check_uptime_pinger, base),
        ])

    by_name = {r.name: r for r in results}

    def ok(name: str) -> bool:
        return by_name.get(name, deploy.CheckResult(name, "SKIPPED")).status in ("PASS", "WARN")

    state = State(
        prereqs=ok("prereqs"),
        app_credentials=ok("local-config"),
        app_installed=ok("github-install"),
        llm_ready=ok("llm-provider"),
        database=ok("database"),
        # Gated on credential-FREE rows on purpose. render-service and
        # uptime-pinger both SKIP without an operator-local API key
        # (RENDER_API_KEY / UPTIMEROBOT_API_KEY), and a SKIPPED row counts as
        # unsatisfied -- so gating hosted's public_url/keepalive on them would
        # strand an operator who never sets those local keys on step 6/8
        # forever. /healthz answering is the credential-free proof the service
        # exists AND is warm, so both steps gate on "health" for the hosted
        # track. render-service and uptime-pinger still run and appear as
        # their own rows above; they just aren't what decides these two.
        public_url=ok("tunnel") if track == "local" else ok("health"),
        webhook=ok("webhook"),
        keepalive=ok("health"),
    )
    return state, results


def render(track: str, step: Step | None, results: list[deploy.CheckResult]) -> str:
    """deploy.py's table, plus the one line doctor exists to print."""
    report = deploy.render_report(results)
    if step is None:
        return f"{report}\n\ntrack: {track} -- setup complete, every step satisfied."
    return (
        f"{report}\n\ntrack: {track} -- you are at step {step.number} of 8: "
        f"{step.title}\nnext: {step.command}"
    )


def as_json(track: str, step: Step | None, results: list[deploy.CheckResult]) -> str:
    return json.dumps(
        {
            "track": track,
            "step": None if step is None
            else {"number": step.number, "title": step.title, "command": step.command},
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail} for r in results
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report where you are in setup and what to run next (read-only)"
    )
    parser.add_argument("--track", choices=TRACKS, default=None,
                        help="grade against this track (default: auto-detect)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable output")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        track = resolve_track(args.track)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    base = deploy.resolve_base_url()
    state, results = build_state(track, base)
    step = current_step(track, state)
    print(as_json(track, step, results) if args.as_json else render(track, step, results))
    # Exit 0 always: "you are mid-setup" is information, not failure. Only a
    # bad invocation is an error (exit 2 above).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
