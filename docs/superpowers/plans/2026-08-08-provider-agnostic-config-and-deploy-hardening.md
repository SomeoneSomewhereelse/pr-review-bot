# Provider-Agnostic Config and Deploy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make provider configuration agnostic and internally consistent across
`.env`, the deploy CLI, and Render; add a DB-backed provider override so the
demo can swap providers with no redeploy; and stop the deploy CLI from reporting
green for a service that cannot do its job.

**Architecture:** One `_PROVIDERS` table in `scripts/deploy.py` becomes the
single source of truth for which credential and model variable each provider
needs. A singleton `runtime_config` row in the existing Postgres holds an
optional provider override, read through one `active_provider()` accessor that
replaces every direct read of `settings.llm_provider`. The deploy CLI gains a
`provider` check so an override can never be invisible to it, and
`check_render_service` starts reporting *what* is live rather than only that
something is.

**Tech Stack:** Python 3.12, FastAPI, psycopg3 + psycopg_pool, pydantic-settings,
pytest + pytest-asyncio + respx, testcontainers Postgres, ruff, `uv`.

## Global Constraints

- **Design source:** `docs/superpowers/specs/2026-08-08-provider-agnostic-config-and-deploy-hardening-design.md`. Read it before starting.
- **Test command:** `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q`. The
  `TESTCONTAINERS_RYUK_DISABLED=1` is mandatory on WSL2 + Docker Desktop — the
  Ryuk reaper container hangs without it.
- **Single-test command:** `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/<file>::<test> -v`
- **Lint:** `uv run ruff check .` must be clean. `line-length = 100`.
- **Async:** async throughout; store functions stay **synchronous** and are
  called from async code via `asyncio.to_thread`. Never add an `async def` to
  `app/queue/store.py`.
- **Timestamps:** TEXT ISO-8601 columns. No `TIMESTAMP` types.
- **Secrets:** no secret value is ever logged or printed. Names and lengths only.
- **Tests never touch Supabase.** `tests/conftest.py` refuses a non-local
  `DATABASE_URL`.
- **Staging:** stage only each task's named files. Never `git add .`
  (`.gitattributes` enforces LF).
- **Commit footer:** every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue
  ```
- **Branch:** all work on `feat/provider-agnostic-config`, created from `master`
  in Task 1. Do **not** commit to `master`.
- **Do not push.** `origin/main` is Render-connected and auto-deploys.

## Resolved Assumptions

Spec §8 listed four assumptions. **Assumption 1 is resolved — verified against a
real Postgres 16 container before this plan was written:**

- psycopg3's `execute()` **does** accept two statements in one call when no
  parameters are passed. `_SCHEMA` stays a single string.
- `CHECK (id = 1)` genuinely rejects a second row (`psycopg.errors.CheckViolation`).
- The `ON CONFLICT (id) DO UPDATE` upsert leaves exactly one row.

Assumptions 2–4 (Render blueprint reconciliation, cancel-vs-queue, and the
Render API response fields) remain unverified and are handled defensively in
Tasks 11–12.

## File Structure

**Created:**
- `app/providers/active.py` — the active-provider accessor and its cache. Deliberately dependency-light: imports only `settings`, never `store`, so `webhook.py` does not pull the DB driver or LLM SDKs through it.
- `scripts/set_provider.py` — operator CLI to set/clear the override.
- `tests/test_provider_override.py` — store layer + accessor tests.
- `tests/test_set_provider_script.py` — setter CLI tests.

**Modified:**
- `app/providers/google_genai.py`, `factory.py`, `pricing.py` — Vertex removal.
- `app/config.py` — drop GCP settings; comment fix.
- `app/orchestrator.py`, `app/webhook.py`, `app/queue/dispatcher.py` — read through `active_provider()`.
- `app/queue/store.py` — `runtime_config` schema + two functions.
- `scripts/deploy.py` — `_PROVIDERS`, argparse, `_private_key_b64`, `provider` check, sync guards, polling fixes, topology reporting.
- `tests/conftest.py` — truncate `runtime_config` between tests.
- `tests/test_providers.py`, `tests/test_deploy_script.py`, `tests/test_dispatcher.py`.
- `.env.example`, `render.yaml`, `pyproject.toml`, `README.md`, `SETUP.md`, `CLAUDE.md`.

---

### Task 1: Retire Vertex from code and configuration

**Files:**
- Modify: `app/providers/google_genai.py:1-17,75-90`
- Modify: `app/providers/factory.py`
- Modify: `app/providers/pricing.py:19`
- Modify: `app/config.py:17,24-25`
- Modify: `app/orchestrator.py:39`
- Modify: `.env.example:26,29-31,47`
- Modify: `README.md:207`, `SETUP.md:43-45`, `CLAUDE.md`
- Test: `tests/test_providers.py:64,134`

**Interfaces:**
- Consumes: nothing.
- Produces: `get_provider()` raises `ValueError` for any provider outside `{"gemini", "groq", "github_models"}`. `settings` no longer has `google_cloud_project` / `google_cloud_location`.

- [ ] **Step 1: Create the branch**

```bash
git checkout master
git status --short          # must be empty
git checkout -b feat/provider-agnostic-config
```

- [ ] **Step 2: Write the failing test**

Replace `test_factory_selects_vertex` (`tests/test_providers.py:134`) with a
test that asserts the retirement rather than merely deleting coverage:

```python
def test_factory_rejects_retired_vertex_provider(monkeypatch):
    """Vertex was evaluated and removed (requires a payment card). A stale
    LLM_PROVIDER=vertex must fail loudly, naming the accepted values."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    with pytest.raises(ValueError) as exc:
        get_provider()
    assert "vertex" in str(exc.value)
    assert "'gemini', 'groq', or 'github_models'" in str(exc.value)
```

Also delete `test_vertex_provider_parses_valid_structured_output`
(`tests/test_providers.py:64-86`) and remove `VertexProvider` from the imports
at the top of the file.

- [ ] **Step 3: Run it to make sure it fails**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_providers.py -v`
Expected: FAIL — `get_provider()` currently returns a `VertexProvider` instead of raising, so `pytest.raises(ValueError)` reports `DID NOT RAISE`.

- [ ] **Step 4: Delete `VertexProvider`**

In `app/providers/google_genai.py`, delete the whole `VertexProvider` class
(lines 75-90) and rewrite the module docstring's first paragraph and the
deviation note:

```python
"""The ``google-genai`` client adapter for Gemini (AI-Studio).

Per SPEC.md section 4 this calls ``client.aio.models.generate_content(...)``
with a JSON-schema response config, then parses+validates the raw text against
``schema`` locally (rather than trusting the SDK's own ``response.parsed``
field) so that ``validate.py``'s repair-retry logic has one single,
provider-agnostic notion of "validation failed" that doesn't depend on
SDK-internal behavior.

