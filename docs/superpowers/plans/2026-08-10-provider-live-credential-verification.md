# Live Provider-Credential Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap where `scripts/deploy.py` reports a clean `provider PASS`
for a credential that was never actually pushed to the live Render service, and
stop `scripts/set_provider.py` from writing an override that would fail every
review the instant it takes effect.

**Architecture:** A new `provider-live` check in `scripts/deploy.py` resolves the
actively-running provider (DB override or plain `LLM_PROVIDER`, unified via a
new helper) and asks Render's own env-vars endpoint whether that provider's
credential is genuinely present — not just locally. `scripts/set_provider.py`
gets a pre-write guard built on the same primitive, refusing by default (with a
`--force` escape hatch) when it would write an override doomed to fail, but
skipping the check entirely when the local `DATABASE_URL` isn't even the one
Render's service reads (so it never blocks legitimate local testing).

**Tech Stack:** Python 3.12, `httpx` (mocked via `respx` in tests), `psycopg`,
pytest/pytest-asyncio, the existing `scripts/deploy.py` / `scripts/set_provider.py`
CLI pair.

## Global Constraints

- Secrets only via env vars; **no secret is ever logged** (`CLAUDE.md`). A
  fetched Render env-var value may only be reduced to a boolean or an in-memory
  equality result — never assigned to a variable that outlives that
  computation, never printed, never part of a `CheckResult.detail`.
- `ruff check .` must pass at `line-length = 100` (E501 enforced).
- Tests are deterministic: `respx` for all `httpx` traffic to Render; no real
  network calls; DB-touching tests use the existing `db` fixture
  (`tests/conftest.py`), never a real Supabase connection.
- `allow_abbrev=False` on every `argparse.ArgumentParser` in this CLI pair —
  an abbreviation match on `--sync-env`/`--clear` has already caused a real
  incident against live infrastructure; the new `--force` flag inherits the
  same parser and is covered by the same guard.
- Follow this repo's existing style exactly: `_safe`-wrapped checks in
  `run_checks`, `CheckResult(name, status, detail)` rows, `# noqa: BLE001` on
  deliberate broad `except Exception` with a one-line comment explaining why.

---

## File Structure

- Modify `scripts/deploy.py`:
  - Extract `_render_env_vars(service_id)` out of `sync_env()` (Task 1).
  - Add `_resolved_provider_or_env()` and `check_provider_live()`; wire into
    `run_checks()` (Task 2).
  - Update the module docstring and `build_parser()`'s description text to
    say "eight" checks (Task 2).
- Modify `scripts/set_provider.py`:
  - Add `_verify_render_credential(provider)` and a `--force` flag; wire into
    `main()` (Task 3).
- Modify `tests/test_deploy_script.py`: new tests for `_render_env_vars`
  (Task 1), `_resolved_provider_or_env` and `check_provider_live` (Task 2).
- Modify `tests/test_set_provider_script.py`: new tests for the verification
  guard (Task 3).
- Modify `README.md` and `SETUP.md`: `provider-live` row and prose (Task 2);
  `--force` and the `DATABASE_URL`-match note (Task 3).

No new files. This is entirely additive to two existing, small (785-line and
~65-line) scripts.

---

### Task 1: Extract `_render_env_vars()` from `sync_env()`

**Files:**
- Modify: `scripts/deploy.py:600-664` (inside `sync_env()`)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `_render_headers()`, `_unwrap()`, `_RENDER_API`, `_HTTP_TIMEOUT`
  (all already defined in `scripts/deploy.py`).
- Produces: `deploy._render_env_vars(service_id: str) -> dict[str, str]` — the
  service's live env-vars, key → value. Tasks 2 and 3 both call this. Every
  caller must treat the returned values as write-once: reduce to a boolean or
  an equality result immediately, never store or print them.

**Why:** `sync_env()` already fetches and unwraps the service's env-vars into
exactly this shape to compute what changed. Pulling that block into its own
function means `check_provider_live()` (Task 2) and
`_verify_render_credential()` (Task 3) reuse one fetch implementation instead
of each writing their own copy of the `_unwrap(item, "envVar")` loop.

