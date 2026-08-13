"""Set or clear the DB-backed cooldown override (base/cap/factor).

    uv run python -m scripts.set_cooldown --base 30 --factor 1.5
    uv run python -m scripts.set_cooldown --cap 600
    uv run python -m scripts.set_cooldown --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local
.env this sets a LOCAL override and nothing reaches production.

Unlike scripts/set_override.py, there is no credential at stake here -- only
numbers. Before writing, this checks (when RENDER_API_KEY is set) whether the
local DATABASE_URL matches the live Render service's, purely as an
informational signal that the write will actually reach production; that
check never refuses the write, so there is no --force flag. It DOES refuse
the write (exit 2) if the merged base/cap/factor -- resolved against env
defaults for any unset field, the same way cooldown_config.effective_config()
resolves it at read time -- would be invalid (factor < 1.0, base > cap, or a
non-positive base/cap): writing such a value would succeed but be silently
discarded as a whole triple on every read, leaving the override inert.

A plain tool, not a slash command -- matches scripts/set_override.py.
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
    set_override.py's credential-leak guard, applied here to DATABASE_URL)."""
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
        # abbreviation of --clear and runs it -- scripts/set_override.py
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
    if args.clear and (args.base is not None or args.cap is not None or args.factor is not None):
        print(
            "--clear cannot be combined with --base/--cap/--factor -- clear "
            "first, then re-set individual fields in a separate call",
            file=sys.stderr,
        )
        return 2
    if args.factor is not None and args.factor < 1.0:
        print(f"--factor must be >= 1.0 (got {args.factor})", file=sys.stderr)
        return 2
    if args.base is not None and args.base <= 0:
        print(f"--base must be > 0 (got {args.base})", file=sys.stderr)
        return 2
    if args.cap is not None and args.cap <= 0:
        print(f"--cap must be > 0 (got {args.cap})", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    store.init_pool()

    if args.clear:
        store.set_cooldown_override(base=None, cap=None, factor=None, now=now)
        print("cleared; falling back to the env-configured cooldown defaults")
        return 0

    current_base, current_cap, current_factor = store.get_cooldown_overrides()
    new_base = args.base if args.base is not None else current_base
    new_cap = args.cap if args.cap is not None else current_cap
    new_factor = args.factor if args.factor is not None else current_factor

    # Resolve the merged triple against env defaults exactly like
    # cooldown_config.effective_config() does at read time, and refuse the
    # write outright if the result would be invalid -- otherwise a partial
    # write (e.g. --cap alone, below the env base) would write successfully
    # but be silently discarded as a WHOLE triple on every read, leaving the
    # override completely inert while this script reports success.
    resolved_base = (
        new_base if new_base is not None else settings.dispatcher_rereview_cooldown_seconds
    )
    resolved_cap = (
        new_cap if new_cap is not None else settings.dispatcher_rereview_cooldown_max_seconds
    )
    resolved_factor = (
        new_factor if new_factor is not None else settings.dispatcher_rereview_cooldown_factor
    )
    if (
        resolved_factor < 1.0
        or resolved_base > resolved_cap
        or resolved_base <= 0
        or resolved_cap <= 0
    ):
        print(
            "refusing to write: the resulting cooldown would resolve to "
            f"base={resolved_base} cap={resolved_cap} factor={resolved_factor}, "
            "which effective_config() would discard entirely (needs "
            "factor >= 1.0, 0 < base <= cap) -- the write would be a no-op",
            file=sys.stderr,
        )
        return 2

    print(_verify_render_reachability())
    store.set_cooldown_override(base=new_base, cap=new_cap, factor=new_factor, now=now)
    print(
        f"cooldown override: base {current_base} -> {new_base}, "
        f"cap {current_cap} -> {new_cap}, factor {current_factor} -> {new_factor}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
