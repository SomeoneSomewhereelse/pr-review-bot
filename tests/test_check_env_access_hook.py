"""Exercises .claude/hooks/check_env_access.py via subprocess, mirroring the
exact exec-form invocation Claude Code's PreToolUse hook actually uses (no
shell, literal argv) -- see the script's own module docstring for what it
does and why. This is a repo-tooling script, not part of the bot/scripts
package, so it's tested by invoking it directly rather than importing it."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check_env_access.py"


def _run(tool_name: str, tool_input: dict) -> tuple[bool, str]:
    """(blocked, raw_stdout). blocked is True iff the hook printed a deny."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip()), result.stdout.strip()


def _is_denied(out: str) -> bool:
    parsed = json.loads(out)
    return parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- The actual danger surface: path-bearing fields and Bash commands ---

def test_blocks_read_of_env_by_path():
    blocked, out = _run("Read", {"file_path": ".env"})
    assert blocked and _is_denied(out)


def test_blocks_edit_targeting_env():
    blocked, out = _run("Edit", {"file_path": ".env", "old_string": "x", "new_string": "y"})
    assert blocked and _is_denied(out)


def test_blocks_write_targeting_env():
    blocked, out = _run("Write", {"file_path": ".env", "content": "GITHUB_APP_ID=1"})
    assert blocked and _is_denied(out)


def test_blocks_grep_path_pointed_at_env():
    blocked, out = _run("Grep", {"pattern": "KEY", "path": ".env"})
    assert blocked and _is_denied(out)


def test_blocks_plain_bash_cat():
    blocked, out = _run("Bash", {"command": "cat .env"})
    assert blocked and _is_denied(out)


def test_blocks_bash_mid_pipeline():
    blocked, _ = _run("Bash", {"command": "cat .env | less"})
    assert blocked


def test_blocks_windows_backslash_path():
    blocked, _ = _run("Bash", {"command": r"type C:\project\.env"})
    assert blocked


# --- Exempted lookalikes ---

def test_allows_env_example():
    blocked, _ = _run("Read", {"file_path": ".env.example"})
    assert not blocked


def test_allows_env_config():
    blocked, _ = _run("Edit", {"file_path": ".env.config", "old_string": "a", "new_string": "b"})
    assert not blocked


def test_allows_env_config_example_copy():
    blocked, _ = _run("Bash", {"command": "cp .env.config.example .env.config"})
    assert not blocked


def test_allows_unrelated_command():
    blocked, _ = _run("Bash", {"command": "git status"})
    assert not blocked


def test_allows_unrelated_file_path():
    blocked, _ = _run("Read", {"file_path": "bot/config.py"})
    assert not blocked


# --- Free-text fields: the false positive this hook shipped with once ---

def test_allows_write_content_mentioning_env_in_prose():
    """Regression: an earlier version matched the whole payload, so writing
    documentation that talks about .env (this project's guide, CLAUDE.md)
    was blocked outright."""
    blocked, _ = _run("Write", {
        "file_path": "guide/setup/02-github-app.md",
        "content": "Paste it into GITHUB_APP_ID in .env. cp .env.example .env first.",
    })
    assert not blocked


def test_allows_edit_old_new_string_mentioning_env():
    blocked, _ = _run("Edit", {
        "file_path": "CLAUDE.md",
        "old_string": "never touch .env",
        "new_string": "never touch .env, full stop",
    })
    assert not blocked


def test_allows_grep_pattern_searching_for_the_literal_string():
    """Searching FOR the string ".env" across other files is not the same
    as operating on the real .env -- only `path` is checked, never `pattern`."""
    blocked, _ = _run("Grep", {"pattern": r"\.env", "path": "guide/"})
    assert not blocked


# --- git/gh message-value exemption ---

def test_allows_simple_git_commit_message_mentioning_env():
    blocked, _ = _run("Bash", {"command": 'git commit -m "explains .env usage"'})
    assert not blocked


def test_allows_single_quoted_git_commit_message():
    blocked, _ = _run("Bash", {"command": "git commit -m 'explains .env usage'"})
    assert not blocked