This is a pure refactor — `sync_env()`'s behavior does not change. Every
existing `sync_env` test continues to pass unmodified; this task adds one
direct test of the extracted function.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deploy_script.py` (near the other Render-env-vars tests,
after `_env_var_list`'s definition around line 861):

```python
def test_render_env_vars_unwraps_the_service_env_list(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"A": "1", "B": "2"}))
        )
        result = deploy._render_env_vars("srv-1")
    assert result == {"A": "1", "B": "2"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k render_env_vars -v`
Expected: FAIL — `deploy` has no attribute `_render_env_vars`.

- [ ] **Step 3: Extract the function**

In `scripts/deploy.py`, add this new function directly above `sync_env()`:

```python
def _render_env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it. See CLAUDE.md's "no secret is ever
    logged" and docs/superpowers/specs/
    2026-08-10-provider-live-credential-verification-design.md section 6.
    """
    resp = httpx.get(
        f"{_RENDER_API}/services/{service_id}/env-vars",
        headers=_render_headers(),
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    current: dict[str, str] = {}
    for item in resp.json():
        env_var = _unwrap(item, "envVar")
        current[env_var.get("key")] = env_var.get("value")
    return current
```

Then replace the inline fetch inside `sync_env()`:

```python
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
    # deliberate: nothing has been pushed yet, so a crashed lookup really is
    # "could not run at all"
    except Exception as exc:  # noqa: BLE001
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 2
```

with:

```python
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            print(f"no Render service named {settings.render_service_name}", file=sys.stderr)
            return 1
        current = _render_env_vars(service_id)
        changed = [key for key, value in wanted.items() if current.get(key) != value]
    # deliberate: nothing has been pushed yet, so a crashed lookup really is
    # "could not run at all"
    except Exception as exc:  # noqa: BLE001
        print(f"Render API error ({type(exc).__name__})", file=sys.stderr)
        return 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS — every existing `sync_env` test still passes (they exercise
the same behavior through the extracted function), plus the new direct test.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "refactor(deploy): extract _render_env_vars() out of sync_env()

Pure refactor, no behavior change. Task 2 and Task 3 of the live-credential
verification plan both need this fetch -- pulling it out now means they
share one implementation instead of each copying the envVar-unwrap loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011of2TjcbdRf9vTrjK268Ss"
```

---

### Task 2: `check_provider_live()` — the new eighth check

**Files:**
- Modify: `scripts/deploy.py` (new helper + check + `run_checks` + docstrings)
- Modify: `README.md` (check table + prose)
- Modify: `SETUP.md` (check table)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `deploy._render_env_vars` (Task 1), `deploy._resolved_provider`,
  `deploy._PROVIDERS`, `deploy._find_render_service_id`, `deploy.settings`.
- Produces: `deploy._resolved_provider_or_env() -> tuple[str, str | None]`;
  `deploy.check_provider_live() -> CheckResult`. `run_checks()` now returns
  **eight** results, with `provider-live` immediately after `provider`.

**Why:** `check_provider()` already resolves the active provider (override or
env) and confirms its credential is set — but only in the local `.env`.
`check_provider_live()` asks Render the same question about the **deployed**
service. Because `_resolved_provider_or_env()` falls back to
`settings.llm_provider` when there's no `DATABASE_URL` to check for an
override, this one check covers both the DB-override case (the one hit live
during the demo rehearsal) and the plain-`LLM_PROVIDER` case, per the design
doc's §2.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deploy_script.py`, near the existing `check_provider` tests
(after `test_check_provider_skips_without_a_database_url`, before
`test_run_checks_includes_the_provider_row`):

```python
def test_resolved_provider_or_env_falls_back_without_a_database_url(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    assert deploy._resolved_provider_or_env() == ("groq", None)


def test_resolved_provider_or_env_resolves_the_override_when_database_url_is_set(
    override_seam,
):
    override_seam(("gemini",))
    assert deploy._resolved_provider_or_env() == ("gemini", "gemini")


def test_resolved_provider_or_env_propagates_a_db_error(override_seam):
    override_seam(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        deploy._resolved_provider_or_env()


def test_provider_live_skips_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_skips_when_the_override_read_raises(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    override_seam(RuntimeError("boom"))
    assert deploy.check_provider_live().status == "SKIPPED"


def test_provider_live_passes_for_the_plain_env_provider_without_a_database_url(
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_provider_live()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "env" in result.detail


def test_provider_live_fails_when_the_overrides_credential_is_missing_on_render(
    override_seam, monkeypatch
):
    """The exact failure hit live during the demo rehearsal: `provider` PASSes
    locally while `provider-live` catches that Render was never given the key."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")  # present locally
    override_seam(("gemini",))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_provider_live()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail
    assert "not present" in result.detail


