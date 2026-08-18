"""init_env never reads an existing VALUE -- only which keys are present. Every
test writes to tmp_path; none touches the repo's real .env."""
from __future__ import annotations

import pytest

from app.config import OPERATIONAL_KEYS
from scripts import init_env

SENTINEL = "SENTINEL-4e8b03d5f7a91c62-EXISTING"


def test_key_names_returns_names_only_never_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"GROQ_API_KEY={SENTINEL}\n"
        f"DATABASE_URL=postgresql://u:{SENTINEL}@h:5432/db?x=1\n"
        "# a comment\n"
        "\n"
        f"export GITHUB_WEBHOOK_SECRET={SENTINEL}\n",
        encoding="utf-8",
    )
    names = init_env.key_names(env)
    assert names == frozenset({"GROQ_API_KEY", "DATABASE_URL", "GITHUB_WEBHOOK_SECRET"})
    for name in names:
        assert SENTINEL not in name


def test_key_names_survives_crlf_and_a_value_containing_equals(tmp_path):
    """A Windows-authored .env and a DATABASE_URL both break a naive parser."""
    env = tmp_path / ".env"
    env.write_bytes(f"DATABASE_URL=postgres://a:b=c@h/db\r\nGROQ_API_KEY={SENTINEL}\r\n".encode())
    assert init_env.key_names(env) == frozenset({"DATABASE_URL", "GROQ_API_KEY"})


def test_key_names_on_a_missing_file_is_empty(tmp_path):
    assert init_env.key_names(tmp_path / "nope") == frozenset()


def test_example_keys_reads_the_committed_examples():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    secrets_keys = init_env.example_keys(root / ".env.example")
    config_keys = init_env.example_keys(root / ".env.config.example")
    assert "GITHUB_WEBHOOK_SECRET" in secrets_keys
    assert "LLM_PROVIDER" in config_keys


def test_split_keys_routes_by_operational_keys():
    secret, operational = init_env.split_keys(("GROQ_API_KEY", "LLM_PROVIDER"))
    assert secret == ("GROQ_API_KEY",)
    assert operational == ("LLM_PROVIDER",)
    assert "LLM_PROVIDER" in OPERATIONAL_KEYS


def test_render_env_emits_one_key_per_line_with_lf(tmp_path):
    text = init_env.render_env({"A": "1", "B": "2"})
    assert text == "A=1\nB=2\n"
    assert "\r" not in text


def test_write_env_refuses_to_clobber_without_an_explicit_opt_in(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEEP=1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        init_env.write_env("NEW=2\n", path)
    assert path.read_text(encoding="utf-8") == "KEEP=1\n"
    init_env.write_env("NEW=2\n", path, overwrite=True)
    assert path.read_text(encoding="utf-8") == "NEW=2\n"


def test_write_env_requires_an_explicit_path():
    import inspect

    assert inspect.signature(init_env.write_env).parameters["path"].default \
        is inspect.Parameter.empty


def test_merge_env_preserves_an_untouched_existing_line(tmp_path):
    existing = f"GITHUB_APP_PRIVATE_KEY={SENTINEL}\n"
    merged = init_env.merge_env(existing, {"GROQ_API_KEY": "gsk_new"})
    assert f"GITHUB_APP_PRIVATE_KEY={SENTINEL}\n" in merged


def test_merge_env_replaces_an_existing_key_present_in_updates(tmp_path):
    existing = f"GROQ_API_KEY={SENTINEL}\n"
    merged = init_env.merge_env(existing, {"GROQ_API_KEY": "gsk_new"})
    assert SENTINEL not in merged
    assert "GROQ_API_KEY=gsk_new\n" in merged


def test_merge_env_appends_brand_new_keys_at_the_end(tmp_path):
    existing = "KEEP=1\n"
    merged = init_env.merge_env(existing, {"NEW_KEY": "2"})
    assert merged == "KEEP=1\nNEW_KEY=2\n"


def test_merge_env_on_empty_existing_text_just_renders_updates(tmp_path):
    assert init_env.merge_env("", {"A": "1"}) == "A=1\n"


def test_merge_env_drops_a_stale_duplicate_key_untouched_by_updates(tmp_path):
    """A malformed existing .env with the same key on two lines must not
    survive as a duplicate -- the second, unreplaced line could shadow the
    first under a "last occurrence wins" dotenv loader."""
    existing = "GITHUB_APP_ID=1\nGITHUB_APP_ID=2\n"
    merged = init_env.merge_env(existing, {})
    assert merged.count("GITHUB_APP_ID=") == 1
    assert "GITHUB_APP_ID=1\n" in merged
    assert "GITHUB_APP_ID=2" not in merged


def test_merge_env_drops_a_stale_duplicate_when_the_second_occurrence_is_updated(tmp_path):
    """The update must apply once, at the first occurrence, and the stale
    second occurrence must not linger afterward with the old value."""
    existing = "A=old\nA=old2\n"
    merged = init_env.merge_env(existing, {"A": "new"})
    assert merged.count("A=") == 1
    assert merged == "A=new\n"


def test_merge_env_reproduces_the_kept_app_credentials_bug_scenario(tmp_path):
    """This is the exact scenario from the bug report: an operator ran
    create_github_app.py, then re-ran init_env.py answering "keep it?" for
    all three App credentials and typing only a new GROQ_API_KEY. The merge
    must not lose the App credentials -- a full-replace write_env() call
    with only the newly-answered keys is exactly what did."""
    existing = (
        "GITHUB_APP_ID=123\n"
        f"GITHUB_APP_PRIVATE_KEY={SENTINEL}\n"
        f"GITHUB_WEBHOOK_SECRET=SENTINEL2-{SENTINEL}\n"
    )
    merged = init_env.merge_env(existing, {"GROQ_API_KEY": "gsk_x"})
    assert "GITHUB_APP_ID=123\n" in merged
    assert f"GITHUB_APP_PRIVATE_KEY={SENTINEL}\n" in merged
    assert f"GITHUB_WEBHOOK_SECRET=SENTINEL2-{SENTINEL}\n" in merged
    assert "GROQ_API_KEY=gsk_x\n" in merged
