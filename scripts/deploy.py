"""Deploy verification CLI for the hosted Render + Supabase deployment.

Runs seven independent checks and prints one aligned table. Every check runs
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

# The service env vars --sync-env always pushes, regardless of provider.
# Authoritative: tests/test_deploy_script.py asserts README.md and SETUP.md
# each mention every name here. (Widening that check to every _PROVIDERS name
# too is deferred -- README.md and SETUP.md do not yet document every
# provider's model var, and updating those docs is a later task's deliverable.)
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_B64",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
)
_DEPLOY_POLL_SECONDS = 10
# A cold Docker build with a full dependency install runs well past five
# minutes; the measured ~60s redeploys had warm layers. Too short a timeout
# makes the FIRST deploy the most likely to report a false failure.
_DEPLOY_TIMEOUT_SECONDS = 900
_DEPLOY_IN_FLIGHT_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
# "canceled" is deliberately absent: it is what a superseding deploy looks
# like, not a build failure, and is reported separately.
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "deactivated",
}
# ~5 minutes between progress lines at the default 10s poll interval, so a
# long in-flight wait never goes more than a few minutes without visible
# output (SETUP.md's documented history includes a real operator mistake
# against live infra made during an apparently-silent stretch).
_IN_FLIGHT_PROGRESS_EVERY = 30


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


def _private_key_b64() -> tuple[str, str]:
    """The PEM in the base64 form Render needs, plus a problem string
    ("" when usable).

    Reads rather than stats: an existing-but-unreadable PEM must not report as
    available, because check_config would pass while _wanted_env raised on the
    same file. Returning the problem instead of raising keeps the CLI's exit
    contract intact -- a config problem is a FAIL row, not a traceback.
    """
    if settings.github_app_private_key_b64:
        return settings.github_app_private_key_b64, ""
    path = Path(settings.github_app_private_key_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return base64.b64encode(path.read_bytes()).decode(), ""
    except FileNotFoundError:
        return "", "GITHUB_APP_PRIVATE_KEY_B64 or _PATH"
    except OSError as exc:
        return "", f"unreadable PEM {path} ({type(exc).__name__})"


def check_config() -> CheckResult:
    """Every value the deployed service needs, resolvable locally.

    Reports missing key NAMES only -- never a value, never a length."""
    missing: list[str] = []
    problems: list[str] = []
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    key_b64, key_problem = _private_key_b64()
    if key_problem and key_problem.startswith("unreadable"):
        problems.append(key_problem)
    elif key_problem:
        missing.append(key_problem)
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


def _resolved_provider() -> tuple[str, str | None]:
    """(active provider, override or None). The override wins at runtime, so
    the CLI must resolve exactly as the dispatcher does.

    Reads via a raw short-timeout connection rather than store.init_pool(),
    for the same reason check_database does: the pool blocks 30s before
    raising, which is a startup-log behaviour, not a checklist one -- and this
    is a one-shot CLI, not a long-lived service amortising that cost.

    A bare psycopg.connect (unlike the store's pool) does not set
    row_factory=dict_row, so the row -- if any -- comes back as a tuple, and
    an empty-string override normalizes to None exactly as
    store.get_provider_override() does, so the CLI and the dispatcher can
    never disagree about whether an override is active.
    """
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    override = (row[0] if row else None) or None
    return (override or settings.llm_provider), override


def check_provider() -> CheckResult:
    """Which provider will actually run, and whether its credential exists.

    Without this, a DB override makes every other check's provider assumption
    unverifiable: the service could run a provider whose key was never checked.
    """
    name = "provider"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to resolve the override")
    try:
        provider, override = _resolved_provider()
    # deliberate: a DB problem is database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIPPED", f"could not read the override ({type(exc).__name__})")
    source = f"DB override; env={settings.llm_provider}" if override else "env"
    entry = _PROVIDERS.get(provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) is not supported (expected: {accepted})"
        )
    credential = entry[0]
    if not getattr(settings, credential.lower(), ""):
        return CheckResult(name, "FAIL", f"{provider} ({source}) -- {credential} missing")
    return CheckResult(name, "PASS", f"{provider} ({source})")


def _render_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def _unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def _local_head() -> tuple[str, bool] | None:
    """(short HEAD sha, working tree is dirty), or None outside a git repo.

    Uses subprocess rather than a dependency: the CLI must run from a plain
    checkout with no extra installs.
    """
    import subprocess

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return (head, dirty) if head else None


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
    deploy_obj = _unwrap(deploys[0], "deploy")
    status = deploy_obj.get("status", "?")
    if status != "live":
        return CheckResult(name, "FAIL", f"latest deploy status: {status}")

    deploy_id = deploy_obj.get("id", "?")
    commit = (deploy_obj.get("commit") or {}).get("id") or ""
    image = (deploy_obj.get("image") or {}).get("ref") or ""

    if image:
        return CheckResult(
            name, "PASS",
            f"live: {deploy_id} @ {image}\n(image-backed; no local comparison possible)",
        )
    if not commit:
        # Assumption 4 is unverified -- degrade rather than invent a failure.
        return CheckResult(name, "PASS", f"live: {deploy_id}")

    local = _local_head()
    if local is None:
        return CheckResult(
            name, "PASS", f"live: {deploy_id} @ {commit[:7]} (no git checkout here)"
        )
    head, dirty = local
    # Compare on a common short prefix: Render returns a full sha, `git
    # rev-parse --short` a 7-char one, so a direct == would always differ.
    if commit[:7] != head[:7]:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]}, but local HEAD is {head}\n"
            f"push, or re-run --sync-env, to deploy what you have",
        )
    if dirty:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]} (local HEAD matches, tree dirty\n"
            f"-- uncommitted changes cannot be in any build)",
        )
    return CheckResult(name, "PASS", f"live: {deploy_id} @ {commit[:7]}")


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
    """Local values for every var --sync-env will push.

    Keys depend on the selected provider: the five always-synced vars, plus
    LLM_PROVIDER, plus the selected provider's credential and model var. Any
    other provider's credential is included only when it has a local value --
    an opt-in .env lists the others empty, and must never be asked to fill them.
    """
    pem_b64, _ = _private_key_b64()
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY_B64": pem_b64,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "LLM_PROVIDER": settings.llm_provider,
    }
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is not None:
        credential, model_var = entry
        wanted[credential] = getattr(settings, credential.lower(), "")
        wanted[model_var] = getattr(settings, model_var.lower(), "")
    for other_credential, _ in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
    return wanted


def _wait_for_in_flight(service_id: str) -> bool:
    """Block until no deploy is building, or the timeout runs out.

    Waits rather than adopts: a deploy that started before the env-var push may
    have resolved its environment already, so adopting it could report "deploy
    live" for a container still running the old config.

    Returns True once settled (or nothing was building), False on timeout. The
    timeout path is deliberately a refusal, not a quiet return: a caller that
    ignored the return value and triggered anyway would stack a second deploy
    on one still building -- the exact collision this function exists to
    prevent. Do not "simplify" this back to a bare return.
    """
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    announced = False
    polls = 0
    deploy_id = None
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
        if not deploys:
            return True
        deploy_obj = _unwrap(deploys[0], "deploy")
        deploy_id = deploy_obj.get("id")
        if deploy_obj.get("status") not in _DEPLOY_IN_FLIGHT_STATUSES:
            return True
        if not announced:
            print(f"waiting for in-flight deploy {deploy_id} to settle")
            announced = True
        elif polls % _IN_FLIGHT_PROGRESS_EVERY == 0:
            print(f"  still waiting for in-flight deploy {deploy_id} to settle")
        polls += 1
        time.sleep(_DEPLOY_POLL_SECONDS)
    print(
        f"timed out waiting for in-flight deploy {deploy_id} to settle -- refusing to "
        f"stack a second deploy on top of it; re-run once it finishes",
        file=sys.stderr,
    )
    return False


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
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(_DEPLOY_POLL_SECONDS)
        poll = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        poll.raise_for_status()
        status = _unwrap(poll.json(), "deploy").get("status", "?")
        if status != last_status:
            print(f"  {status}")          # visible progress on a long build
            last_status = status
        if status == "live":
            print("deploy live")
            return 0
        if status == "canceled":
            print(
                f"deploy {deploy_id} was superseded (canceled) -- env vars WERE "
                f"pushed and a newer deploy is running; re-run to confirm it goes live",
                file=sys.stderr,
            )
            return 1
        if status in _DEPLOY_FAILED_STATUSES:
            print(f"deploy {status}", file=sys.stderr)
            return 1
    print("timed out waiting for the deploy to go live", file=sys.stderr)
    return 1


def _render_env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it. See CLAUDE.md's "no secret is ever
    logged" and docs/superpowers/specs/
    2026-08-10-provider-live-credential-verification-design.md section 6.
    """
    resp = httpx.get(
        f"{_RENDER_API}/services/{service_id}/env-vars",
        headers=_render_headers(),
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    current: dict[str, str] = {}
    for item in resp.json():
        env_var = _unwrap(item, "envVar")
        current[env_var.get("key")] = env_var.get("value")
    return current


def sync_env() -> int:
    """Push local config to the Render service, then deploy and wait.

    Only ever uses the single-key endpoint: the bulk
    PUT /v1/services/{id}/env-vars replaces the entire list and would silently
    delete every variable not in the payload, DATABASE_URL included.
    """
    if not settings.render_api_key:
        print("--sync-env requires RENDER_API_KEY", file=sys.stderr)
        return 2
    if settings.llm_provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"refusing to sync LLM_PROVIDER={settings.llm_provider!r}: "
            f"not a supported provider (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if settings.database_url:
        try:
            _, override = _resolved_provider()
        # deliberate: the provider check reports DB trouble
        except Exception:  # noqa: BLE001
            override = None
        if override and override != settings.llm_provider:
            print(
                f"refusing to sync: a DB provider override ({override}) is active and "
                f"wins over the LLM_PROVIDER={settings.llm_provider} being pushed. "
                "Clear it first: uv run python -m scripts.set_provider --clear",
                file=sys.stderr,
            )
            return 2
    wanted = _wanted_env()
    empty = sorted(key for key, value in wanted.items() if not value)
    if empty:
        # This guard alone runs before any HTTP request, so refusing here
        # can never leave a partial push behind. Only the keys this provider
        # actually needs are in `wanted`, so this can never name another
        # provider's credential.
        print(
            f"refusing to push empty values; fix .env first: {', '.join(empty)}",
            file=sys.stderr,
        )
        return 2
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            print(f"no Render service named {settings.render_service_name}", file=sys.stderr)
            return 1
        current = _render_env_vars(service_id)
        changed = [key for key, value in wanted.items() if current.get(key) != value]
    # deliberate: nothing has been pushed yet, so a crashed lookup really is
    # "could not run at all"
    except Exception as exc:  # noqa: BLE001
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 2

    # Tracks which keys have actually been written to the service. Once this
    # is non-empty, a later failure is a PARTIAL push, not a failure to
    # start -- it must be reported as exit 1 (with the pushed names), never
    # exit 2, or an operator would read "never really started" and walk away
    # from a service that is now half-configured.
    pushed: list[str] = []
    for key in changed:
        try:
            put = httpx.put(
                f"{_RENDER_API}/services/{service_id}/env-vars/{key}",
                headers=_render_headers(),
                json={"value": wanted[key]},
                timeout=_HTTP_TIMEOUT,
            )
            put.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            if pushed:
                return _report_partial_push(pushed, exc)
            # deliberate: no push has succeeded yet, so this is still
            # "could not run at all"
            print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
            return 2
        pushed.append(key)
        print(f"pushed {key} (len {len(wanted[key])})")   # names and lengths only
    if not changed:
        print("env vars already in sync; no deploy triggered")
        return 0
    try:
        if not _wait_for_in_flight(service_id):
            return 1
        return _trigger_and_wait(service_id)
    # deliberate: every key in `pushed` is already live on the service, so a
    # failure past this point is a partial push, not a failure to start
    except Exception as exc:  # noqa: BLE001
        return _report_partial_push(pushed, exc)


def _report_partial_push(pushed: list[str], exc: BaseException) -> int:
    """Exit 1, naming (never valuing) the vars that made it to the service
    before ``exc`` -- the operator's next step is to fix the problem and
    re-run --sync-env, not to assume nothing happened."""
    print(
        f"partial push: {', '.join(pushed)} already pushed to the service "
        f"before a {type(exc).__name__}; fix the problem and re-run --sync-env "
        "to finish (this is NOT \"could not run\" -- some vars already changed)",
        file=sys.stderr,
    )
    return 1


def _safe(name: str, fn, *args) -> CheckResult:
    """No check may abort the run: a complete table is the deliverable."""
    try:
        return fn(*args)
    # deliberate: any failure becomes a row
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"unexpected {type(exc).__name__}")


def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All seven, cheapest and most foundational first, so a misconfiguration
    is reported before the checks that would fail as a consequence of it."""
    return [
        _safe("config", check_config),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("provider", check_provider),
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
            "and webhook, health endpoint, database, active provider, Render service, "
            "and keep-warm pinger. Exit 0 all passed, 1 a check failed, 2 could not run."
        ),
    )
    parser.add_argument(
        "--sync-env",
        action="store_true",
        help=(
            "push local config to the Render service, deploy, and wait for live "
            "(worst case ~30 minutes: up to 900s waiting out an in-flight deploy, "
            "then up to 900s for the newly triggered one)"
        ),
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