def test_allows_gh_pr_create_body_mentioning_env():
    blocked, _ = _run("Bash", {
        "command": 'gh pr create --title "x" --body "mentions .env in the description"',
    })
    assert not blocked


def test_allows_the_real_heredoc_commit_message_shape():
    """The exact pattern this project's own commits use for multi-line
    messages -- the shape that broke twice before this exemption existed."""
    command = (
        "git commit -m \"$(cat <<'EOF'\n"
        "docs: explains .env in the body\n\n"
        "Also mentions cp .env.example .env for context.\n"
        "EOF\n"
        ")\""
    )
    blocked, _ = _run("Bash", {"command": command})
    assert not blocked


def test_allows_a_heredoc_body_that_literally_describes_command_substitution():
    """The exact failure this fix addresses: a commit message describing
    THIS mechanism used backticks for markdown code formatting and literally
    showed "$(cat <<'EOF2' ...)" as an example -- text ABOUT the syntax, not
    an active instance of it. A single-quoted heredoc delimiter makes the
    body provably inert regardless of what characters it contains, so this
    must be exempted even though it "looks" like it contains a command."""
    command = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: mentions .env and uses backticks like `this` in prose\n\n"
        "Also literally shows an example: -m \\\"$(cat <<'EOF2' ... EOF2)\\\"\n"
        "EOF\n"
        ")\""
    )
    blocked, _ = _run("Bash", {"command": command})
    assert not blocked


def test_allows_a_single_quoted_value_even_if_its_content_looks_dangerous():
    """Single quotes are unconditionally inert in bash -- no expansion of
    any kind happens inside them, so even a value that LOOKS like a command
    substitution must still be exempted; it can never actually execute."""
    blocked, _ = _run("Bash", {"command": "git commit -m '$(cat .env) mentioned only as text'"})
    assert not blocked


def test_still_blocks_git_add_env():
    """The exemption must never cover an actual pathspec, only message-flag
    values."""
    blocked, _ = _run("Bash", {"command": "git add .env"})
    assert blocked


def test_still_blocks_git_show_reading_env():
    blocked, _ = _run("Bash", {"command": "git show HEAD:.env"})
    assert blocked


def test_still_blocks_a_smuggled_command_substitution_inside_dash_m():
    """A message value containing $( or a backtick could be executing a real
    command rather than writing prose -- must not be exempted."""
    blocked, _ = _run("Bash", {"command": 'git commit -m "$(cat .env)"'})
    assert blocked


def test_message_exemption_does_not_apply_to_non_git_commands():
    """Only git/gh get this treatment -- an arbitrary command with a -m flag
    (unrelated to git) must still be checked in full."""
    blocked, _ = _run("Bash", {"command": 'mytool -m "mentions .env" .env'})
    assert blocked


# --- PowerShell coverage ---
#
# Regression: an earlier version only checked tool_name == "Bash", so the
# `command` field was never inspected at all for the "PowerShell" tool --
# confirmed live on a native-Windows session, where `Get-Content .env`
# executed with no warning while Read on the same path was denied.

def test_blocks_powershell_get_content_on_env():
    blocked, out = _run("PowerShell", {"command": "Get-Content .env"})
    assert blocked and _is_denied(out)


def test_blocks_powershell_mid_pipeline():
    blocked, _ = _run("PowerShell", {"command": "Get-Content .env | Out-Null"})
    assert blocked


def test_allows_powershell_env_example():
    blocked, _ = _run("PowerShell", {"command": "Get-Content .env.example"})
    assert not blocked


def test_allows_powershell_unrelated_command():
    blocked, _ = _run("PowerShell", {"command": "git status"})
    assert not blocked


def test_still_blocks_powershell_git_add_env():
    blocked, _ = _run("PowerShell", {"command": "git add .env"})
    assert blocked


def test_allows_powershell_single_quoted_git_commit_message():
    blocked, _ = _run("PowerShell", {"command": "git commit -m 'explains .env usage'"})
    assert not blocked