def test_provider_live_never_leaks_a_fetched_value(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    override_seam(None)
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"GROQ_API_KEY": "gsk_SUPER_SECRET"})
            )
        )
        result = deploy.check_provider_live()
    assert "gsk_SUPER_SECRET" not in result.detail


def test_run_checks_includes_the_provider_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider_live",
                        lambda: deploy.CheckResult("provider-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider-live" in names
    assert names.index("provider-live") > names.index("provider")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "resolved_provider_or_env or provider_live" -v`
Expected: FAIL — neither `_resolved_provider_or_env` nor `check_provider_live`
exist yet, and `run_checks` returns seven rows.

- [ ] **Step 3: Add the helper and the check**

In `scripts/deploy.py`, add directly below `check_provider()`:

```python
def _resolved_provider_or_env() -> tuple[str, str | None]:
    """Like _resolved_provider(), but usable without DATABASE_URL: without a
    database there is no override to check, so this falls back to the
    env-configured provider instead of requiring a connection. Used by
    check_provider_live(), which -- unlike check_provider() -- must answer
    "what's actually running" even when there's no override to resolve.
    """
    if not settings.database_url:
        return settings.llm_provider, None
    return _resolved_provider()


def check_provider_live() -> CheckResult:
    """Whether the actively-resolved provider's credential is genuinely
    present on the live Render service -- not just locally.

    `provider` validates the local `.env`; this is the check that would have
    caught the demo-rehearsal failure where a DB override named a provider
    whose key was never pushed to Render.
    """
    name = "provider-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "SKIPPED", "set RENDER_API_KEY to verify credentials against the live service"
        )
    try:
        provider, override = _resolved_provider_or_env()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active provider ({type(exc).__name__})"
        )
    source = f"DB override; env={settings.llm_provider}" if override else "env"
    entry = _PROVIDERS.get(provider)
    if entry is None:
        # check_config / check_provider already FAIL on an unsupported name;
        # there is no credential key to look up without a table entry.
        return CheckResult(name, "SKIPPED", f"{provider} ({source}) is not a supported provider")
    credential = entry[0]
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live_value = _render_env_vars(service_id).get(credential) or ""
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not live_value:
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) -- {credential} not present on Render"
        )
    return CheckResult(name, "PASS", f"{provider} ({source}) -- {credential} present on Render")
