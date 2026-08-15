"""Set or clear the DB-backed per-key usage-cap override (tokens/cost/reset).

    uv run python -m scripts.set_usage_cap --tokens 20000
    uv run python -m scripts.set_usage_cap --cost 0.50 --reset 06:30
    uv run python -m scripts.set_usage_cap --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local .env
this sets a LOCAL override and nothing reaches production.

Unlike scripts/set_override.py, there is no credential at stake here -- only
numbers and a wall-clock time. Before writing, this checks (when RENDER_API_KEY
is set) whether the local DATABASE_URL matches the live Render service's, purely
as an informational signal that the write will actually reach production; that
check never refuses the write, so there is no --force flag.

It DOES refuse the write (exit 2) if the merged trio -- resolved against env
defaults for any unset field, exactly the way usage_cap_config.effective_caps()
resolves it at read time -- would be discarded as invalid. Writing such a value
would succeed and then be silently ignored on every read, leaving the override
inert while this script reported success. That matters more here than for the
cooldown override: a cap the dispatcher does honour but which is wrong-way-round
(non-positive) defers EVERY ticket, and the deferral is STICKY -- a ticket's
not_before is already a real future timestamp by then, so correcting the
override afterwards does not release already-deferred tickets.

A plain tool, not a slash command -- matches scripts/set_cooldown.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timezone

from app.config import settings
from app.queue import store
from scripts import _render


def _verify_render_reachability() -> str:
    """A human-readable status line about whether this write reaches the
    Render-hosted production database. Never blocks the write -- see the module
    docstring. Never returns, prints, or logs a fetched Render value, only
    presence/absence and in-memory equality results (matches
    scripts/set_cooldown.py's identical guard)."""
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
        prog="set_usage_cap",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_override.py and
        # scripts/set_cooldown.py carry the same guard after an identical
        # abbreviation match fired a live production sync.
        allow_abbrev=False,
        description="Set or clear the DB-backed per-key usage-cap override.",
    )
    parser.add_argument("--tokens", type=int, help="daily token cap for the active key slot")
    parser.add_argument("--cost", type=float, help="daily USD cost cap for the active key slot")
    parser.add_argument("--reset", help="usage-day rollover, UTC HH:MM or HH:MM:SS")
    parser.add_argument(
        "--clear", action="store_true", help="remove all three overrides; fall back to env vars"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and args.tokens is None and args.cost is None and args.reset is None:
        print(
            "at least one of --tokens/--cost/--reset is required (or --clear)",
            file=sys.stderr,
        )
        return 2
    if args.clear and (
        args.tokens is not None or args.cost is not None or args.reset is not None
    ):
        print(
            "--clear cannot be combined with --tokens/--cost/--reset -- clear "
            "first, then re-set individual fields in a separate call",
            file=sys.stderr,
        )
        return 2
    if args.tokens is not None and args.tokens <= 0:
        print(f"--tokens must be > 0 (got {args.tokens})", file=sys.stderr)
        return 2
    if args.cost is not None and args.cost <= 0:
        print(f"--cost must be > 0 (got {args.cost})", file=sys.stderr)
        return 2
    if args.reset is not None:
        try:
            time.fromisoformat(args.reset)
        except ValueError:
            print(
                f"--reset must be UTC HH:MM or HH:MM:SS (got {args.reset!r})",
                file=sys.stderr,
            )
            return 2

    now = datetime.now(timezone.utc).isoformat()
    store.init_pool()

    if args.clear:
        store.set_usage_cap_override(tokens=None, cost=None, reset=None, now=now)
        print("cleared; falling back to the env-configured usage caps")
        return 0

    current_tokens, current_cost, current_reset = store.get_usage_cap_overrides()
    new_tokens = args.tokens if args.tokens is not None else current_tokens
    new_cost = args.cost if args.cost is not None else current_cost
    new_reset = args.reset if args.reset is not None else current_reset

    # Check whether the merged trio would be discarded as invalid by
    # effective_caps(). This mirrors effective_caps()'s own discard predicate:
    # non-positive numeric caps, or an unparseable reset string. We refuse
    # based on whether the merged trio *itself* is invalid, never based on
    # whether it happens to equal env defaults (which would be a false positive
    # if the operator's values coincidentally match the configured defaults).
    if new_tokens is not None and new_tokens <= 0:
        print(
            "refusing to write: the resulting override would have a "
            f"non-positive token cap ({new_tokens}) -- effective_caps() would "
            "discard the entire trio, leaving the override inert while this "
            "script reported success",
            file=sys.stderr,
        )
        return 2
    if new_cost is not None and new_cost <= 0:
        print(
            "refusing to write: the resulting override would have a "
            f"non-positive cost cap ({new_cost}) -- effective_caps() would "
            "discard the entire trio, leaving the override inert while this "
            "script reported success",
            file=sys.stderr,
        )
        return 2
    if new_reset is not None:
        try:
            time.fromisoformat(new_reset)
        except ValueError:
            print(
                "refusing to write: the resulting override would have an "
                f"unparseable reset time ({new_reset!r}) -- effective_caps() "
                "would discard the entire trio, leaving the override inert while "
                "this script reported success",
                file=sys.stderr,
            )
            return 2

    print(_verify_render_reachability())
    store.set_usage_cap_override(
        tokens=new_tokens, cost=new_cost, reset=new_reset, now=now
    )
    print(
        f"usage cap override: tokens {current_tokens} -> {new_tokens}, "
        f"cost {current_cost} -> {new_cost}, reset {current_reset} -> {new_reset}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
