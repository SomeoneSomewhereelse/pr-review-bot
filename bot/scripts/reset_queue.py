"""Truncate the `tickets` and `reviews` tables -- a clean-slate reset for
manual/live test runs (e.g. before a multi-repo test pass) so the dashboard
and queue start empty instead of carrying over old test data.

    uv run python -m bot.scripts.reset_queue           # dry run: reports counts only
    uv run python -m bot.scripts.reset_queue --yes      # actually truncates

Writes to whatever DATABASE_URL points at -- against a local .env this resets
a LOCAL database and nothing reaches production. Never touches `runtime_config`
(provider/model/cooldown overrides), only the queue and review-history tables.
No FKs reference either table, so no CASCADE is needed.

Mirrors scripts/deploy.py::sync_config_db()'s reachability check: never prints
DATABASE_URL, only a presence/equality check against the live Render service
when RENDER_API_KEY is available, and row counts -- never row content.
"""

from __future__ import annotations

import argparse
import sys

from bot import render_client as _render
from bot.config import settings
from bot.queue import store


def _verify_render_reachability() -> str:
    """Human-readable status about whether this write reaches the Render-hosted
    production database. Never blocks the write -- purely informational, so an
    operator running this locally knows whether they're about to wipe
    production or a local database. Never prints a fetched Render value, only
    presence/absence and in-memory equality results."""
    if not settings.render_api_key:
        return (
            "could not verify against Render (no RENDER_API_KEY); "
            "resetting without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); resetting without live verification"
            )
        env_vars = _render.env_vars(service_id)
    except Exception as exc:  # noqa: BLE001 -- degrade to a warning, never a refusal
        return (
            f"could not verify against Render ({type(exc).__name__}); "
            "resetting without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return (
            "could not confirm this DATABASE_URL is the one the Render "
            "service reads -- resetting anyway"
        )
    return "DATABASE_URL verified against the live Render service"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reset_queue",
        allow_abbrev=False,
        description="Truncate the tickets and reviews tables (queue + review history).",
    )
    parser.add_argument(
        "--yes", action="store_true", help="actually truncate; without this, dry-run only"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    store.init_pool()
    pool = store._require_pool()
    with pool.connection() as conn:
        tickets_n = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
        reviews_n = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]

        print(_verify_render_reachability())

        if not args.yes:
            print(f"dry run -- would remove {tickets_n} ticket row(s), {reviews_n} review row(s)")
            print("re-run with --yes to actually truncate")
            return 0

        conn.execute("TRUNCATE TABLE tickets, reviews RESTART IDENTITY")

    print(f"tickets: {tickets_n} row(s) removed")
    print(f"reviews: {reviews_n} row(s) removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
