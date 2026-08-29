# Deploy Verification CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `scripts/deploy.py` from a two-step registration script into a standalone six-check deploy-verification CLI, plus an opt-in `--sync-env` mode that pushes config to Render and deploys.

**Architecture:** Six independent `check_*()` functions each return a `CheckResult(name, status, detail)`; `main()` runs all six regardless of failures, renders one aligned table, and exits non-zero if any failed. GitHub calls reuse `app/github_app.py` (PyGithub/`requests`); Render, UptimeRobot, and `/healthz` are raw `httpx`; the database check is raw `psycopg` deliberately bypassing the app's connection pool. A thin `.claude/commands/deploy.md` wraps the CLI for Claude Code users but holds no logic.

**Tech Stack:** Python 3.12+, `httpx`, `psycopg`, PyGithub, `pytest`, `respx` (HTTP mocking for `httpx`), `uv`.

**Spec:** `docs/superpowers/specs/2026-08-05-deploy-command-design.md`

## Global Constraints

- Python `>=3.12`, managed with `uv`. Tests: `uv run pytest`; lint: `uv run ruff check .`. `ruff` line-length **100**.
- **No secret is ever printed, logged, or interpolated into a `CheckResult`.** `DATABASE_URL` carries a password; failure details name the exception type and the (non-secret) hostname, never the connection string. Task 6 pins this with a sentinel test.
- **No new packages.** `httpx`, `psycopg`, `respx` are all already declared in `pyproject.toml`.
- **CI stays deterministic and offline.** Every test mocks HTTP (`respx` for `httpx`; the existing `fake_transport` fixture for PyGithub) or monkeypatches the seam. No test makes a live call to GitHub, Render, UptimeRobot, or Supabase.
- **The CLI never depends on Claude Code.** Every `FAIL` detail is actionable on its own; exit codes are scriptable (0 ok / 1 check failed / 2 unusable input).
- **Output is terse by contract** (spec §7.4): one aligned line per check, details are fragments not sentences, no trailing periods, no emoji, no ANSI colour, explanations live in the docs.
- **`--sync-env` must only ever use the single-key endpoint** `PUT /v1/services/{id}/env-vars/{KEY}`. The bulk `PUT /v1/services/{id}/env-vars` replaces the entire list and would delete `DATABASE_URL`. Task 10 pins this with a test asserting the bulk route is never called.
- **Environment:** driven from WSL — pass Linux paths (`/mnt/c/...`) to file tools; `git` runs natively. Stage only each task's named files (never `git add .`; `.gitattributes` enforces LF).
- Docker (or a reachable local Postgres via `DATABASE_URL`) must be running for the full suite — see `SETUP.md` §2c.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/config.py` | Three optional operator-tooling settings | Modify |
| `app/github_app.py` | `AppNotInstalledError`, `get_webhook_url()` | Modify |
| `scripts/deploy.py` | All CLI logic: six checks, renderer, `main()`, `--sync-env` | Rewrite (same entry point) |
| `tests/test_deploy_script.py` | Unit tests for every check, the renderer, `main()`, `--sync-env` | Create |
| `tests/test_github_app.py` | Tests for the two new `github_app` additions | Modify |
| `.claude/commands/deploy.md` | Thin Claude Code wrapper, no logic | Create |
| `README.md`, `SETUP.md`, `.env.example` | Deployment docs at full parity | Modify |

**Task order:** 1 foundation → 2 `check_config` → 3 `check_installation_and_webhook` → 4 `check_health_endpoint` → 5 `check_database` → 6 secret-leak guard → 7 `check_render_service` → 8 `check_uptime_pinger` → 9 `main()` wiring → 10 `--sync-env` → 11 command file + docs.

### Background every implementer needs

**Two different HTTP mocking harnesses, and picking the wrong one wastes an hour.**

- `app/github_app.py` uses **PyGithub, which sends over `requests`**. `respx` only intercepts `httpx` and **cannot see PyGithub traffic at all**. GitHub-facing tests use the existing `fake_transport` fixture in `tests/test_github_app.py` (it patches `requests.adapters.HTTPAdapter.send`).
- Everything new in `scripts/deploy.py` that talks to Render, UptimeRobot, or `/healthz` uses **`httpx` directly**, so those tests use **`respx`**. `respx` is already a dev dependency but has no prior users in this repo — verified working for sync `httpx.get`/`httpx.head`/`httpx.put`, and `route.called` correctly reports `False` for a route that was never hit (which several tests rely on).
- `tests/test_deploy_script.py` therefore **monkeypatches `github_app.*` functions directly** rather than mocking GitHub HTTP. The check's job is the decision logic (read → compare → conditionally write); `github_app`'s own HTTP behavior is tested in `tests/test_github_app.py` where the right harness already lives.

**`scripts` is an implicit namespace package** (no `__init__.py`), and `pyproject.toml` sets `pythonpath = ["."]` for pytest, so `from scripts import deploy` works in tests. Verified.

---

### Task 1: Foundation — config fields, `CheckResult`, report renderer

**Files:**
- Modify: `app/config.py` (append to the `Settings` class body, after `dispatcher_notice_sweep_batch_size`)
- Modify: `scripts/deploy.py`
- Create: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, and every later task depends on these exact names:
  - `settings.uptimerobot_api_key: str`, `settings.render_api_key: str`, `settings.render_service_name: str`
  - `bot.scripts.deploy.CheckResult` — frozen dataclass, fields `name: str`, `status: Literal["PASS","FAIL","SKIPPED"]`, `detail: str = ""`
  - `bot.scripts.deploy.render_report(results: list[CheckResult]) -> str`
  - `bot.scripts.deploy.resolve_base_url() -> str` (returns the base URL already `rstrip("/")`-normalized, or `""`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy_script.py`:

```python
"""Deterministic tests for scripts/deploy.py.

Two mocking harnesses are in play and they are not interchangeable:
`respx` intercepts `httpx` (Render, UptimeRobot, /healthz), while GitHub calls
go through PyGithub/`requests` and are therefore monkeypatched at the
`github_app` function boundary rather than at the HTTP layer. See
tests/test_github_app.py for why respx cannot see PyGithub traffic.
"""

from __future__ import annotations

import pytest

from app.config import settings
from scripts import deploy


def test_resolve_base_url_prefers_settings_and_strips_trailing_slash(monkeypatch):
    """A trailing slash would make check_uptime_pinger's exact-URL comparison
    fail against a correctly configured monitor (spec section 7.1)."""
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com/")
    assert deploy.resolve_base_url() == "https://x.onrender.com"


def test_resolve_base_url_falls_back_to_render_external_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://y.onrender.com/")
    assert deploy.resolve_base_url() == "https://y.onrender.com"


def test_resolve_base_url_empty_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.resolve_base_url() == ""


def test_render_report_aligns_columns_and_summarizes():
    report = deploy.render_report(
        [
            deploy.CheckResult("config", "PASS", ""),
            deploy.CheckResult("health", "FAIL", "HEAD /healthz -> 405 (GET ok)"),
            deploy.CheckResult("database", "SKIPPED", "set DATABASE_URL"),
        ]
    )
    lines = report.split("\n")
    # Status starts at the same column on every row.
    status_columns = {line.index(s) for line, s in zip(lines[:3], ["PASS", "FAIL", "SKIPPED"])}
    assert len(status_columns) == 1
    assert lines[-1] == "1 failed, 1 skipped -- see README.md#deploying-to-production"


def test_render_report_indents_continuation_lines():
    """A detail may wrap only to enumerate observed values; the continuation
    must align under the detail column, not the name column."""
    report = deploy.render_report(
        [deploy.CheckResult("uptime-pinger", "FAIL", "no monitor matches /healthz\nfound: /healthz,")]
    )
    first, second = report.split("\n")[:2]
    assert second.startswith(" ")
    assert second.index("found:") == first.index("no monitor")


def test_render_report_summary_when_everything_passes():
    report = deploy.render_report([deploy.CheckResult("config", "PASS", "")])
    assert report.split("\n")[-1] == "all checks passed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'resolve_base_url'` (and the same for `CheckResult`/`render_report`).

