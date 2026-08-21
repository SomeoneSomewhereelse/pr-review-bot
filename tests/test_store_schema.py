"""The provisioned schema is declared, not migrated -- app/queue/store.py's
_SCHEMA is a CREATE TABLE declaration of the final shape, with no ALTER
statements (design spec 2026-08-18 section 6d). These tests lock the column
set so the ALTER-folding refactor cannot silently change it."""
from __future__ import annotations

from app.queue import store

EXPECTED_COLUMNS = {
    "tickets": {
        "id", "repo_full_name", "pr_number", "head_sha", "status", "provider",
        "not_before", "attempts", "comment_id", "enqueued_at", "updated_at",
        "rereview_requested", "last_reviewed_at", "cooldown_level",
        "notice_not_before", "last_error", "defer_reason",
    },
    "runtime_config": {
        "id", "provider", "updated_at", "cooldown_base_seconds",
        "cooldown_max_seconds", "cooldown_factor", "gemini_key_index",
        "groq_key_index", "vertex_key_index", "gemini_model", "groq_model",
        "vertex_model", "key_usage_token_cap",
        "key_usage_reset_time_utc", "review_draft_prs",
    },
    "reviews": {
        "id", "repo_full_name", "pr_number", "provider", "model", "comment_id",
        "created_at", "total_elapsed_ms", "total_tokens_in", "total_tokens_out",
        "est_cost_usd", "results", "key_index",
    },
}


def _columns(db_query, table: str) -> set[str]:
    rows = db_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {r[0] for r in rows}


def test_schema_declares_every_expected_column(db, db_query):
    for table, expected in EXPECTED_COLUMNS.items():
        assert _columns(db_query, table) == expected, f"{table} column set changed"


def test_schema_contains_no_alter_statements():
    assert "ALTER TABLE" not in store._SCHEMA.upper(), (
        "_SCHEMA must declare the final shape via CREATE TABLE only -- an ALTER "
        "is migration code, which a fresh clone must not carry (spec section 6d)"
    )


def test_runtime_config_has_no_cost_cap_column(db, db_query):
    assert "key_usage_cost_cap_usd" not in _columns(db_query, "runtime_config")


def test_est_cost_usd_is_nullable(db, db_query):
    # db_query (not db_exec) is this file's actual raw-query fixture --
    # tests/conftest.py's db_exec only executes/commits, it returns nothing to
    # index into. Rows come back as tuples, matching this file's other tests.
    rows = db_query(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'reviews' AND column_name = 'est_cost_usd'"
    )
    assert rows[0][0] == "YES", (
        "an unpriced review records NULL, not 0.0 -- 0.0 would corrupt the "
        "dashboard's SUM(est_cost_usd) aggregate"
    )
