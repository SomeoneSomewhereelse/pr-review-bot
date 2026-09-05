"""The command must carry the credential handoff rule. CLAUDE.md forbids an
agent from opening .env at all, so a slash command that walks a human through
setup has to hand off the moment a real secret is involved -- otherwise the
first thing it does is break the project's highest-priority rule."""
from __future__ import annotations

from pathlib import Path

_COMMAND = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "setup.md"


def test_the_command_exists_with_frontmatter():
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "description:" in text


def test_it_drives_the_doctor_cli_and_holds_no_logic_of_its_own():
    text = _COMMAND.read_text(encoding="utf-8")
    assert "scripts.doctor" in text
    assert "--json" in text
    assert "works identically for people who do not use Claude Code" in text


def test_it_hands_off_every_credential_writing_tool():
    """Both writers must be handed to the human, never run by the agent."""
    text = _COMMAND.read_text(encoding="utf-8")
    for tool in ("scripts.init_env", "scripts.create_github_app"):
        assert tool in text, f"{tool} must be named"
    assert "! uv run" in text, "the `!` prefix is how the user runs it themselves"
    assert ".env" in text