- [ ] **Step 3: Add the three config fields**

In `app/config.py`, append to the `Settings` class body, immediately after the
`dispatcher_notice_sweep_batch_size` line and before the closing of the class:

```python

    # --- Optional operator tooling: read only by scripts/deploy.py on the
    # operator's own machine. Never set on the deployed service, never added
    # to render.yaml. Absence degrades a check to SKIPPED, never to an error.
    uptimerobot_api_key: str = ""
    render_api_key: str = ""
    render_service_name: str = "pr-review-engine"
```

- [ ] **Step 4: Add `CheckResult`, `resolve_base_url`, and `render_report`**

Replace the whole of `scripts/deploy.py` with:

```python
"""Deploy verification CLI for the hosted Render + Supabase deployment.

Runs six independent checks and prints one aligned table. Every check runs
regardless of earlier failures, so a single run surfaces every problem rather
than only the first. Exit codes: 0 all ok, 1 at least one check failed, 2 the
CLI could not run at all.

Standalone by design: nothing here assumes Claude Code, an assistant, or an
interactive terminal. `.claude/commands/deploy.md` is a convenience wrapper
that holds no logic.

Output is terse by contract (design spec section 7.4): details are fragments
naming the observed fact and the next action, never the reasoning -- the
explanations live in README.md.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal

from app.config import settings

_NAME_WIDTH = 18
_STATUS_WIDTH = 9
_README_ANCHOR = "README.md#deploying-to-production"


@dataclass(frozen=True)
class CheckResult:
    """One row of the report. ``detail`` is the whole user experience for a
    failing line: it must name what is wrong and what to do, because a terminal
    user has nothing else to work from. A newline in ``detail`` renders as an
    indented continuation line, used only to enumerate observed values."""

    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    detail: str = ""


def resolve_base_url() -> str:
    """This deployment's public origin, normalized exactly once.

    The rstrip is not cosmetic: check_uptime_pinger compares the monitor's URL
    by exact equality, so a trailing slash here would produce a doubled slash
    and fail a correctly configured pinger.
    """
    base = settings.public_base_url or os.environ.get("RENDER_EXTERNAL_URL", "")
    return base.rstrip("/")


def render_report(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for result in results:
        first, *rest = (result.detail or "").split("\n")
        lines.append(
            f"{result.name:<{_NAME_WIDTH}}{result.status:<{_STATUS_WIDTH}}{first}".rstrip()
        )
        lines.extend(" " * (_NAME_WIDTH + _STATUS_WIDTH) + line for line in rest)
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    lines.append("")
    if failed:
        lines.append(f"{failed} failed, {skipped} skipped -- see {_README_ANCHOR}")
    elif skipped:
        lines.append(f"all checks passed, {skipped} skipped")
    else:
        lines.append("all checks passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("wired up in a later task")


if __name__ == "__main__":
    raise SystemExit(main())
```

> The old `main()` body is deliberately removed here and rebuilt in Task 9. Between
> Task 1 and Task 9 the CLI is not runnable; the test suite is the gate, and every
> task in between still ends green.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add app/config.py scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): CheckResult, report renderer, and operator-tooling settings

Foundation for the deploy verification CLI. Adds the three optional
operator-local settings (UptimeRobot key, Render key, Render service name),
the CheckResult row type, and the terse aligned-table renderer.

resolve_base_url normalizes with rstrip('/') exactly once, because the pinger
check compares the monitor URL by exact equality and a trailing slash would
fail a correctly configured monitor."
```

---

### Task 2: `check_config()`

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult`, `resolve_base_url` (Task 1).
- Produces: `check_config() -> CheckResult` (name `"config"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
@pytest.fixture
def complete_config(monkeypatch, tmp_path):
    """Every value check_config requires, present and valid."""
    pem = tmp_path / "key.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    return pem


def test_check_config_passes_when_everything_is_present(complete_config):
    assert deploy.check_config().status == "PASS"


def test_check_config_accepts_base64_key_without_a_pem_file(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    monkeypatch.setattr(settings, "github_app_private_key_b64", "aGVsbG8=")
    assert deploy.check_config().status == "PASS"


def test_check_config_fails_when_the_pem_path_does_not_exist(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail


def test_check_config_names_every_missing_key_at_once(complete_config, monkeypatch):
    """One run should surface all of them, not the first alphabetically."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "github_target_repo", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert "GITHUB_TARGET_REPO" in result.detail


def test_check_config_requires_the_key_for_the_selected_provider(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "github_models")
    monkeypatch.setattr(settings, "github_models_token", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_MODELS_TOKEN" in result.detail


def test_check_config_ignores_provider_keys_for_other_providers(complete_config, monkeypatch):
    """groq is selected, so a missing GITHUB_MODELS_TOKEN is irrelevant."""
    monkeypatch.setattr(settings, "github_models_token", "")
    assert deploy.check_config().status == "PASS"


def test_check_config_never_prints_a_secret_value(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET_VALUE")
    result = deploy.check_config()
    assert "gsk_SUPER_SECRET_VALUE" not in result.detail
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k check_config -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_config'`.

- [ ] **Step 3: Implement `check_config`**

Add to `scripts/deploy.py`. Extend the imports at the top with `from pathlib import Path`, then add after `resolve_base_url`:

```python
# The provider key each LLM_PROVIDER value requires. An unrecognized provider
# contributes no requirement rather than a false failure.
_PROVIDER_KEYS = {
    "groq": "GROQ_API_KEY",
    "github_models": "GITHUB_MODELS_TOKEN",
    "gemini": "GEMINI_API_KEY",
}


def _private_key_available() -> bool:
    if settings.github_app_private_key_b64:
        return True
    path = Path(settings.github_app_private_key_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.is_file()


def check_config() -> CheckResult:
    """Every value the deployed service needs, resolvable locally.

    Reports missing key NAMES only -- never a value, never a length."""
    missing: list[str] = []
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    if not _private_key_available():
        missing.append("GITHUB_APP_PRIVATE_KEY_B64 or _PATH")
    if not settings.github_webhook_secret:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not settings.github_target_repo:
        missing.append("GITHUB_TARGET_REPO")
    if not resolve_base_url():
        missing.append("PUBLIC_BASE_URL or RENDER_EXTERNAL_URL")
    provider_key = _PROVIDER_KEYS.get(settings.llm_provider)
    if provider_key and not getattr(settings, provider_key.lower(), ""):
        missing.append(provider_key)
    if missing:
        return CheckResult("config", "FAIL", "missing: " + ", ".join(missing))
    return CheckResult("config", "PASS", "")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: 13 passed.

- [ ] **Step 5: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): check_config verifies every required setting

Names every missing key in one run rather than failing on the first, and
reports key names only -- never values. The provider key requirement follows
LLM_PROVIDER, so an unset GITHUB_MODELS_TOKEN is not a failure when groq is
selected."
```

---

### Task 3: `check_installation_and_webhook()` — read first, write only on drift

**Files:**
- Modify: `app/github_app.py`
- Modify: `tests/test_github_app.py`
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1).
- Produces:
  - `github_app.AppNotInstalledError(RuntimeError)` — raised by `discover_installation_id` on a 404 only.
  - `github_app.get_webhook_url() -> str` — the App's configured webhook URL, `""` when never configured. Raises `GithubException` on API failure.
  - `bot.scripts.deploy.check_installation_and_webhook(repo: str, base: str) -> CheckResult` (name `"github-app"`).

