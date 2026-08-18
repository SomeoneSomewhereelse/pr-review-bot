"""Interactively scaffold .env and .env.config from the committed examples.

    uv run python -m scripts.init_env

HUMAN-RUN ONLY -- it prompts for and writes real credentials. An agent must
never invoke it (same rule as scripts/encode_credential.py).

It is idempotent, and does that WITHOUT ever reading an existing value: to
decide whether a key is already set it reads key NAMES only, via the
'^[A-Z_0-9]+=' idiom CLAUDE.md prescribes (see tests/test_config.py's
_key_names for the same shape). So re-running is safe, and no existing secret
is ever held in memory by this script.

Which file a key belongs in comes from app/config.py's OPERATIONAL_KEYS:
listed = operational (.env.config), everything else = secret (.env). That is
the same split tests/test_config.py enforces, so this cannot drift from it.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

from app.config import OPERATIONAL_KEYS

# Captures the NAME and discards the value -- the whole point. A value may
# contain '=', spaces, quotes, or '#' and none of it can reach the result.
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_0-9]+)=")

# Keys whose value is a file's base64 form rather than something typed.
_FILE_ENCODED_KEYS = frozenset({"GITHUB_APP_PRIVATE_KEY", "GCP_SERVICE_ACCOUNT_KEY"})


def key_names(path: Path) -> frozenset[str]:
    """The env-var NAMES defined in `path`; empty if it does not exist.

    Names only, never values. Handles CRLF, 'export ' prefixes, comments,
    blank lines, and values containing '=' -- because a regex that returns
    whole lines is exactly how a secret leaks (CLAUDE.md).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        match.group(1) for line in text.splitlines() if (match := _KEY_LINE.match(line))
    )


def example_keys(path: Path) -> tuple[str, ...]:
    """Every key an example file declares, in file order (commented-out
    optional settings included, so nothing silently goes unasked)."""
    text = path.read_text(encoding="utf-8")
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip().removeprefix("# ").removeprefix("#")
        match = _KEY_LINE.match(stripped)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return tuple(names)


def split_keys(keys: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(secret keys, operational keys). Secret-by-default: anything not on
    OPERATIONAL_KEYS is treated as a credential."""
    secret = tuple(k for k in keys if k not in OPERATIONAL_KEYS)
    operational = tuple(k for k in keys if k in OPERATIONAL_KEYS)
    return secret, operational


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in values.items())


def write_env(text: str, path: Path, overwrite: bool = False) -> None:
    """`path` is REQUIRED with no default, and an existing file is refused
    unless overwrite=True -- so neither a test nor a mis-run can destroy a
    working credential file."""
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} already exists; re-run with --overwrite to replace it.")
    path.write_text(text, encoding="utf-8", newline="\n")


def _ask(name: str, already_set: bool) -> str | None:
    """Prompt for one key. None means 'leave it out of the written file'."""
    if already_set:
        keep = input(f"{name} is already set -- keep it? [Y/n] ").strip().lower()
        if keep in ("", "y", "yes"):
            return None
    if name in _FILE_ENCODED_KEYS:
        location = input(f"{name}: path to the key file (blank to skip): ").strip()
        if not location:
            return None
        try:
            return base64.b64encode(Path(location).read_bytes()).decode()
        except OSError as exc:
            print(f"could not read that file ({type(exc).__name__}); skipping", file=sys.stderr)
            return None
    value = input(f"{name}: ").strip()
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold .env and .env.config")
    parser.add_argument("--overwrite", action="store_true", help="replace existing files")
    parser.add_argument("--root", default=".", help="where the example files live")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root)
    secret_path, config_path = root / ".env", root / ".env.config"
    declared = example_keys(root / ".env.example") + example_keys(root / ".env.config.example")
    secret_keys, operational_keys = split_keys(declared)
    existing = key_names(secret_path) | key_names(config_path)

    print("Values are written straight to .env / .env.config and never echoed back.\n")
    answers: dict[str, str] = {}
    for name in (*secret_keys, *operational_keys):
        value = _ask(name, name in existing)
        if value is not None:
            answers[name] = value

    for path, keys in ((secret_path, secret_keys), (config_path, operational_keys)):
        chosen = {k: v for k, v in answers.items() if k in keys}
        if not chosen:
            continue
        write_env(render_env(chosen), path, overwrite=args.overwrite)
        print(f"wrote {path} ({len(chosen)} keys)")

    print("next: uv run python -m scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
