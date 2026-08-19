"""The site config is the one thing that makes the guide navigable, so its
shape is asserted rather than assumed. Everything here reads files -- no test
in Stage 3b touches the network (see the plan's Global Constraints)."""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_MKDOCS = _ROOT / "mkdocs.yml"


def _config() -> dict:
    # mkdocs.yml uses !!python/name: tags for some Material extensions; the
    # base loader keeps them as plain strings instead of refusing to parse.
    return yaml.load(_MKDOCS.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_site_builds_from_the_guide_directory():
    config = _config()
    assert config["docs_dir"] == "guide", (
        "docs/ holds internal engineering notes and superpowers specs; pointing "
        "MkDocs at it would publish all of them"
    )
    assert config["site_name"]


def test_material_features_the_guide_relies_on_are_enabled():
    config = _config()
    assert config["theme"]["name"] == "material"
    extensions = config["markdown_extensions"]
    flat = [e if isinstance(e, str) else next(iter(e)) for e in extensions]
    # Admonitions carry the gotchas that must not read as body text; tabbed
    # content collapses the bash/PowerShell duplication README carries today.
    assert "admonition" in flat
    assert "pymdownx.tabbed" in flat
    assert "pymdownx.superfences" in flat


def test_every_nav_target_exists_on_disk():
    """A nav entry pointing at a missing file builds a broken site silently."""

    def targets(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from targets(value)
        elif isinstance(node, list):
            for item in node:
                yield from targets(item)

    for target in targets(_config()["nav"]):
        assert (_ROOT / "guide" / target).is_file(), f"nav points at missing {target}"


def test_the_generated_reference_pages_are_in_the_nav():
    flat = list(_MKDOCS.read_text(encoding="utf-8").splitlines())
    joined = "\n".join(flat)
    for name in ("config.md", "pricing.md", "checks.md", "sync-env.md"):
        assert f"reference/{name}" in joined


def test_landing_page_states_the_prerequisites_up_front():
    text = (_ROOT / "guide" / "index.md").read_text(encoding="utf-8")
    assert "3.12" in text, "the Python floor is in .python-version and nowhere a reader looks"
    assert "uv" in text
    assert "Postgres" in text


_SETUP = _ROOT / "guide" / "setup"


def test_shared_pages_match_doctors_step_titles():
    """If the guide and doctor disagree about a step's name, an operator
    following one while running the other cannot tell where they are."""
    from scripts import doctor

    shared = [s for s in doctor.steps_for("local") if s.number <= 4]
    pages = {
        1: "01-prerequisites.md",
        2: "02-github-app.md",
        3: "03-install-app.md",
        4: "04-llm-provider.md",
    }
    for step in shared:
        text = (_SETUP / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text, f"step {step.number} page must carry doctor's title"


def test_prerequisites_page_uses_portable_commands_only():
    """spec section 5: `base64 -w0` is GNU-only and `curl` on Windows
    PowerShell aliases Invoke-WebRequest, which takes different arguments."""
    text = (_SETUP / "01-prerequisites.md").read_text(encoding="utf-8")
    assert "base64 -w0" not in text
    assert "winget install Docker" not in text or "=== " in text, (
        "a Windows-only install line must sit inside tabbed content"
    )


def test_github_app_page_encodes_the_pem_with_the_project_script():
    text = (_SETUP / "02-github-app.md").read_text(encoding="utf-8")
    assert "scripts.encode_credential" in text
    assert "base64 -w0" not in text


def test_setup_index_sends_the_reader_to_a_track():
    text = (_SETUP / "index.md").read_text(encoding="utf-8")
    assert "local/05" in text and "hosted/05" in text


def test_local_track_pages_match_doctors_titles():
    from scripts import doctor

    pages = {5: "05-postgres.md", 6: "06-tunnel.md", 7: "07-webhook.md", 8: "08-run.md"}
    for step in doctor.steps_for("local"):
        if step.number < 5:
            continue
        text = (_SETUP / "local" / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text


def test_tunnel_page_explains_the_ephemeral_url():
    text = (_SETUP / "local" / "06-tunnel.md").read_text(encoding="utf-8")
    assert "cloudflared" in text
    assert "changes" in text.lower(), "the URL changing each restart must be stated"


def test_local_verify_page_uses_the_project_health_check():
    """spec section 5: curl on Windows PowerShell aliases Invoke-WebRequest."""
    text = (_SETUP / "local" / "07-webhook.md").read_text(encoding="utf-8")
    assert "--health-only" in text
    assert "curl " not in text


def test_hosted_track_pages_match_doctors_titles():
    from scripts import doctor

    pages = {5: "05-supabase.md", 6: "06-render.md", 7: "07-sync.md", 8: "08-pinger.md"}
    for step in doctor.steps_for("hosted"):
        if step.number < 5:
            continue
        text = (_SETUP / "hosted" / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text


def test_supabase_page_pins_the_session_pooler_port():
    text = (_SETUP / "hosted" / "05-supabase.md").read_text(encoding="utf-8")
    assert "5432" in text and "6543" in text, "both ports named, so the wrong one is unmistakable"


def test_render_page_lists_exactly_the_four_boot_vars():
    """spec section 4b: SETUP.md walked through nine; only four are needed to
    boot, and deploy.py's own check already names which."""
    from scripts import deploy

    text = (_SETUP / "hosted" / "06-render.md").read_text(encoding="utf-8")
    for name in deploy._BOOT_CREDENTIAL_NAMES:
        assert name in text
    assert "RENDER_API_KEY" in text, "must say it is NOT a service env var"


def test_pinger_page_warns_about_the_exact_url():
    text = (_SETUP / "hosted" / "08-pinger.md").read_text(encoding="utf-8")
    assert "healthz" in text
    assert "HEAD" in text, "UptimeRobot's free tier sends HEAD, not GET"


_OPS = _ROOT / "guide" / "operations"


def test_operations_pages_link_generated_tables_instead_of_restating_them():
    """A hand-copied check table is precisely the drift Stage 3a's generation
    and CI job exist to make impossible."""
    deploy_page = (_OPS / "deploy.md").read_text(encoding="utf-8")
    assert "reference/checks" in deploy_page
    assert "reference/sync-env" in deploy_page
    assert "| Check | Verifies |" not in deploy_page, "do not restate the generated table"


def test_deploy_page_documents_the_exit_codes():
    text = (_OPS / "deploy.md").read_text(encoding="utf-8")
    for code in ("exit 0", "exit 1", "exit 2"):
        assert code in text


def test_config_files_page_states_the_two_file_split():
    text = (_OPS / "config-files.md").read_text(encoding="utf-8")
    assert ".env.config" in text and ".env" in text
    assert "OPERATIONAL_KEYS" in text


def test_tuning_page_says_the_db_only_settings_need_no_redeploy():
    text = (_OPS / "tuning.md").read_text(encoding="utf-8")
    assert "--sync-config-db" in text
    assert "runtime_config" in text


def test_no_operations_page_mentions_the_removed_cost_cap():
    """KEY_USAGE_COST_CAP_USD was removed in Stage 1; documenting it would
    invite someone to set a variable nothing reads."""
    for page in _OPS.glob("*.md"):
        assert "KEY_USAGE_COST_CAP_USD" not in page.read_text(encoding="utf-8")


_BG = _ROOT / "guide" / "background"


def test_provider_history_survives_the_migration():
    """This is the record of what was actually tried and what it cost --
    the thing most easily lost when a 750-line journal is restructured."""
    text = (_BG / "providers.md").read_text(encoding="utf-8")
    assert "PERMISSION_DENIED" in text, "the Gemini Trust & Safety block"
    assert "github_models" in text or "GitHub Models" in text
    assert "gemini-2.5-flash" in text, "the Vertex catalog finding"


def test_rehearsal_history_keeps_the_measured_timings():
    text = (_BG / "rehearsals.md").read_text(encoding="utf-8")
    assert "PR #3" in text or "#3" in text
    assert "8s" in text or "8 s" in text


def test_background_is_not_in_the_setup_reading_path():
    setup_pages = list((_ROOT / "guide" / "setup").rglob("*.md"))
    assert setup_pages
    for page in setup_pages:
        assert "background/" not in page.read_text(encoding="utf-8"), (
            "background is optional context; a setup step must not depend on it"
        )
