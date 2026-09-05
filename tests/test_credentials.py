"""providers/credentials.py -- resolving which env var backs a
provider's currently-active API-key slot."""
from __future__ import annotations

from config import settings
from providers import credentials


def test_index_zero_resolves_to_the_base_env_var_via_settings(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_from_settings")
    assert credentials.resolve("groq", 0) == ("GROQ_API_KEY", "gsk_from_settings")


def test_index_one_resolves_to_the_suffixed_env_var_via_os_environ(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_slot_1")
    assert credentials.resolve("groq", 1) == ("GROQ_API_KEY_1", "gsk_slot_1")


def test_a_missing_numbered_slot_resolves_to_an_empty_string(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY_7", raising=False)
    assert credentials.resolve("groq", 7) == ("GROQ_API_KEY_7", "")


def test_resolves_correctly_for_every_provider(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    assert credentials.resolve("gemini", 0) == ("GEMINI_API_KEY", "gk_x")
