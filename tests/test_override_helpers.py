"""Direct unit coverage for scripts/_override.py -- the shared local-value
discovery and Render-verification logic behind scripts/set_override.py and
scripts/deploy.py's numbered-slot sync-env fix. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md."""
from __future__ import annotations

from app.config import settings
from scripts import _override


def test_local_numbered_slots_finds_matching_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=gsk_one\nGROQ_API_KEY_2=gsk_two\nOTHER_VAR=x\n")
    slots = _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_1": "gsk_one", "GROQ_API_KEY_2": "gsk_two"}


def test_local_numbered_slots_ignores_empty_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=\nGROQ_API_KEY_2=gsk_two\n")
    slots = _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_2": "gsk_two"}


def test_local_numbered_slots_returns_empty_for_a_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.env"
    assert _override.local_numbered_slots("GROQ_API_KEY", env_path=str(missing)) == {}


def test_local_numbered_slots_does_not_match_a_different_base(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY_1=gk_one\n")
    assert _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file)) == {}


def test_local_value_index_0_reads_through_settings(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_base")
    assert _override.local_value("groq", 0) == "gsk_base"


def test_local_value_index_n_reads_the_scan(monkeypatch):
    monkeypatch.setattr(
        _override, "local_numbered_slots",
        lambda base, env_path=".env": {"GROQ_API_KEY_2": "gsk_two"},
    )
    assert _override.local_value("groq", 2) == "gsk_two"


def test_local_value_index_n_returns_empty_when_unprovisioned(monkeypatch):
    monkeypatch.setattr(_override, "local_numbered_slots", lambda base, env_path=".env": {})
    assert _override.local_value("groq", 3) == ""