```

- [ ] **Step 4: Wire it into `run_checks`**

In `run_checks()`, insert immediately after the `provider` line and update the
docstring's count:

```python
def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All eight, cheapest and most foundational first, so a misconfiguration
    is reported before the checks that would fail as a consequence of it."""
    return [
        _safe("config", check_config),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("provider", check_provider),
        _safe("provider-live", check_provider_live),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]
```

Also update the module docstring at the top of `scripts/deploy.py` ("Runs
seven independent checks" → "Runs eight independent checks") and
`build_parser()`'s `description=` string ("active provider, Render service"
→ "active provider, whether that provider's credential is actually live on
Render, Render service").

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 6: Mutation check — prove the FAIL path bites**

Temporarily change `if not live_value:` to `if False:` in
`check_provider_live()`, then run:
`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k missing_on_render -v`
Expected: **FAIL**. Revert and confirm green again.

- [ ] **Step 7: Update README.md**

In `README.md`:

1. Line 104: `"It prints one line per check and always runs all seven,"` →
   `"It prints one line per check and always runs all eight,"`.
2. After line 113 (the `provider` row), insert:
   ```
   | `provider-live` | The actively-resolved provider's credential (env or DB override) is present on the deployed Render service — not just locally | optional |
   ```
3. Line 132 ("Four checks are skipped...") → "Five checks are skipped...".
4. Line 136-137 (the `RENDER_API_KEY` bullet) → `"RENDER_API_KEY` (Render →
   Account Settings → API Keys) enables `render-service`, `provider-live`,
   and `--sync-env`."`
5. In the "Switching providers without a redeploy" section (lines 177-196),
   replace the sentence `"A typo'd provider name still surfaces as a FAIL,
   but a provider whose key was never pushed to Render will report PASS
   here and then fail every real review. To be sure a credential actually
   exists on the service, run --sync-env (see "Deploying" above), which is
   what actually gets it there."` with: `"A typo'd provider name still
   surfaces as a FAIL, and a provider whose key was never pushed to Render
   is now caught automatically by provider-live (set RENDER_API_KEY to
   enable it) rather than only surfacing as a failed review. To push a
   missing credential, run --sync-env (see "Deploying" above)."`

- [ ] **Step 8: Update SETUP.md**

In `SETUP.md`:

1. Lines 285-286: `"always runs all seven"` → `"always runs all eight"`.
2. After line 294 (the `provider` row), insert:
   ```
   | `provider-live` | The actively-resolved provider's credential (env or DB override) is present on the deployed Render service — not just locally | optional |
   ```
3. Line 315 ("Four checks are skipped...") → "Five checks are skipped...".
4. Lines 318-319 (the `RENDER_API_KEY` bullet) → same wording as README's
   Step 7.4 above.

- [ ] **Step 9: Confirm the docs-parity test still passes**

`test_env_var_names_match_the_docs` (`tests/test_deploy_script.py:1174`)
checks env-var *names*, not check names, so it is unaffected — but run it
anyway to be sure nothing in Step 7/8's edits broke a name it scans for:

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k docs -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py README.md SETUP.md
git commit -m "feat(deploy): add provider-live, verifying credentials against Render itself

provider already resolves the active provider (DB override or LLM_PROVIDER)
and checks its credential -- but only in the local .env. This adds the check
that asks Render's own env-vars endpoint the same question, closing the gap
confirmed live during the demo rehearsal: a DB override naming a provider
whose key was never pushed reported a clean PASS and then failed every
review.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011of2TjcbdRf9vTrjK268Ss"
```

---

### Task 3: `set_provider.py` verifies before it writes

**Files:**
- Modify: `scripts/set_provider.py`
- Modify: `README.md` (§ "Switching providers without a redeploy")
- Modify: `SETUP.md` (§3.6)
- Test: `tests/test_set_provider_script.py`

**Interfaces:**
- Consumes: `deploy._find_render_service_id`, `deploy._render_env_vars`
  (Task 1), `_PROVIDERS` (already imported).
- Produces: `set_provider._verify_render_credential(provider: str) -> tuple[bool, str]`.
  `set_provider.main()` gains a `--force` flag and calls this before writing
  a non-`None` override.

**Why:** Today `set_provider.py` is a pure write — it validates only the
provider *name*, then writes the row, with no way to know whether that
provider even has a credential on the live service. This adds the same
Render-backed verification `provider-live` performs, but as a pre-write
guard that can refuse. It reuses the one env-vars fetch to also compare local
`DATABASE_URL` against Render's live one, so a globally-set `RENDER_API_KEY`
never blocks a purely local override — a write to a different database
cannot affect production, so verification is skipped outright rather than
warned about.

- [ ] **Step 1: Write the failing tests**

`tests/test_set_provider_script.py` does not yet import `httpx`, `respx`, or
`settings` — add these to its existing import block at the top of the file
(`pytest`, `store`, and `set_provider` are already imported there):

```python
import httpx
import respx

from app.config import settings
```

Then add, below the existing tests:

```python
RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sets_the_override_without_a_render_api_key(capsys):
    """No RENDER_API_KEY: verification degrades to a warning and the write
    proceeds -- matches this CLI's SKIPPED-on-absent-key convention."""
    assert set_provider.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"
    assert "could not verify against Render" in capsys.readouterr().out


