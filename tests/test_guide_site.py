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