def test_allows_powershell_single_quoted_herestring_commit_message():
    """The real multi-line commit convention for the PowerShell tool, per
    its own instructions -- a single-quoted here-string, always inert."""
    command = (
        "git commit -m @'\n"
        "docs: explains .env in the body\n\n"
        "Also mentions cp .env.example .env for context.\n"
        "'@"
    )
    blocked, _ = _run("PowerShell", {"command": command})
    assert not blocked


def test_still_blocks_smuggled_command_substitution_in_powershell_double_quotes():
    blocked, _ = _run("PowerShell", {"command": 'git commit -m "$(Get-Content .env)"'})
    assert blocked


def test_still_blocks_smuggled_command_substitution_in_powershell_herestring():
    command = (
        "git commit -m @\"\n"
        "$(Get-Content .env)\n"
        "\"@"
    )
    blocked, _ = _run("PowerShell", {"command": command})
    assert blocked


def test_allows_powershell_double_quoted_herestring_mentioning_env_in_prose():
    command = (
        "git commit -m @\"\n"
        "docs: explains .env in the body\n"
        "\"@"
    )
    blocked, _ = _run("PowerShell", {"command": command})
    assert not blocked


# --- Malformed input ---

def test_malformed_json_fails_toward_checking_the_raw_payload():
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="not json but mentions .env anyway",
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip()


def test_malformed_json_without_env_mention_is_silent():
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="not json at all",
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == ""


# --- The real configured invocation, not just the script directly ---
#
# Every test above runs the script via sys.executable, bypassing the `uv run
# ...` command Claude Code actually invokes per .claude/settings.json. That
# gap is exactly how a real bug escaped notice: the hook was configured with
# `uv run --no-sync python ...`, which still lets uv touch the project's own
# .venv -- and broke outright on a native-Windows session sharing this
# checkout with WSL, where uv's Windows build couldn't repair a .venv built
# by Linux uv (a lib64 symlink it can't delete). --no-project avoids .venv
# entirely, which these tests both verify functionally and pin by name so
# the invocation can't silently drift back to something venv-dependent.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"


def _configured_hook_command() -> list[str]:
    with _SETTINGS.open() as f:
        settings = json.load(f)
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    return [hook["command"], *hook["args"]]


def _run_via_configured_invocation(tool_name: str, tool_input: dict) -> bool:
    """Runs the exact configured command, with Claude Code's own
    ${CLAUDE_PROJECT_DIR} path-placeholder substitution applied by hand --
    Claude Code expands it before spawning the hook subprocess, but a bare
    subprocess.run here (no shell, no Claude Code in the loop) never would,
    so without this the literal placeholder string would be passed straight
    to `python` as an unresolvable path."""
    command = [
        arg.replace("${CLAUDE_PROJECT_DIR}", str(_REPO_ROOT)) for arg in _configured_hook_command()
    ]
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run(
        command,
        input=payload, capture_output=True, text=True, cwd=_REPO_ROOT, check=True,
    )
    return bool(result.stdout.strip())


def test_configured_invocation_blocks_env_by_path():
    assert _run_via_configured_invocation("Read", {"file_path": ".env"})


def test_configured_invocation_allows_env_example():
    assert not _run_via_configured_invocation("Read", {"file_path": ".env.example"})


def test_configured_invocation_does_not_depend_on_project_sync():
    command = _configured_hook_command()
    assert "--no-project" in command
    assert "--no-sync" not in command


def test_configured_invocation_does_not_depend_on_session_cwd():
    """Regression: the hook script's own path was relative
    (.claude/hooks/check_env_access.py), so it crashed with "No such file or
    directory" whenever the invoking process's cwd wasn't the project root
    -- confirmed live after a `cd` into a subdirectory left every subsequent
    tool call broken. ${CLAUDE_PROJECT_DIR} is Claude Code's own placeholder
    for the absolute project root, substituted before the subprocess starts
    regardless of cwd -- pinned by name so this can't silently drift back to
    a bare relative path."""
    command = _configured_hook_command()
    assert any("${CLAUDE_PROJECT_DIR}" in arg for arg in command)
