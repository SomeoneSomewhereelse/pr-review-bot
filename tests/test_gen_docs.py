"""gen_docs reads CLASS metadata, never the live settings instance.

That distinction is the whole safety story: Settings.model_fields carries
declared defaults, while app.config.settings carries this machine's real
credentials -- and everything generated here is committed and published.
"""
from __future__ import annotations

from app.config import OPERATIONAL_KEYS, settings
from scripts import gen_docs

SENTINEL = "SENTINEL-6d21fa48c093be75-MUST-NOT-BE-PUBLISHED"


def test_config_table_lists_every_settings_field():
    from app.config import Settings

    table = gen_docs.render_config()
    for name in Settings.model_fields:
        assert name.upper() in table, f"{name} missing from the generated table"


def test_config_table_never_contains_a_configured_value(monkeypatch):
    """The regression guard for the rule this stage turns on. If a generator is
    ever changed to read `settings` instead of `Settings`, this fails."""
    for field in ("database_url", "github_webhook_secret", "groq_api_key",
                  "gemini_api_key", "gcp_service_account_key", "github_app_private_key"):
        monkeypatch.setattr(settings, field, SENTINEL, raising=False)
    assert SENTINEL not in gen_docs.render_config()


def test_gen_docs_module_does_not_import_the_settings_instance():
    """Static guard complementing the behavioural one above: importing the
    singleton at all is the mistake.

    Parsed with ast rather than grepped for a substring. A source grep would
    also match the module's own docstring explaining the rule -- a false
    positive that has already bitten this project once (ISSUES.md, Stage 2
    Task 4, where a docstring naming a forbidden function failed that task's
    own read-only source check).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_docs))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "settings" not in imported, (
                f"line {node.lineno} imports the settings instance from {node.module}"
            )


def test_config_table_marks_where_each_key_belongs():
    table = gen_docs.render_config()
    for line in table.splitlines():
        if line.startswith("| `LLM_PROVIDER`"):
            assert ".env.config" in line
        if line.startswith("| `GROQ_API_KEY`"):
            assert ".env" in line and ".env.config" not in line
    assert "LLM_PROVIDER" in OPERATIONAL_KEYS


def test_generated_output_carries_the_do_not_edit_header():
    assert gen_docs.render_config().startswith(gen_docs.GENERATED_HEADER)
    assert "do not edit" in gen_docs.GENERATED_HEADER.lower()
    assert "scripts.gen_docs" in gen_docs.GENERATED_HEADER


def test_render_config_is_deterministic():
    """CI compares byte-for-byte, so any run-to-run variation is a red build."""
    assert gen_docs.render_config() == gen_docs.render_config()


def test_pricing_table_carries_every_rate_with_its_provenance():
    from app.providers import pricing

    table = gen_docs.render_pricing()
    for (provider, model), rate in pricing._RATES.items():
        assert model in table
        assert provider in table
        assert rate.verified in table
        assert rate.source_url in table


def test_pricing_table_surfaces_an_inherited_rates_caveat():
    """A `verified` date that records no independent check must not be
    presented as though it did -- the note exists precisely to say so."""
    table = gen_docs.render_pricing()
    assert "not independently checked" in table


def test_pricing_table_explains_that_an_unpriced_model_still_runs():
    table = gen_docs.render_pricing()
    assert "without a cost estimate" in table


def test_sync_env_table_separates_pushed_from_never_pushed():
    from scripts import deploy

    table = gen_docs.render_sync_env()
    for name in deploy._ALWAYS_SYNCED:
        assert name in table
    for name in deploy._DB_SYNCED_OPERATIONAL_KEYS:
        assert name in table, "the DB-only keys must be listed as deliberately never pushed"
    for name in deploy._NEVER_SYNCED_OPERATIONAL_KEYS:
        assert name in table
    assert "runtime_config" in table, "must say WHERE the DB-only keys actually live"


def test_sync_env_table_lists_every_providers_model_var():
    from app.providers import registry

    table = gen_docs.render_sync_env()
    for _credential, model_var in registry.PROVIDERS.values():
        assert model_var in table


def test_the_new_renderers_are_deterministic():
    assert gen_docs.render_pricing() == gen_docs.render_pricing()
    assert gen_docs.render_sync_env() == gen_docs.render_sync_env()