**Background:** `discover_installation_id` already distinguishes a 404 ("not
installed") from any other status; only the *exception type* changes here so the
caller can branch without matching on message text. `AppNotInstalledError`
subclasses `RuntimeError` and the message is unchanged, so the existing
`pytest.raises(RuntimeError, match="not installed")` test still passes.

- [ ] **Step 1: Write the failing tests for the `github_app` additions**

Append to `tests/test_github_app.py`:

```python
def test_discover_installation_id_raises_app_not_installed_on_404(fake_transport):
    """A distinct type lets callers branch without matching on message text."""
    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Not Found"}, 404
    )
    with pytest.raises(github_app.AppNotInstalledError):
        github_app.discover_installation_id(REPO_FULL_NAME)


def test_discover_installation_id_non_404_is_not_app_not_installed(fake_transport):
    """A 401 from a bad key must not be reported as a missing installation."""
    fake_transport.route(
        "GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Bad credentials"}, 401
    )
    with pytest.raises(RuntimeError) as excinfo:
        github_app.discover_installation_id(REPO_FULL_NAME)
    assert not isinstance(excinfo.value, github_app.AppNotInstalledError)


def test_get_webhook_url_returns_the_configured_url(fake_transport):
    fake_transport.route("GET", "/app/hook/config", {"url": "https://x.test/webhook"})
    assert github_app.get_webhook_url() == "https://x.test/webhook"


def test_get_webhook_url_returns_empty_when_never_configured(fake_transport):
    """An App whose webhook was never set is the genuine first-deploy state,
    not an error."""
    fake_transport.route("GET", "/app/hook/config", {})
    assert github_app.get_webhook_url() == ""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_app.py -k "app_not_installed or webhook_url" -v`
Expected: FAIL — `AttributeError: module 'app.github_app' has no attribute 'AppNotInstalledError'`.

- [ ] **Step 3: Implement the `github_app` additions**

In `app/github_app.py`, add above `discover_installation_id`:

```python
class AppNotInstalledError(RuntimeError):
    """The App is not installed on the target repo (GitHub returned 404).

    Subclasses RuntimeError so existing callers and tests that catch
    RuntimeError keep working; the distinct type exists so a caller can branch
    on "not installed" without matching on message text.
    """
```

In `discover_installation_id`, change the 404 branch's `raise RuntimeError(` to
`raise AppNotInstalledError(` — the message and every other line stay exactly as
they are.

Add after `set_webhook_url`:

```python
def get_webhook_url() -> str:
    """Return the App's currently configured webhook URL (App JWT).

    Returns "" when the App has no webhook URL set, which is the genuine
    first-deploy state rather than an error. Any API failure propagates as
    GithubException so the caller can decline to write after a failed read.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/hook/config")
    return (data or {}).get("url") or ""
```

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: all pass (24 tests — the 20 existing plus 4 new).

- [ ] **Step 5: Write the failing tests for the check**

Append to `tests/test_deploy_script.py`:

```python
@pytest.fixture
def github_seam(monkeypatch):
    """Monkeypatch the github_app boundary and record webhook writes.

    The check's job is the decision logic (read -> compare -> conditionally
    write); github_app's own HTTP behavior is covered in tests/test_github_app.py
    with the requests-level fake_transport harness, which respx cannot replace.
    """
    from app import github_app

    state = {"installation_id": 424242, "current_url": "", "written": []}

    monkeypatch.setattr(github_app, "discover_installation_id", lambda repo: state["installation_id"])
    monkeypatch.setattr(github_app, "get_webhook_url", lambda: state["current_url"])
    monkeypatch.setattr(github_app, "set_webhook_url", lambda url: state["written"].append(url))
    return state


def test_webhook_already_correct_passes_without_writing(github_seam):
    github_seam["current_url"] = "https://x.onrender.com/webhook"
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert "already correct" in result.detail
    assert github_seam["written"] == []          # no PATCH issued


def test_webhook_mismatch_is_updated(github_seam):
    github_seam["current_url"] = "https://old.example/webhook"
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]
    assert "https://old.example/webhook" in result.detail


def test_webhook_absent_is_set_on_first_deploy(github_seam):
    github_seam["current_url"] = ""
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "PASS"
    assert github_seam["written"] == ["https://x.onrender.com/webhook"]


def test_app_not_installed_fails_with_an_actionable_detail(github_seam, monkeypatch):
    from app import github_app

    def _raise(repo):
        raise github_app.AppNotInstalledError("not installed")

    monkeypatch.setattr(github_app, "discover_installation_id", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "install" in result.detail.lower()
    assert github_seam["written"] == []


def test_failed_webhook_read_does_not_write(github_seam, monkeypatch):
    """Writing blind after a failed read is how a correct URL gets clobbered."""
    from github import GithubException

    from app import github_app

    def _raise():
        raise GithubException(500, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "get_webhook_url", _raise)
    result = deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")
    assert result.status == "FAIL"
    assert "500" in result.detail
    assert github_seam["written"] == []
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k webhook -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_installation_and_webhook'`.

- [ ] **Step 7: Implement the check**

Add to `scripts/deploy.py`. Extend the imports with `from github import GithubException`
and `from app import github_app`, then add after `check_config`:

```python
def check_installation_and_webhook(repo: str, base: str) -> CheckResult:
    """Installation discovery plus an idempotent webhook registration.

    Reads the current webhook URL before writing so a re-run reports "already
    correct" rather than silently re-PATCHing, and so a failed read never
    triggers a blind write that could clobber a good URL.
    """
    name = "github-app"
    try:
        installation_id = github_app.discover_installation_id(repo)
    except github_app.AppNotInstalledError:
        return CheckResult(name, "FAIL", f"App not installed on {repo}; install via GitHub UI")
    except RuntimeError:
        return CheckResult(name, "FAIL", "installation lookup failed; check App ID / private key")

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook read failed ({exc.status})"
        )
    if current == wanted:
        return CheckResult(name, "PASS", f"installation={installation_id}; webhook already correct")
    github_app.set_webhook_url(wanted)
    if current:
        return CheckResult(
            name, "PASS", f"installation={installation_id}; webhook updated from {current}"
        )
    return CheckResult(name, "PASS", f"installation={installation_id}; webhook set")
```

- [ ] **Step 8: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add app/github_app.py tests/test_github_app.py scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): read the webhook URL before writing it

check_installation_and_webhook now GETs /app/hook/config and PATCHes only on
mismatch, so a re-run honestly reports 'already correct' instead of silently
re-writing, and a failed read declines to write rather than clobbering a good
URL with a stale one.

Adds AppNotInstalledError (a RuntimeError subclass, so existing callers and
tests are unaffected) so the caller can branch on 'not installed' without
matching on message text."
```

---

### Task 4: `check_health_endpoint()` — GET and HEAD

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1).
- Produces: `check_health_endpoint(base: str) -> CheckResult` (name `"health"`), and the module constant `_HTTP_TIMEOUT: float` reused by Tasks 7, 8, 10.

**Background:** this check exists because of a real 71-minute outage. UptimeRobot's
free tier sends `HEAD`; `/healthz` was `GET`-only and returned `405`, and no
dashboard setting could fix it. `app/main.py` now carries both `@app.get` and
`@app.head`, and outside `tests/test_skeleton.py` nothing else guards it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py` (add `import httpx` and `import respx` to the imports at the top of the file):

```python
BASE = "https://x.onrender.com"
HEALTH = f"{BASE}/healthz"


def test_health_passes_when_get_and_head_both_return_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "PASS"


def test_health_fails_when_head_is_405_even_though_get_is_200():
    """The exact regression that silently broke keep-warm for 71 minutes."""
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(200))
        respx.head(HEALTH).mock(return_value=httpx.Response(405))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "HEAD" in result.detail


def test_health_fails_when_get_is_not_200():
    with respx.mock:
        respx.get(HEALTH).mock(return_value=httpx.Response(503))
        respx.head(HEALTH).mock(return_value=httpx.Response(200))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
    assert "503" in result.detail


def test_health_fails_on_a_transport_error():
    with respx.mock:
        respx.get(HEALTH).mock(side_effect=httpx.ConnectError("refused"))
        result = deploy.check_health_endpoint(BASE)
    assert result.status == "FAIL"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k health -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_health_endpoint'`.

