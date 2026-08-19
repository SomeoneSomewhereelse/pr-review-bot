"""Tests for scripts/test_db.py. All docker/subprocess calls are mocked --
no real Docker required to run this file."""

import subprocess

import pytest

from scripts import test_db


def _fake_run(responses):
    """responses: dict mapping a keyword to look for in the command list to
    (returncode, stdout). First matching keyword wins. Raises if a command
    doesn't match anything, so an unexpected call fails loudly."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        for keyword, (returncode, stdout) in responses.items():
            if keyword in cmd:
                return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    _run.calls = calls
    return _run


def _explode(*_args, **_kwargs):
    raise AssertionError("subprocess.run must not be called when docker is missing")


def test_up_starts_a_container_when_absent(monkeypatch, capsys):
    fake_run = _fake_run({"inspect": (1, ""), "run": (0, ""), "pg_isready": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)

    assert test_db.up() == 0
    assert any("run" in cmd for cmd in fake_run.calls)
    assert not any("start" in cmd for cmd in fake_run.calls)
    out = capsys.readouterr().out
    assert "export DATABASE_URL=postgresql://postgres:x@localhost:5433/postgres" in out


def test_up_is_idempotent_when_already_running(monkeypatch, capsys):
    fake_run = _fake_run({"inspect": (0, "true"), "pg_isready": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)

    assert test_db.up() == 0
    assert not any("run" in cmd for cmd in fake_run.calls)
    assert not any("start" in cmd for cmd in fake_run.calls)
    assert "export DATABASE_URL=" in capsys.readouterr().out


def test_up_starts_a_stopped_container(monkeypatch, capsys):
    fake_run = _fake_run({"inspect": (0, "false"), "start": (0, ""), "pg_isready": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)

    assert test_db.up() == 0
    assert any("start" in cmd for cmd in fake_run.calls)
    assert not any("run" in cmd for cmd in fake_run.calls)


def test_up_fails_when_the_container_never_becomes_ready(monkeypatch, capsys):
    fake_run = _fake_run({"inspect": (1, ""), "run": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)
    monkeypatch.setattr(test_db, "_READY_TIMEOUT_SECONDS", 0)

    assert test_db.up() == 1
    out, err = capsys.readouterr()
    assert "did not become ready" in err
    assert "export DATABASE_URL" not in out


def test_up_reports_a_clear_error_when_docker_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(test_db.shutil, "which", lambda name: None)
    monkeypatch.setattr(test_db.subprocess, "run", _explode)

    assert test_db.up() == 1
    assert "Docker not found" in capsys.readouterr().err


def test_down_removes_the_container(monkeypatch):
    fake_run = _fake_run({"rm": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)

    assert test_db.down() == 0
    assert any("rm" in cmd and "-f" in cmd for cmd in fake_run.calls)


def test_down_reports_a_clear_error_when_docker_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(test_db.shutil, "which", lambda name: None)
    monkeypatch.setattr(test_db.subprocess, "run", _explode)

    assert test_db.down() == 1
    assert "Docker not found" in capsys.readouterr().err


def test_constructed_database_url_is_recognized_as_local():
    assert test_db._looks_like_local_test_db(test_db._DATABASE_URL) is True


def test_up_refuses_to_print_a_non_local_database_url(monkeypatch, capsys):
    """The pre-print gate is a real `raise`, not a bare `assert` -- so it still
    fires under `python -O`, where asserts are stripped."""
    fake_run = _fake_run({"inspect": (0, "true"), "pg_isready": (0, "")})
    monkeypatch.setattr(test_db.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(test_db.subprocess, "run", fake_run)
    monkeypatch.setattr(test_db, "_DATABASE_URL", "postgresql://u:p@db.example.com/postgres")

    with pytest.raises(RuntimeError, match="refusing to print"):
        test_db.up()
    assert "export DATABASE_URL" not in capsys.readouterr().out


def test_main_dispatches_to_up_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(test_db, "up", lambda: calls.append("up") or 0)
    monkeypatch.setattr(test_db, "down", lambda: calls.append("down") or 0)

    assert test_db.main([]) == 0
    assert calls == ["up"]


def test_main_dispatches_to_down(monkeypatch):
    calls = []
    monkeypatch.setattr(test_db, "up", lambda: calls.append("up") or 0)
    monkeypatch.setattr(test_db, "down", lambda: calls.append("down") or 0)

    assert test_db.main(["down"]) == 0
    assert calls == ["down"]
