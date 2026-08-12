"""Set or clear the DB-backed API-key-slot index override for a provider.

    uv run python -m scripts.set_api_key groq 2
    uv run python -m scripts.set_api_key groq --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local
.env this sets a LOCAL override and nothing reaches production.

Naming convention: index 0 is the base env var (e.g. GROQ_API_KEY); index
N >= 1 is the base name with an "_N" suffix (GROQ_API_KEY_1, _2, ...). This
script never reads, prints, or stores a credential VALUE -- only the index,
and only a presence check against Render before writing.

Before writing a non-cleared override, this verifies the target env var's
PRESENCE (not its value, and not a live call to the provider) against the
live Render service (when RENDER_API_KEY is set and the local DATABASE_URL
is the one Render actually reads) and refuses by default if it's missing --
pass --force to write anyway. Unlike scripts/set_provider.py's
_verify_render_credential, this does not compare against a local .env value:
a numbered slot typically has no local counterpart at all, so presence on
the live service is the only meaningful check.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.providers import registry
from app.queue import store
from scripts import _render


def _env_name(provider: str, index: int) -> str:
    base, _ = registry.PROVIDERS[provider]
    return base if index == 0 else f"{base}_{index}"


def _verify_render_key_slot(provider: str, index: int) -> tuple[bool, str]:
    """(ok_to_proceed, message). Never returns, prints, or logs a fetched
    Render value -- only presence/absence -- mirroring
    scripts/set_provider.py's _verify_render_credential.
    """
    env_name = _env_name(provider, index)
    if not settings.render_api_key:
        return True, (
            "could not verify against Render (no RENDER_API_KEY); "
            "setting override without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return True, (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); setting override without live verification"
            )
        env_vars = _render.env_vars(service_id)
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

    live_value = env_vars.get(env_name) or ""
    if not live_value:
        return False, (
            f"{env_name} is missing on the Render service; the override would fail "
            "every review immediately. Push it first, or pass --force"
        )
    return True, f"{env_name} verified present on Render"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_api_key",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_provider.py and
        # scripts/deploy.py carry the same guard after an identical
        # abbreviation match fired a live production sync.
        allow_abbrev=False,
        description="Set or clear the DB-backed API-key-slot index override for a provider.",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(registry.PROVIDERS))}",
    )
    parser.add_argument(
        "index",
        nargs="?",
        type=int,
        help="the slot index to activate (0 = the base env var, N = the _N suffix)",
    )
    parser.add_argument(
        "--clear", action="store_true", help="remove the override; fall back to index 0"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the override even if live verification against Render finds a problem",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.provider:
        print("a provider is required", file=sys.stderr)
        return 2
    if args.provider not in registry.PROVIDERS:
        accepted = ", ".join(sorted(registry.PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if args.clear and args.index is not None:
        print("--clear and an index are mutually exclusive", file=sys.stderr)
        return 2
    if not args.clear and args.index is None:
        print("an index is required (or --clear)", file=sys.stderr)
        return 2
    if not args.clear and args.index < 0:
        print(f"index must be >= 0, got {args.index}", file=sys.stderr)
        return 2

    index = None if args.clear else args.index
    if index is not None:
        ok, message = _verify_render_key_slot(args.provider, index)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2
    store.init_pool()
    store.set_key_index_override(args.provider, index, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to index 0" if index is None
          else f"override set to index {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