def test_refuses_when_the_credential_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_provider.main(["groq"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert "GROQ_API_KEY" in err


def test_force_writes_the_override_despite_a_missing_credential(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_provider.main(["groq", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "GROQ_API_KEY" in err
    assert "--force" in err


def test_refuses_when_the_credential_differs_from_local_env(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_remote"})
            )
        )
        code = set_provider.main(["groq"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert "GROQ_API_KEY" in err
    assert "differs" in err


def test_proceeds_when_the_credential_matches_local_env(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_match")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_match"})
            )
        )
        code = set_provider.main(["groq"])
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "verified" in capsys.readouterr().out


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_SUPER_SECRET_REMOTE"}
                ),
            )
        )
        set_provider.main(["groq"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err


def test_proceeds_without_refusal_when_local_database_url_does_not_match_render(
    monkeypatch, capsys
):
    """The closed edge case: RENDER_API_KEY set globally but DATABASE_URL
    points at a local/test database -- this write cannot affect production,
    so it must not be refused no matter what Render's credentials look like."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "groq_api_key", "")  # would refuse if compared
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        code = set_provider.main(["groq"])
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "does not match" in capsys.readouterr().out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_provider, "_verify_render_credential", _boom)
    assert set_provider.main(["--clear"]) == 0


def test_rejects_an_abbreviated_force_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_provider.main(["groq", "--for"])
    assert exc.value.code == 2
    assert "--for" in capsys.readouterr().err
```

Note: every new test that sets `render_api_key` and expects live traffic runs
inside `with respx.mock:`, following this test module's existing pattern —
`_quarantine_operator_apis` (`tests/conftest.py`) already blanks
`render_api_key` by default, and these tests override it via `monkeypatch`
exactly like `tests/test_deploy_script.py` already does, so no additional
opt-in fixture is needed.

- [ ] **Step 2: Run them to verify they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -v`
Expected: FAIL — `_verify_render_credential` does not exist, `--force` is an
unrecognized argument, and the "without a Render API key" test fails because
nothing prints the new warning yet.

- [ ] **Step 3: Add `_verify_render_credential()`**

In `scripts/set_provider.py`, change the import line:

```python
from scripts.deploy import _PROVIDERS
```

to:

```python
from scripts.deploy import _PROVIDERS, _find_render_service_id, _render_env_vars
```

and add `from app.config import settings` to the imports. Then add, above
`build_parser()`:

```python
def _verify_render_credential(provider: str) -> tuple[bool, str]:
    """(ok_to_proceed, message). Never returns, prints, or logs a fetched
    Render value -- only presence/absence and in-memory equality results. See
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6 for the invariant this maintains.
    """
    if not settings.render_api_key:
        return True, (
            "could not verify against Render (no RENDER_API_KEY); "
            "setting override without live verification"
        )
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return True, (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); setting override without live verification"
            )
        env_vars = _render_env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return True, (
            f"could not verify against Render ({type(exc).__name__}); "
            "setting override without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, (
            "local DATABASE_URL does not match the Render service's; "
            "this override has no effect on production -- skipping live verification"
        )

    credential, _ = _PROVIDERS[provider]
    live_value = env_vars.get(credential) or ""
    local_value = getattr(settings, credential.lower(), "")
    if not live_value:
        return False, (
            f"{credential} is missing on the Render service; the override would fail "
            "every review immediately. Push it first (uv run python -m scripts.deploy "
            "--sync-env) or pass --force"
        )
    if not local_value:
        return True, f"{credential} present on Render (no local value to compare)"
    if live_value != local_value:
        return False, (
            f"{credential} on Render differs from your local .env value; the running "
            "service may use an unexpected key. Sync first, or pass --force"
        )
    return True, f"{credential} verified on Render (matches local .env)"
```

- [ ] **Step 4: Add `--force` and wire `main()`**

In `build_parser()`, add after the `--clear` argument:

```python
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the override even if live verification against Render finds a problem",
    )
```

Replace `main()`'s body from `provider = None if args.clear else args.provider`
onward with:

```python
    provider = None if args.clear else args.provider
    if provider is not None:
        ok, message = _verify_render_credential(provider)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2
    store.init_pool()
    store.set_provider_override(provider, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to LLM_PROVIDER" if provider is None
          else f"override set to {provider}")
    return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -v`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 6: Mutation checks**

1. In `_verify_render_credential`, temporarily change
   `if env_vars.get("DATABASE_URL") != settings.database_url:` to `if False:`,
   then run:
   `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -k does_not_match -v`
   Expected: **FAIL** (the DB-mismatch write now gets wrongly refused). Revert.
2. In `main()`, temporarily change `elif args.force:` to `elif False:`, then
   run:
   `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -k force_writes -v`
   Expected: **FAIL**. Revert and confirm both are green again.

- [ ] **Step 7: Update `scripts/set_provider.py`'s module docstring**

Replace:

```
Validation is limited to the provider name: this runs on the operator's machine
and cannot know whether that provider's credential exists on the deployed
service. `scripts/deploy.py`'s `provider` check is the safety net for that.
```

with:

```
Before writing a non-cleared override, this verifies the target provider's
credential against the live Render service (when RENDER_API_KEY is set and
the local DATABASE_URL is the one Render actually reads) and refuses by
default if it's missing or differs from the local .env value -- pass --force
to write anyway. `scripts/deploy.py`'s `provider-live` check is the read-only
counterpart to this write-time guard.
```

- [ ] **Step 8: Update README.md**

In the "Switching providers without a redeploy" section (§ around line
177-196), replace the closing sentences (from `"A typo'd provider name still
surfaces as a FAIL..."` — already rewritten once in Task 2's Step 7.5 — through
the end of that paragraph) with:

```
`scripts/set_provider.py` itself now verifies the target provider's
credential against Render before writing the override (when `RENDER_API_KEY`
is set), and refuses by default if it's missing or differs from your local
`.env` — pass `--force` to write anyway. If your local `DATABASE_URL` isn't
the one Render's service actually reads (e.g. you're testing against a local
database), this verification is skipped automatically, since the write
cannot affect production either way.
```

- [ ] **Step 9: Update SETUP.md §3.6**

Replace the paragraph starting `"This writes a row to the runtime_config
table..."` (lines 392-408) with:

```
This writes a row to the `runtime_config` table in whatever database
`DATABASE_URL` currently resolves to — **not** necessarily production. Run it
with your local `.env` and you get a purely local override; it reaches the
production service only if your local `DATABASE_URL` happens to be the
production one. The override takes effect on the **next ticket the
dispatcher claims** — no restart, no redeploy, which is what makes it useful
for a live provider-swap demo (build step 7's `demo_provider_swap.py`
predates this and used two `uvicorn` restarts instead; a follow-up spec
rewrite is tracked but deferred).

Before writing a non-cleared override, `set_provider.py` verifies the target
provider's credential against the live Render service — when `RENDER_API_KEY`
is set, and only when the local `DATABASE_URL` is actually the one Render
reads (if it isn't, e.g. you're testing against a local database, the write
cannot affect production and verification is skipped automatically). It
refuses by default (exit 2) if the credential is missing on Render or differs
from your local `.env` value; pass `--force` to write the override anyway.
`scripts/deploy.py`'s `provider-live` check is the read-only counterpart to
this guard, and both are built on the same `GET
/v1/services/{id}/env-vars` call.
```

- [ ] **Step 10: Full-suite check**

Run: `uv run ruff check . && TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -v`
Expected: PASS, no lint errors, full test suite green.

- [ ] **Step 11: Commit**

```bash
git add scripts/set_provider.py tests/test_set_provider_script.py README.md SETUP.md
git commit -m "feat(set_provider): refuse to write an override doomed to fail

set_provider.py was a pure write with no way to know whether the provider it
just selected has a credential on the live service. It now verifies against
Render before writing (when RENDER_API_KEY is set and the local DATABASE_URL
is the one Render actually reads) and refuses by default -- --force writes
anyway. A local DATABASE_URL that doesn't match Render's skips verification
outright, since that write can never affect production.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011of2TjcbdRf9vTrjK268Ss"
```

---

## Final Verification

- [ ] `uv run ruff check .` — clean.
- [ ] `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -v` — full suite green.
- [ ] Manually re-read `README.md`'s "Verifying a deployment" and "Switching
  providers without a redeploy" sections, and `SETUP.md` §3.4/§3.6, end to
  end — confirm they read coherently with the new check and flag in place,
  not just that the individual edits landed.
- [ ] Grep the diff for any fetched Render value reaching a `print(`,
  `f-string` in a `CheckResult`, or log call outside the boolean/equality
  reduction described in this plan's Global Constraints — this is the
  invariant the handoff doc flagged as having been violated once already,
  live.

## Deferred to a follow-up

- Consolidating the Render-API and provider-resolution access code across
  `scripts/deploy.py` and `scripts/set_provider.py` into a shared internal
  module. Flagged during this plan's design review; the three planes
  involved (local `.env`, Render's live env, the DB override) encode
  genuinely different facts on purpose, so this is an access-layer refactor,
  not a data-model change — scoped as its own design session.
