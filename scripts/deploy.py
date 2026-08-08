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

import os
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
_README_ANCHOR = "README.md#deploying-to-production"
_HTTP_TIMEOUT = 10.0
_DB_CONNECT_TIMEOUT = 10


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


# The provider key each LLM_PROVIDER value requires. An unrecognized provider
# contributes no requirement rather than a false failure.
_PROVIDER_KEYS = {
    "groq": "GROQ_API_KEY",
    "github_models": "GITHUB_MODELS_TOKEN",
    "gemini": "GEMINI_API_KEY",
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
    provider_key = _PROVIDER_KEYS.get(settings.llm_provider)
    if provider_key and not getattr(settings, provider_key.lower(), ""):
        missing.append(provider_key)
    if missing:
        return CheckResult("config", "FAIL", "missing: " + ", ".join(missing))
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
    except RuntimeError:
        return CheckResult(name, "FAIL", "installation lookup failed; check App ID / private key")

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook read failed ({exc.status})"
        )
    if current == wanted:
        return CheckResult(name, "PASS", f"installation={installation_id}; webhook already correct")
    github_app.set_webhook_url(wanted)
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


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("wired up in a later task")


if __name__ == "__main__":
    raise SystemExit(main())
