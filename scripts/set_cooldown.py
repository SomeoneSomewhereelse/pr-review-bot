"""Set or clear the DB-backed cooldown override (base/cap/factor).

    uv run python -m scripts.set_cooldown --base 30 --factor 1.5
    uv run python -m scripts.set_cooldown --cap 600
    uv run python -m scripts.set_cooldown --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local
.env this sets a LOCAL override and nothing reaches production.

Unlike scripts/set_provider.py, there is no credential at stake here -- only
numbers. Before writing, this checks (when RENDER_API_KEY is set) whether the
local DATABASE_URL matches the live Render service's, purely as an
informational signal that the write will actually reach production; it never
refuses the write, so there is no --force flag.

A plain tool, not a slash command -- matches scripts/set_provider.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.queue import store
from scripts import _render


def _verify_render_reachability() -> str:
    """A human-readable status line about whether this write reaches the
    Render-hosted production database. Never blocks the write -- see the
    module docstring. Never returns, prints, or logs a fetched Render value,
    only presence/absence and in-memory equality results (matches
    set_provider.py's credential-leak guard, applied here to DATABASE_URL)."""
    if not settings.render_api_key:
        return (
            "could not verify against Render (no RENDER_API_KEY); "
            "writing without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); writing without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return (
            f"could not verify against Render ({type(exc).__name__}); "
            "writing without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return (
            "could not confirm this DATABASE_URL is the one the Render "
            "service reads -- writing anyway"
        )
    return "DATABASE_URL verified against the live Render service"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_cooldown",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_provider.py
        # carries the same guard after an identical abbreviation match fired
        # a live production incident on a different script.
        allow_abbrev=False,
        description="Set or clear the DB-backed re-review cooldown override (base/cap/factor).",
    )
    parser.add_argument("--base", type=float, help="base cooldown in seconds")
    parser.add_argument("--cap", type=float, help="cooldown cap in seconds")
    parser.add_argument("--factor", type=float, help="escalation factor (must be >= 1.0)")
    parser.add_argument(
        "--clear", action="store_true", help="remove all three overrides; fall back to env vars"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and args.base is None and args.cap is None and args.factor is None:
        print("at least one of --base/--cap/--factor is required (or --clear)", file=sys.stderr)
        return 2
    if args.factor is not None and args.factor < 1.0:
        print(f"--factor must be >= 1.0 (got {args.factor})", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    store.init_pool()

    if args.clear:
        store.set_cooldown_override(base=None, cap=None, factor=None, now=now)
        print("cleared; falling back to the env-configured cooldown defaults")
        return 0

    print(_verify_render_reachability())
    current_base, current_cap, current_factor = store.get_cooldown_overrides()
    new_base = args.base if args.base is not None else current_base
    new_cap = args.cap if args.cap is not None else current_cap
    new_factor = args.factor if args.factor is not None else current_factor
    store.set_cooldown_override(base=new_base, cap=new_cap, factor=new_factor, now=now)
    print(
        f"cooldown override: base {current_base} -> {new_base}, "
        f"cap {current_cap} -> {new_cap}, factor {current_factor} -> {new_factor}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
