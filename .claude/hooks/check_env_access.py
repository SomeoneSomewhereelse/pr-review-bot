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

GIT/GH MESSAGE EXEMPTION: Bash's `command` string is checked in full (see
above -- no separate path field to isolate), which also caught a git/gh
commit/PR message that happens to mention ".env" in prose, e.g. `git commit
-m "explains .env usage"` -- discovered when this project's own commit
convention (a heredoc-wrapped multi-line `-m`) got blocked writing a commit
message ABOUT the previous fix. For a command whose first word is `git` or
`gh`, the VALUE of a message-bearing flag (-m/--message/-b/--body/--title,
either shape: `-m "text"` or the heredoc idiom `-m "$(cat <<'EOF'
...body...
EOF
)"`) is dropped from the text being checked, but only if that value itself
contains no `$(` or backtick -- a value that does keeps its full text in the
check, since that could be smuggling an actual command (e.g. `-m "$(cat
.env)"`) rather than writing prose. Everything else in a git/gh command
(the subcommand, a real pathspec like `git add .env` or `git show
HEAD:.env`) is never exempted and stays fully checked.
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

_GIT_LIKE = re.compile(r"^\s*(git|gh)\b")
_HEREDOC_MESSAGE_FLAG = re.compile(
    r"\$\(\s*cat\s*<<\s*'(?P<delim>[A-Za-z_][A-Za-z0-9_]*)'\s*\n"
    r"(?P<body>.*?)\n(?P=delim)\s*\n?\)",
    re.DOTALL,
)
_QUOTED_MESSAGE_FLAG = re.compile(
    r"(?:-m|--message|-b|--body|--title)(?:=|\s+)(\"[^\"]*\"|'[^']*')"
)
_LOOKS_LIKE_A_COMMAND = re.compile(r"\$\(|`")


def _neutralize_git_message_values(command: str) -> str:
    """Drop the plain-text VALUE of a git/gh message-bearing flag from
    `command`, leaving everything else (subcommand, real pathspecs, flag
    names themselves) untouched. See the module docstring's "GIT/GH MESSAGE
    EXEMPTION" section."""
    if not _GIT_LIKE.match(command):
        return command

    def _drop_if_plain(match: re.Match, value: str) -> str:
        return match.group(0) if _LOOKS_LIKE_A_COMMAND.search(value) else ""

    command = _HEREDOC_MESSAGE_FLAG.sub(
        lambda m: _drop_if_plain(m, m.group("body")), command
    )
    command = _QUOTED_MESSAGE_FLAG.sub(
        lambda m: _drop_if_plain(m, m.group(1)), command
    )
    return command


def _texts_to_check(tool_name: str, tool_input: dict) -> list[str]:
    texts = [str(tool_input[field]) for field in _PATH_FIELDS if field in tool_input]
    if tool_name == "Bash" and "command" in tool_input:
        texts.append(_neutralize_git_message_values(str(tool_input["command"])))
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
