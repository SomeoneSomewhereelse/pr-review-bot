"""scripts/encode_credential.py -- prints a local file's base64 form.

Human-run only: never invoke this against a real credential file from an
agent session. Printing secret-derived bytes into a tool result is exactly
the failure mode CLAUDE.md's Secret handling section exists to prevent --
these tests use throwaway, obviously-fake bytes, never real material.
"""
from __future__ import annotations

import base64

from scripts import encode_credential


def test_prints_the_base64_form_of_the_file(tmp_path, capsys):
    payload = b"hello world, this is a fake credential\n"
    path = tmp_path / "fake-key.pem"
    path.write_bytes(payload)

    exit_code = encode_credential.main([str(path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.strip() == base64.b64encode(payload).decode()


def test_prints_nothing_else(tmp_path, capsys):
    """Exactly one line of output -- the base64 form, nothing else -- so a
    caller can pipe or paste it directly with no cleanup."""
    path = tmp_path / "fake-key.json"
    path.write_bytes(b'{"type": "service_account"}')

    encode_credential.main([str(path)])

    out = capsys.readouterr().out
    assert out.count("\n") == 1


def test_returns_two_and_names_the_path_on_a_missing_file(tmp_path, capsys):
    missing = tmp_path / "nope.pem"

    exit_code = encode_credential.main([str(missing)])

    assert exit_code == 2
    assert "nope.pem" in capsys.readouterr().err


def test_requires_exactly_one_argument(capsys):
    assert encode_credential.main([]) == 2
    assert encode_credential.main(["a", "b"]) == 2
