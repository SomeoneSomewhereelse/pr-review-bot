"""Deploy verification CLI for the hosted Render + Supabase deployment.

Runs six independent checks and prints one aligned table. Every check runs
regardless of earlier failures, so a single run surfaces every problem rather
than only the first. Exit codes: 0 all ok, 1 at least one check failed, 2 the
CLI could not run at all.

Standalone by design: nothing here assumes Claude Code, an assistant, or an
interactive terminal. `.claude/commands/deploy.md` is a convenience wrapper
that holds no logic.

Output is terse by contract (design spec section 7.4): details are fragments
naming the observed fact and the next action, never the reasoning -- the
explanations live in README.md.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import psycopg
from github import GithubException

from app import github_app
from app.config import settings

_NAME_WIDTH = 18
_STATUS_WIDTH = 9
_README_ANCHOR = "README.md#deploying-to-production-render--supabase"
_HTTP_TIMEOUT = 10.0
_DB_CONNECT_TIMEOUT = 10
_RENDER_API = "https://api.render.com/v1"
_UPTIMEROBOT_API = "https://api.uptimerobot.com/v2/getMonitors"
# Render free instances spin down after ~15 minutes idle; 10 minutes leaves margin.
_MAX_PINGER_INTERVAL_SECONDS = 600

# The service env vars --sync-env pushes. Authoritative: tests/test_deploy_script.py
# asserts README.md and SETUP.md each mention every name here.
_SYNCED_ENV_VARS = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_B64",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "LLM_PROVIDER",
    "GROQ_API_KEY",
    "GITHUB_MODELS_TOKEN",
)
_DEPLOY_POLL_SECONDS = 10
_DEPLOY_TIMEOUT_SECONDS = 300
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "canceled",
    "deactivated",
}


@dataclass(frozen=True)
class CheckResult:
    """One row of the report. ``detail`` is the whole user experience for a
    failing line: it must name what is wrong and what to do, because a terminal
    user has nothing else to work from. A newline in ``detail`` renders as an
    indented continuation line, used only to enumerate observed values."""

    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    detail: str = ""


def resolve_base_url() -> str:
    """This deployment's public origin, normalized exactly once.

    The rstrip is not cosmetic: check_uptime_pinger compares the monitor's URL
    by exact equality, so a trailing slash here would produce a doubled slash
    and fail a correctly configured pinger.
    """
    base = settings.public_base_url or os.environ.get("RENDER_EXTERNAL_URL", "")
    return base.rstrip("/")


# The credential and model env var each LLM_PROVIDER value requires. This is the
# single source of truth: check_config, --sync-env and scripts/set_provider.py
# all read it, so a provider cannot be known to one and unknown to another.
# provider -> (credential env var, model env var)
_PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
}


def _private_key_available() -> bool:
    if settings.github_app_private_key_b64:
        return True
    path = Path(settings.github_app_private_key_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.is_file()


def check_config() -> CheckResult:
    """Every value the deployed service needs, resolvable locally.

    Reports missing key NAMES only -- never a value, never a length."""
    missing: list[str] = []
    problems: list[str] = []
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    if not _private_key_available():
        missing.append("GITHUB_APP_PRIVATE_KEY_B64 or _PATH")
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.github_target_repo:
        missing.append("GITHUB_TARGET_REPO")
    if not resolve_base_url():
        missing.append("PUBLIC_BASE_URL or RENDER_EXTERNAL_URL")
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        problems.append(
            f"LLM_PROVIDER={settings.llm_provider!r} is not supported "
            f"(expected one of: {accepted})"
        )
    else:
        credential = entry[0]
        if not getattr(settings, credential.lower(), ""):
            missing.append(credential)

    detail_lines = []
    if missing:
        detail_lines.append("missing: " + ", ".join(missing))
    detail_lines.extend(problems)
    if detail_lines:
        return CheckResult("config", "FAIL", "\n".join(detail_lines))
    return CheckResult("config", "PASS", "")


def check_installation_and_webhook(repo: str, base: str) -> CheckResult:
    """Installation discovery plus an idempotent webhook registration.

    Reads the current webhook URL before writing so a re-run reports "already
    correct" rather than silently re-PATCHing, and so a failed read never
    triggers a blind write that could clobber a good URL.
    """
    name = "github-app"
    try:
        installation_id = github_app.discover_installation_id(repo)
    except github_app.AppNotInstalledError:
        return CheckResult(name, "FAIL", f"App not installed on {repo}; install via GitHub UI")
    except RuntimeError as exc:
        status = getattr(exc.__cause__, "status", None)
        detail = "installation lookup failed; check App ID / private key"
        if status is not None:
            detail += f" ({status})"
        return CheckResult(name, "FAIL", detail)

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook read failed ({exc.status})"
        )
    if current == wanted:
        return CheckResult(name, "PASS", f"installation={installation_id}; webhook already correct")
    try:
        github_app.set_webhook_url(wanted)
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook write failed ({exc.status})"
        )
    if current:
        return CheckResult(
            name, "PASS", f"installation={installation_id}; webhook updated from {current}"
        )
    return CheckResult(name, "PASS", f"installation={installation_id}; webhook set")


def check_health_endpoint(base: str) -> CheckResult:
    """Both verbs must answer 200.

    HEAD is not redundant: UptimeRobot's free tier sends HEAD by default, so a
    GET-only /healthz returns 405 to the pinger and the instance sleeps -- a
    failure invisible from a browser.
    """
    name = "health"
    url = f"{base}/healthz"
    try:
        get_status = httpx.get(url, timeout=_HTTP_TIMEOUT).status_code
        head_status = httpx.head(url, timeout=_HTTP_TIMEOUT).status_code
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"{type(exc).__name__} reaching {url}")
    if get_status != 200 and head_status != 200:
        return CheckResult(name, "FAIL", f"GET -> {get_status}, HEAD -> {head_status}")
    if get_status != 200:
        return CheckResult(name, "FAIL", f"GET /healthz -> {get_status} (HEAD ok)")
    if head_status != 200:
        return CheckResult(
            name, "FAIL", f"HEAD /healthz -> {head_status} (GET ok); pinger sends HEAD"
        )
    return CheckResult(name, "PASS", "GET + HEAD -> 200")


def check_database() -> CheckResult:
    """Reachability AND provisioning, via a raw connection.

    Deliberately not store.init_pool(): the pool waits 30s before raising and
    its message is written for a startup log, not a checklist. A raw connect
    with a short timeout reports the driver's real failure in about a second.

    The failure detail names the exception type and the (non-secret) hostname
    only -- settings.database_url carries the password.
    """
    name = "database"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to check the queue database")
    host = urlsplit(settings.database_url).hostname or "?"
    try:
        with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
            conn.execute("SELECT 1")
            provisioned = conn.execute("SELECT to_regclass('public.tickets')").fetchone()[0]
    except psycopg.Error as exc:
        return CheckResult(name, "FAIL", f"cannot connect to {host} ({type(exc).__name__})")
    if provisioned is None:
        return CheckResult(name, "FAIL", "connected; tickets absent -- app never booted on this DB")
    return CheckResult(name, "PASS", "connected; tickets present")


def _render_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def _unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def _find_render_service_id() -> str | None:
    resp = httpx.get(f"{_RENDER_API}/services", headers=_render_headers(), timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json():
        service = _unwrap(item, "service")
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    return None


def check_render_service() -> CheckResult:
    """Why the service is or is not serving -- health already covers whether."""
    name = "render-service"
    if not settings.render_api_key:
        return CheckResult(name, "SKIPPED", "set RENDER_API_KEY to check deploy status")
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not deploys:
        return CheckResult(name, "FAIL", "service exists but has no deploys")
    status = _unwrap(deploys[0], "deploy").get("status", "?")
    if status != "live":
        return CheckResult(name, "FAIL", f"latest deploy status: {status}")
    return CheckResult(name, "PASS", "latest deploy live")


def check_uptime_pinger(base: str) -> CheckResult:
    """The keep-warm monitor exists, is active, and polls often enough.

    The URL is compared by exact equality on purpose: the real outage was a
    trailing comma, which fired perfectly on schedule and 404'd every time.
    """
    name = "uptime-pinger"
    if not settings.uptimerobot_api_key:
        return CheckResult(name, "SKIPPED", "set UPTIMEROBOT_API_KEY to check keep-warm")
    wanted = f"{base}/healthz"
    try:
        resp = httpx.post(
            _UPTIMEROBOT_API,
            data={"api_key": settings.uptimerobot_api_key, "format": "json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        monitors = resp.json().get("monitors") or []
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"UptimeRobot API error ({type(exc).__name__})")

    match = next((m for m in monitors if m.get("url") == wanted), None)
    if match is None:
        found = ", ".join(m.get("url", "?") for m in monitors) or "none"
        return CheckResult(name, "FAIL", f"no monitor matches {wanted}\nfound: {found}")
    if match.get("status") == 0:
        return CheckResult(name, "FAIL", "monitor is paused")
    interval = int(match.get("interval") or 0)
    if interval > _MAX_PINGER_INTERVAL_SECONDS:
        return CheckResult(
            name, "FAIL", f"interval {interval}s > {_MAX_PINGER_INTERVAL_SECONDS}s; will sleep"
        )
    return CheckResult(name, "PASS", f"interval {interval}s; status={match.get('status')}")


def render_report(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for result in results:
        first, *rest = (result.detail or "").split("\n")
        lines.append(
            f"{result.name:<{_NAME_WIDTH}}{result.status:<{_STATUS_WIDTH}}{first}".rstrip()
        )
        lines.extend(" " * (_NAME_WIDTH + _STATUS_WIDTH) + line for line in rest)
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    lines.append("")
    if failed:
        lines.append(f"{failed} failed, {skipped} skipped -- see {_README_ANCHOR}")
    elif skipped:
        lines.append(f"all checks passed, {skipped} skipped")
    else:
        lines.append("all checks passed")
    return "\n".join(lines)


def _wanted_env() -> dict[str, str]:
    """Local values for every synced var. The PEM is base64-encoded on the fly
    when only the file path is configured locally, since Render needs the b64 form."""
    pem_b64 = settings.github_app_private_key_b64
    if not pem_b64:
        path = Path(settings.github_app_private_key_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_file():
            pem_b64 = base64.b64encode(path.read_bytes()).decode()
    return {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY_B64": pem_b64,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "LLM_PROVIDER": settings.llm_provider,
        "GROQ_API_KEY": settings.groq_api_key,
        "GITHUB_MODELS_TOKEN": settings.github_models_token,
    }


def _trigger_and_wait(service_id: str) -> int:
    """Render env-var changes do not auto-deploy, so a sync that skipped this
    would report success while the service kept serving the old values."""
    resp = httpx.post(
        f"{_RENDER_API}/services/{service_id}/deploys",
        headers=_render_headers(),
        json={},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    deploy_id = _unwrap(resp.json(), "deploy").get("id")
    print(f"deploy {deploy_id} triggered; waiting for live")
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_DEPLOY_POLL_SECONDS)
        poll = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        poll.raise_for_status()
        status = _unwrap(poll.json(), "deploy").get("status", "?")
        if status == "live":
            print("deploy live")
            return 0
        if status in _DEPLOY_FAILED_STATUSES:
            print(f"deploy {status}", file=sys.stderr)
            return 1
    print("timed out waiting for the deploy to go live", file=sys.stderr)
    return 1


def sync_env() -> int:
    """Push local config to the Render service, then deploy and wait.

    Only ever uses the single-key endpoint: the bulk
    PUT /v1/services/{id}/env-vars replaces the entire list and would silently
    delete every variable not in the payload, DATABASE_URL included.
    """
    if not settings.render_api_key:
        print("--sync-env requires RENDER_API_KEY", file=sys.stderr)
        return 2
    wanted = _wanted_env()
    empty = sorted(key for key, value in wanted.items() if not value)
    if empty:
        # Before any request, so a partial push cannot happen.
        print(f"refusing to push empty values; fix .env first: {', '.join(empty)}", file=sys.stderr)
        return 2
    entry = _PROVIDERS.get(wanted["LLM_PROVIDER"])
    provider_key = entry[0] if entry else None
    if provider_key and provider_key not in wanted:
        print(
            f"refusing to sync LLM_PROVIDER={wanted['LLM_PROVIDER']}: {provider_key} is not "
            f"in the synced set, so the service would run without its provider key",
            file=sys.stderr,
        )
        return 2
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            print(f"no Render service named {settings.render_service_name}", file=sys.stderr)
            return 1
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/env-vars",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        current = {}
        for item in resp.json():
            env_var = _unwrap(item, "envVar")
            current[env_var.get("key")] = env_var.get("value")

        changed = [key for key, value in wanted.items() if current.get(key) != value]
        for key in changed:
            put = httpx.put(
                f"{_RENDER_API}/services/{service_id}/env-vars/{key}",
                headers=_render_headers(),
                json={"value": wanted[key]},
                timeout=_HTTP_TIMEOUT,
            )
            put.raise_for_status()
            print(f"pushed {key} (len {len(wanted[key])})")   # names and lengths only
        if not changed:
            print("env vars already in sync; no deploy triggered")
            return 0
        return _trigger_and_wait(service_id)
    except Exception as exc:  # noqa: BLE001 - deliberate: a crashed sync is "could not run"
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 2


def _safe(name: str, fn, *args) -> CheckResult:
    """No check may abort the run: a complete table is the deliverable."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - deliberate: any failure becomes a row
        return CheckResult(name, "FAIL", f"unexpected {type(exc).__name__}")


def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All six, cheapest and most foundational first, so a misconfiguration is
    reported before the checks that would fail as a consequence of it."""
    return [
        _safe("config", check_config),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy",
        # Without this, argparse treats --sync-en as an abbreviation of
        # --sync-env and RUNS the sync. Not hypothetical: it fired a real
        # deploy against live infrastructure during development.
        allow_abbrev=False,
        description=(
            "Verify the hosted deployment: configuration, GitHub App installation "
            "and webhook, health endpoint, database, Render service, and keep-warm "
            "pinger. Exit 0 all passed, 1 a check failed, 2 could not run."
        ),
    )
    parser.add_argument(
        "--sync-env",
        action="store_true",
        help="push local config to the Render service, deploy, and wait for live",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    repo = settings.github_target_repo
    base = resolve_base_url()
    if not repo or not base:
        print(
            "GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
            "are required",
            file=sys.stderr,
        )
        return 2
    if args.sync_env:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(repo, base)
    print(render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
