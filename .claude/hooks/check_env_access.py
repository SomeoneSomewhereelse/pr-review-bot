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
Write, NotebookEdit, Grep, Glob all use one of these), plus the shell
`command` string in full for shell-executing tools, since a shell command
has no separate "path field" to isolate -- `cat .env`, `grep x .env`, etc.
can put the filename anywhere in the string. Free-text fields (content,
old_string, new_string, a Grep pattern, an Agent prompt) are never checked,
on purpose: mentioning .env in prose must stay legal.

SHELL-TOOL COVERAGE: Claude Code exposes exactly two shell-executing tools
-- `Bash` (Git Bash / POSIX sh; used on Linux, macOS, and WSL, and on native
Windows when Git Bash is available) and `PowerShell` (pwsh; used on native
Windows without Git Bash, or whenever explicitly invoked). There is no
separate tool for cmd.exe or MSYS2 -- Claude Code never shells out through
either directly, so nothing is missing by not special-casing them. An
earlier version of this hook only checked `tool_name == "Bash"`, which meant
`command` was never inspected at all for the `PowerShell` tool -- confirmed
live on a native-Windows session: `Get-Content .env` executed with no
warning, while `Read` on the same path was correctly denied. Both tool
names are checked identically now.

Matches `.env` as a path component -- preceded by start-of-string or a
non-identifier/non-dot character, followed by end-of-string or the same --
so `.env.example`, `.env.config`, and `.env.config.example` are excluded,
including under a Windows backslash path separator (a lone backslash isn't a
letter/digit/underscore/dot, so it counts as a boundary without special-
casing it). See CLAUDE.md's Secret Handling section and
`.env`-mixes-secrets-with-other-content: never open it with any tool, full
stop, not even a narrow/safe-looking pattern.

GIT/GH MESSAGE EXEMPTION: the shell `command` string is checked in full (see
above -- no separate path field to isolate), which also caught a git/gh
commit/PR message that happens to mention ".env" in prose, e.g. `git commit
-m "explains .env usage"` -- discovered when this project's own commit
convention (a heredoc-wrapped multi-line `-m`) got blocked writing a commit
message ABOUT the previous fix. For a command whose first word is `git` or
`gh`, the VALUE of a message-bearing flag (-m/--message/-b/--body/--title)
is dropped from the text being checked -- but WHETHER it's safe to drop is
decided by the shell's own quoting grammar, not by scanning the value's
content for scary-looking characters. Bash and PowerShell disagree on what
"inert" looks like, so each gets its own shapes:

Bash:
- A single-quoted value (-m '...') is ALWAYS inert -- bash performs zero
  expansion inside single quotes, full stop. Always safe to drop, whatever
  it contains.
- A heredoc body whose delimiter is quoted (-m "$(cat <<'EOF' ...
  EOF)"  -- this project's own multi-line commit convention) is ALSO always
  inert for the same reason: quoting any part of a heredoc delimiter word
  disables all expansion within the body. `$(cat .env)` typed INSIDE such a
  body is never executed -- `cat` just prints those literal characters, and
  the outer $(...) captures that literal string. Always safe to drop.
- A double-quoted value (-m "...") is the one shape that genuinely expands
  $()/backticks inside it. This is the only bash shape where dropping it
  unconditionally would be unsafe (e.g. -m "$(cat .env)" would really run
  `cat .env`), so it's only dropped when it contains neither -- a value
  that does keeps its full text in the check.

PowerShell (this project's own multi-line commit convention there uses
here-strings, per the PowerShell tool's own instructions -- so this needs
the same two-sided treatment, not just a straight copy of the bash rules):
- A single-quoted value (-m '...') is ALWAYS inert in PowerShell too --
  same rule, same handling, no separate pattern needed.
- A single-quoted here-string (-m @'...'@) is ALWAYS inert: PowerShell
  performs zero interpolation inside single-quoted text, multi-line or not.
  Always safe to drop.
- A double-quoted value or here-string (-m "..." / -m @"..."@) genuinely
  expands $(...) and $variable references. PowerShell's backtick is an
  escape character, not command substitution, but treating a bare backtick
  as "could expand" anyway is harmless -- it only ever makes the check
  MORE conservative (leaves more text checked, never creates a false
  exemption), so both shapes reuse the same bash content check rather than
  needing a PowerShell-specific one.