Deviation from SPEC.md (see CLAUDE.md's "Substitutions from the brief"): the
``vertex`` adapter that once lived here was removed. Vertex AI requires an
attached payment card, which this project's no-card constraint rules out, so it
was never live-runnable here and could only ever be covered by mocked tests.
"""
```

- [ ] **Step 5: Update the factory**

Rewrite `app/providers/factory.py`'s import and body:

```python
from app.providers.google_genai import GeminiProvider


def get_provider() -> LLMProvider:
    provider = settings.llm_provider

    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        return GroqProvider()
    if provider == "github_models":
        return GitHubModelsProvider()

    raise ValueError(
        f"Unknown LLM_PROVIDER: {provider!r} "
        "(expected 'gemini', 'groq', or 'github_models')"
    )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_providers.py -v`
Expected: PASS.

- [ ] **Step 7: Remove the remaining code and config references**

`app/providers/pricing.py:19` — delete the line:
```python
    ("vertex", "gemini-flash-latest"): (0.30, 2.50),
```

`app/config.py` — delete lines 24-25 (`google_cloud_project`,
`google_cloud_location`) and change the `llm_model` comment at line 17 from
"the google-genai family (vertex/gemini) only" to:
```python
    # ``llm_model`` is consumed by the gemini (google-genai) provider only.
```

`app/orchestrator.py:39` — in `_active_model`'s docstring, change
"the google-genai family (vertex/gemini)" to "the gemini provider".

`.env.example` — change line 26 to `# One of: gemini | groq | github_models`,
delete lines 29-31 (the vertex stanza plus `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION`), and change line 47's comment from
"the google-genai family (vertex/gemini) ONLY" to "the gemini provider ONLY".

- [ ] **Step 8: Reword the two documentation claims that point at deleted code**

`README.md:207` currently says Vertex is "implemented per spec, covered by
mocked tests only — never [run]". Replace that bullet with:

```markdown
- **Vertex AI**: evaluated and **removed**. It requires an attached payment
  card, which this project's no-card constraint rules out, so it was never
  live-runnable here. The adapter existed under mocked tests only and was
  deleted rather than carried as a fourth code path no test could exercise for
  real. `SPEC.md` still records it as the brief's default provider.
```

`SETUP.md:43-45` currently says "code path exists per spec". Replace the
parenthetical with "the adapter was implemented, then removed — see
`CLAUDE.md`'s 'Substitutions from the brief'".

`CLAUDE.md` — add a third bullet to "Substitutions from the brief (and why)":

```markdown
- **`vertex` adapter removed** — Vertex AI requires an attached payment card,
  which the no-card constraint rules out (see SETUP.md §2), so it was never
  live-runnable and its tests could only ever be mocked. `SPEC.md` still records
  it as the brief's default; `cost.md` keeps the $300-trial-credit costing as
  the evaluation record.
```

Also change `CLAUDE.md`'s Tech stack line from "four adapters — `vertex`/`gemini`"
to "three adapters — `gemini`".

- [ ] **Step 9: Verify nothing references Vertex in code or config**

Run:
```bash
grep -rn "vertex\|VERTEX\|google_cloud\|GOOGLE_CLOUD" --include="*.py" --include="*.toml" --include="*.yaml" --include="*.example" . | grep -v "^./.venv" | grep -v "^./docs"
```
Expected: only the new `test_factory_rejects_retired_vertex_provider` and the
`google_genai.py` docstring sentence explaining the removal.

- [ ] **Step 10: Run the full suite and lint**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass, ruff clean. Test count drops by 1 (two deleted, one added).

- [ ] **Step 11: Commit**

```bash
git add app/providers/google_genai.py app/providers/factory.py app/providers/pricing.py \
        app/config.py app/orchestrator.py .env.example tests/test_providers.py \
        README.md SETUP.md CLAUDE.md
git commit -m "refactor: retire the vertex provider from code and config

Vertex AI requires an attached payment card, ruled out by the no-card
constraint, so it was never live-runnable and its tests could only be mocked.
The evaluation and rejection stay documented; get_provider now rejects it
explicitly rather than the path silently disappearing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 2: argparse for the deploy CLI, and enforce E501

**Files:**
- Modify: `scripts/deploy.py:461-478`
- Modify: `pyproject.toml:29-30`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `deploy.main(argv: list[str] | None = None) -> int` parses with argparse. `deploy.build_parser() -> argparse.ArgumentParser`. Later tasks add flags to `build_parser()`.

**Why this is first:** `main` currently tests `"--sync-env" in args`
(`deploy.py:472`), so a typo like `--sync-en` silently runs checks only and
reports success for a sync that never happened. Later tasks add more flags.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
def test_main_rejects_an_unknown_flag(monkeypatch, capsys):
    """A typo must not silently degrade to a checks-only run that reports
    success for a sync that never happened."""
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--sync-en"])
    assert exc.value.code == 2
    assert "--sync-en" in capsys.readouterr().err


def test_main_supports_help(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--help"])
    assert exc.value.code == 0
    assert "--sync-env" in capsys.readouterr().out
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "unknown_flag or supports_help" -v`
Expected: FAIL — no `SystemExit` is raised; `main` returns an int.

- [ ] **Step 3: Add argparse**

Add `import argparse` at the top of `scripts/deploy.py`, then replace `main`:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy",
        # Without this, argparse treats --sync-en as an abbreviation of
        # --sync-env and RUNS the sync. That is not hypothetical: it fired a
        # real deploy against live infrastructure during this task.
        allow_abbrev=False,
        description=(
            "Verify the hosted deployment: configuration, GitHub App installation "
            "and webhook, health endpoint, database, Render service, and keep-warm "
            "pinger. Exit 0 all passed, 1 a check failed, 2 could not run."
        ),
    )
    parser.add_argument(
        "--sync-env",
        action="store_true",
        help="push local config to the Render service, deploy, and wait for live",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    repo = settings.github_target_repo
    base = resolve_base_url()
    if not repo or not base:
        print(
            "GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
            "are required",
            file=sys.stderr,
        )
        return 2
    if args.sync_env:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(repo, base)
    print(render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "unknown_flag or supports_help" -v`
Expected: PASS.

- [ ] **Step 5: Enforce the documented line length**

`CLAUDE.md` states `ruff line-length 100`, but `pyproject.toml` sets only the
width, leaving `E501` unselected. Add the rule selection:

```toml
[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "E501"]
```

- [ ] **Step 6: Fix every line the new rule flags**

Run: `uv run ruff check .`
Expected: roughly six `E501` violations in `tests/test_deploy_script.py` plus
any introduced above. Wrap each — do not raise the limit and do not add
`# noqa`.

Re-run until clean: `uv run ruff check .`

- [ ] **Step 7: Run the full suite**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass, ruff clean.

- [ ] **Step 8: Commit**

```bash
git add scripts/deploy.py pyproject.toml tests/test_deploy_script.py
git commit -m "fix(deploy): parse args with argparse; enforce E501 at 100

A typo like --sync-en silently ran checks only and reported success for a sync
that never happened. CLAUDE.md documented line-length 100 but E501 was never
selected, so it was unenforced.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 2b: Quarantine live operator APIs in tests

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an autouse fixture in `tests/conftest.py` that blanks
  `settings.render_api_key` and `settings.uptimerobot_api_key` for every test,
  plus `live_operator_apis_allowed` — the explicit opt-in fixture a test must
  request before either key is readable.

**Why this task exists.** During Task 2 a test reached the real Render API with
the operator's real `RENDER_API_KEY` and overwrote `GITHUB_TARGET_REPO` on the
live service with a dummy value, triggering a failed deploy. The trigger was an
argparse abbreviation, which is now fixed — but the *property* that made it
damaging is still there: any test that forgets to monkeypatch
`settings.render_api_key` runs against production.

`tests/conftest.py` already carries exactly this kind of guard for the database:
it refuses a `DATABASE_URL` that does not look local, "solely so an accidentally
-exported DATABASE_URL pointing at a real Supabase database can never get
truncated by a test run". This extends that same reasoning to the operator APIs.

Tasks 10, 11, and 12 all add tests around the Render code paths, so this lands
before them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deploy_script.py`:

```python
def test_operator_api_keys_are_blank_by_default():
    """A test that forgets to monkeypatch these must hit no live API. Task 2
    overwrote a live Render env var because this guard did not exist."""
    assert settings.render_api_key == ""
    assert settings.uptimerobot_api_key == ""


def test_check_render_service_skips_rather_than_calling_out(monkeypatch):
    """With the keys quarantined, the Render check degrades to SKIPPED --
    it can never reach api.render.com from a default test run."""
    result = deploy.check_render_service()
    assert result.status == "SKIPPED"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "blank_by_default or skips_rather" -v`

Expected: FAIL — a developer `.env` supplies real values, so `settings.render_api_key` is a real key and `check_render_service` would attempt a live call.

If both tests happen to pass on a machine whose `.env` has no keys, that is not
proof: temporarily export `RENDER_API_KEY=dummy` and confirm the first test
fails. The guard must hold regardless of the local `.env`.

- [ ] **Step 3: Add the guard**

Append to `tests/conftest.py`:

```python
# Operator-tooling credentials are read only by scripts/deploy.py, and they
# point at REAL infrastructure. A test that forgets to monkeypatch them runs
# against production: during this plan's Task 2 exactly that happened, and a
# live Render service had GITHUB_TARGET_REPO overwritten with a dummy value.
# Same reasoning as the DATABASE_URL guard above -- default to inert, and make
# reaching a live API something a test has to ask for by name.
_LIVE_OPERATOR_KEYS = ("render_api_key", "uptimerobot_api_key")


@pytest.fixture(autouse=True)
def _quarantine_operator_apis(request, monkeypatch):
    if "live_operator_apis_allowed" in request.fixturenames:
        return
    for name in _LIVE_OPERATOR_KEYS:
        monkeypatch.setattr(settings, name, "")


@pytest.fixture
def live_operator_apis_allowed():
    """Opt out of the quarantine. Requesting this fixture is a deliberate
    statement that the test mocks its own transport (respx) or genuinely
    intends a live call."""
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "blank_by_default or skips_rather" -v`
Expected: PASS.

- [ ] **Step 5: Repair the tests the guard breaks**

Existing tests that monkeypatch `settings.render_api_key` to a dummy still work
— the autouse fixture runs first and the test's own `monkeypatch.setattr` wins.
Tests that relied on a *real* key would now SKIP.

Run the full suite: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q`

For each failure, fix it by setting the key explicitly in that test (the
respx-mocked Render tests already do this). Do **not** hand out
`live_operator_apis_allowed` to make a failure go away — that fixture exists for
tests that deliberately want the real thing, and no test in this suite should.

- [ ] **Step 6: Prove the guard bites**

Temporarily comment out the `monkeypatch.setattr` loop in
`_quarantine_operator_apis`, then run:
`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k blank_by_default -v`

Expected on a machine with a real `.env`: **FAIL**. Restore the loop and confirm
it passes. If it passed with the guard disabled, the test is not proving
anything — say so in your report rather than committing.

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_deploy_script.py
git commit -m "test: quarantine live operator API keys by default

A test that forgot to monkeypatch render_api_key reached the real Render API
and overwrote a live env var. Same reasoning as the existing DATABASE_URL
guard: default to inert, and make reaching a live API an explicit opt-in.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 3: The `_PROVIDERS` table and a stricter `check_config`

**Files:**
- Modify: `scripts/deploy.py:91-97,109-129`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: Task 1's three-provider world.
- Produces: `deploy._PROVIDERS: dict[str, tuple[str, str]]` mapping provider name to `(credential_env_var, model_env_var)`. Tasks 4, 9, and 10 read it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
def test_providers_table_covers_every_supported_provider():
    """One table, read by check_config, --sync-env and set_provider.py, so a
    provider cannot be known to one consumer and unknown to another."""
    assert set(deploy._PROVIDERS) == {"gemini", "groq", "github_models"}
    for credential, model_var in deploy._PROVIDERS.values():
        assert credential and model_var


def test_check_config_fails_on_an_unrecognized_provider(complete_config, monkeypatch):
    """An unrecognized value used to contribute no requirement and pass with
    nothing verified -- which after the vertex retirement includes 'vertex'."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "vertex" in result.detail
    assert "gemini" in result.detail


def test_check_config_requires_the_gemini_key_when_gemini_selected(
    complete_config, monkeypatch
):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GEMINI_API_KEY" in result.detail
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "providers_table or unrecognized_provider or gemini_key_when_gemini" -v`
Expected: FAIL — `_PROVIDERS` does not exist; an unrecognized provider currently returns PASS.

- [ ] **Step 3: Replace `_PROVIDER_KEYS` with `_PROVIDERS`**

In `scripts/deploy.py`, replace lines 91-97:

```python
# The credential and model env var each LLM_PROVIDER value requires. This is the
# single source of truth: check_config, --sync-env and scripts/set_provider.py
# all read it, so a provider cannot be known to one and unknown to another.
# provider -> (credential env var, model env var)
_PROVIDERS = {
    "gemini":        ("GEMINI_API_KEY",      "LLM_MODEL"),
    "groq":          ("GROQ_API_KEY",        "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
}
```

- [ ] **Step 4: Make `check_config` require the credential and reject unknowns**

Replace the provider block in `check_config` (`deploy.py:124-126`):

```python
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        return CheckResult(
            "config",
            "FAIL",
            f"LLM_PROVIDER={settings.llm_provider!r} is not supported "
            f"(expected one of: {accepted})",
        )
    credential = entry[0]
    if not getattr(settings, credential.lower(), ""):
        missing.append(credential)
```

Note: `check_config` validates the **environment-configured** provider even when
a DB override (Task 10) is active and working. That is intentional — a missing
environment credential is a real latent fault, because the environment value is
what governs the moment the override is cleared or the database is unreachable.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS. `test_check_config_requires_the_key_for_the_selected_provider`
and `test_check_config_ignores_provider_keys_for_other_providers` must still
pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): one _PROVIDERS table; reject unknown LLM_PROVIDER

An unrecognized provider previously contributed no requirement and passed with
nothing verified. The table now also carries each provider's model var, which
Task 4 pushes.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 4: Provider-aware `--sync-env` push set, and `render.yaml`

**Files:**
- Modify: `scripts/deploy.py:45-56,327-346,381-404`
- Modify: `render.yaml`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `deploy._PROVIDERS` (Task 3).
- Produces: `deploy._ALWAYS_SYNCED: tuple[str, ...]` (five names) and `deploy._wanted_env() -> dict[str, str]` whose keys now depend on `settings.llm_provider`.

**The bug being fixed:** `settings.llm_provider` defaults to `"gemini"`
(`app/config.py:16`), while `_wanted_env` always includes `GROQ_API_KEY` and
`GITHUB_MODELS_TOKEN`. The empty-value guard therefore fires first and tells a
default-configured user to fill in keys for two providers they never chose,
and the existing provider guard never runs.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def gemini_only_config(complete_config, monkeypatch):
    """A first-time user's .env: LLM_PROVIDER at its 'gemini' default, with the
    other providers' keys listed but empty."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "github_models_token", "")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    return None


def test_wanted_env_pushes_the_selected_providers_credential_and_model(
    gemini_only_config, monkeypatch
):
    monkeypatch.setattr(settings, "llm_model", "gemini-flash-latest")
    wanted = deploy._wanted_env()
    assert wanted["GEMINI_API_KEY"] == "gk_x"
    assert wanted["LLM_MODEL"] == "gemini-flash-latest"
    assert wanted["LLM_PROVIDER"] == "gemini"


def test_wanted_env_omits_unset_credentials_of_other_providers(gemini_only_config):
    """A Groq-only or Gemini-only .env must never be asked for another
    provider's key -- the whole point of opt-in provider config."""
    wanted = deploy._wanted_env()
    assert "GROQ_API_KEY" not in wanted
    assert "GITHUB_MODELS_TOKEN" not in wanted


def test_wanted_env_includes_other_credentials_that_are_set(
    gemini_only_config, monkeypatch
):
    """Pushed when locally filled, so a later dashboard-side switch works."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    assert deploy._wanted_env()["GROQ_API_KEY"] == "gsk_x"


def test_sync_env_does_not_demand_other_providers_keys(
    gemini_only_config, monkeypatch, capsys
):
    """The regression this task exists for: the default config could not sync
    at all, and the error named two providers the user never chose."""
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GROQ_API_KEY" not in err
    assert "GITHUB_MODELS_TOKEN" not in err
    assert code == 1          # got past the guards, failed on the missing service


def test_sync_env_refuses_when_the_selected_credential_is_empty(
    gemini_only_config, monkeypatch, capsys
):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert called == []       # refused before any HTTP
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "wanted_env or sync_env_does_not_demand or refuses_when_the_selected" -v`
Expected: FAIL — `_wanted_env` returns a fixed eight-key dict, so `GROQ_API_KEY` is present and `GEMINI_API_KEY`/`LLM_MODEL` are absent.

- [ ] **Step 3: Replace the fixed synced set**

In `scripts/deploy.py`, replace lines 45-56:

```python
# The service env vars --sync-env always pushes, regardless of provider.
# Authoritative: tests/test_deploy_script.py asserts README.md and SETUP.md
# each mention every name here and every _PROVIDERS name.
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_B64",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
)
```

- [ ] **Step 4: Make `_wanted_env` provider-aware**

Replace `_wanted_env` (`deploy.py:327-346`):

```python
def _wanted_env() -> dict[str, str]:
    """Local values for every var --sync-env will push.

    Keys depend on the selected provider: the five always-synced vars, plus
    LLM_PROVIDER, plus the selected provider's credential and model var. Any
    other provider's credential is included only when it has a local value --
    an opt-in .env lists the others empty, and must never be asked to fill them.
    """
    pem_b64 = settings.github_app_private_key_b64
    if not pem_b64:
        path = Path(settings.github_app_private_key_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_file():
            pem_b64 = base64.b64encode(path.read_bytes()).decode()
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY_B64": pem_b64,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "LLM_PROVIDER": settings.llm_provider,
    }
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is not None:
        credential, model_var = entry
        wanted[credential] = getattr(settings, credential.lower(), "")
        wanted[model_var] = getattr(settings, model_var.lower(), "")
    for other_credential, _ in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
    return wanted
```

- [ ] **Step 5: Scope the empty guard and delete the stopgap**

In `sync_env` (`deploy.py:391-404`), replace everything from `wanted = _wanted_env()`
through the old provider guard's closing `return 2` with:

```python
    if settings.llm_provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"refusing to sync LLM_PROVIDER={settings.llm_provider!r}: "
            f"not a supported provider (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    wanted = _wanted_env()
    empty = sorted(key for key, value in wanted.items() if not value)
    if empty:
        # Before any request, so a partial push cannot happen. Only the keys
        # this provider actually needs are in `wanted`, so this can never name
        # another provider's credential.
        print(
            f"refusing to push empty values; fix .env first: {', '.join(empty)}",
            file=sys.stderr,
        )
        return 2
```

The old `provider_key not in wanted` stopgap is deleted — the push set is now
provider-derived, so the condition it guarded cannot arise.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS. Any existing test asserting the old eight-name set must be
updated to the new provider-derived behavior, not deleted.

- [ ] **Step 7: Update `render.yaml`**

Declare every provider credential and model var, and stop the blueprint owning
a value for `LLM_PROVIDER` — it is currently the only var the blueprint asserts
while every other is `sync: false`, which makes two writers for one key:

```yaml
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: GITHUB_APP_ID
        sync: false
      - key: GITHUB_APP_PRIVATE_KEY_B64
        sync: false
      - key: GITHUB_WEBHOOK_SECRET
        sync: false
      - key: GITHUB_TARGET_REPO
        sync: false
      - key: LLM_PROVIDER
        sync: false
      - key: GEMINI_API_KEY
        sync: false
      - key: LLM_MODEL
        sync: false
      - key: GROQ_API_KEY
        sync: false
      - key: GROQ_MODEL
        sync: false
      - key: GITHUB_MODELS_TOKEN
        sync: false
      - key: GITHUB_MODELS_MODEL
        sync: false
```

- [ ] **Step 8: Run the full suite and lint**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass, ruff clean.

- [ ] **Step 9: Commit**

```bash
git add scripts/deploy.py render.yaml tests/test_deploy_script.py
git commit -m "fix(deploy): derive the --sync-env push set from the provider

LLM_PROVIDER defaults to gemini while the push set hardcoded GROQ_API_KEY and
GITHUB_MODELS_TOKEN, so the default config could not sync at all and the error
named two providers the user never chose. The set is now provider-derived and
includes the model var. render.yaml stops asserting a LLM_PROVIDER value, which
made the blueprint a second writer for that key.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 5: Read the PEM instead of stat-ing it

**Files:**
- Modify: `scripts/deploy.py:100-106,116-117,327-346`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: Task 4's `_wanted_env`.
- Produces: `deploy._private_key_b64() -> tuple[str, str]` returning `(b64_value, problem)`; `problem` is `""` when usable. Replaces `_private_key_available()`.

**The bug:** `_private_key_available` *stats* (`is_file()`) while `_wanted_env`
*reads* (`read_bytes()`). An existing-but-unreadable PEM therefore reports
`config: PASS` and then raises `OSError` outside `sync_env`'s `try`, producing a
traceback and exit 1 — which the CLI's own contract defines as "a check failed".

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def unreadable_pem(complete_config):
    """chmod 000 -- root reads anything, so this cannot be tested as root."""
    import os
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; cannot test an unreadable PEM")
    complete_config.chmod(0o000)
    yield complete_config
    complete_config.chmod(0o600)


def test_check_config_fails_on_an_unreadable_pem(unreadable_pem):
    """is_file() said yes while read_bytes() raised, so config passed and the
    failure surfaced later as a traceback."""
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "unreadable" in result.detail


def test_check_config_distinguishes_unreadable_from_missing(unreadable_pem):
    """Different problems need different actions: fix permissions vs create a
    key. Reporting both as 'missing' sends the operator to the wrong fix."""
    detail = deploy.check_config().detail
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" not in detail


def test_check_config_reports_a_missing_pem_as_missing(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" in result.detail


def test_check_config_uses_b64_without_touching_the_filesystem(
    complete_config, monkeypatch
):
    monkeypatch.setattr(settings, "github_app_private_key_b64", "Zm9v")
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    assert deploy.check_config().status == "PASS"


def test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback(
    unreadable_pem, monkeypatch, capsys
):
    """The parked residual: _wanted_env's OSError sat outside sync_env's try."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GITHUB_APP_PRIVATE_KEY_B64" in capsys.readouterr().err
    assert called == []
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "unreadable or distinguishes or missing_pem or without_touching" -v`
Expected: FAIL — `check_config` returns PASS on the unreadable PEM, and `sync_env` raises `PermissionError` rather than returning 2.

- [ ] **Step 3: Add the shared helper**

Replace `_private_key_available` (`deploy.py:100-106`):

```python
def _private_key_b64() -> tuple[str, str]:
    """The PEM in the base64 form Render needs, plus a problem string
    ("" when usable).

    Reads rather than stats: an existing-but-unreadable PEM must not report as
    available, because check_config would pass while _wanted_env raised on the
    same file. Returning the problem instead of raising keeps the CLI's exit
    contract intact -- a config problem is a FAIL row, not a traceback.
    """
    if settings.github_app_private_key_b64:
        return settings.github_app_private_key_b64, ""
    path = Path(settings.github_app_private_key_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return base64.b64encode(path.read_bytes()).decode(), ""
    except FileNotFoundError:
        return "", "GITHUB_APP_PRIVATE_KEY_B64 or _PATH"
    except OSError as exc:
        return "", f"unreadable PEM {path} ({type(exc).__name__})"
```

- [ ] **Step 4: Use it in `check_config`**

Replace `deploy.py:116-117`:

```python
    key_b64, key_problem = _private_key_b64()
    if key_problem and key_problem.startswith("unreadable"):
        problems.append(key_problem)
    elif key_problem:
        missing.append(key_problem)
```

Add `problems: list[str] = []` beside the existing `missing` list, and replace
the final assembly of `check_config`:

```python
    detail_lines = []
    if missing:
        detail_lines.append("missing: " + ", ".join(missing))
    detail_lines.extend(problems)
    if detail_lines:
        return CheckResult("config", "FAIL", "\n".join(detail_lines))
    return CheckResult("config", "PASS", "")
```

- [ ] **Step 5: Use it in `_wanted_env`**

Replace `_wanted_env`'s inline PEM block (the `pem_b64 = ...` lines from Task 4)
with a call to the shared helper, so the path is resolved in exactly one place:

```python
    pem_b64, _ = _private_key_b64()
```

and use `pem_b64` for the `GITHUB_APP_PRIVATE_KEY_B64` entry. An unreadable PEM
now yields `""`, which the existing empty-value guard reports at exit 2.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS, including the pre-existing
`test_check_config_accepts_base64_key_without_a_pem_file`.

- [ ] **Step 7: Mutation check — prove the unreadable test bites**

Temporarily change `except OSError` to `except FileNotFoundError` in
`_private_key_b64`, then run:
`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k unreadable -v`
Expected: the tests **FAIL** (a `PermissionError` escapes). Revert the change
and confirm they pass again. If they passed with the mutation in place, the
tests are not exercising what they claim — fix them before committing.

- [ ] **Step 8: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "fix(deploy): read the PEM rather than stat it

check_config used is_file() while _wanted_env used read_bytes(), so an
unreadable key passed the check and then raised outside sync_env's try --
a traceback and exit 1, which the contract defines as 'a check failed'.
Absent and unreadable now report differently because they need different fixes,
and the path is resolved in one place instead of two.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 6: `runtime_config` schema and store functions

**Files:**
- Modify: `app/queue/store.py:20-39`
- Modify: `tests/conftest.py` (the `db` fixture's TRUNCATE)
- Test: `tests/test_provider_override.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `store.get_provider_override() -> str | None` and `store.set_provider_override(provider: str | None, now: str) -> None`. Both **synchronous**, called via `asyncio.to_thread` from async code.

**Verified before writing this plan:** psycopg3's `execute()` accepts both
statements in one call (no parameters), `CHECK (id = 1)` raises
`psycopg.errors.CheckViolation` on a second row, and the `ON CONFLICT (id) DO
UPDATE` upsert leaves exactly one row.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_override.py`:

```python
"""The DB-backed provider override: a singleton row that lets the hosted
service swap providers without a redeploy.

Uses the shared Postgres test harness (``db`` from tests/conftest.py).
"""
from __future__ import annotations

import psycopg
import pytest

from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_override_defaults_to_none():
    assert store.get_provider_override() is None


def test_set_then_get_returns_the_override():
    store.set_provider_override("groq", T0)
    assert store.get_provider_override() == "groq"


def test_setting_twice_replaces_rather_than_inserting(db_query):
    store.set_provider_override("groq", T0)
    store.set_provider_override("gemini", T1)
    assert store.get_provider_override() == "gemini"
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_restores_none():
    store.set_provider_override("groq", T0)
    store.set_provider_override(None, T1)
    assert store.get_provider_override() is None


def test_a_second_row_is_rejected(db_exec):
    """The singleton CHECK is what makes 'which row wins' unambiguous."""
    store.set_provider_override("groq", T0)
    with pytest.raises(psycopg.errors.CheckViolation):
        db_exec(
            "INSERT INTO runtime_config (id, provider, updated_at) "
            "VALUES (2, 'gemini', %s)",
            (T1,),
        )
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_provider_override.py -v`
Expected: FAIL — `store.get_provider_override` does not exist.

- [ ] **Step 3: Add the table to the schema**

Append to `_SCHEMA` in `app/queue/store.py` (inside the same string — psycopg3
accepts multiple statements when no parameters are passed, verified):

```sql
CREATE TABLE IF NOT EXISTS runtime_config (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider   TEXT,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 4: Add the two functions**

Append to `app/queue/store.py`:

```python
def get_provider_override() -> str | None:
    """The provider override in force, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT provider FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get("provider") or None


def set_provider_override(provider: str | None, now: str) -> None:
    """Set the override, or clear it with provider=None.

    Upserts the singleton row: CHECK (id = 1) makes a second row impossible, so
    there is never ambiguity about which row wins.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config (id, provider, updated_at) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, "
            "updated_at = EXCLUDED.updated_at",
            (provider, now),
        )
```

- [ ] **Step 5: Truncate the new table between tests**

In `tests/conftest.py`'s `db` fixture, change the TRUNCATE so an override set by
one test cannot leak into the next:

```python
    with store._require_pool().connection() as conn:
        conn.execute("TRUNCATE tickets, runtime_config RESTART IDENTITY")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_provider_override.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Mutation check — prove the leak guard works**

Temporarily revert Step 5 to `TRUNCATE tickets RESTART IDENTITY`, then run the
whole file: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_provider_override.py -v`
Expected: `test_override_defaults_to_none` **FAILS** when it runs after a test
that set an override. Restore Step 5 and confirm green. This proves the fixture
change is load-bearing rather than decorative.

- [ ] **Step 8: Run the full suite**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add app/queue/store.py tests/conftest.py tests/test_provider_override.py
git commit -m "feat(store): add the runtime_config provider override

A singleton row (CHECK id = 1) holding an optional provider override, so the
hosted service can swap providers without a redeploy. Sync functions, TEXT
timestamps, provisioned by init_pool like tickets.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 7: The `active_provider()` accessor and its five call sites

**Files:**
- Create: `app/providers/active.py`
- Modify: `app/providers/factory.py:17`, `app/orchestrator.py:43,45,102`, `app/webhook.py:65`, `app/queue/dispatcher.py:138`
- Test: `tests/test_provider_override.py`

**Interfaces:**
- Consumes: nothing at runtime (the cache is filled by Task 8).
- Produces: `active.active_provider() -> str`, `active.set_override_cache(value: str | None) -> None`, `active.reset_override_cache() -> None`.

**Why one accessor:** partial adoption is a bug. If only the dispatcher consulted
the override, `factory.py` would still build the environment-configured provider
— gating on one provider while calling another.

**Why a cache, not a read-through:** a read-through would put a blocking DB call
inside `factory.py` on the event loop, against the project's
sync-store-plus-`asyncio.to_thread` convention. `active.py` deliberately imports
only `settings`, never `store`, so `webhook.py` does not pull the DB driver
through it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_override.py`:

```python
from app.config import settings
from app.providers import active


@pytest.fixture(autouse=True)
def _clean_cache():
    active.reset_override_cache()
    yield
    active.reset_override_cache()


def test_active_provider_falls_back_to_the_env_value(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    assert active.active_provider() == "gemini"


def test_active_provider_prefers_the_cached_override(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    active.set_override_cache("groq")
    assert active.active_provider() == "groq"


def test_clearing_the_cache_returns_to_the_env_value(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    active.set_override_cache("groq")
    active.set_override_cache(None)
    assert active.active_provider() == "gemini"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_provider_override.py -k active -v`
Expected: FAIL — `app.providers.active` does not exist.

- [ ] **Step 3: Create the accessor**

Create `app/providers/active.py`:

```python
"""The provider actually in force: a DB override when set, else LLM_PROVIDER.

Every read of the active provider goes through active_provider(). Partial
adoption would be a bug -- if only the dispatcher consulted the override, the
factory would still build the env-configured provider, gating on one provider
while calling another.

This module deliberately imports only ``settings``: the DB read lives in the
dispatcher (where the asyncio.to_thread convention applies) and is pushed in
via set_override_cache. That keeps webhook.py from pulling the DB driver in
through this import, and keeps active_provider() non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails or the database is unreachable -- the service
degrades to its configured provider rather than to no provider.
"""

from __future__ import annotations

from app.config import settings

_override: str | None = None


def active_provider() -> str:
    return _override or settings.llm_provider


def set_override_cache(value: str | None) -> None:
    global _override
    _override = value


def reset_override_cache() -> None:
    set_override_cache(None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_provider_override.py -k active -v`
Expected: PASS.

- [ ] **Step 5: Replace all five read sites**

`app/providers/factory.py` — add `from app.providers.active import active_provider`
and change line 17 to `provider = active_provider()`.

`app/orchestrator.py` — add the same import; in `_active_model`, replace both
`settings.llm_provider` comparisons with a single local:
```python
    provider = active_provider()
    if provider == "groq":
        return settings.groq_model
    if provider == "github_models":
        return settings.github_models_model
    return settings.llm_model
```
and at line 102 change `provider = settings.llm_provider` to
`provider = active_provider()`.

`app/webhook.py` — add the import and change line 65 to
`provider=active_provider(),`.

`app/queue/dispatcher.py` — add the import and change line 138 to
`provider = active_provider()`. Update the comment above it to say the gate
follows the *active* provider (override or env), not the ticket's recorded one.

- [ ] **Step 6: Verify no direct reads remain outside config and active.py**

Run:
```bash
grep -rn "settings.llm_provider" app/ | grep -v "app/config.py" | grep -v "app/providers/active.py"
```
Expected: no output.

- [ ] **Step 7: Run the full suite**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass. Existing tests monkeypatch `settings.llm_provider` and still
work, because with an empty cache `active_provider()` returns exactly that.

- [ ] **Step 8: Commit**

```bash
git add app/providers/active.py app/providers/factory.py app/orchestrator.py \
        app/webhook.py app/queue/dispatcher.py tests/test_provider_override.py
git commit -m "feat: route every provider read through active_provider()

One accessor, so an override cannot apply to the dispatcher's gate while the
factory still builds the env-configured provider. Cached rather than
read-through to keep a blocking DB call off the event loop.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 8: Refresh the override at claim time

**Files:**
- Modify: `app/queue/dispatcher.py:115-125`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `store.get_provider_override` (Task 6), `active.set_override_cache` (Task 7).
- Produces: no new public API. `process_next_due` refreshes the cache after claiming a ticket.

**Placement:** after the claim, so the extra SELECT costs one query per review
rather than one per idle tick (the dispatcher idles at 1s).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatcher.py`:

```python
async def test_claimed_ticket_runs_against_the_db_override(monkeypatch):
    """The behavioral guarantee: a mid-session override changes which provider
    actually runs, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    store.set_provider_override("groq", NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == ["groq"]


async def test_claim_falls_back_to_env_when_the_override_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to the configured
    provider, never abort the review."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "gemini")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_provider_override", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers.active import active_provider
        seen.append(active_provider())
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == ["gemini"]
    assert result.action == "ran"
```

`ReviewCompleted` requires `review=` (`app/orchestrator.py:49-51`); the
`type("R", (), {})()` placeholder mirrors the existing stubs at
`tests/test_dispatcher.py:58-60`. `attempt_review`'s signature is
`(repo_full_name, pr_number, comment_id=None)`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_dispatcher.py -k "db_override or override_read_fails" -v`
Expected: FAIL — the first sees `"gemini"` (no refresh happens); the second errors on the raising stub.

- [ ] **Step 3: Refresh after the claim**

In `app/queue/dispatcher.py`, add `from app.providers import active` to the
imports, then insert immediately after the `if ticket is None: return
StepResult(action="idle")` block in `process_next_due`:

```python
    # Refresh the provider override once per claimed ticket, not once per idle
    # tick. A failure here must never abort a review: active_provider() falls
    # back to settings.llm_provider whenever the cache is empty or stale.
    try:
        override = await asyncio.to_thread(store.get_provider_override)
        active.set_override_cache(override)
    except Exception:  # noqa: BLE001 - deliberate: degrade to the env provider
        logger.exception("failed to refresh the provider override; using LLM_PROVIDER")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_dispatcher.py -v`
Expected: PASS, including every pre-existing dispatcher test.

- [ ] **Step 5: Mutation check — prove the override test bites**

Temporarily change `active.set_override_cache(override)` to
`active.set_override_cache(None)`, then run:
`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_dispatcher.py -k db_override -v`
Expected: **FAIL** with `assert ['gemini'] == ['groq']`. Revert and confirm
green. If it passed with the mutation in place, the test is not proving the
override reaches the review.

- [ ] **Step 6: Run the full suite**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): refresh the provider override at claim time

One extra SELECT per review rather than per idle tick. A failed refresh
degrades to LLM_PROVIDER rather than aborting the review.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 9: `scripts/set_provider.py`

**Files:**
- Create: `scripts/set_provider.py`
- Test: `tests/test_set_provider_script.py` (create)

**Interfaces:**
- Consumes: `store.set_provider_override` (Task 6), `deploy._PROVIDERS` (Task 3).
- Produces: `set_provider.main(argv: list[str] | None = None) -> int`.

A tool, not a slash command — the demonstrator runs it directly, and a demo that
proves provider-agnosticism must not itself depend on Claude being present.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_set_provider_script.py`:

```python
"""The operator CLI that sets the DB provider override. Uses the shared
Postgres test harness -- it writes to the same table the service reads."""
from __future__ import annotations

import pytest

from app.queue import store
from scripts import set_provider


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_the_override():
    assert set_provider.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"


def test_clear_removes_the_override():
    set_provider.main(["groq"])
    assert set_provider.main(["--clear"]) == 0
    assert store.get_provider_override() is None


def test_rejects_an_unsupported_provider(capsys):
    """It can only validate the name -- it runs locally and cannot know
    whether the credential exists on the service."""
    assert set_provider.main(["vertex"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_provider_override() is None


def test_requires_a_provider_or_clear(capsys):
    assert set_provider.main([]) == 2
    assert "provider" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -v`
Expected: FAIL — `bot.scripts.set_provider` does not exist.

- [ ] **Step 3: Write the script**

Create `scripts/set_provider.py`:

```python
"""Set or clear the DB-backed provider override.

    uv run python scripts/set_provider.py groq
    uv run python scripts/set_provider.py --clear

The override takes effect on the next claimed ticket -- no restart, no redeploy.
It writes to whatever DATABASE_URL points at, so against a local .env this sets
a LOCAL override and nothing reaches production.

Validation is limited to the provider name: this runs on the operator's machine
and cannot know whether that provider's credential exists on the deployed
service. `scripts/deploy.py`'s `provider` check is the safety net for that.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.queue import store
from bot.scripts.deploy import _PROVIDERS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_provider",
        description="Set or clear the DB-backed LLM provider override.",
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(_PROVIDERS))}",
    )
    parser.add_argument(
        "--clear", action="store_true", help="remove the override; fall back to LLM_PROVIDER"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and not args.provider:
        print("a provider is required (or --clear)", file=sys.stderr)
        return 2
    if args.provider and args.provider not in _PROVIDERS:
        accepted = ", ".join(sorted(_PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    provider = None if args.clear else args.provider
    store.init_pool()
    store.set_provider_override(provider, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to LLM_PROVIDER" if provider is None
          else f"override set to {provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_set_provider_script.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full suite**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/set_provider.py tests/test_set_provider_script.py
git commit -m "feat: add scripts/set_provider.py for the provider override

A plain tool the demonstrator runs directly -- a demo proving
provider-agnosticism must not itself depend on Claude being present.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 10: The `provider` check and the sync masking guard

**Files:**
- Modify: `scripts/deploy.py` (new check + `run_checks` + `sync_env`)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `store.get_provider_override` (Task 6), `deploy._PROVIDERS` (Task 3).
- Produces: `deploy.check_provider() -> CheckResult`. `run_checks` returns **seven** results.

**Why:** a runtime override reintroduces the failure this whole plan targets
unless the CLI can see it. If the live provider can differ from the configured
one, validating the configured provider's credential proves nothing.

Ordering: after `database` (it needs the connection), so `check_config` stays
environment-only and the cheapest-first ordering holds.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def override_seam(complete_config, monkeypatch):
    """check_provider needs a DATABASE_URL to resolve at all (it SKIPs without
    one), and complete_config does not set it. Stubs the store seam so these
    stay offline."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(deploy.store, "init_pool", lambda: None)
    return monkeypatch


def test_check_provider_reports_the_env_value_when_no_override(override_seam):
    override_seam.setattr(deploy.store, "get_provider_override", lambda: None)
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "env" in result.detail


def test_check_provider_reports_a_satisfied_override(override_seam, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(deploy.store, "get_provider_override", lambda: "groq")
    result = deploy.check_provider()
    assert result.status == "PASS"
    assert "groq" in result.detail
    assert "override" in result.detail


def test_check_provider_fails_when_the_overrides_credential_is_missing(
    override_seam, monkeypatch
):
    """Green rows on a service failing every review is the exact failure this
    check exists to prevent."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(deploy.store, "get_provider_override", lambda: "groq")
    result = deploy.check_provider()
    assert result.status == "FAIL"
    assert "GROQ_API_KEY" in result.detail


def test_check_provider_skips_without_a_database_url(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_provider().status == "SKIPPED"


def test_run_checks_includes_the_provider_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider",
                        lambda: deploy.CheckResult("provider", "PASS", ""))
    for name in ("check_config", "check_installation_and_webhook",
                 "check_health_endpoint", "check_database",
                 "check_render_service", "check_uptime_pinger"):
        monkeypatch.setattr(deploy, name,
                            lambda *a, _n=name: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider" in names
    assert names.index("provider") > names.index("database")


def test_sync_env_refuses_when_an_override_would_mask_the_push(
    complete_config, monkeypatch, capsys
):
    """--sync-env would otherwise report a provider change that silently does
    nothing, because the override wins at runtime."""
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy.store, "get_provider_override", lambda: "groq")
    monkeypatch.setattr(deploy.store, "init_pool", lambda: None)
    called = []
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    err = capsys.readouterr().err
    assert "groq" in err and "set_provider" in err
    assert called == []
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "check_provider or provider_row or would_mask" -v`
Expected: FAIL — `check_provider` does not exist and `run_checks` returns six rows.

- [ ] **Step 3: Add the check**

Add `from app.queue import store` to `scripts/deploy.py`'s imports, then add:

```python
def _resolved_provider() -> tuple[str, str | None]:
    """(active provider, override or None). The override wins at runtime, so
    the CLI must resolve exactly as the dispatcher does."""
    store.init_pool()
    override = store.get_provider_override()
    return (override or settings.llm_provider), override


def check_provider() -> CheckResult:
    """Which provider will actually run, and whether its credential exists.

    Without this, a DB override makes every other check's provider assumption
    unverifiable: the service could run a provider whose key was never checked.
    """
    name = "provider"
    if not settings.database_url:
        return CheckResult(name, "SKIPPED", "set DATABASE_URL to resolve the override")
    try:
        provider, override = _resolved_provider()
    except Exception as exc:  # noqa: BLE001 - a DB problem is database's row, not ours
        return CheckResult(name, "SKIPPED", f"could not read the override ({type(exc).__name__})")
    source = f"DB override; env={settings.llm_provider}" if override else "env"
    entry = _PROVIDERS.get(provider)
    if entry is None:
        accepted = ", ".join(sorted(_PROVIDERS))
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) is not supported (expected: {accepted})"
        )
    credential = entry[0]
    if not getattr(settings, credential.lower(), ""):
        return CheckResult(name, "FAIL", f"{provider} ({source}) -- {credential} missing")
    return CheckResult(name, "PASS", f"{provider} ({source})")
```

- [ ] **Step 4: Add it to `run_checks`**

Insert after the `database` row:

```python
        _safe("provider", check_provider),
```

- [ ] **Step 5: Add the masking guard to `sync_env`**

Immediately after the unsupported-provider guard added in Task 4:

```python
    if settings.database_url:
        try:
            store.init_pool()
            override = store.get_provider_override()
        except Exception:  # noqa: BLE001 - the provider check reports DB trouble
            override = None
        if override and override != settings.llm_provider:
            print(
                f"refusing to sync: a DB provider override ({override}) is active and "
                f"wins over the LLM_PROVIDER={settings.llm_provider} being pushed. "
                f"Clear it first: uv run python scripts/set_provider.py --clear",
                file=sys.stderr,
            )
            return 2
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS. Fix any existing test that asserts a six-row report.

- [ ] **Step 7: Mutation check — prove the masking guard bites**

Temporarily change `if override and override != settings.llm_provider:` to
`if False:`, then run:
`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k would_mask -v`
Expected: **FAIL**. Revert and confirm green.

- [ ] **Step 8: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): add the provider check and the sync masking guard

A runtime override made every other check's provider assumption unverifiable.
The CLI now resolves the provider exactly as the dispatcher does, and refuses
to sync an LLM_PROVIDER an active override would silently win over.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 11: Deploy-polling fixes

**Files:**
- Modify: `scripts/deploy.py:57-65,349-378,405-437`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `deploy._DEPLOY_TIMEOUT_SECONDS = 900`; `"canceled"` removed from `_DEPLOY_FAILED_STATUSES`; `deploy._wait_for_in_flight(service_id: str) -> None`.

**Three defects.** `"canceled"` sits in `_DEPLOY_FAILED_STATUSES` next to
`build_failed`, but cancellation is what happens when a second deploy supersedes
the first — the expected outcome of a collision, not a build failure. The 300s
timeout is shorter than a cold Docker build (the measured 65.5s/56.7s in
`SETUP.md:362` were redeploys with warm layers), so the first deploy is the one
most likely to report a false failure. And `--sync-env` stacks a second build on
top of a push-triggered one.

**Wait, do not adopt:** a deploy that started before the env-var push may have
resolved its environment already, so adopting it could report "deploy live" for
a container running the old config.

- [ ] **Step 1: Write the failing tests**

```python
def test_canceled_is_not_treated_as_a_build_failure():
    """Cancellation is what a superseding deploy looks like, not a failure."""
    assert "canceled" not in deploy._DEPLOY_FAILED_STATUSES
    assert "build_failed" in deploy._DEPLOY_FAILED_STATUSES


def test_deploy_timeout_allows_for_a_cold_docker_build():
    assert deploy._DEPLOY_TIMEOUT_SECONDS >= 900


@respx.mock
def test_trigger_and_wait_reports_a_superseded_deploy_distinctly(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    respx.post("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=httpx.Response(201, json={"id": "dep-1"})
    )
    respx.get("https://api.render.com/v1/services/svc-1/deploys/dep-1").mock(
        return_value=httpx.Response(200, json={"status": "canceled"})
    )
    code = deploy._trigger_and_wait("svc-1")
    err = capsys.readouterr().err
    assert code == 1
    assert "superseded" in err
    assert "env vars" in err          # says what did happen, not just what failed


@respx.mock
def test_sync_env_waits_for_an_in_flight_deploy_before_triggering(monkeypatch):
    """Triggering on top of a running build stacks two; waiting guarantees the
    pushed values are in the live container."""
    monkeypatch.setattr(deploy, "_DEPLOY_POLL_SECONDS", 0)
    statuses = iter([
        [{"deploy": {"id": "dep-0", "status": "build_in_progress"}}],
        [{"deploy": {"id": "dep-0", "status": "live"}}],
    ])
    route = respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        side_effect=lambda request: httpx.Response(200, json=next(statuses))
    )
    deploy._wait_for_in_flight("svc-1")
    assert route.call_count == 2
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "canceled or cold_docker or superseded or in_flight" -v`
Expected: FAIL — `"canceled"` is in the set, the timeout is 300, and `_wait_for_in_flight` does not exist.

- [ ] **Step 3: Fix the constants**

```python
# A cold Docker build with a full dependency install runs well past five
# minutes; the measured ~60s redeploys had warm layers. Too short a timeout
# makes the FIRST deploy the most likely to report a false failure.
_DEPLOY_TIMEOUT_SECONDS = 900
_DEPLOY_IN_FLIGHT_STATUSES = {"created", "queued", "build_in_progress",
                              "update_in_progress", "pre_deploy_in_progress"}
# "canceled" is deliberately absent: it is what a superseding deploy looks
# like, not a build failure, and is reported separately.
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "deactivated",
}
```

- [ ] **Step 4: Report cancellation distinctly and print transitions**

In `_trigger_and_wait`, replace the poll loop body:

```python
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(_DEPLOY_POLL_SECONDS)
        poll = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        poll.raise_for_status()
        status = _unwrap(poll.json(), "deploy").get("status", "?")
        if status != last_status:
            print(f"  {status}")          # visible progress on a long build
            last_status = status
        if status == "live":
            print("deploy live")
            return 0
        if status == "canceled":
            print(
                f"deploy {deploy_id} was superseded (canceled) -- env vars WERE "
                f"pushed and a newer deploy is running; re-run to confirm it goes live",
                file=sys.stderr,
            )
            return 1
        if status in _DEPLOY_FAILED_STATUSES:
            print(f"deploy {status}", file=sys.stderr)
            return 1
```

- [ ] **Step 5: Add the in-flight wait**

```python
def _wait_for_in_flight(service_id: str) -> None:
    """Block until no deploy is building.

    Waits rather than adopts: a deploy that started before the env-var push may
    have resolved its environment already, so adopting it could report "deploy
    live" for a container still running the old config.
    """
    deadline = time.monotonic() + _DEPLOY_TIMEOUT_SECONDS
    announced = False
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{_RENDER_API}/services/{service_id}/deploys",
            params={"limit": 1},
            headers=_render_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        deploys = resp.json()
        if not deploys:
            return
        deploy_obj = _unwrap(deploys[0], "deploy")
        if deploy_obj.get("status") not in _DEPLOY_IN_FLIGHT_STATUSES:
            return
        if not announced:
            print(f"waiting for in-flight deploy {deploy_obj.get('id')} to settle")
            announced = True
        time.sleep(_DEPLOY_POLL_SECONDS)
```

Call it in `sync_env` immediately before `return _trigger_and_wait(service_id)`:

```python
        _wait_for_in_flight(service_id)
        return _trigger_and_wait(service_id)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "fix(deploy): stop reporting a superseded deploy as a failure

canceled sat beside build_failed, so a git push landing mid-poll produced an
indistinguishable failure on a converging service. The 300s timeout was shorter
than a cold Docker build. --sync-env now waits for an in-flight deploy rather
than stacking a second one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 12: `check_render_service` reports what is actually live

**Files:**
- Modify: `scripts/deploy.py:245-269`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `deploy._local_head() -> tuple[str, bool] | None` returning `(sha, is_dirty)`, or `None` outside a git repo.

**Why:** the check reads only `status` today. `_trigger_and_wait` POSTs an empty
body, so an image-backed service re-pulls the *same* image and a repo-backed one
rebuilds the *connected branch's* latest commit. Neither reads the local working
tree — so an operator whose changes never reached the build sees green.

**Assumption 4 is unverified.** Treat every Render field as optional: if the
commit or image ref is absent, degrade to today's `latest deploy live` rather
than failing. Never let a missing field produce a FAIL.

- [ ] **Step 1: Write the failing tests**

```python
def _deploys_response(deploy_obj):
    return httpx.Response(200, json=[{"deploy": deploy_obj}])


@respx.mock
def test_render_service_reports_the_live_commit(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live",
             "commit": {"id": "4e39cdaffffffff"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "4e39cda" in result.detail


@respx.mock
def test_render_service_fails_when_local_head_is_not_deployed(monkeypatch):
    """The never-push operator's trap: changes that never reached the build."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("1b10b18", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "4e39cda" in result.detail and "1b10b18" in result.detail


@respx.mock
def test_render_service_fails_on_a_dirty_working_tree(monkeypatch):
    """Uncommitted changes can be in no build, by construction."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", True))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "FAIL"
    assert "dirty" in result.detail


@respx.mock
def test_render_service_reports_an_image_without_claiming_verification(monkeypatch):
    """No local comparison is possible, and the row must not imply one."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live",
             "image": {"ref": "ghcr.io/you/pr-review:v3"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "ghcr.io/you/pr-review:v3" in result.detail
    assert "no local comparison" in result.detail


@respx.mock
def test_render_service_degrades_when_render_reports_no_artifact(monkeypatch):
    """Assumption 4 is unverified: a missing field must never produce a FAIL."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: ("4e39cda", False))
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response({"id": "dep-abc", "status": "live"})
    )
    assert deploy.check_render_service().status == "PASS"


@respx.mock
def test_render_service_skips_the_comparison_outside_a_git_repo(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
    monkeypatch.setattr(deploy, "_local_head", lambda: None)
    respx.get("https://api.render.com/v1/services/svc-1/deploys").mock(
        return_value=_deploys_response(
            {"id": "dep-abc", "status": "live", "commit": {"id": "4e39cda"}}
        )
    )
    result = deploy.check_render_service()
    assert result.status == "PASS"
    assert "no git" in result.detail
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k render_service -v`
Expected: FAIL — `_local_head` does not exist and the detail is always `latest deploy live`.

- [ ] **Step 3: Add the git helper**

```python
def _local_head() -> tuple[str, bool] | None:
    """(short HEAD sha, working tree is dirty), or None outside a git repo.

    Uses subprocess rather than a dependency: the CLI must run from a plain
    checkout with no extra installs.
    """
    import subprocess

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return (head, dirty) if head else None
```

- [ ] **Step 4: Report the live artifact**

Replace the tail of `check_render_service` (from `status = ...` onward):

```python
    deploy_obj = _unwrap(deploys[0], "deploy")
    status = deploy_obj.get("status", "?")
    if status != "live":
        return CheckResult(name, "FAIL", f"latest deploy status: {status}")

    deploy_id = deploy_obj.get("id", "?")
    commit = (deploy_obj.get("commit") or {}).get("id") or ""
    image = (deploy_obj.get("image") or {}).get("ref") or ""

    if image:
        return CheckResult(
            name, "PASS",
            f"live: {deploy_id} @ {image}\n(image-backed; no local comparison possible)",
        )
    if not commit:
        # Assumption 4 is unverified -- degrade rather than invent a failure.
        return CheckResult(name, "PASS", f"live: {deploy_id}")

    local = _local_head()
    if local is None:
        return CheckResult(name, "PASS", f"live: {deploy_id} @ {commit[:7]} (no git checkout here)")
    head, dirty = local
    # Compare on a common short prefix: Render returns a full sha, `git
    # rev-parse --short` a 7-char one, so a direct == would always differ.
    if commit[:7] != head[:7]:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]}, but local HEAD is {head}\n"
            f"push, or re-run --sync-env, to deploy what you have",
        )
    if dirty:
        return CheckResult(
            name, "FAIL",
            f"live: {deploy_id} @ {commit[:7]} (local HEAD matches, tree dirty\n"
            f"-- uncommitted changes cannot be in any build)",
        )
    return CheckResult(name, "PASS", f"live: {deploy_id} @ {commit[:7]}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat(deploy): report which artifact is actually live

The check read only status, so an operator whose changes never reached the
build saw green. Repo-backed services now compare against local HEAD and flag a
dirty tree; image-backed ones name the ref and state that no local comparison
was possible rather than implying one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

### Task 13: Documentation parity and the remaining recorded items

**Files:**
- Modify: `README.md`, `SETUP.md`, `.env.example`
- Modify: `scripts/deploy.py` (stale docstring)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `test_env_var_names_match_the_docs` reading repo-root-relative paths and covering `_ALWAYS_SYNCED` + every `_PROVIDERS` name.

- [ ] **Step 1: Update the docs-parity test**

The synced set is now provider-dependent, so the test must cover the union, and
its CWD-relative paths must be anchored to the repo root:

```python
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_env_var_names_match_the_docs():
    """Every name --sync-env can push must be documented, or an operator has no
    way to know what the service needs."""
    readme = (_REPO_ROOT / "README.md").read_text()
    setup = (_REPO_ROOT / "SETUP.md").read_text()
    names = set(deploy._ALWAYS_SYNCED) | {"LLM_PROVIDER"}
    for credential, model_var in deploy._PROVIDERS.values():
        names.add(credential)
        names.add(model_var)
    for name in sorted(names):
        assert name in readme, f"{name} missing from README.md"
        assert name in setup, f"{name} missing from SETUP.md"


def test_exit_codes_are_documented():
    """Spec section 7.2 lists three causes for exit 2; the docs must carry
    them, or the contract exists only in the code."""
    for doc in ("README.md", "SETUP.md"):
        text = (_REPO_ROOT / doc).read_text()
        assert "exit 0" in text and "exit 1" in text and "exit 2" in text
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -k "match_the_docs or exit_codes" -v`
Expected: FAIL — `GEMINI_API_KEY`, the model vars, and the exit-code table are not yet in both documents.

- [ ] **Step 3: Bring README.md and SETUP.md to parity**

Both documents must, at equal depth (`SETUP.md` is the reference for depth):

- List every name in `_ALWAYS_SYNCED`, `LLM_PROVIDER`, and every credential and
  model var in `_PROVIDERS`.
- Carry this exit-code table verbatim (spec §7.2 lists the exit-2 causes; §10
  never required the docs to carry them, so the contract lived only in code):

  ```markdown
  | Exit | Meaning |
  | --- | --- |
  | 0 | every check passed (skipped checks do not fail the run) |
  | 1 | at least one check failed |
  | 2 | the run could not proceed: `GITHUB_TARGET_REPO` or a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values, an unsupported `LLM_PROVIDER`, or an active DB override that would mask the push) |
  ```
- Document `scripts/set_provider.py`: usage, that it takes effect on the next
  claimed ticket with no redeploy, and that it writes to whatever `DATABASE_URL`
  points at — so a local `.env` sets a *local* override.
- Document the `provider` check row and what a `DB override` source means.
- Document the image-backed workflow for a service with no connected repo:
  build locally, push to a registry, point the service at the tag, then run
  `--sync-env` to push config and redeploy. State plainly that Render always
  builds on Render and never uploads a local working tree.
- Update the check list from six checks to seven.

- [ ] **Step 4: Fix the stale docstring**

`app/github_app.py`'s `discover_installation_id` docstring is stale — read the
function and correct the docstring to describe what it actually does now.

- [ ] **Step 5: Amend the old spec's unimplementable detail budget**

`docs/superpowers/specs/2026-08-05-deploy-command-design.md` §11 states a
detail-length budget that cannot hold as literally written — `check_config`'s
missing-key enumeration legitimately exceeds it, and Tasks 5 and 12 add more
newline-continuation cases. Restate it to apply **per line** rather than per
detail, and note that a detail may wrap to enumerate observed values (which is
what `CheckResult`'s docstring already says). This is a documentation amendment
to the older spec, not a code change.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest tests/test_deploy_script.py -v`
Expected: PASS.

- [ ] **Step 7: Full verification**

Run:
```bash
TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q
uv run ruff check .
uv run python -m bot.scripts.deploy --help
```
Expected: all tests pass, ruff clean, `--help` prints usage and exits 0.

- [ ] **Step 8: Smoke-test the failure path**

Run: `env -u GITHUB_TARGET_REPO uv run python -m bot.scripts.deploy; echo "exit=$?"`
Expected: a clean one-line message and `exit=2`, with no traceback.

- [ ] **Step 9: Commit**

```bash
git add README.md SETUP.md .env.example scripts/deploy.py app/github_app.py \
        tests/test_deploy_script.py \
        docs/superpowers/specs/2026-08-05-deploy-command-design.md
git commit -m "docs: bring README and SETUP to parity with the new config surface

Documents every syncable var including the model vars, the exit-code contract,
set_provider.py, the provider check, and the image-backed deploy path. The
parity test now anchors to the repo root rather than the CWD.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BhcpvrVNCucAkhLCz857Ue"
```

---

## Final Verification

Before finishing the branch:

- [ ] `TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q` — all pass
- [ ] `uv run ruff check .` — clean, with `E501` now enforced
- [ ] `grep -rn "settings.llm_provider" app/ | grep -v config.py | grep -v active.py` — no output
- [ ] `grep -rn "vertex" --include="*.py" --include="*.yaml" --include="*.example" . | grep -v .venv | grep -v docs` — only the retirement test and the explanatory docstring
- [ ] `uv run python -m bot.scripts.deploy --help` — exits 0
- [ ] Spec §6's non-absorbed items are still *not* done, and that is correct: the health URL built in two checks, unbounded monitor enumeration, and Render list pagination.

Then use `superpowers:finishing-a-development-branch`. **Do not push** —
`origin/main` is Render-connected and auto-deploys.

## Deferred to a follow-up

- Rewriting `docs/superpowers/specs/2026-08-03-demo-plan-design.md` Segment B,
  whose two `uvicorn` restarts become two `set_provider.py` calls (spec §3.6).
- Spec assumptions 2 and 3 (Render blueprint reconciliation; cancel vs queue),
  observable on the first push after `render.yaml` changes.