- [ ] **Step 3: Implement the check**

Add `import httpx` to `scripts/deploy.py`'s imports, the constant next to the
other module constants, and the function after `check_installation_and_webhook`:

```python
_HTTP_TIMEOUT = 10.0
```

```python
def check_health_endpoint(base: str) -> CheckResult:
    """Both verbs must answer 200.

    HEAD is not redundant: UptimeRobot's free tier sends HEAD by default, so a
    GET-only /healthz returns 405 to the pinger and the instance sleeps -- a
    failure invisible from a browser.
    """
    name = "health"
    url = f"{base}/healthz"
    try:
        get_status = httpx.get(url, timeout=_HTTP_TIMEOUT).status_code
        head_status = httpx.head(url, timeout=_HTTP_TIMEOUT).status_code
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"{type(exc).__name__} reaching {url}")
    if get_status != 200 and head_status != 200:
        return CheckResult(name, "FAIL", f"GET -> {get_status}, HEAD -> {head_status}")
    if get_status != 200:
        return CheckResult(name, "FAIL", f"GET /healthz -> {get_status} (HEAD ok)")
    if head_status != 200:
        return CheckResult(
            name, "FAIL", f"HEAD /healthz -> {head_status} (GET ok); pinger sends HEAD"
        )
    return CheckResult(name, "PASS", "GET + HEAD -> 200")
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): check_health_endpoint verifies GET and HEAD

UptimeRobot's free tier sends HEAD, so a GET-only /healthz returns 405 to the
pinger and the instance sleeps -- invisible from a browser, and the cause of a
real 71-minute keep-warm outage. Encoding it as a check means a refactor that
drops @app.head fails loudly instead of silently weeks later."
```

---

### Task 5: `check_database()` — connectivity plus provisioning

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1).
- Produces: `check_database() -> CheckResult` (name `"database"`).

**Background:** this must use a raw `psycopg.connect` with a short timeout, **not**
`store.init_pool()`. The pool waits `_POOL_TIMEOUT_SECONDS` (30) before raising,
and its `RuntimeError` is written for a Render startup log, not a checklist. A raw
connect reports the driver's real failure in about a second. The first hosted run
confirmed a healthy pooler connection completes in well under a second.

`to_regclass` is what separates "reachable" from "correctly provisioned": a
`DATABASE_URL` pointing at the wrong or a brand-new Supabase project answers
`SELECT 1` happily while having no `tickets` table at all.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
DEAD_DB_URL = "postgresql://u:pw@127.0.0.1:1/postgres?connect_timeout=1"


def test_database_skips_with_a_hint_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    result = deploy.check_database()
    assert result.status == "SKIPPED"
    assert "DATABASE_URL" in result.detail


def test_database_fails_fast_when_unreachable(monkeypatch):
    """127.0.0.1:1 refuses immediately, so this costs about a second."""
    monkeypatch.setattr(settings, "database_url", DEAD_DB_URL)
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "connect" in result.detail.lower()


def test_database_passes_against_the_provisioned_test_database(db, db_url, monkeypatch):
    """The `db` fixture opens the pool and creates the schema, so `tickets` exists."""
    monkeypatch.setattr(settings, "database_url", db_url)
    result = deploy.check_database()
    assert result.status == "PASS"
    assert "tickets present" in result.detail


def test_database_distinguishes_reachable_but_unprovisioned(db, db_url, db_exec, monkeypatch):
    """A DATABASE_URL pointing at the wrong Supabase project answers SELECT 1
    but has no tickets table -- a setup mistake a bare SELECT 1 calls success."""
    monkeypatch.setattr(settings, "database_url", db_url)
    db_exec("DROP TABLE IF EXISTS tickets")
    result = deploy.check_database()
    assert result.status == "FAIL"
    assert "tickets" in result.detail
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k database -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_database'`.

- [ ] **Step 3: Implement the check**

Add `import psycopg` and `from urllib.parse import urlsplit` to `scripts/deploy.py`'s
imports, the constant with the others, and the function after `check_health_endpoint`:

```python
_DB_CONNECT_TIMEOUT = 10
```

```python
def check_database() -> CheckResult:
    """Reachability AND provisioning, via a raw connection.

    Deliberately not store.init_pool(): the pool waits 30s before raising and
    its message is written for a startup log, not a checklist. A raw connect
    with a short timeout reports the driver's real failure in about a second.

    The failure detail names the exception type and the (non-secret) hostname
    only -- settings.database_url carries the password.
    """
    name = "database"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to check the queue database")
    host = urlsplit(settings.database_url).hostname or "?"
    try:
        with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
            conn.execute("SELECT 1")
            provisioned = conn.execute("SELECT to_regclass('public.tickets')").fetchone()[0]
    except psycopg.Error as exc:
        return CheckResult(name, "FAIL", f"cannot connect to {host} ({type(exc).__name__})")
    if provisioned is None:
        return CheckResult(name, "FAIL", "connected; tickets absent -- app never booted on this DB")
    return CheckResult(name, "PASS", "connected; tickets present")
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green. Docker (or a `DATABASE_URL` pointing at a local Postgres) must be available for the two `db`-fixture tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): check_database verifies connectivity and provisioning

SELECT 1 alone proves only that the connection string works. to_regclass
additionally proves the app completed a successful boot against THIS database,
distinguishing 'wrong Supabase project' from 'cannot connect' -- a setup
mistake a bare SELECT 1 reports as success.

Uses a raw psycopg.connect rather than store.init_pool() so a failure reports
the driver's own error in about a second instead of waiting out the pool's 30s
startup-oriented timeout."
```

---

### Task 6: Secret-leak guard for the database detail

**Files:**
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `check_database` (Task 5).
- Produces: nothing consumed downstream.

**Why its own task:** `CLAUDE.md`'s "no secret is ever logged" is the one rule in
this feature whose violation is invisible in normal use — the leak only appears in
a failure path an operator may not hit for months. It gets a dedicated,
independently rejectable guard, mirroring the existing
`test_init_pool_error_never_leaks_the_connection_string`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deploy_script.py`:

```python
SENTINEL_PASSWORD = "sentinel-pw-must-not-appear"


def test_check_database_failure_never_leaks_the_connection_string(monkeypatch):
    """CLAUDE.md: no secret is ever logged. database_url carries the password,
    so the failure detail must describe the failure shape -- exception type and
    the non-secret hostname -- and never interpolate the URL."""
    monkeypatch.setattr(
        settings,
        "database_url",
        f"postgresql://someuser:{SENTINEL_PASSWORD}@127.0.0.1:1/postgres?connect_timeout=1",
    )
    result = deploy.check_database()
    assert result.status == "FAIL"
    rendered = result.detail + repr(result) + deploy.render_report([result])
    assert SENTINEL_PASSWORD not in rendered
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_deploy_script.py -k never_leaks -v`
Expected: PASS immediately. This is a characterization test — Task 5 already
built the detail from the exception type and hostname. **If it FAILS, stop:** a
real secret leak exists and must be fixed in `check_database`, not papered over
by weakening the assertion.

- [ ] **Step 3: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_deploy_script.py
git commit -m "test: pin that a DB check failure never leaks the connection string

DATABASE_URL carries a password and the failure path is one an operator may
not hit for months, so the no-secret-logged rule gets an explicit guard rather
than relying on the current implementation staying careful."
```

---

### Task 7: `check_render_service()`

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1), `_HTTP_TIMEOUT` (Task 4).
- Produces: `check_render_service() -> CheckResult` (name `"render-service"`), plus
  `_RENDER_API: str`, `_render_headers() -> dict[str, str]`, and
  `_find_render_service_id() -> str | None` — all three reused by Task 10.

