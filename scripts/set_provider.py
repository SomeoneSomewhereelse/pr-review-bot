"""Set or clear the DB-backed provider override.

    uv run python scripts/set_provider.py groq
    uv run python scripts/set_provider.py --clear

The override takes effect on the next claimed ticket -- no restart, no redeploy.
It writes to whatever DATABASE_URL points at, so against a local .env this sets
a LOCAL override and nothing reaches production.

Validation is limited to the provider name: this runs on the operator's machine
and cannot know whether that provider's credential exists on the deployed
service. `scripts/deploy.py`'s `provider` check is the safety net for that.

A plain tool, not a slash command -- a demo proving provider-agnosticism must
not itself depend on Claude being present.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.queue import store
from scripts.deploy import _PROVIDERS


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
    store.init_pool()
    store.set_provider_override(provider, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to LLM_PROVIDER" if provider is None
          else f"override set to {provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
