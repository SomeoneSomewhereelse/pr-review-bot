"""app/providers/registry.py -- the single provider -> env-var-name mapping,
shared by app/ (credential resolution) and scripts/ (deploy checks, CLI
overrides). Replaces what was previously scripts/deploy.py's private
_PROVIDERS dict -- app-side code now needs the same mapping, and app/ must
not import from scripts/."""
from __future__ import annotations

from app.providers import registry
from scripts import deploy


def test_registry_lists_all_three_providers():
    assert set(registry.PROVIDERS) == {"gemini", "groq", "github_models"}


def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "LLM_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["github_models"] == (
        "GITHUB_MODELS_TOKEN",
        "GITHUB_MODELS_MODEL",
    )


def test_registry_lists_a_key_index_column_per_provider():
    assert set(registry.KEY_INDEX_COLUMNS) == {"gemini", "groq", "github_models"}
    assert registry.KEY_INDEX_COLUMNS["gemini"] == "gemini_key_index"
    assert registry.KEY_INDEX_COLUMNS["groq"] == "groq_key_index"
    assert registry.KEY_INDEX_COLUMNS["github_models"] == "github_models_key_index"


def test_deploy_script_imports_the_shared_registry():
    """scripts/deploy.py must not keep its own copy of this mapping -- a
    provider added to one and not the other is exactly the drift this
    registry exists to prevent (see _PROVIDERS's own prior docstring, which
    already called it 'the single source of truth')."""
    assert deploy._PROVIDERS is registry.PROVIDERS