An earlier version applied the double-quote-only suspicion check to ALL
shapes, which meant a heredoc body merely describing this exact mechanism
in prose -- using backticks for markdown code formatting, or literally
showing "$(cat <<'EOF'...)" as an example -- got treated as "could be a
real command" and left fully checked, blocking the very commit that
introduced this exemption. Splitting by each shell's actual grammar instead
of by surface content fixes that: inert-by-construction shapes are always
exempt, and only the genuinely-expanding shapes need the content check.

Everything else in a git/gh command (the subcommand, a real pathspec like
`git add .env` or `git show HEAD:.env`) is never exempted and stays fully
checked, in either shell.
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
_MESSAGE_FLAG = r"(?:-m|--message|-b|--body|--title)"

# Any part of the delimiter word quoted (single OR double) disables all
# expansion in the heredoc body -- POSIX/bash grammar, not a heuristic. The
# whole $(...) is typically wrapped in an outer quote too (`-m "$(cat <<..."`
# -- this project's own convention), needed so a multi-line result reaches
# -m as one argument; `(?P<outer>...)?` plus the trailing conditional makes
# that outer quote optional but, if present, must be closed with the SAME
# character (regression: an earlier version required no outer quote at all
# and so never matched this project's real commits).
_HEREDOC_MESSAGE_FLAG = re.compile(
    _MESSAGE_FLAG + r"(?:=|\s+)(?P<outer>[\"'])?"
    r"\$\(\s*cat\s*<<\s*(?P<dquote>['\"])(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=dquote)\s*\n"
    r"(?P<body>.*?)\n(?P=delim)\s*\n?\)(?(outer)(?P=outer))",
    re.DOTALL,
)
# Single quotes are ALWAYS inert in both bash and PowerShell -- no expansion
# of any kind, no content check needed regardless of what's inside.
_SINGLE_QUOTED_MESSAGE_FLAG = re.compile(_MESSAGE_FLAG + r"(?:=|\s+)'[^']*'")
# PowerShell single-quoted here-string: also always inert, same reasoning.
# The closing '@ must start a line (PowerShell's own syntax rule).
_PS_HERESTRING_SINGLE_MESSAGE_FLAG = re.compile(
    _MESSAGE_FLAG + r"(?:=|\s+)@'\r?\n.*?\r?\n'@", re.DOTALL
)
# Double quotes (bash or PowerShell) genuinely expand $(...)/backticks --
# this is the shape where a content check is actually load-bearing. Capture
# group 1 is the full quoted-value text (delimiters included) so the same
# _drop_if_cannot_expand helper can scan it and drop the whole match.
_DOUBLE_QUOTED_MESSAGE_FLAG = re.compile(_MESSAGE_FLAG + r"(?:=|\s+)(\"[^\"]*\")")
# PowerShell double-quoted here-string: same expansion risk as a plain
# double-quoted value, just spanning multiple lines.
_PS_HERESTRING_DOUBLE_MESSAGE_FLAG = re.compile(
    _MESSAGE_FLAG + r'(?:=|\s+)(@"\r?\n.*?\r?\n"@)', re.DOTALL
)
_COULD_EXPAND_TO_A_COMMAND = re.compile(r"\$\(|`")


def _neutralize_git_message_values(command: str) -> str:
    """Drop the VALUE of a git/gh message-bearing flag from `command`,
    leaving everything else (subcommand, real pathspecs, flag names
    themselves) untouched. See the module docstring's "GIT/GH MESSAGE
    EXEMPTION" section for why each shape is or isn't safe to drop
    unconditionally, in bash and in PowerShell."""
    if not _GIT_LIKE.match(command):
        return command

    command = _HEREDOC_MESSAGE_FLAG.sub("", command)
    command = _PS_HERESTRING_SINGLE_MESSAGE_FLAG.sub("", command)
    command = _SINGLE_QUOTED_MESSAGE_FLAG.sub("", command)

    def _drop_if_cannot_expand(match: re.Match) -> str:
        return "" if not _COULD_EXPAND_TO_A_COMMAND.search(match.group(1)) else match.group(0)

    command = _DOUBLE_QUOTED_MESSAGE_FLAG.sub(_drop_if_cannot_expand, command)
    command = _PS_HERESTRING_DOUBLE_MESSAGE_FLAG.sub(_drop_if_cannot_expand, command)
    return command


# Claude Code's only two shell-executing tools -- see the module docstring's
# "SHELL-TOOL COVERAGE" section for why there is nothing else to add here.
_SHELL_TOOLS = ("Bash", "PowerShell")


def _texts_to_check(tool_name: str, tool_input: dict) -> list[str]:
    texts = [str(tool_input[field]) for field in _PATH_FIELDS if field in tool_input]
    if tool_name in _SHELL_TOOLS and "command" in tool_input:
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
