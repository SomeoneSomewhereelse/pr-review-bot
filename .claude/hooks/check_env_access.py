"""PreToolUse hook: deny any tool call that would OPERATE ON the literal
file `.env`, regardless of tool or OS.

Why Python, not a shell one-liner: the previous version of this hook was a
bash `grep` command, which is not portable -- Claude Code's hook runner
defaults command hooks to PowerShell on native Windows without Git Bash, and
`grep` is not on PATH there even when bash IS used. This script is invoked
via the exec-form `args` mechanism (no shell involved at all, so bash vs.
PowerShell syntax differences never matter) through `uv run --no-sync
python`, which is the one thing this project already guarantees is on PATH
identically across every OS and shell (it's Step 1's first prerequisite).

FIELD-SCOPED, not whole-payload: an earlier version matched the entire raw
stdin JSON, which caught ".env" appearing ANYWHERE -- including inside a
Write's `content` or an Edit's old_string/new_string. This project's own
docs (CLAUDE.md, the setup guide, this file's own docstring) legitimately
say ".env" constantly; matching free text made it impossible to write or
edit documentation ABOUT .env, which is a real and common operation, not a
near-miss edge case -- discovered the hard way when writing this guide's
own rewrite got blocked. Only checked here: fields that name a path the
tool will actually act on (file_path/path/notebook_path -- Read, Edit,
Write, NotebookEdit, Grep, Glob all use one of these), plus Bash's
`command` string in full, since a shell command has no separate "path
field" to isolate -- `cat .env`, `grep x .env`, etc. can put the filename
anywhere in the string. Free-text fields (content, old_string, new_string,
a Grep pattern, an Agent prompt) are never checked, on purpose: mentioning
.env in prose must stay legal.

Matches `.env` as a path component -- preceded by start-of-string or a
non-identifier/non-dot character, followed by end-of-string or the same --
so `.env.example`, `.env.config`, and `.env.config.example` are excluded,
including under a Windows backslash path separator (a lone backslash isn't a
letter/digit/underscore/dot, so it counts as a boundary without special-
casing it). See CLAUDE.md's Secret Handling section and
`.env`-mixes-secrets-with-other-content: never open it with any tool, full
stop, not even a narrow/safe-looking pattern.
"""

from __future__ import annotations

import json
import re
import sys

_PATTERN = re.compile(r"(?:^|[^A-Za-z0-9_.])\.env(?:[^A-Za-z0-9_.]|$)")

# Fields that name a path the tool will act on directly -- the actual danger
# surface. Checked for every tool, since several (Read, Edit, Write,
# NotebookEdit, Grep, Glob) use one of these names for "the file/dir in
# play".
_PATH_FIELDS = ("file_path", "path", "notebook_path")

_REASON = (
    "Blocked: this tool call operates on .env. CLAUDE.md's absolute rule -- "
    "never touch .env with any tool, full stop, not even a narrow/safe-looking "
    "pattern (see the Secret handling section). Ask the user to check or edit "
    "it themselves."
)


def _texts_to_check(tool_name: str, tool_input: dict) -> list[str]:
    texts = [str(tool_input[field]) for field in _PATH_FIELDS if field in tool_input]
    if tool_name == "Bash" and "command" in tool_input:
        texts.append(str(tool_input["command"]))
    return texts


def _deny() -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _REASON,
        }
    })


def main() -> int:
    payload = sys.stdin.read()
    try:
        data = json.loads(payload)
        texts = _texts_to_check(data.get("tool_name", ""), data.get("tool_input") or {})
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Malformed input is unexpected -- fail toward checking the whole
        # raw payload rather than silently skipping the check entirely.
        texts = [payload]

    if any(_PATTERN.search(t) for t in texts):
        print(_deny())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
