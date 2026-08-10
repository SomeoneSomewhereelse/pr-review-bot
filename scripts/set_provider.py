"""Set or clear the DB-backed provider override.

    uv run python -m scripts.set_provider groq
    uv run python -m scripts.set_provider --clear

The override takes effect on the next claimed ticket -- no restart, no redeploy.
It writes to whatever DATABASE_URL points at, so against a local .env this sets
a LOCAL override and nothing reaches production.

Before writing a non-cleared override, this verifies the target provider's
credential against the live Render service (when RENDER_API_KEY is set and
the local DATABASE_URL is the one Render actually reads) and refuses by
default if it's missing or differs from the local .env value -- pass --force
to write anyway. `scripts/deploy.py`'s `provider-live` check is the read-only
counterpart to this write-time guard.

A plain tool, not a slash command -- a demo proving provider-agnosticism must
not itself depend on Claude being present.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.queue import store
from scripts.deploy import _PROVIDERS, _find_render_service_id, _render_env_vars


def _verify_render_credential(provider: str) -> tuple[bool, str]:
    """(ok_to_proceed, message). Never returns, prints, or logs a fetched
    Render value -- only presence/absence and in-memory equality results. See
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6 for the invariant this maintains.
    """
    if not settings.render_api_key:
        return True, (
            "could not verify against Render (no RENDER_API_KEY); "
            "setting override without live verification"
        )
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return True, (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); setting override without live verification"
            )
        env_vars = _render_env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return True, (
            f"could not verify against Render ({type(exc).__name__}); "
            "setting override without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, (
            "could not confirm this DATABASE_URL is the one the Render service reads "
            "-- skipping live verification"
        )

    credential, _ = _PROVIDERS[provider]
    live_value = env_vars.get(credential) or ""
    local_value = getattr(settings, credential.lower(), "")
    if not live_value:
        return False, (
            f"{credential} is missing on the Render service; the override would fail "
            "every review immediately. Push it first (uv run python -m scripts.deploy "
            "--sync-env) or pass --force"
        )
    if not local_value:
        return True, f"{credential} present on Render (no local value to compare)"
    if live_value != local_value:
        return False, (
            f"{credential} on Render differs from your local .env value; the running "
            "service may use an unexpected key. Sync first, or pass --force"
        )
    return True, f"{credential} verified on Render (matches local .env)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_provider",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/deploy.py carries the
        # same guard after an identical abbreviation match fired a live
        # production sync. See its build_parser() for the incident.
        allow_abbrev=False,
        description="Set or clear the DB-backed LLM provider override.",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(_PROVIDERS))}",
    )
    parser.add_argument(
        "--clear", action="store_true", help="remove the override; fall back to LLM_PROVIDER"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the override even if live verification against Render finds a problem",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and not args.provider:
        print("a provider is required (or --clear)", file=sys.stderr)
        return 2
    if args.provider and args.provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    provider = None if args.clear else args.provider
    if provider is not None:
        ok, message = _verify_render_credential(provider)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2
    store.init_pool()
    store.set_provider_override(provider, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to LLM_PROVIDER" if provider is None
          else f"override set to {provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