**Background:** this check answers *why*, not *whether*. `check_health_endpoint`
already establishes whether the service responds from the outside, which is what
matters to a user of the bot. This turns a failing health check from a symptom
into a cause: a suspended free instance, a crash-loop on a bad env var, and a
deploy still building are indistinguishable over plain HTTP.

Render's list endpoint returns objects wrapped as `{"service": {...}}`, and the
deploys endpoint as `{"deploy": {...}}`; the helpers below unwrap defensively so
an unwrapped shape also works.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(name="pr-review-engine", service_id="srv-1"):
    return [{"service": {"id": service_id, "name": name}}]


def _deploy_list(status):
    return [{"deploy": {"id": "dep-1", "status": status}}]


def test_render_service_skips_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    result = deploy.check_render_service()
    assert result.status == "SKIPPED"
    assert "RENDER_API_KEY" in result.detail


def test_render_service_passes_when_latest_deploy_is_live(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("live"))
        )
        result = deploy.check_render_service()
    assert result.status == "PASS"


def test_render_service_fails_and_names_a_non_live_status(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(200, json=_deploy_list("build_failed"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "build_failed" in result.detail


def test_render_service_fails_when_the_configured_name_is_absent(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(200, json=_service_list(name="something-else"))
        )
        result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "pr-review-engine" in result.detail


def test_render_service_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(401, json={}))
        result = deploy.check_render_service()
    assert "rnd_SUPER_SECRET" not in result.detail
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k render_service -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_render_service'`.

- [ ] **Step 3: Implement the check and its helpers**

Add the constant with the others and the code after `check_database` in `scripts/deploy.py`:

```python
_RENDER_API = "https://api.render.com/v1"
```

```python
def _render_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def _unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def _find_render_service_id() -> str | None:
    resp = httpx.get(f"{_RENDER_API}/services", headers=_render_headers(), timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json():
        service = _unwrap(item, "service")
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    return None


def check_render_service() -> CheckResult:
    """Why the service is or is not serving -- health already covers whether."""
    name = "render-service"
    if not settings.render_api_key:
        return CheckResult(name, "SKIPPED", "set RENDER_API_KEY to check deploy status")
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not deploys:
        return CheckResult(name, "FAIL", "service exists but has no deploys")
    status = _unwrap(deploys[0], "deploy").get("status", "?")
    if status != "live":
        return CheckResult(name, "FAIL", f"latest deploy status: {status}")
    return CheckResult(name, "PASS", "latest deploy live")
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): optional check_render_service reports deploy status

Turns a failing health check from a symptom into a cause -- a suspended free
instance, a crash-loop on a bad env var, and a deploy still building are
indistinguishable over plain HTTP. Skipped entirely when RENDER_API_KEY is
unset, so it costs nothing to ignore."
```

---

### Task 8: `check_uptime_pinger()`

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1), `_HTTP_TIMEOUT` (Task 4).
- Produces: `check_uptime_pinger(base: str) -> CheckResult` (name `"uptime-pinger"`).

**Background:** both real pinger failures from the hosted run must land in `FAIL`
here. The monitor URL is compared by **exact equality** because the actual outage
was a trailing comma (`/healthz,`) that fired perfectly on schedule and 404'd every
time. The interval ceiling exists because a monitor that polls every 30 minutes
loses to Render's ~15-minute spin-down — an existence-only check would bless it.

UptimeRobot's `status` field: `0` = paused, `1` = not checked yet, `2` = up,
`8` = seems down, `9` = down. Only `0` is a configuration failure; up/down is
reported as information, since `check_health_endpoint` already covers reachability.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
UPTIMEROBOT = "https://api.uptimerobot.com/v2/getMonitors"


def _monitors(*monitors):
    return {"stat": "ok", "monitors": list(monitors)}


def test_uptime_pinger_skips_with_a_hint_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "")
    result = deploy.check_uptime_pinger(BASE)
    assert result.status == "SKIPPED"
    assert "UPTIMEROBOT_API_KEY" in result.detail


def test_uptime_pinger_passes_for_an_active_five_minute_monitor(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 2, "interval": 300}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "PASS"


def test_uptime_pinger_fails_on_a_near_miss_url(monkeypatch):
    """The real outage: a trailing comma, firing on schedule, 404ing every time."""
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(
                200, json=_monitors({"url": HEALTH + ",", "status": 2, "interval": 300})
            )
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert HEALTH + "," in result.detail       # the near-miss is visible on sight


def test_uptime_pinger_fails_when_paused(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 0, "interval": 300}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "paused" in result.detail


def test_uptime_pinger_fails_when_the_interval_lets_the_instance_sleep(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_x")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(
            return_value=httpx.Response(200, json=_monitors({"url": HEALTH, "status": 2, "interval": 1800}))
        )
        result = deploy.check_uptime_pinger(BASE)
    assert result.status == "FAIL"
    assert "1800" in result.detail


