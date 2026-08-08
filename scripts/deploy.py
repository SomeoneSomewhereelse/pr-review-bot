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

from app.config import settings

_NAME_WIDTH = 18
_STATUS_WIDTH = 9
_README_ANCHOR = "README.md#deploying-to-production"


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
