"""PreToolUse hook: deny any tool call whose arguments reference the literal
file `.env`, regardless of tool or OS.

Why Python, not a shell one-liner: the previous version of this hook was a
bash `grep` command, which is not portable -- Claude Code's hook runner
defaults command hooks to PowerShell on native Windows without Git Bash, and
`grep` is not on PATH there even when bash IS used. This script is invoked
via the exec-form `args` mechanism (no shell involved at all, so bash vs.
PowerShell syntax differences never matter) through `uv run --no-sync
python`, which is the one thing this project already guarantees is on PATH
identically across every OS and shell (it's Step 1's first prerequisite).

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

_REASON = (
    "Blocked: this tool call references .env. CLAUDE.md's absolute rule -- "
    "never touch .env with any tool, full stop, not even a narrow/safe-looking "
    "pattern (see the Secret handling section). Ask the user to check or edit "
    "it themselves."
)


def main() -> int:
    payload = sys.stdin.read()
    if _PATTERN.search(payload):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _REASON,
            }
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