def test_uptime_pinger_never_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "uptimerobot_api_key", "u_SUPER_SECRET")
    with respx.mock:
        respx.post(UPTIMEROBOT).mock(return_value=httpx.Response(500, json={}))
        result = deploy.check_uptime_pinger(BASE)
    assert "u_SUPER_SECRET" not in result.detail
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k uptime -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute 'check_uptime_pinger'`.

- [ ] **Step 3: Implement the check**

Add the constants with the others and the function after `check_render_service`:

```python
_UPTIMEROBOT_API = "https://api.uptimerobot.com/v2/getMonitors"
# Render free instances spin down after ~15 minutes idle; 10 minutes leaves margin.
_MAX_PINGER_INTERVAL_SECONDS = 600
```

```python
def check_uptime_pinger(base: str) -> CheckResult:
    """The keep-warm monitor exists, is active, and polls often enough.

    The URL is compared by exact equality on purpose: the real outage was a
    trailing comma, which fired perfectly on schedule and 404'd every time.
    """
    name = "uptime-pinger"
    if not settings.uptimerobot_api_key:
        return CheckResult(name, "SKIPPED", "set UPTIMEROBOT_API_KEY to check keep-warm")
    wanted = f"{base}/healthz"
    try:
        resp = httpx.post(
            _UPTIMEROBOT_API,
            data={"api_key": settings.uptimerobot_api_key, "format": "json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        monitors = resp.json().get("monitors") or []
    except httpx.HTTPError as exc:
        return CheckResult(name, "FAIL", f"UptimeRobot API error ({type(exc).__name__})")

    match = next((m for m in monitors if m.get("url") == wanted), None)
    if match is None:
        found = ", ".join(m.get("url", "?") for m in monitors) or "none"
        return CheckResult(name, "FAIL", f"no monitor matches {wanted}\nfound: {found}")
    if match.get("status") == 0:
        return CheckResult(name, "FAIL", "monitor is paused")
    interval = int(match.get("interval") or 0)
    if interval > _MAX_PINGER_INTERVAL_SECONDS:
        return CheckResult(
            name, "FAIL", f"interval {interval}s > {_MAX_PINGER_INTERVAL_SECONDS}s; will sleep"
        )
    return CheckResult(name, "PASS", f"interval {interval}s; status={match.get('status')}")
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): optional check_uptime_pinger verifies keep-warm

Both real pinger failures from the first hosted run now fail this check: the
URL is compared by exact equality (the outage was a trailing comma that fired
on schedule and 404'd every time), and an interval above 10 minutes fails
because it loses to Render's ~15-minute spin-down."
```

---

### Task 9: `main()` — ordering, isolation, exit codes

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: all six checks and `render_report` (Tasks 1-8).
- Produces: `run_checks(repo: str, base: str) -> list[CheckResult]` and
  `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
def _stub_all_checks(monkeypatch, statuses):
    """Replace all six checks with constant results, in report order."""
    names = ["config", "github-app", "health", "database", "render-service", "uptime-pinger"]
    fns = [
        "check_config",
        "check_installation_and_webhook",
        "check_health_endpoint",
        "check_database",
        "check_render_service",
        "check_uptime_pinger",
    ]
    for fn, name, status in zip(fns, names, statuses):
        monkeypatch.setattr(
            deploy, fn, (lambda n, s: lambda *args: deploy.CheckResult(n, s, ""))(name, status)
        )


@pytest.fixture
def runnable(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)


def test_main_returns_zero_when_all_pass_or_skip(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS"] * 4 + ["SKIPPED"] * 2)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out


def test_main_returns_one_when_any_check_fails(runnable, monkeypatch, capsys):
    _stub_all_checks(monkeypatch, ["PASS", "FAIL", "PASS", "PASS", "SKIPPED", "SKIPPED"])
    assert deploy.main([]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_main_returns_two_without_a_target_repo(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    assert deploy.main([]) == 2


def test_main_returns_two_without_a_base_url(monkeypatch):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert deploy.main([]) == 2


def test_run_checks_reports_all_six_in_order(runnable, monkeypatch):
    _stub_all_checks(monkeypatch, ["PASS"] * 6)
    results = deploy.run_checks("owner/repo", BASE)
    assert [r.name for r in results] == [
        "config", "github-app", "health", "database", "render-service", "uptime-pinger"
    ]


def test_an_exploding_check_becomes_a_fail_and_does_not_abort_the_run(runnable, monkeypatch):
    """A complete table is the deliverable; one broken check must not deprive
    the operator of the other five diagnoses (spec section 7.3)."""
    def _boom():
        raise ValueError("unexpected")

    _stub_all_checks(monkeypatch, ["PASS"] * 6)
    monkeypatch.setattr(deploy, "check_database", _boom)
    results = deploy.run_checks("owner/repo", BASE)
    assert len(results) == 6
    database = next(r for r in results if r.name == "database")
    assert database.status == "FAIL"
    assert "ValueError" in database.detail
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "main or run_checks or exploding" -v`
Expected: FAIL — `NotImplementedError` from the Task 1 placeholder, and
`AttributeError` for `run_checks`.

- [ ] **Step 3: Implement `run_checks` and `main`**

Replace the placeholder `main` at the bottom of `scripts/deploy.py` with:

```python
def _safe(name: str, fn, *args) -> CheckResult:
    """No check may abort the run: a complete table is the deliverable."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - deliberate: any failure becomes a row
        return CheckResult(name, "FAIL", f"unexpected {type(exc).__name__}")


def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All six, cheapest and most foundational first, so a misconfiguration is
    reported before the checks that would fail as a consequence of it."""
    return [
        _safe("config", check_config),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = settings.github_target_repo
    base = resolve_base_url()
    if not repo or not base:
        print(
            "GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
            "are required",
            file=sys.stderr,
        )
        return 2
    if "--sync-env" in args:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(repo, base)
    print(render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0
```

Add a temporary stub above `main` so this task stands alone; Task 10 replaces it:

```python
def sync_env() -> int:
    raise NotImplementedError("implemented in the next task")
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): run all six checks and exit on the aggregate result

Every check runs regardless of earlier failures, so one run surfaces every
problem rather than only the first, and an unexpected exception inside a check
becomes a FAIL row instead of aborting the report. Exit codes: 0 all ok, 1 at
least one failure, 2 the CLI could not run at all."
```

---

### Task 10: `--sync-env` — push config, deploy, wait

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `_RENDER_API`, `_render_headers`, `_find_render_service_id`, `_unwrap` (Task 7); `main` (Task 9).
- Produces: `_SYNCED_ENV_VARS: tuple[str, ...]` (the authoritative list Task 11's docs test reads) and `sync_env() -> int`.

**Background — two constraints that are not stylistic:**

1. **Only ever `PUT /v1/services/{id}/env-vars/{KEY}`.** The bulk
   `PUT /v1/services/{id}/env-vars` (no key) replaces the *entire* list and would
   silently delete every variable not in the payload, `DATABASE_URL` included.
2. **Render env-var changes do not auto-deploy.** Pushing without triggering a
   deploy leaves the service serving the old values, so the command would report
   success while nothing changed. The trigger-and-poll is what makes the report
   honest. The first hosted run measured 55-65s to `live`.

The clobber guard runs before any request so a partial push cannot happen: a blank
local value must never overwrite a working remote secret.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
@pytest.fixture
def sync_ready(monkeypatch, tmp_path):
    """Every value _wanted_env() reads, non-empty, plus a Render key."""
    pem = tmp_path / "key.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/postgres")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(settings, "github_models_token", "ghp_x")
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sync_env_requires_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.sync_env() == 2


def test_sync_env_refuses_to_push_an_empty_value(sync_ready, monkeypatch, capsys):
    """A blank .env entry must never overwrite a working remote secret, and the
    guard must fire before any request is issued."""
    monkeypatch.setattr(settings, "groq_api_key", "")
    with respx.mock:
        route = respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        assert deploy.sync_env() == 2
        assert not route.called
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_sync_env_pushes_only_changed_keys_via_the_single_key_endpoint(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        current = dict.fromkeys(deploy._SYNCED_ENV_VARS, "stale")
        current["GITHUB_TARGET_REPO"] = "owner/repo"       # already correct
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(current))
        )
        bulk = respx.put(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json={})
        )
        single = respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1", "status": "created"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        assert deploy.sync_env() == 0
        assert not bulk.called          # the bulk PUT would delete DATABASE_URL
        # Seven of eight differ; GITHUB_TARGET_REPO already matched.
        assert single.call_count == len(deploy._SYNCED_ENV_VARS) - 1


def test_sync_env_skips_the_deploy_when_nothing_changed(sync_ready, capsys):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list(deploy._wanted_env()))
        )
        triggered = respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={})
        )
        assert deploy.sync_env() == 0
        assert not triggered.called
    assert "already in sync" in capsys.readouterr().out


def test_sync_env_fails_when_the_deploy_fails(sync_ready):
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "build_failed"}})
        )
        assert deploy.sync_env() == 1


def test_sync_env_never_prints_a_secret_value(sync_ready, monkeypatch, capsys):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_SUPER_SECRET")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({}))
        )
        respx.put(url__regex=rf"{RENDER_SERVICES}/srv-1/env-vars/.+").mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(f"{RENDER_SERVICES}/srv-1/deploys").mock(
            return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}})
        )
        respx.get(f"{RENDER_SERVICES}/srv-1/deploys/dep-1").mock(
            return_value=httpx.Response(200, json={"deploy": {"status": "live"}})
        )
        deploy.sync_env()
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET" not in captured.out + captured.err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k sync_env -v`
Expected: FAIL — `NotImplementedError` from the Task 9 stub.

- [ ] **Step 3: Implement `--sync-env`**

Add `import base64` and `import time` to `scripts/deploy.py`'s imports, then the
constants with the others:

```python
# The service env vars --sync-env pushes. Authoritative: tests/test_deploy_script.py
# asserts README.md and SETUP.md each mention every name here.
_SYNCED_ENV_VARS = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_B64",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "LLM_PROVIDER",
    "GROQ_API_KEY",
    "GITHUB_MODELS_TOKEN",
)
_DEPLOY_POLL_SECONDS = 10
_DEPLOY_TIMEOUT_SECONDS = 300
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "canceled",
    "deactivated",
}
```

Replace the `sync_env` stub with:

```python
def _wanted_env() -> dict[str, str]:
    """Local values for every synced var. The PEM is base64-encoded on the fly
    when only the file path is configured locally, since Render needs the b64 form."""
    pem_b64 = settings.github_app_private_key_b64
    if not pem_b64:
        path = Path(settings.github_app_private_key_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_file():
            pem_b64 = base64.b64encode(path.read_bytes()).decode()
    return {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY_B64": pem_b64,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "LLM_PROVIDER": settings.llm_provider,
        "GROQ_API_KEY": settings.groq_api_key,
        "GITHUB_MODELS_TOKEN": settings.github_models_token,
    }


def _trigger_and_wait(service_id: str) -> int:
    """Render env-var changes do not auto-deploy, so a sync that skipped this
    would report success while the service kept serving the old values."""
    resp = httpx.post(
        f"{_RENDER_API}/services/{service_id}/deploys",
        headers=_render_headers(),
        json={},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    deploy_id = _unwrap(resp.json(), "deploy").get("id")
    print(f"deploy {deploy_id} triggered; waiting for live")
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_DEPLOY_POLL_SECONDS)
        poll = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        poll.raise_for_status()
        status = _unwrap(poll.json(), "deploy").get("status", "?")
        if status == "live":
            print("deploy live")
            return 0
        if status in _DEPLOY_FAILED_STATUSES:
            print(f"deploy {status}", file=sys.stderr)
            return 1
    print("timed out waiting for the deploy to go live", file=sys.stderr)
    return 1


def sync_env() -> int:
    """Push local config to the Render service, then deploy and wait.

    Only ever uses the single-key endpoint: the bulk
    PUT /v1/services/{id}/env-vars replaces the entire list and would silently
    delete every variable not in the payload, DATABASE_URL included.
    """
    if not settings.render_api_key:
        print("--sync-env requires RENDER_API_KEY", file=sys.stderr)
        return 2
    wanted = _wanted_env()
    empty = sorted(key for key, value in wanted.items() if not value)
    if empty:
        # Before any request, so a partial push cannot happen.
        print(f"refusing to push empty values; fix .env first: {', '.join(empty)}", file=sys.stderr)
        return 2
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            print(f"no Render service named {settings.render_service_name}", file=sys.stderr)
            return 1
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/env-vars",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        current = {}
        for item in resp.json():
            env_var = _unwrap(item, "envVar")
            current[env_var.get("key")] = env_var.get("value")

        changed = [key for key, value in wanted.items() if current.get(key) != value]
        for key in changed:
            put = httpx.put(
                f"{_RENDER_API}/services/{service_id}/env-vars/{key}",
                headers=_render_headers(),
                json={"value": wanted[key]},
                timeout=_HTTP_TIMEOUT,
            )
            put.raise_for_status()
            print(f"pushed {key} (len {len(wanted[key])})")   # names and lengths only
        if not changed:
            print("env vars already in sync; no deploy triggered")
            return 0
        return _trigger_and_wait(service_id)
    except httpx.HTTPError as exc:
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 1
```

- [ ] **Step 4: Run the tests, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): --sync-env pushes config, deploys, and waits for live

Only ever uses the single-key env-var endpoint; the bulk PUT replaces the
entire list and would silently delete DATABASE_URL. A clobber guard runs
before any request so a blank local value can never overwrite a working
remote secret, and a partial push cannot happen.

Render env-var changes do not auto-deploy, so the sync triggers a deploy and
polls until live -- without that the command would report success while the
service kept serving the old values."
```

---

### Task 11: Slash command and documentation

**Files:**
- Create: `.claude/commands/deploy.md`
- Modify: `README.md` (the "Deploying to production (Render + Supabase)" section)
- Modify: `SETUP.md` (§3.4 and §3.5)
- Modify: `.env.example`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `_SYNCED_ENV_VARS` (Task 10).
- Produces: nothing consumed downstream.

- [ ] **Step 1: Write the failing docs-sync test**

`README.md` and `SETUP.md` carry the same procedure at the same depth, and
duplicated prose drifts. Wording may diverge freely; the env-var contract may not.

Append to `tests/test_deploy_script.py` (add `from pathlib import Path` to the imports):

```python
@pytest.mark.parametrize("doc", ["README.md", "SETUP.md"])
def test_env_var_names_match_the_docs(doc):
    """README.md and SETUP.md are kept at full parity by convention; this test
    is the mechanism behind it. A var pushed by --sync-env but undocumented
    means nobody knows to set it; a var documented but missing from the list is
    silently never deployed. Checks NAMES only -- wording is free to differ."""
    text = Path(doc).read_text(encoding="utf-8")
    missing = [name for name in deploy._SYNCED_ENV_VARS if name not in text]
    assert not missing, f"{doc} does not mention: {missing}"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_deploy_script.py -k env_var_names -v`
Expected: **both** parameterizations FAIL, with exactly these missing names
(verified against the current files):

- `README.md` — missing `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
  `GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `GROQ_API_KEY`,
  `GITHUB_MODELS_TOKEN` (it already mentions `DATABASE_URL` and `LLM_PROVIDER`).
- `SETUP.md` — missing `GITHUB_MODELS_TOKEN` only.

Steps 4 and 5 below add every one of them, so both go green together.

- [ ] **Step 3: Create the slash command**

Create `.claude/commands/deploy.md`:

```markdown
---
description: Verify the hosted Render + Supabase deployment and register the webhook
---

Run the deploy verification CLI and report its results:

```bash
uv run python -m bot.scripts.deploy
```

Show the printed table to the user verbatim — it is already terse by design, so
do not summarize, reformat, or add emoji to it.

If the exit code is non-zero, help the user act on each line marked `FAIL`,
using the hint that line already printed. Full explanations of each check live
in `README.md` under "Deploying to production (Render + Supabase)"; read that
section before speculating about a cause.

If the diagnosis is that the Render service's environment variables have
drifted from the local `.env`, the follow-up is:

```bash
uv run python -m bot.scripts.deploy --sync-env
```

That pushes the changed variables, triggers a deploy, waits for it to go live,
and then re-runs the checklist. It requires `RENDER_API_KEY` and refuses to run
if any local value is empty.

This command holds no verification logic of its own — `scripts/deploy.py` is the
tool, and it works identically for people who do not use Claude Code.
```

- [ ] **Step 4: Rewrite README.md's deployment section**

Replace the whole "### Deploying to production (Render + Supabase)" section
(currently lines 70-87, ending just before "## Testing") with:

````markdown
### Deploying to production (Render + Supabase)

The bot runs as a Docker container on Render's free tier with its durable queue
in Supabase Postgres, kept awake by a free UptimeRobot monitor. `scripts/deploy.py`
is the tool for both verifying and performing a deploy; it is a plain CLI and
needs no editor, assistant, or Claude Code.

#### One-time setup

These four steps need a browser and cannot be automated — the first is
*structurally* impossible, since GitHub does not permit an App to install itself.

1. **Install the GitHub App on the target repo** — repo Settings → GitHub Apps.
   A repo admin authorizes it once.
2. **Create the Supabase project**, wait until it reports ready, and copy the
   **Session-mode pooler** connection string (port 5432, not 6543) as
   `DATABASE_URL`.
3. **Create the Render service** from `render.yaml` (New + → Blueprint).
4. **Create an UptimeRobot monitor** on `https://<your-service>.onrender.com/healthz`
   with a **5-minute interval**. The URL must match exactly — a stray trailing
   character 404s on every check while looking perfectly healthy in the dashboard.

Full click-by-click detail for each: [`SETUP.md`](SETUP.md) §3.

#### Verifying a deployment

```bash
PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m bot.scripts.deploy
```

Run it from your own machine, not inside the Render container — `scripts/` is not
copied into the Docker image, and `RENDER_EXTERNAL_URL` only exists inside
Render's own container, which is why `PUBLIC_BASE_URL` is passed explicitly here.

It prints one line per check and always runs all six, so a single run surfaces
every problem rather than only the first:

| Check | Verifies | Required? |
|---|---|---|
| `config` | Every setting the service needs is resolvable locally | yes |
| `github-app` | The App is installed, and its webhook points here (set only if wrong) | yes |
| `health` | `/healthz` answers **both** `GET` and `HEAD` — UptimeRobot's free tier sends `HEAD`, so a `GET`-only endpoint lets the instance sleep | yes |
| `database` | Postgres is reachable **and** the app has provisioned its `tickets` table there | optional |
| `render-service` | The latest Render deploy is `live` | optional |
| `uptime-pinger` | A monitor targets `/healthz` exactly, is active, and polls at most every 10 minutes | optional |

Exit codes: `0` everything passed or was skipped, `1` at least one check failed,
`2` the CLI could not run at all (no `GITHUB_TARGET_REPO` or public base URL).

The two optional checks are skipped with a hint unless you set the matching
operator-local key. Neither is ever set on the Render service:

- `RENDER_API_KEY` (Render → Account Settings → API Keys) enables
  `render-service` and `--sync-env`.
- `UPTIMEROBOT_API_KEY` (a read-only key) enables `uptime-pinger`.
- `DATABASE_URL` enables `database`. It is normally a Render dashboard secret;
  export it locally, temporarily, to check it.

#### Deploying

With `RENDER_API_KEY` set, this is a complete, repeatable deploy:

```bash
PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m bot.scripts.deploy --sync-env
```

It pushes any of `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, `GROQ_API_KEY`
and `GITHUB_MODELS_TOKEN` that differ from your local `.env`, triggers a deploy,
waits for it to go live (~60s), and then runs the checklist above.

It refuses to start if any of those values is empty locally, so a blank `.env`
entry can never overwrite a working secret on the service. Only changed
variables are pushed, and if nothing differs no deploy is triggered.

Claude Code users can run `/deploy` instead, which wraps the same CLI.
````

- [ ] **Step 5: Update SETUP.md §3.4 and §3.5**

In §3.4, replace the bare `python -m bot.scripts.deploy` registration step with the
same content as README's "Verifying a deployment" and "Deploying" subsections —
the six-check table, the three exit codes, the three optional keys with what each
unlocks, and the `--sync-env` command. Keep the existing runs-locally and
`PUBLIC_BASE_URL` guidance, which is still correct.

The `--sync-env` paragraph **must name all eight** variables it pushes
(`DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, `GROQ_API_KEY`,
`GITHUB_MODELS_TOKEN`). `GITHUB_MODELS_TOKEN` is the one name `SETUP.md` does
not currently contain anywhere, and Step 1's test fails until it does.

In §3.5, add to the UptimeRobot instructions:

```markdown
The monitor's URL must be exactly `https://<your-service>.onrender.com/healthz`
— a stray trailing character (a comma pasted from prose, for instance) returns
404 on every check while the dashboard still shows the monitor firing on
schedule. Use an interval of **5 minutes**; anything above 10 lets Render's
~15-minute spin-down win. UptimeRobot's free tier sends `HEAD` rather than
`GET`, which is why `/healthz` answers both verbs.
```

- [ ] **Step 6: Update `.env.example`**

In the "Optional operator tooling" block at the end, replace the stale line
`# Never add this to render.yaml. Unknown vars are ignored by app/config.py.`
(`RENDER_API_KEY` is now a real `Settings` field, so it is no longer an unknown
var) and add the two new entries, so the block reads:

```
# --- Optional operator tooling (NOT used by the deployed service) ---
# Read only by scripts/deploy.py on your own machine. Never add these to
# render.yaml and never set them on the Render service. Leaving any of them
# empty simply skips the check it enables.
# Render API key (Account Settings -> API Keys): enables the render-service
# check and `--sync-env`.
RENDER_API_KEY=
# Render service name; must match `name:` in render.yaml.
RENDER_SERVICE_NAME=pr-review-engine
# UptimeRobot read-only API key: enables the uptime-pinger check.
UPTIMEROBOT_API_KEY=
```

- [ ] **Step 7: Run the docs test, the full suite, and the linter**

Run: `uv run pytest tests/test_deploy_script.py -v && uv run pytest -q && uv run ruff check .`
Expected: all green, including both parameterizations of `test_env_var_names_match_the_docs`.

- [ ] **Step 8: Verify the CLI runs end-to-end against nothing**

Run: `uv run python -m bot.scripts.deploy` with `GITHUB_TARGET_REPO` unset.
Expected: exit code 2 and the "GITHUB_TARGET_REPO and a public base URL ... are
required" message on stderr — no traceback. Confirm with `echo $?`.

- [ ] **Step 9: Commit**

```bash
git add .claude/commands/deploy.md README.md SETUP.md .env.example tests/test_deploy_script.py
git commit -m "docs: document the deploy CLI at full parity in README and SETUP

README is where people actually look, so its deployment section now carries the
complete story -- the one-time manual prerequisites, the repeatable --sync-env
deploy, what each of the six checks means, the exit codes, and the three
optional keys -- rather than pointing at SETUP.md.

Duplicated prose drifts, so a test asserts both documents mention every env-var
name in the script's authoritative list. Names only; wording stays free to
differ.

Adds .claude/commands/deploy.md, a thin wrapper holding no logic -- the CLI
works identically without Claude Code."
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every section maps to a task:

- §4.1 `CheckResult` → Task 1. §4.2 automation boundary → Task 11 (README's
  one-time-setup and deploying subsections). §5 config fields → Task 1.
- §6.1 → Task 2. §6.2 → Task 3. §6.3 → Task 4. §6.4 → Task 5. §6.5 → Task 7.
  §6.6 → Task 8.
- §7.1 default mode + base normalization → Tasks 1 and 9. §7.2 exit codes →
  Task 9 (0/1/2) and Task 10 (the clobber-guard 2). §7.3 no check aborts →
  Task 9's `_safe`. §7.4 output contract → Task 1's renderer and its tests.
  §7.5 pool bypass → Task 5.
- §8 `--sync-env`, all seven steps → Task 10. §9 secret handling → Tasks 2, 5,
  6, 7, 8, 10 (each has an explicit no-leak assertion). §10 docs → Task 11.
  §10.1 sync mechanism → Task 11's `test_env_var_names_match_the_docs`.
  §11 testing → distributed across every task, TDD.
- §12 non-goals respected: nothing provisions infrastructure, nothing rotates
  secrets, no live LLM call is made, only UptimeRobot is integrated, one service.

**2. Placeholder scan** — no "TBD", no "add error handling", no "similar to Task
N". Every code step contains the actual code. Two deliberate temporary stubs
(`main` in Task 1, `sync_env` in Task 9) are explicitly labeled with the task
that replaces them, and each task still ends with a green suite.

**3. Type consistency** — `CheckResult(name, status, detail)` is constructed
identically in all six checks. Check names are fixed strings used in three
places and they agree: the check bodies, `run_checks`'s `_safe` calls, and
`_stub_all_checks` in the tests (`config`, `github-app`, `health`, `database`,
`render-service`, `uptime-pinger`). `_HTTP_TIMEOUT` (Task 4) is reused by Tasks
7, 8, 10. `_RENDER_API`/`_render_headers`/`_find_render_service_id`/`_unwrap`
(Task 7) are reused by Task 10. `_SYNCED_ENV_VARS` (Task 10) is read by Task
11's test. `resolve_base_url` (Task 1) is used by Task 2 and Task 9.
`AppNotInstalledError` (Task 3) is raised in `app/github_app.py` and caught in
`scripts/deploy.py`.

**4. One deliberate refinement of the spec.** Spec §11 asks for an
output-contract test asserting "no `detail` exceeding the §7.4 length budget".
The plan does not assert a hard character limit, because several legitimate
details embed a full URL (`no monitor matches https://…/healthz`) and would trip
it. Task 1 instead pins the contract's *structure* — column alignment,
continuation-line indentation, and the summary line — and the ~70-character
budget stays what §7.4 calls it: a signal to move prose into the docs, enforced
in review rather than by a brittle assertion.
