# Operational Config Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent change every non-secret operational setting — provider, model, usage
caps — without ever opening a file that also holds credentials.

**Architecture:** Two layers. A **file split** (`.env` = credentials, `.env.config` =
operational settings, both read by one `Settings`) makes non-secret config directly
editable; an **extension of the existing DB-override pattern** (the one
`scripts/set_override.py` and `scripts/set_cooldown.py` already use) adds per-provider model
and usage-cap overrides so the settings a live demo tunes change with no redeploy. A third
strand finishes the gemini/vertex model-var split, without which the redeploy-free provider
flip is broken.

**Tech Stack:** Python 3.12, pydantic-settings 2.14 (multi-`env_file`), psycopg3 + Postgres
(`runtime_config` singleton row), pytest + pytest-asyncio (`asyncio_mode = "auto"`), respx
for HTTP mocking, ruff (line-length 100).

**Spec:** `docs/superpowers/specs/2026-08-15-operational-config-split-design.md`

## Global Constraints

- **No secret value may ever reach stdout, stderr, a log line, or a tool result.** Emit
  names, lengths, booleans, or equality results only. This is `CLAUDE.md`'s highest-priority
  section and it binds every line of code in this plan.
- **Do not open, read, edit, or `cat`/`grep` `.env`.** Code may read it programmatically and
  reduce to names; you may not view it. If a step seems to need it, stop and report.
- Every credential-adjacent failure message describes the failure **structurally** ("missing",
  "empty", "differs from local") — never by echoing the value.
- Ruff line-length is **100**. Run `uv run ruff check .` before each commit.
- Tests run with `uv run pytest`. DB-touching modules request the `db` fixture via an autouse
  wrapper (see `tests/conftest.py`).
- **The credential env-var names do not change in this plan.** `GCP_SERVICE_ACCOUNT_KEY_B64`
  keeps its name; the rename is spec §9's follow-up.
- New `Settings` fields default to values that keep an existing deployment behaving
  identically (spec §1's compatibility posture).

---

### Task 1: Config sourcing, the operational allowlist, and its guards

**Files:**
- Modify: `app/config.py:8` (env_file), and add `OPERATIONAL_KEYS` at module level
- Create: `.env.config.example`
- Modify: `.env.example` (remove the operational sections that moved)
- Modify: `.gitignore` (add `.env.config`)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `app.config.OPERATIONAL_KEYS: frozenset[str]` — the literal env-var names that
  belong in `.env.config`. Later tasks add `VERTEX_MODEL` to it (Task 2).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
import re
from pathlib import Path

from app.config import OPERATIONAL_KEYS, Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KEY_RE = re.compile(r"^([A-Z_0-9]+)=")


def _key_names(path: Path) -> set[str]:
    """Env-var NAMES only -- values are discarded before returning.

    This function is the reason a test may look at .env at all: it can only
    ever produce names, so no assertion built on it can print a secret. See
    CLAUDE.md's "Secret handling" section.
    """
    if not path.is_file():
        return set()
    return {
        match.group(1)
        for line in path.read_text().splitlines()
        if (match := _KEY_RE.match(line))
    }


def test_env_config_wins_over_env(tmp_path):
    """.env.config is the designated home for operational config, so it must
    win if a key somehow appears in both files."""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=from_config_file\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.llm_provider == "from_config_file"


def test_both_files_merge(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=sentinel-key\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=groq\n")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.gemini_api_key == "sentinel-key"
    assert settings.llm_provider == "groq"


def test_process_env_beats_both_files(tmp_path, monkeypatch):
    """This is what makes Render unaffected by the split: neither file exists
    in the container, and injected env vars outrank both anyway."""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=from_secrets_file\n")
    config = tmp_path / ".env.config"
    config.write_text("LLM_PROVIDER=from_config_file\n")
    monkeypatch.setenv("LLM_PROVIDER", "from_process_env")
    settings = Settings(_env_file=(str(env), str(config)))
    assert settings.llm_provider == "from_process_env"


def test_every_operational_key_is_a_real_settings_field():
    """A typo in the allowlist would classify a key that cannot be read,
    silently exempting a real key from the placement guard below."""
    fields = set(Settings.model_fields)
    unknown = {key for key in OPERATIONAL_KEYS if key.lower() not in fields}
    assert not unknown, f"OPERATIONAL_KEYS names no such Settings field: {sorted(unknown)}"


def test_no_operational_key_lives_in_the_secrets_file():
    """Operational config must not sit in .env, because an agent may never open
    .env -- which is the entire point of the split. Reports NAMES only."""
    misplaced = _key_names(_REPO_ROOT / ".env") & OPERATIONAL_KEYS
    assert not misplaced, (
        f"move these keys from .env to .env.config: {sorted(misplaced)}"
    )


def test_no_unlisted_key_lives_in_the_config_file():
    """Secret-by-default: anything not on the allowlist must stay in .env."""
    intruders = _key_names(_REPO_ROOT / ".env.config") - OPERATIONAL_KEYS
    assert not intruders, (
        f"these keys are not on OPERATIONAL_KEYS and must live in .env: {sorted(intruders)}"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k "operational or env_config or both_files or process_env"`
Expected: FAIL — `ImportError: cannot import name 'OPERATIONAL_KEYS' from 'app.config'`

- [ ] **Step 3: Add the allowlist and multi-file sourcing**

In `app/config.py`, change line 8 and add the allowlist above the `Settings` class:

```python
# Env-var names that hold plain operational config, not credentials. LISTED =
# OPERATIONAL (lives in .env.config, freely editable by anyone including an
# agent); EVERYTHING ELSE IS SECRET BY DEFAULT (lives in .env, which an agent
# must never open -- see CLAUDE.md's "Secret handling" section).
#
# Every entry is a LITERAL key name, enumerated one by one -- never a prefix or
# glob. A pattern would silently classify future keys that happen to match,
# which is exactly the secret-by-default guarantee this list exists to provide.
#
# Adding a setting here is a deliberate classification decision, not a
# formality: tests/test_config.py fails if a listed key is found in .env or an
# unlisted key is found in .env.config.
OPERATIONAL_KEYS = frozenset(
    {
        "LLM_PROVIDER",
        "LLM_MODEL",
        "GROQ_MODEL",
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_COST_CAP_USD",
        "KEY_USAGE_RESET_TIME_UTC",
        "GCP_PROJECT",
        "GCP_LOCATION",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "DISPATCHER_IDLE_SLEEP_SECONDS",
        "DEFAULT_RETRY_AFTER_SECONDS",
        "DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS",
        "DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS",
        "DISPATCHER_MAX_FAILURE_ATTEMPTS",
        "DISPATCHER_MAX_NOTICE_POST_ATTEMPTS",
        "DISPATCHER_MIN_RETRY_AFTER_SECONDS",
        "DISPATCHER_BACKOFF_JITTER_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS",
        "DISPATCHER_REREVIEW_COOLDOWN_FACTOR",
        "DISPATCHER_NOTICE_SWEEP_BATCH_SIZE",
        "RENDER_SERVICE_NAME",
        "GITHUB_TARGET_REPO",
        "PUBLIC_BASE_URL",
    }
)


class Settings(BaseSettings):
    # Two files, one Settings. .env holds credentials and identity; .env.config
    # holds operational settings (OPERATIONAL_KEYS above). The LAST file wins on
    # a key present in both, so .env.config -- the designated home -- outranks a
    # stale line left in .env. A real process env var still beats both, which is
    # why Render is unaffected: neither file exists in the container.
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.config"), extra="ignore"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — **except** `test_no_operational_key_lives_in_the_secrets_file`, which fails
if this working copy's `.env` still holds operational keys. That failure is the migration
checklist (spec §7) and is resolved by the user, not by you. Record the key names it reports
in your task report; do not edit `.env`.

- [ ] **Step 5: Create `.env.config.example`**

```bash
# Operational config -- NO SECRETS. Copy to .env.config and edit freely.
# .env.config is gitignored (machine-specific choices), but unlike .env it
# holds nothing that authenticates anything, so it is safe for anyone -- and
# any agent -- to open and edit directly.
#
# Anything not listed in app/config.py's OPERATIONAL_KEYS belongs in .env
# instead. tests/test_config.py enforces that split by key NAME.

# --- LLM provider ---
# One of: gemini | groq | vertex
LLM_PROVIDER=gemini

# Model alias for the gemini provider (google-genai, AI Studio backend),
# pinnable to a dated version for demo reproducibility.
LLM_MODEL=gemini-flash-latest

# groq's own model var -- a different vendor and model family, so a single
# shared LLM_MODEL stopped making sense once groq entered the picture.
GROQ_MODEL=llama-3.3-70b-versatile

# Optional: derived from the service-account key's own project_id when unset.
GCP_PROJECT=
# Optional: which Vertex regional endpoint to call.
GCP_LOCATION=us-central1

# --- Target repository (owner/repo) ---
GITHUB_TARGET_REPO=

# --- Public base URL ---
# Optional; auto-populated on Render as RENDER_EXTERNAL_URL. Set locally so a
# health check can reach the deployment without needing any credential.
PUBLIC_BASE_URL=

# --- Render service name; must match `name:` in render.yaml ---
RENDER_SERVICE_NAME=pr-review-engine

# --- Optional: proactive per-key daily usage cap ---
# Unset by default (cap off). When set, the dispatcher refuses to START a
# review once the ACTIVE (provider, key slot) has already used this much since
# the last reset, deferring the ticket to the next reset instead.
# KEY_USAGE_TOKEN_CAP wins outright when both are set -- the cost cap is then
# not consulted at all.
# KEY_USAGE_TOKEN_CAP=20000
# KEY_USAGE_COST_CAP_USD=0.50
# When the usage day rolls over, UTC. Any HH:MM or HH:MM:SS -- not
# whole-hours-only, so a demo can set a reset a couple of minutes out.
KEY_USAGE_RESET_TIME_UTC=04:00
```

- [ ] **Step 6: Remove the moved sections from `.env.example`**

Delete these blocks from `.env.example` (they now live in `.env.config.example`): the
`PUBLIC_BASE_URL` block, `GITHUB_TARGET_REPO`, `LLM_PROVIDER`, `GROQ_MODEL`, `GCP_PROJECT`,
`GCP_LOCATION`, the `LLM_MODEL` block, `RENDER_SERVICE_NAME`, and the whole usage-cap
section. **Keep** every credential line (`DATABASE_URL`, `GITHUB_APP_*`, `GEMINI_API_KEY*`,
`GROQ_API_KEY*`, `GCP_SERVICE_ACCOUNT_KEY_*`, `RENDER_API_KEY`, `UPTIMEROBOT_API_KEY`). Add
this note at the top of `.env.example`, under the existing first line:

```bash
# Credentials and identity ONLY. Operational settings (provider, model, usage
# caps, ...) live in .env.config -- see .env.config.example. An agent must
# never open this file; see CLAUDE.md's "Secret handling" section.
```

- [ ] **Step 7: Add `.env.config` to `.gitignore`**

Under the `# Secrets` heading's block, add a separate stanza:

```
# Operational config (no secrets, but machine-specific)
.env.config
```

- [ ] **Step 8: Run the full suite and ruff**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS, except the migration-checklist failure from Step 4 if `.env` still holds
operational keys.

- [ ] **Step 9: Commit**

```bash
git add app/config.py .env.example .env.config.example .gitignore tests/test_config.py
git commit -m "feat: split operational config out of .env into .env.config"
```

---

### Task 2: Finish the gemini/vertex model-var split (`VERTEX_MODEL`)

**Files:**
- Modify: `app/providers/registry.py:22` (vertex's model var)
- Modify: `app/config.py` (add `vertex_model`, add `VERTEX_MODEL` to `OPERATIONAL_KEYS`)
- Modify: `app/orchestrator.py:42-52` (`_active_model` becomes registry-driven)
- Modify: `scripts/deploy.py:596-607` (`_wanted_env` pushes every provider's model var)
- Modify: `render.yaml` (add `VERTEX_MODEL` and the missing GCP vars)
- Modify: `.env.config.example` (add `VERTEX_MODEL`)
- Test: `tests/test_provider_registry.py`, `tests/test_orchestrator.py`,
  `tests/test_deploy_script.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `app.config.OPERATIONAL_KEYS` (Task 1).
- Produces: `Settings.vertex_model: str` (default `"gemini-2.5-flash"`);
  `registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL")`.
  Task 4 replaces `orchestrator._active_model()`'s body but keeps its name and `-> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_registry.py`:

```python
def test_vertex_owns_its_own_model_var():
    """gemini and vertex shared LLM_MODEL, but gemini-flash-latest does not
    exist in Vertex's catalog (404) -- so the shared var made the redeploy-free
    provider flip guaranteed-broken. Each provider owns its model."""
    from app.providers import registry

    assert registry.PROVIDERS["vertex"][1] == "VERTEX_MODEL"
    assert registry.PROVIDERS["gemini"][1] == "LLM_MODEL"
    assert registry.PROVIDERS["groq"][1] == "GROQ_MODEL"
    model_vars = [model for _, model in registry.PROVIDERS.values()]
    assert len(model_vars) == len(set(model_vars)), "two providers share a model var"
```

Append to `tests/test_config.py`:

```python
def test_vertex_model_defaults_to_the_confirmed_working_vertex_model(monkeypatch):
    """gemini-flash-latest 404s on Vertex; gemini-2.5-flash is the value this
    project confirmed live (ISSUES.md). A non-empty default also keeps
    --sync-env's empty-value guard from ever tripping on it."""
    monkeypatch.delenv("VERTEX_MODEL", raising=False)
    assert Settings(_env_file=None).vertex_model == "gemini-2.5-flash"
```

Append to `tests/test_orchestrator.py`:

```python
def test_active_model_resolves_per_provider_through_the_registry(monkeypatch):
    from app import orchestrator
    from app.config import settings
    from app.providers import active

    monkeypatch.setattr(settings, "llm_model", "model-gemini")
    monkeypatch.setattr(settings, "groq_model", "model-groq")
    monkeypatch.setattr(settings, "vertex_model", "model-vertex")
    for provider, expected in (
        ("gemini", "model-gemini"),
        ("groq", "model-groq"),
        ("vertex", "model-vertex"),
    ):
        active.set_override_cache(provider)
        assert orchestrator._active_model() == expected
    active.reset_override_cache()
```

Append to `tests/test_deploy_script.py`:

```python
def test_wanted_env_pushes_every_providers_model_var(monkeypatch):
    """A redeploy-free DB provider flip can activate ANY provider, so every
    provider's model var must already be on the service -- not just the
    currently-selected one's."""
    from app.config import settings
    from scripts import deploy

    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "llm_model", "model-gemini")
    monkeypatch.setattr(settings, "groq_model", "model-groq")
    monkeypatch.setattr(settings, "vertex_model", "model-vertex")
    monkeypatch.setattr(deploy, "_private_key_b64", lambda: ("pem-b64", ""))
    wanted = deploy._wanted_env()
    assert wanted["LLM_MODEL"] == "model-gemini"
    assert wanted["GROQ_MODEL"] == "model-groq"
    assert wanted["VERTEX_MODEL"] == "model-vertex"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_provider_registry.py tests/test_config.py tests/test_orchestrator.py tests/test_deploy_script.py -v -k "vertex_owns or vertex_model_defaults or active_model_resolves or every_providers_model"`
Expected: FAIL — registry still returns `"LLM_MODEL"` for vertex; `Settings` has no
`vertex_model`.

- [ ] **Step 3: Update the registry**

In `app/providers/registry.py`, change the vertex entry and its comment:

```python
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # app/providers/vertex_credentials.py layers the local-file and
    # implicit-ADC fallbacks on top of what this entry resolves.
    #
    # VERTEX_MODEL, not LLM_MODEL: vertex and gemini are the same SDK but
    # different model catalogs -- gemini-flash-latest does not exist as a
    # Vertex publisher model (404). Sharing one var made a DB provider flip
    # between them guaranteed-broken. Completes the split whose reasoning
    # app/config.py already records for GROQ_MODEL.
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL"),
```

- [ ] **Step 4: Add the `Settings` field and allowlist entry**

In `app/config.py`, add `"VERTEX_MODEL"` to `OPERATIONAL_KEYS` (next to `"GROQ_MODEL"`), and
add the field after `groq_model`:

```python
    # Vertex's own model var. Default is the model confirmed live against this
    # project's Vertex catalog; the gemini default (gemini-flash-latest) 404s
    # there, which is exactly why these two no longer share a var.
    vertex_model: str = "gemini-2.5-flash"
```

Also update the `llm_model` comment above it: it currently says "consumed by the gemini
(google-genai) provider only" — that is now literally true, so change "gemini
(google-genai)" to "gemini" and drop any implication that vertex reads it.

- [ ] **Step 5: Make `_active_model` registry-driven**

In `app/orchestrator.py`, replace `_active_model` (lines 42-52) and add the import:

```python
from app.providers import registry
```

```python
def _active_model() -> str:
    """The model name for whichever provider is actually active.

    Each provider owns its own model var (registry.PROVIDERS) -- there is no
    special case left to hardcode here. An unknown provider (a hand-edited DB
    override, say) falls back to the gemini model rather than raising: this
    value is reported in the PR comment, and a reporting path must not be able
    to abort a review.
    """
    entry = registry.PROVIDERS.get(active_provider())
    if entry is None:
        return settings.llm_model
    return getattr(settings, entry[1].lower(), "")
```

- [ ] **Step 6: Push every provider's model var in `_wanted_env`**

In `scripts/deploy.py`, replace the provider block inside `_wanted_env()` (lines 596-604):

```python
    entry = _PROVIDERS.get(settings.llm_provider)
    if entry is not None:
        credential, _ = entry
        wanted[credential] = getattr(settings, credential.lower(), "")
    for other_credential, model_var in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
        # EVERY provider's model var, not just the selected one's: a DB
        # provider override can activate any provider with no redeploy, and a
        # provider whose model var was never pushed would read a missing or
        # stale value on the service. All model vars have non-empty defaults,
        # so this can never trip the empty-value guard in sync_env().
        wanted[model_var] = getattr(settings, model_var.lower(), "")
```

Also update the docstring's second paragraph to say "plus every provider's model var" rather
than "the selected provider's credential and model var".

- [ ] **Step 7: Declare the new vars in `render.yaml`**

Add under `envVars:` (all `sync: false`, matching every existing entry):

```yaml
      - key: VERTEX_MODEL
        sync: false
      - key: GCP_SERVICE_ACCOUNT_KEY_B64
        sync: false
      - key: GCP_PROJECT
        sync: false
      - key: GCP_LOCATION
        sync: false
```

- [ ] **Step 8: Add `VERTEX_MODEL` to `.env.config.example`**

Insert after the `GROQ_MODEL` block:

```bash
# vertex's own model var. gemini-flash-latest does NOT exist as a Vertex
# publisher model (404) -- gemini-2.5-flash is confirmed working. See SETUP.md
# section 2 for how to check your own project's Vertex catalog.
VERTEX_MODEL=gemini-2.5-flash
```

- [ ] **Step 9: Run the full suite; fix fallout**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS. Existing tests in `tests/test_providers.py` and `tests/test_orchestrator.py`
may assert that vertex reads `settings.llm_model`; update those assertions to
`settings.vertex_model` — that change of behavior is this task's deliverable, not a
regression. Do not weaken any test to make it pass; if one seems to contradict the spec,
stop and report.

- [ ] **Step 10: Commit**

```bash
git add app/providers/registry.py app/config.py app/orchestrator.py scripts/deploy.py \
  render.yaml .env.config.example tests/
git commit -m "feat: give vertex its own VERTEX_MODEL var"
```

---

### Task 3: Store schema and accessors for the new overrides

**Files:**
- Modify: `app/providers/registry.py` (add `MODEL_COLUMNS`)
- Modify: `app/queue/store.py:50-60` (DDL) and append accessors near the existing override
  functions (after `get_all_key_index_overrides`, ~line 715)
- Test: `tests/test_queue_store.py` (append)

**Interfaces:**
- Consumes: `registry.PROVIDERS` (Task 2).
- Produces:
  - `registry.MODEL_COLUMNS: dict[str, str]` — `{"gemini": "gemini_model", "groq":
    "groq_model", "vertex": "vertex_model"}`
  - `store.get_model_override(provider: str) -> str | None`
  - `store.set_model_override(provider: str, model: str | None, now: str) -> None`
  - `store.get_all_model_overrides() -> dict[str, str]`
  - `store.get_usage_cap_overrides() -> tuple[int | None, float | None, str | None]`
  - `store.set_usage_cap_override(tokens: int | None, cost: float | None, reset: str | None,
    now: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_queue_store.py`:

```python
def test_model_override_round_trips_per_provider():
    """Per-provider, not one shared column: flipping which provider is active
    must never disturb another provider's model."""
    store.set_model_override("vertex", "gemini-2.5-flash", "2026-08-15T00:00:00+00:00")
    store.set_model_override("groq", "llama-3.1-8b-instant", "2026-08-15T00:00:00+00:00")
    assert store.get_model_override("vertex") == "gemini-2.5-flash"
    assert store.get_model_override("groq") == "llama-3.1-8b-instant"
    assert store.get_model_override("gemini") is None


def test_model_override_clears_with_none():
    store.set_model_override("vertex", "gemini-2.5-flash", "2026-08-15T00:00:00+00:00")
    store.set_model_override("vertex", None, "2026-08-15T00:01:00+00:00")
    assert store.get_model_override("vertex") is None


def test_get_all_model_overrides_omits_unset_providers():
    store.set_model_override("groq", "llama-3.1-8b-instant", "2026-08-15T00:00:00+00:00")
    assert store.get_all_model_overrides() == {"groq": "llama-3.1-8b-instant"}


def test_get_all_model_overrides_is_empty_before_any_write():
    assert store.get_all_model_overrides() == {}


def test_usage_cap_overrides_round_trip():
    store.set_usage_cap_override(20000, 0.5, "04:30", "2026-08-15T00:00:00+00:00")
    assert store.get_usage_cap_overrides() == (20000, 0.5, "04:30")


def test_usage_cap_overrides_default_to_all_none():
    assert store.get_usage_cap_overrides() == (None, None, None)


def test_usage_cap_overrides_write_exactly_what_they_are_given():
    """Like set_cooldown_override, this writes all three fields every time; a
    caller wanting to change one is responsible for read-modify-write."""
    store.set_usage_cap_override(20000, 0.5, "04:30", "2026-08-15T00:00:00+00:00")
    store.set_usage_cap_override(None, 0.25, None, "2026-08-15T00:01:00+00:00")
    assert store.get_usage_cap_overrides() == (None, 0.25, None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -v -k "model_override or usage_cap_overrides"`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute
'set_model_override'`

- [ ] **Step 3: Add the column whitelist**

In `app/providers/registry.py`, after `KEY_INDEX_COLUMNS`:

```python
# provider -> the runtime_config column holding its model override. Same
# hardcoded-whitelist role as KEY_INDEX_COLUMNS above: psycopg parameterizes
# values but not column identifiers, so looking the name up here -- rather
# than building it from a caller's `provider` string -- IS the injection
# guard for every statement that touches one of these columns.
MODEL_COLUMNS = {
    "gemini": "gemini_model",
    "groq": "groq_model",
    "vertex": "vertex_model",
}
```

- [ ] **Step 4: Add the DDL**

In `app/queue/store.py`'s `_SCHEMA`, after the `vertex_key_index` line:

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS gemini_model TEXT;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS groq_model   TEXT;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_model TEXT;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS key_usage_token_cap INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS key_usage_cost_cap_usd DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS key_usage_reset_time_utc TEXT;
```

- [ ] **Step 5: Add the accessors**

Append to `app/queue/store.py`, after `get_all_key_index_overrides`:

```python
def get_model_override(provider: str) -> str | None:
    """The model override for `provider`, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread. An empty string normalizes to None so a cleared-to-blank
    row and an unset one can never mean different things.
    """
    column = registry.MODEL_COLUMNS[provider]
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get(column) or None


def set_model_override(provider: str, model: str | None, now: str) -> None:
    """Set the model override for `provider`, or clear it with model=None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. `column` comes from registry.MODEL_COLUMNS, a
    hardcoded whitelist, and is never built from `provider` directly: psycopg
    parameterizes values but not column identifiers, so this lookup IS the
    injection guard.
    """
    column = registry.MODEL_COLUMNS[provider]
    with _require_pool().connection() as conn:
        conn.execute(
            f"INSERT INTO runtime_config (id, {column}, updated_at) VALUES (1, %s, %s) "
            f"ON CONFLICT (id) DO UPDATE SET {column} = EXCLUDED.{column}, "
            "updated_at = EXCLUDED.updated_at",
            (model, now),
        )


def get_all_model_overrides() -> dict[str, str]:
    """{provider: model} for every provider with a non-empty override.

    One query reading all three columns -- the dispatcher calls this once per
    claimed ticket, not once per provider (mirrors
    get_all_key_index_overrides).
    """
    columns = registry.MODEL_COLUMNS
    select = ", ".join(columns.values())
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {select} FROM runtime_config WHERE id = 1").fetchone()
    if row is None:
        return {}
    return {provider: row[column] for provider, column in columns.items() if row[column]}


def get_usage_cap_overrides() -> tuple[int | None, float | None, str | None]:
    """(token cap, cost cap, reset time) overrides, or Nones when unset.

    The reset time comes back as the raw "HH:MM"/"HH:MM:SS" TEXT it was stored
    as; parsing (and rejecting garbage) belongs to
    app/queue/usage_cap_config.py, which is where the fail-safe policy lives.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT key_usage_token_cap, key_usage_cost_cap_usd, key_usage_reset_time_utc "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    if row is None:
        return (None, None, None)
    return (
        row["key_usage_token_cap"],
        row["key_usage_cost_cap_usd"],
        row["key_usage_reset_time_utc"],
    )


def set_usage_cap_override(
    tokens: int | None, cost: float | None, reset: str | None, now: str
) -> None:
    """Set the (token cap, cost cap, reset time) override trio, or clear a
    field with None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. Writes exactly the three values it's given; a caller
    wanting to change only one field is responsible for reading the current
    trio first (see scripts/set_usage_cap.py).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config "
            "(id, key_usage_token_cap, key_usage_cost_cap_usd, "
            "key_usage_reset_time_utc, updated_at) "
            "VALUES (1, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "key_usage_token_cap = EXCLUDED.key_usage_token_cap, "
            "key_usage_cost_cap_usd = EXCLUDED.key_usage_cost_cap_usd, "
            "key_usage_reset_time_utc = EXCLUDED.key_usage_reset_time_utc, "
            "updated_at = EXCLUDED.updated_at",
            (tokens, cost, reset, now),
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py tests/test_store_init.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/providers/registry.py app/queue/store.py tests/test_queue_store.py
git commit -m "feat: add model and usage-cap override columns to runtime_config"
```

---

### Task 4: `active_model` — the single model resolver

**Files:**
- Create: `app/providers/active_model.py`
- Modify: `app/providers/google_genai.py:60-70,86-110` (both adapters take `model`)
- Modify: `app/providers/groq.py:~55-65` (adapter takes `model`)
- Modify: `app/providers/factory.py:33-120` (`_build` signature, cache key, `get_provider`)
- Modify: `app/orchestrator.py` (`_active_model` delegates)
- Modify: `app/queue/dispatcher.py:~184` (refresh block)
- Test: Create `tests/test_active_model.py`; modify `tests/test_providers.py`,
  `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `registry.PROVIDERS` (Task 2), `store.get_all_model_overrides` (Task 3).
- Produces:
  - `active_model.active_model(provider: str) -> str`
  - `active_model.set_override_cache(overrides: dict[str, str]) -> None`
  - `active_model.reset_override_cache() -> None`
  - `factory._build(provider: str, index: int, model: str) -> LLMProvider`
  - Adapter constructors: `GeminiProvider(api_key: str, model: str)`,
    `VertexProvider(project: str, location: str, service_account_info: dict | None, model: str)`,
    `GroqProvider(api_key: str, model: str)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_active_model.py`:

```python
"""The model actually in force per provider: a DB override when set, else the
env value from registry.PROVIDERS. Mirrors tests/test_key_index_override.py."""
from __future__ import annotations

import pytest

from app.config import settings
from app.providers import active_model


@pytest.fixture(autouse=True)
def _clean_cache():
    active_model.reset_override_cache()
    yield
    active_model.reset_override_cache()


def test_falls_back_to_the_env_model_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "vertex_model", "env-vertex")
    assert active_model.active_model("vertex") == "env-vertex"


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "vertex_model", "env-vertex")
    active_model.set_override_cache({"vertex": "override-vertex"})
    assert active_model.active_model("vertex") == "override-vertex"


def test_each_provider_tracks_its_own_model(monkeypatch):
    """A provider flip must not drag another provider's model with it."""
    monkeypatch.setattr(settings, "groq_model", "env-groq")
    active_model.set_override_cache({"vertex": "override-vertex"})
    assert active_model.active_model("groq") == "env-groq"


def test_empty_override_degrades_to_env(monkeypatch):
    """Fail-safe: a blank hand-edited row must not blank out the model."""
    monkeypatch.setattr(settings, "groq_model", "env-groq")
    active_model.set_override_cache({"groq": ""})
    assert active_model.active_model("groq") == "env-groq"


def test_unknown_provider_degrades_to_the_gemini_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "env-gemini")
    assert active_model.active_model("nonesuch") == "env-gemini"
```

Append to `tests/test_providers.py`:

```python
def test_a_model_change_is_a_cache_miss(monkeypatch):
    """Adapters bake the model in at construction and factory._instances is
    process-lifetime, so a model override would silently no-op on a warm
    process unless the model is part of the cache key."""
    from app.config import settings
    from app.providers import active, active_model, factory, key_index

    factory.reset_provider_cache()
    active.set_override_cache("groq")
    key_index.reset_override_cache()
    monkeypatch.setattr(settings, "groq_api_key", "sentinel-key")
    monkeypatch.setattr(settings, "groq_model", "model-a")

    first = factory.get_provider()
    active_model.set_override_cache({"groq": "model-b"})
    second = factory.get_provider()

    assert first is not second
    assert first._model == "model-a"
    assert second._model == "model-b"

    active.reset_override_cache()
    active_model.reset_override_cache()
    factory.reset_provider_cache()


def test_reported_model_equals_executed_model(monkeypatch):
    """orchestrator._active_model() feeds the PR comment; the adapter's
    _model is what actually runs. If these diverge, the comment reports a
    model that never ran -- a silent partial failure."""
    from app import orchestrator
    from app.config import settings
    from app.providers import active, active_model, factory, key_index

    factory.reset_provider_cache()
    active.set_override_cache("groq")
    key_index.reset_override_cache()
    monkeypatch.setattr(settings, "groq_api_key", "sentinel-key")
    monkeypatch.setattr(settings, "groq_model", "model-a")
    active_model.set_override_cache({"groq": "model-b"})

    assert orchestrator._active_model() == factory.get_provider()._model

    active.reset_override_cache()
    active_model.reset_override_cache()
    factory.reset_provider_cache()
```

Append to `tests/test_dispatcher.py`:

```python
async def test_model_override_refresh_degrades_to_env_on_db_failure(monkeypatch):
    """Same fail-safe shape as the provider/cooldown/key-index refreshes: a
    failing refresh must never abort a review and never leave a stale cache."""
    from app.providers import active_model
    from app.queue import dispatcher, store

    active_model.set_override_cache({"groq": "stale-model"})

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_all_model_overrides", _boom)
    await dispatcher._refresh_model_overrides()
    assert active_model.active_model("groq") != "stale-model"
    active_model.reset_override_cache()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_active_model.py tests/test_providers.py tests/test_dispatcher.py -v -k "active_model or cache_miss or reported_model or model_override_refresh"`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.providers.active_model'`

- [ ] **Step 3: Create `app/providers/active_model.py`**

```python
"""The model name actually in force per provider: a DB override when set, else
the env-configured value named by registry.PROVIDERS.

Every read of the active model goes through active_model(). Mirrors
app/providers/key_index.py exactly, including the reason for the split: the DB
read lives in the dispatcher (where the asyncio.to_thread convention applies)
and is pushed in via set_override_cache, keeping this module import-light and
non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- every provider degrades to its env model
rather than to a crash or an empty string. An empty cached value (hand-edited
row, or a future bug) is treated as "no override" for the same reason: an empty
model name is not a model, and sending one to a provider SDK is a guaranteed
failure where the env value is a working default.
"""

from __future__ import annotations

from app.config import settings
from app.providers import registry

_overrides: dict[str, str] = {}


def active_model(provider: str) -> str:
    """The model for `provider` -- its DB override when set and non-empty,
    else the env value named by registry.PROVIDERS.

    An unknown provider falls back to the gemini model rather than raising:
    callers include the PR-comment reporting path, which must never be able to
    abort a review.
    """
    override = _overrides.get(provider)
    if override:
        return override
    entry = registry.PROVIDERS.get(provider)
    if entry is None:
        return settings.llm_model
    return getattr(settings, entry[1].lower(), "")


def set_override_cache(overrides: dict[str, str]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
```

- [ ] **Step 4: Give the adapters an explicit `model` parameter**

In `app/providers/google_genai.py`, change both constructors:

```python
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        # Passed in, never read from Settings here: app/providers/active_model.py
        # is the single resolver, so a DB model override and the model reported
        # in the PR comment can never disagree with what actually runs.
        self._model = model
```

```python
    def __init__(
        self, project: str, location: str, service_account_info: dict | None, model: str
    ) -> None:
```

…with the same `self._model = model` and comment replacing `self._model = settings.llm_model`.

In `app/providers/groq.py`, do the same: add `model: str` to `__init__` and replace
`self._model = settings.groq_model` with `self._model = model` plus the same comment.

- [ ] **Step 5: Thread the model through the factory**

In `app/providers/factory.py`:

```python
from app.providers.active_model import active_model
```

Change `_build`'s signature to `def _build(provider: str, index: int, model: str) -> LLMProvider:`
and its three construction sites:

```python
        return VertexProvider(
            project=project,
            location=settings.gcp_location,
            service_account_info=info,
            model=model,
        )
```

```python
    if provider == "gemini":
        return GeminiProvider(api_key=api_key, model=model)
    if provider == "groq":
        return GroqProvider(api_key=api_key, model=model)
```

And `get_provider`:

```python
def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    # The model is part of the cache key, not just a constructor argument:
    # adapters bake it in at construction and this cache is process-lifetime,
    # so without it a DB model override would silently no-op on a warm process
    # while the PR comment reported the new model. Same mechanism this cache
    # already relies on for a key swap -- a changed value is simply a miss on a
    # new tuple.
    model = active_model(provider)
    cache_key = (provider, index, model)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index, model)
    return _instances[cache_key]
```

Update the module docstring's cache paragraph: `(provider name, key index)` becomes
`(provider name, key index, model)`, and `_instances`' type annotation becomes
`dict[tuple[str, int, str], LLMProvider]`.

- [ ] **Step 6: Delegate `orchestrator._active_model`**

```python
def _active_model() -> str:
    """The model name for whichever provider is actually active.

    Delegates to app/providers/active_model.py, the single resolver shared with
    factory.get_provider() -- so the model reported in the PR comment is
    always the model the call actually used.
    """
    return active_model(active_provider())
```

Add `from app.providers.active_model import active_model`, and drop the now-unused
`registry` import from Task 2 if nothing else in the module uses it.

- [ ] **Step 7: Refresh the cache in the dispatcher**

In `app/queue/dispatcher.py`, add `active_model` to the `from app.providers import ...`
line, then add this helper next to the existing refresh code and call it from
`process_next_due` right after the key-index refresh block (~line 184):

```python
async def _refresh_model_overrides() -> None:
    """Refresh the per-provider model overrides once per claimed ticket, same
    cadence and fail-safe shape as the provider/cooldown/key-index refreshes: a
    failure here must never abort a review, and must never leave a stale cached
    override in place -- degrade all the way to the env-configured models."""
    try:
        overrides = await asyncio.to_thread(store.get_all_model_overrides)
        active_model.set_override_cache(overrides)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh model overrides; using env models")
        active_model.reset_override_cache()
```

```python
    await _refresh_model_overrides()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_active_model.py tests/test_providers.py tests/test_dispatcher.py tests/test_orchestrator.py -v`
Expected: PASS. Existing adapter tests that construct `GeminiProvider(api_key=...)` without
a model now need `model=...`; update them — the constructor change is this task's
deliverable.

- [ ] **Step 9: Run the full suite and ruff**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS (modulo Task 1's migration-checklist failure).

- [ ] **Step 10: Commit**

```bash
git add app/providers/active_model.py app/providers/google_genai.py app/providers/groq.py \
  app/providers/factory.py app/orchestrator.py app/queue/dispatcher.py tests/
git commit -m "feat: per-provider model override with a single resolver"
```

---

### Task 5: `usage_cap_config` — DB-overridable usage caps

**Files:**
- Create: `app/queue/usage_cap_config.py`
- Modify: `app/queue/dispatcher.py:215-236` (read through `effective_caps`), plus a refresh
  block
- Modify: `render.yaml` (declare the `KEY_USAGE_*` vars)
- Test: Create `tests/test_usage_cap_config.py`; modify `tests/test_key_usage_cap.py`

**Interfaces:**
- Consumes: `store.get_usage_cap_overrides` (Task 3).
- Produces:
  - `usage_cap_config.effective_caps() -> tuple[int | None, float | None, time]`
  - `usage_cap_config.set_override_cache(tokens: int | None, cost: float | None,
    reset: str | None) -> None` — `reset` is the raw `"HH:MM"`/`"HH:MM:SS"` text from the DB
  - `usage_cap_config.reset_override_cache() -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_usage_cap_config.py`:

```python
"""The usage caps actually in force: a DB override when set and valid, else the
env-configured values. Mirrors tests/test_cooldown_config.py."""
from __future__ import annotations

from datetime import time

import pytest

from app.config import settings
from app.queue import usage_cap_config


@pytest.fixture(autouse=True)
def _clean_cache():
    usage_cap_config.reset_override_cache()
    yield
    usage_cap_config.reset_override_cache()


def test_falls_back_to_env_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_override_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, 0.5, "06:30")
    assert usage_cap_config.effective_caps() == (20000, 0.5, time(6, 30))


def test_reset_time_accepts_seconds():
    usage_cap_config.set_override_cache(None, None, "23:59:30")
    assert usage_cap_config.effective_caps()[2] == time(23, 59, 30)


def test_an_unparseable_reset_time_discards_the_whole_trio(monkeypatch):
    """All-or-nothing, exactly like cooldown_config: a bad field must never
    pair with a stale override in another field."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(20000, 0.5, "not-a-time")
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_a_non_positive_cap_discards_the_whole_trio(monkeypatch):
    """A 0 cap makes the dispatcher's `tokens >= cap` comparison
    unconditionally true -- every ticket deferred forever, and STICKILY, since
    not_before is already a real future timestamp by then."""
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(0, 0.5, "06:30")
    assert usage_cap_config.effective_caps() == (5000, 0.25, time(4, 0))


def test_a_partial_override_fills_the_rest_from_env(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 5000)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.25)
    monkeypatch.setattr(settings, "key_usage_reset_time_utc", time(4, 0))
    usage_cap_config.set_override_cache(None, 0.75, None)
    assert usage_cap_config.effective_caps() == (5000, 0.75, time(4, 0))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_usage_cap_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue.usage_cap_config'`

- [ ] **Step 3: Create `app/queue/usage_cap_config.py`**

```python
"""The per-key daily usage caps actually in force: a DB override (token cap /
cost cap / reset time) when set and valid, else the env-configured defaults.

Every read of the effective caps goes through effective_caps(). Mirrors
app/queue/cooldown_config.py exactly, including the reason for the split: the
DB read lives in the dispatcher (where the asyncio.to_thread convention
applies) and is pushed in via set_override_cache, keeping this module
import-light and non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- the service degrades to its configured
defaults. An override that reads back invalid (a non-positive cap, or a reset
time that will not parse) is discarded as a WHOLE TRIO, never partially
applied, so a bad field can never pair with a stale override in another field.

Why non-positive caps are treated as invalid rather than clamped: the
dispatcher's gate is `tokens >= cap`, which a 0 cap makes unconditionally true
-- every ticket deferred forever. That deferral is STICKY, because a ticket's
not_before is already set to a real future timestamp by the time it happens, so
correcting the override afterwards does not release already-deferred tickets.
"""

from __future__ import annotations

from datetime import time

from app.config import settings

_tokens: int | None = None
_cost: float | None = None
_reset: str | None = None


def _env_caps() -> tuple[int | None, float | None, time]:
    return (
        settings.key_usage_token_cap,
        settings.key_usage_cost_cap_usd,
        settings.key_usage_reset_time_utc,
    )


def effective_caps() -> tuple[int | None, float | None, time]:
    """(token cap, cost cap, reset time) -- the DB override where fully valid,
    else the env defaults. A None cap means that cap is not enforced."""
    tokens = _tokens if _tokens is not None else settings.key_usage_token_cap
    cost = _cost if _cost is not None else settings.key_usage_cost_cap_usd
    if _reset is not None:
        try:
            reset = time.fromisoformat(_reset)
        except ValueError:
            return _env_caps()
    else:
        reset = settings.key_usage_reset_time_utc
    if (tokens is not None and tokens <= 0) or (cost is not None and cost <= 0):
        return _env_caps()
    return tokens, cost, reset


def set_override_cache(tokens: int | None, cost: float | None, reset: str | None) -> None:
    """`reset` is the raw "HH:MM"/"HH:MM:SS" text as stored; parsing (and
    rejecting garbage) happens in effective_caps, so a malformed value degrades
    the whole trio at read time rather than raising inside a refresh."""
    global _tokens, _cost, _reset
    _tokens, _cost, _reset = tokens, cost, reset


def reset_override_cache() -> None:
    set_override_cache(None, None, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_usage_cap_config.py -v`
Expected: PASS

- [ ] **Step 5: Read through `effective_caps` in the dispatcher**

In `app/queue/dispatcher.py`, import `usage_cap_config` alongside `cooldown_config`, add the
refresh helper, and call it right after `_refresh_model_overrides()`:

```python
async def _refresh_usage_cap_overrides() -> None:
    """Refresh the usage-cap override once per claimed ticket, same cadence and
    fail-safe shape as the refreshes above: degrade to the env defaults rather
    than keep a stale cache."""
    try:
        tokens, cost, reset = await asyncio.to_thread(store.get_usage_cap_overrides)
        usage_cap_config.set_override_cache(tokens, cost, reset)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh usage-cap overrides; using env defaults")
        usage_cap_config.reset_override_cache()
```

Then replace the cap gate's reads (lines 215-231). Resolve the trio ONCE, before the `if`,
so the gate and the bucket can never read different values:

```python
    cap_reset_at: datetime | None = None
    token_cap, cost_cap, reset_time = usage_cap_config.effective_caps()
    if token_cap is not None or cost_cap is not None:
        try:
            bucket_start = store.usage_bucket_start(now, reset_time)
            tokens, cost = await asyncio.to_thread(
                store.get_key_usage,
                provider,
                key_index.active_key_index(provider),
                bucket_start.isoformat(),
            )
            # The token cap WINS OUTRIGHT when both are set: the cost cap is
            # not consulted at all, not used as a tiebreak.
            over_cap = (
                tokens >= token_cap if token_cap is not None else cost >= cost_cap
            )
            if over_cap:
                cap_reset_at = bucket_start + timedelta(hours=24)
        except Exception:  # noqa: BLE001
            logger.exception("failed to check key usage cap; proceeding without it")
            cap_reset_at = None
```

Leave the surrounding FAILS OPEN comment intact — it still describes this block exactly.

- [ ] **Step 6: Declare the cap vars in `render.yaml`**

```yaml
      - key: KEY_USAGE_TOKEN_CAP
        sync: false
      - key: KEY_USAGE_COST_CAP_USD
        sync: false
      - key: KEY_USAGE_RESET_TIME_UTC
        sync: false
```

These are declared but never pushed by `--sync-env`, exactly like
`DISPATCHER_REREVIEW_COOLDOWN_*`: a dashboard-set baseline, with the DB override as the
live-change path.

- [ ] **Step 7: Run the full suite and ruff**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS (modulo Task 1's migration-checklist failure). `tests/test_key_usage_cap.py`
monkeypatches `settings.key_usage_*` directly; those tests still pass because
`effective_caps()` falls back to exactly those fields with an empty cache — but add
`usage_cap_config.reset_override_cache()` to that module's autouse fixture so cache leakage
between tests can never make them flaky.

- [ ] **Step 8: Commit**

```bash
git add app/queue/usage_cap_config.py app/queue/dispatcher.py render.yaml tests/
git commit -m "feat: DB-overridable per-key usage caps"
```

---

### Task 6: `set_override.py --model` / `--clear-model`

**Files:**
- Modify: `scripts/set_override.py` (parser, validation, write ordering, docstring)
- Test: `tests/test_set_override_script.py` (append)

**Interfaces:**
- Consumes: `store.set_model_override` / `get_model_override` (Task 3).
- Produces: no new importable symbols; the CLI contract
  `set_override.main(argv) -> int` is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_set_override_script.py`:

```python
def test_sets_model_and_activates_provider():
    assert set_override.main(["vertex", "--model", "gemini-2.5-flash"]) == 0
    assert store.get_model_override("vertex") == "gemini-2.5-flash"
    assert store.get_provider_override() == "vertex"


def test_sets_model_without_activating():
    assert set_override.main(
        ["vertex", "--model", "gemini-2.5-flash", "--no-activate"]
    ) == 0
    assert store.get_model_override("vertex") == "gemini-2.5-flash"
    assert store.get_provider_override() is None


def test_clear_model_leaves_other_providers_alone():
    set_override.main(["groq", "--model", "llama-3.1-8b-instant", "--no-activate"])
    set_override.main(["vertex", "--model", "gemini-2.5-flash", "--no-activate"])
    assert set_override.main(["vertex", "--clear-model", "--no-activate"]) == 0
    assert store.get_model_override("vertex") is None
    assert store.get_model_override("groq") == "llama-3.1-8b-instant"


def test_model_and_clear_model_are_mutually_exclusive(capsys):
    assert set_override.main(["vertex", "--model", "x", "--clear-model"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_no_activate_alone_still_requires_something_to_do(capsys):
    assert set_override.main(["vertex", "--no-activate"]) == 2
    assert "--no-activate requires" in capsys.readouterr().err


def test_empty_model_is_refused(capsys):
    """An empty model name is not a model; active_model() would treat it as
    "no override" and silently ignore the write."""
    assert set_override.main(["vertex", "--model", "  ", "--no-activate"]) == 2
    assert "must not be empty" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_set_override_script.py -v -k "model"`
Expected: FAIL — `error: unrecognized arguments: --model`

- [ ] **Step 3: Add the flags**

In `scripts/set_override.py`'s `build_parser()`, after `--clear-index`:

```python
    parser.add_argument(
        "--model", help="set this provider's model override to NAME"
    )
    parser.add_argument(
        "--clear-model", action="store_true", help="clear this provider's model override"
    )
```

Update the parser description to "...the DB-backed provider override and/or a provider's
API-key-slot index or model override."

- [ ] **Step 4: Add validation and the write**

In `main()`, extend the `--clear` guard's "must be used alone" check to include the new flags:

```python
    if args.clear:
        if (
            args.provider
            or args.index is not None
            or args.clear_index
            or args.no_activate
            or args.model is not None
            or args.clear_model
        ):
            print("--clear must be used alone", file=sys.stderr)
            return 2
```

Add these validations next to the existing mutually-exclusive checks:

```python
    if args.model is not None and args.clear_model:
        print("--model and --clear-model are mutually exclusive", file=sys.stderr)
        return 2
    if args.model is not None and not args.model.strip():
        print("--model must not be empty", file=sys.stderr)
        return 2
```

Replace the `--no-activate` guard so a model-only call is legal:

```python
    if (
        args.no_activate
        and args.index is None
        and not args.clear_index
        and args.model is None
        and not args.clear_model
    ):
        print(
            "--no-activate requires --index, --clear-index, --model, or --clear-model",
            file=sys.stderr,
        )
        return 2
```

Then, in the write block, insert the model write **between** the index write and the
provider activation:

```python
    # Model write sits between the index write and provider activation, for the
    # same partial-failure reasoning as the index write above: if a later write
    # never happens, the model changed but the provider is not active yet, so
    # nothing behavior-visible has changed. Activating first would leave a
    # provider live against a stale model -- exactly the gemini/vertex breakage
    # this override exists to prevent.
    if args.model is not None:
        store.set_model_override(args.provider, args.model.strip(), now)
        print(f"{args.provider} model override set to {args.model.strip()}")
    elif args.clear_model:
        store.set_model_override(args.provider, None, now)
        print(f"{args.provider} model override cleared")
```

- [ ] **Step 5: Extend the verification skip**

The existing skip is `if not (args.clear_index and args.no_activate):`. A pure
model-only change touches no credential, so it should not require a credential round trip
either. Replace with:

```python
    # Credential verification exists to catch "activating a provider whose key
    # slot has no credential". A call that only touches the model or only
    # clears the index -- and does not activate anything -- puts no credential
    # into production, so there is nothing to verify and a verification failure
    # must not be able to block it.
    _credential_untouched = args.no_activate and args.index is None
    if not _credential_untouched:
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: PASS

- [ ] **Step 7: Update the module docstring**

Add the new usages to the docstring's example block and a paragraph explaining that the model
override is per-provider — so flipping providers carries each one's correct model, which is
what makes `set_override.py vertex` safe when gemini and vertex have different catalogs.

```
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash --no-activate
    uv run python -m scripts.set_override vertex --clear-model --no-activate
```

- [ ] **Step 8: Run the full suite, ruff, and commit**

```bash
uv run pytest && uv run ruff check .
git add scripts/set_override.py tests/test_set_override_script.py
git commit -m "feat: set_override --model/--clear-model"
```

---

### Task 7: Value-free slot discovery and a single slot-naming seam

**Files:**
- Modify: `scripts/_override.py:28-56` (split the discovery function; use the new seam)
- Modify: `app/providers/registry.py` (add `slot_env_name`)
- Modify: `app/providers/credentials.py:38-43` (use the seam)
- Modify: `tests/conftest.py:131-149` (the quarantine fixture patches the renamed function)
- Modify: `scripts/deploy.py:605-606` (call the renamed value-bearing function)
- Test: `tests/test_override_helpers.py`, `tests/test_provider_registry.py`

**Interfaces:**
- Consumes: `registry.PROVIDERS` (Task 2).
- Produces:
  - `registry.slot_env_name(provider: str, index: int) -> str`
  - `_override.local_slot_indices(base: str, env_path: str = ".env") -> tuple[int, ...]`
  - `_override.local_slot_values(base: str, env_path: str = ".env") -> dict[str, str]`
    (renamed from `local_numbered_slots`; **the old name is removed**)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_registry.py`:

```python
def test_slot_env_name_is_the_single_naming_seam():
    from app.providers import registry

    assert registry.slot_env_name("groq", 0) == "GROQ_API_KEY"
    assert registry.slot_env_name("groq", 2) == "GROQ_API_KEY_2"
    assert registry.slot_env_name("vertex", 1) == "GCP_SERVICE_ACCOUNT_KEY_B64_1"
```

Append to `tests/test_override_helpers.py`:

```python
def test_local_slot_indices_returns_indices_not_values(tmp_path, local_numbered_slots_allowed):
    """Discovery answers a NAMES question, so it must not hand back values --
    a caller cannot leak what it never received."""
    from scripts import _override

    env = tmp_path / ".env"
    env.write_text(
        "GROQ_API_KEY=sentinel-zero\n"
        "GROQ_API_KEY_1=sentinel-one\n"
        "GROQ_API_KEY_2=sentinel-two\n"
    )
    indices = _override.local_slot_indices("GROQ_API_KEY", env_path=str(env))
    assert indices == (1, 2)
    assert all(isinstance(i, int) for i in indices)


def test_local_slot_indices_skips_empty_slots(tmp_path, local_numbered_slots_allowed):
    from scripts import _override

    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY_1=sentinel-one\nGROQ_API_KEY_2=\n")
    assert _override.local_slot_indices("GROQ_API_KEY", env_path=str(env)) == (1,)


def test_local_slot_indices_is_empty_for_a_missing_file(local_numbered_slots_allowed):
    from scripts import _override

    assert _override.local_slot_indices("GROQ_API_KEY", env_path="no-such-file") == ()


def test_local_slot_values_still_carries_values_for_sync_env(
    tmp_path, local_numbered_slots_allowed
):
    """The value-bearing variant survives, narrowly: --sync-env genuinely has
    to push the values."""
    from scripts import _override

    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY_1=sentinel-one\n")
    assert _override.local_slot_values("GROQ_API_KEY", env_path=str(env)) == {
        "GROQ_API_KEY_1": "sentinel-one"
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_override_helpers.py tests/test_provider_registry.py -v -k "slot_env_name or local_slot"`
Expected: FAIL — `AttributeError: module 'scripts._override' has no attribute
'local_slot_indices'`

- [ ] **Step 3: Add the naming seam**

In `app/providers/registry.py`:

```python
def slot_env_name(provider: str, index: int) -> str:
    """The env-var name for `provider`'s API-key slot `index`.

    THE single place the `{base}` / `{base}_{n}` naming scheme is written down.
    It was previously reconstructed independently in app/providers/credentials.py
    and scripts/_override.py, which meant the scheme had no seam to change --
    and a future credential store (one secret per file, say) would have been a
    sweep instead of a one-module edit.

    Index 0 is the base, unsuffixed var; indices >= 1 are the numbered slots.
    """
    base, _ = PROVIDERS[provider]
    return base if index == 0 else f"{base}_{index}"
```

- [ ] **Step 4: Split the discovery function**

In `scripts/_override.py`, replace `local_numbered_slots` with both functions:

```python
def local_slot_indices(base: str, env_path: str = ".env") -> tuple[int, ...]:
    """Ascending indices of every ``{base}_{N}`` slot with a non-empty value.

    THE DEFAULT discovery entry point, and deliberately value-free: it answers
    a names question, so values are discarded INSIDE this function and never
    reach a caller. A caller cannot print, log, or leak what it never
    received. Use local_slot_values() only where the values are genuinely the
    point (--sync-env pushing them).

    N >= 1 only -- index 0 is the base var itself, read through Settings by
    local_value() below. Returns () if env_path doesn't exist (dotenv_values
    degrades gracefully) or nothing matches.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    return tuple(
        sorted(
            int(match.group(1))
            for key, value in dotenv_values(env_path).items()
            if value and (match := pattern.match(key))
        )
    )


def local_slot_values(base: str, env_path: str = ".env") -> dict[str, str]:
    """Every ``{base}_{N}`` key with a non-empty value, WITH its value.

    Value-bearing on purpose and narrow on purpose: scripts/deploy.py's
    _wanted_env() has to push these to Render, and nothing else should call
    this. Same contract as scripts/_render.py::env_vars() -- reduce a returned
    value to a boolean or an equality result immediately; never store it beyond
    that computation, print it, or pass it to anything that might log it. When
    you only need to know WHICH slots exist, call local_slot_indices().

    Reads the file directly (python-dotenv, not os.environ or Settings) because
    Settings can't declare an unbounded family of numbered fields -- mirrors
    app/providers/credentials.py's identical reasoning for the runtime side.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    values = dotenv_values(env_path)
    return {key: value for key, value in values.items() if value and pattern.match(key)}
```

Then update `local_value()` and `verify_render_slot()` to build names through the seam:

```python
def local_value(provider: str, index: int) -> str:
    """The local value for (provider, index) -- index 0 via Settings (the
    same attribute-name convention scripts/deploy.py's check_provider and
    _verify_render_credential already use), index >= 1 via the scan above."""
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return getattr(settings, base.lower(), "")
    env_name = registry.slot_env_name(provider, index)
    return local_slot_values(base).get(env_name, "")
```

In `verify_render_slot()`, replace
`env_name = base if index == 0 else f"{base}_{index}"` with
`env_name = registry.slot_env_name(provider, index)` (and drop `base` if now unused).

- [ ] **Step 5: Use the seam in `credentials.resolve`**

In `app/providers/credentials.py`:

```python
def resolve(provider: str, index: int) -> tuple[str, str]:
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return base, getattr(settings, base.lower(), "")
    env_name = registry.slot_env_name(provider, index)
    return env_name, os.environ.get(env_name, "")
```

- [ ] **Step 6: Update the two remaining call sites**

`scripts/deploy.py:605-606` — `_override.local_numbered_slots(credential)` becomes
`_override.local_slot_values(credential)`.

`tests/conftest.py:131-149` — the autouse quarantine must patch the renamed function, or it
silently stops quarantining and `_wanted_env()` starts reading a contributor's real `.env`:

```python
    monkeypatch.setattr(
        _override, "local_slot_values", lambda base, env_path=".env": {}
    )
    monkeypatch.setattr(
        _override, "local_slot_indices", lambda base, env_path=".env": ()
    )
```

Update that fixture's docstring to name both functions.

- [ ] **Step 7: Confirm the old name is gone**

Run: `grep -rn "local_numbered_slots" app/ scripts/ tests/`
Expected: no output. A stale reference here is a silently-disabled test quarantine, not a
harmless leftover.

- [ ] **Step 8: Run the full suite, ruff, and commit**

```bash
uv run pytest && uv run ruff check .
git add app/providers/registry.py app/providers/credentials.py scripts/_override.py \
  scripts/deploy.py tests/
git commit -m "refactor: value-free slot discovery and one slot-naming seam"
```

---

### Task 8: `set_override.py --list`

**Files:**
- Modify: `scripts/set_override.py` (the `--list` branch)
- Test: `tests/test_set_override_script.py` (append)

**Interfaces:**
- Consumes: `_override.local_slot_indices` (Task 7), `registry.slot_env_name` (Task 7),
  `_render.env_vars` / `find_service_id`, `store.get_all_key_index_overrides`,
  `store.get_all_model_overrides` (Task 3), `active_model.active_model` (Task 4).
- Produces: no new importable symbols.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_set_override_script.py`:

```python
def test_list_reports_slots_and_active_state(capsys, monkeypatch):
    from app.config import settings
    from scripts import _override

    monkeypatch.setattr(settings, "groq_api_key", "sentinel-groq")
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    monkeypatch.setattr(
        _override, "local_slot_indices",
        lambda base, env_path=".env": (1,) if base == "GROQ_API_KEY" else (),
    )
    assert set_override.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "groq" in out
    assert "GROQ_API_KEY_1" in out
    assert "llama-3.3-70b-versatile" in out


def test_list_never_prints_a_credential_value(capsys, monkeypatch):
    """The whole point of --list: an agent can answer "is --index 2 valid?"
    without opening .env, and nothing it prints can be a secret."""
    from app.config import settings
    from scripts import _override

    monkeypatch.setattr(settings, "groq_api_key", "SENTINEL-SECRET-VALUE")
    monkeypatch.setattr(
        _override, "local_slot_indices", lambda base, env_path=".env": (1,)
    )
    set_override.main(["--list"])
    captured = capsys.readouterr()
    assert "SENTINEL-SECRET-VALUE" not in captured.out
    assert "SENTINEL-SECRET-VALUE" not in captured.err


def test_list_must_be_used_alone(capsys):
    assert set_override.main(["--list", "groq"]) == 2
    assert "--list must be used alone" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_set_override_script.py -v -k "list"`
Expected: FAIL — `error: unrecognized arguments: --list`

- [ ] **Step 3: Add the flag and the branch**

In `build_parser()`:

```python
    parser.add_argument(
        "--list",
        action="store_true",
        help="show each provider's slots, active index, and active model (must be used alone)",
    )
```

In `main()`, before the `--clear` branch:

```python
    if args.list:
        if (
            args.provider
            or args.index is not None
            or args.clear_index
            or args.no_activate
            or args.clear
            or args.model is not None
            or args.clear_model
        ):
            print("--list must be used alone", file=sys.stderr)
            return 2
        return _print_inventory()
```

Add the helper above `main()`:

```python
def _print_inventory() -> int:
    """Per provider: which key slots exist locally, which exist on Render, the
    active index, and the active model.

    NAMES AND BOOLEANS ONLY. Every value this touches -- a local slot value, a
    Render env-var value -- is reduced to presence before anything is printed,
    per scripts/_render.py::env_vars()'s contract. This is what lets an agent
    answer "is --index 2 valid?" without ever opening .env, which it may not do.
    """
    render_keys: set[str] = set()
    render_note = ""
    if settings.render_api_key:
        try:
            service_id = _render.find_service_id()
            if service_id is None:
                render_note = f"(no Render service named {settings.render_service_name})"
            else:
                render_keys = {
                    key for key, value in _render.env_vars(service_id).items() if value
                }
        # deliberate: inability to reach Render degrades to a note, never a failure
        except Exception as exc:  # noqa: BLE001
            render_note = f"(could not reach Render: {type(exc).__name__})"
    else:
        render_note = "(no RENDER_API_KEY; local slots only)"

    index_overrides: dict[str, int] = {}
    model_overrides: dict[str, str] = {}
    if settings.database_url:
        try:
            store.init_pool()
            index_overrides = store.get_all_key_index_overrides()
            model_overrides = store.get_all_model_overrides()
            active_model.set_override_cache(model_overrides)
        # deliberate: the DB being unreachable degrades to "env values", never a failure
        except Exception as exc:  # noqa: BLE001
            render_note = f"{render_note} (DB unreachable: {type(exc).__name__})".strip()

    if render_note:
        print(render_note)
    for provider in sorted(registry.PROVIDERS):
        base, _ = registry.PROVIDERS[provider]
        local = ((0,) if getattr(settings, base.lower(), "") else ()) + (
            _override.local_slot_indices(base)
        )
        # Derived from the names Render actually reports, not a scanned index
        # range: a range would silently stop reporting slots past its bound,
        # and "no slot 12" reads identically to "slot 12 not checked".
        slot_pattern = re.compile(rf"^{re.escape(base)}(?:_(\d+))?$")
        hosted = sorted(
            int(match.group(1) or 0)
            for key in render_keys
            if (match := slot_pattern.match(key))
        )
        index_source = "override" if provider in index_overrides else "default"
        model_source = "override" if provider in model_overrides else "env"
        print(
            f"{provider}: local slots {list(local) or '-'}, "
            f"render slots {hosted or '-'}, "
            f"active index {index_overrides.get(provider, 0)} ({index_source}), "
            f"model {active_model.active_model(provider)} ({model_source})"
        )
        for index in sorted(set(local) | set(hosted)):
            name = registry.slot_env_name(provider, index)
            print(
                f"    {name}: local {'yes' if index in local else 'no'}, "
                f"render {'yes' if index in hosted else 'no'}"
            )
    return 0
```

Add the imports this needs at the top of `scripts/set_override.py`:

```python
import re

from app.config import settings
from app.providers import active_model, registry
from scripts import _render
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: PASS

- [ ] **Step 5: Document it in the module docstring**

Add to the usage block:

```
    uv run python -m scripts.set_override --list
```

…plus a sentence noting `--list` prints names and booleans only, never a credential value,
so it is safe to run and to paste anywhere.

- [ ] **Step 6: Run the full suite, ruff, and commit**

```bash
uv run pytest && uv run ruff check .
git add scripts/set_override.py tests/test_set_override_script.py
git commit -m "feat: set_override --list shows slot inventory without values"
```

---

### Task 9: `scripts/set_usage_cap.py`

**Files:**
- Create: `scripts/set_usage_cap.py`
- Test: Create `tests/test_set_usage_cap_script.py`

**Interfaces:**
- Consumes: `store.get_usage_cap_overrides` / `set_usage_cap_override` (Task 3),
  `usage_cap_config` (Task 5), `_render` (existing).
- Produces: `set_usage_cap.main(argv: list[str] | None = None) -> int`,
  `set_usage_cap.build_parser() -> argparse.ArgumentParser`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_set_usage_cap_script.py`:

```python
"""The operator CLI that sets the DB usage-cap override. Uses the shared
Postgres test harness -- it writes to the same table the service reads. Mirrors
tests/test_set_cooldown_script.py: no secret is at stake, only numbers, so the
Render check degrades to a notice and there is no --force flag."""
from __future__ import annotations

import pytest

from app.queue import store
from scripts import set_usage_cap


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_tokens_only_leaves_others_untouched():
    assert set_usage_cap.main(["--tokens", "20000"]) == 0
    assert store.get_usage_cap_overrides() == (20000, None, None)


def test_sets_all_three():
    assert set_usage_cap.main(
        ["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"]
    ) == 0
    assert store.get_usage_cap_overrides() == (20000, 0.5, "06:30")


def test_a_second_call_with_one_flag_preserves_the_others():
    set_usage_cap.main(["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"])
    assert set_usage_cap.main(["--cost", "0.25"]) == 0
    assert store.get_usage_cap_overrides() == (20000, 0.25, "06:30")


def test_clear_removes_all_three():
    set_usage_cap.main(["--tokens", "20000", "--cost", "0.5", "--reset", "06:30"])
    assert set_usage_cap.main(["--clear"]) == 0
    assert store.get_usage_cap_overrides() == (None, None, None)


def test_clear_cannot_be_combined(capsys):
    assert set_usage_cap.main(["--clear", "--tokens", "20000"]) == 2
    assert "--clear cannot be combined" in capsys.readouterr().err


def test_requires_at_least_one_flag(capsys):
    assert set_usage_cap.main([]) == 2
    assert "at least one of" in capsys.readouterr().err


def test_refuses_a_non_positive_token_cap(capsys):
    assert set_usage_cap.main(["--tokens", "0"]) == 2
    assert "must be > 0" in capsys.readouterr().err


def test_refuses_an_unparseable_reset_time(capsys):
    assert set_usage_cap.main(["--reset", "half-past-four"]) == 2
    assert "HH:MM" in capsys.readouterr().err


def test_refuses_a_write_that_would_read_back_inert(capsys, monkeypatch):
    """A cap that effective_caps() discards would be written successfully and
    then ignored on every read -- the script would report success while
    changing nothing. Refuse instead."""
    from app.config import settings

    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    assert set_usage_cap.main(["--tokens", "-5"]) == 2
    assert store.get_usage_cap_overrides() == (None, None, None)


def test_accepts_seconds_in_the_reset_time():
    assert set_usage_cap.main(["--reset", "23:59:30"]) == 0
    assert store.get_usage_cap_overrides()[2] == "23:59:30"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_set_usage_cap_script.py -v`
Expected: FAIL — `ImportError: cannot import name 'set_usage_cap' from 'scripts'`

- [ ] **Step 3: Create the script**

```python
"""Set or clear the DB-backed per-key usage-cap override (tokens/cost/reset).

    uv run python -m scripts.set_usage_cap --tokens 20000
    uv run python -m scripts.set_usage_cap --cost 0.50 --reset 06:30
    uv run python -m scripts.set_usage_cap --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local .env
this sets a LOCAL override and nothing reaches production.

Unlike scripts/set_override.py, there is no credential at stake here -- only
numbers and a wall-clock time. Before writing, this checks (when RENDER_API_KEY
is set) whether the local DATABASE_URL matches the live Render service's, purely
as an informational signal that the write will actually reach production; that
check never refuses the write, so there is no --force flag.

It DOES refuse the write (exit 2) if the merged trio -- resolved against env
defaults for any unset field, exactly the way usage_cap_config.effective_caps()
resolves it at read time -- would be discarded as invalid. Writing such a value
would succeed and then be silently ignored on every read, leaving the override
inert while this script reported success. That matters more here than for the
cooldown override: a cap the dispatcher does honour but which is wrong-way-round
(non-positive) defers EVERY ticket, and the deferral is STICKY -- a ticket's
not_before is already a real future timestamp by then, so correcting the
override afterwards does not release already-deferred tickets.

A plain tool, not a slash command -- matches scripts/set_cooldown.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timezone

from app.config import settings
from app.queue import store, usage_cap_config
from scripts import _render


def _verify_render_reachability() -> str:
    """A human-readable status line about whether this write reaches the
    Render-hosted production database. Never blocks the write -- see the module
    docstring. Never returns, prints, or logs a fetched Render value, only
    presence/absence and in-memory equality results (matches
    scripts/set_cooldown.py's identical guard)."""
    if not settings.render_api_key:
        return (
            "could not verify against Render (no RENDER_API_KEY); "
            "writing without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); writing without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return (
            f"could not verify against Render ({type(exc).__name__}); "
            "writing without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return (
            "could not confirm this DATABASE_URL is the one the Render "
            "service reads -- writing anyway"
        )
    return "DATABASE_URL verified against the live Render service"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_usage_cap",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_override.py and
        # scripts/set_cooldown.py carry the same guard after an identical
        # abbreviation match fired a live production sync.
        allow_abbrev=False,
        description="Set or clear the DB-backed per-key usage-cap override.",
    )
    parser.add_argument("--tokens", type=int, help="daily token cap for the active key slot")
    parser.add_argument("--cost", type=float, help="daily USD cost cap for the active key slot")
    parser.add_argument("--reset", help="usage-day rollover, UTC HH:MM or HH:MM:SS")
    parser.add_argument(
        "--clear", action="store_true", help="remove all three overrides; fall back to env vars"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and args.tokens is None and args.cost is None and args.reset is None:
        print(
            "at least one of --tokens/--cost/--reset is required (or --clear)",
            file=sys.stderr,
        )
        return 2
    if args.clear and (
        args.tokens is not None or args.cost is not None or args.reset is not None
    ):
        print(
            "--clear cannot be combined with --tokens/--cost/--reset -- clear "
            "first, then re-set individual fields in a separate call",
            file=sys.stderr,
        )
        return 2
    if args.tokens is not None and args.tokens <= 0:
        print(f"--tokens must be > 0 (got {args.tokens})", file=sys.stderr)
        return 2
    if args.cost is not None and args.cost <= 0:
        print(f"--cost must be > 0 (got {args.cost})", file=sys.stderr)
        return 2
    if args.reset is not None:
        try:
            time.fromisoformat(args.reset)
        except ValueError:
            print(
                f"--reset must be UTC HH:MM or HH:MM:SS (got {args.reset!r})",
                file=sys.stderr,
            )
            return 2

    now = datetime.now(timezone.utc).isoformat()
    store.init_pool()

    if args.clear:
        store.set_usage_cap_override(tokens=None, cost=None, reset=None, now=now)
        print("cleared; falling back to the env-configured usage caps")
        return 0

    current_tokens, current_cost, current_reset = store.get_usage_cap_overrides()
    new_tokens = args.tokens if args.tokens is not None else current_tokens
    new_cost = args.cost if args.cost is not None else current_cost
    new_reset = args.reset if args.reset is not None else current_reset

    # Resolve the merged trio exactly like usage_cap_config.effective_caps()
    # does at read time, and refuse outright if the result would be discarded --
    # otherwise the write succeeds and is silently ignored on every read, and
    # this script reports success for a no-op.
    usage_cap_config.set_override_cache(new_tokens, new_cost, new_reset)
    resolved = usage_cap_config.effective_caps()
    usage_cap_config.reset_override_cache()
    env_defaults = (
        settings.key_usage_token_cap,
        settings.key_usage_cost_cap_usd,
        settings.key_usage_reset_time_utc,
    )
    intended_change = (new_tokens, new_cost, new_reset) != (None, None, None)
    if intended_change and resolved == env_defaults:
        print(
            "refusing to write: the resulting override would resolve to "
            f"tokens={new_tokens} cost={new_cost} reset={new_reset!r}, which "
            "effective_caps() would discard entirely (needs positive caps and a "
            "parseable HH:MM[:SS] reset) -- the write would be a no-op",
            file=sys.stderr,
        )
        return 2

    print(_verify_render_reachability())
    store.set_usage_cap_override(
        tokens=new_tokens, cost=new_cost, reset=new_reset, now=now
    )
    print(
        f"usage cap override: tokens {current_tokens} -> {new_tokens}, "
        f"cost {current_cost} -> {new_cost}, reset {current_reset} -> {new_reset}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_set_usage_cap_script.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite, ruff, and commit**

```bash
uv run pytest && uv run ruff check .
git add scripts/set_usage_cap.py tests/test_set_usage_cap_script.py
git commit -m "feat: set_usage_cap CLI for the DB usage-cap override"
```

---

### Task 10: `sync_env` model guard, docs, and the migration note

**Files:**
- Modify: `scripts/deploy.py` (add `_resolved_model_override`, extend `sync_env`)
- Modify: `README.md`, `SETUP.md`, `CLAUDE.md`
- Test: `tests/test_deploy_script.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-9.
- Produces: `deploy._resolved_model_override(provider: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`:

```python
def test_sync_env_refuses_when_a_model_override_disagrees(monkeypatch, capsys):
    """Symmetric with the provider-override refusal: an active model override
    wins at runtime, so pushing a different model would report success while
    the service kept running the overridden one."""
    from app.config import settings
    from scripts import deploy

    monkeypatch.setattr(settings, "render_api_key", "sentinel-render-key")
    monkeypatch.setattr(settings, "database_url", "postgresql://localhost/x")
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_model", "gemini-2.5-flash")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: ("vertex", None))
    monkeypatch.setattr(deploy, "_resolved_model_override", lambda provider: "some-other-model")

    assert deploy.sync_env() == 2
    err = capsys.readouterr().err
    assert "model override" in err
    assert "--clear-model" in err


def test_sync_env_allows_an_agreeing_model_override(monkeypatch, capsys):
    from app.config import settings
    from scripts import deploy

    monkeypatch.setattr(settings, "render_api_key", "sentinel-render-key")
    monkeypatch.setattr(settings, "database_url", "postgresql://localhost/x")
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_model", "gemini-2.5-flash")
    monkeypatch.setattr(deploy, "_resolved_provider", lambda: ("vertex", None))
    monkeypatch.setattr(deploy, "_resolved_model_override", lambda provider: "gemini-2.5-flash")
    monkeypatch.setattr(deploy, "_wanted_env", lambda: {"LLM_PROVIDER": "vertex"})
    # Mocked so this test makes no live Render call; returning None makes the
    # script stop at "no such service" -- which proves it got PAST the model
    # guard, the thing under test, without needing a full push to succeed.
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)

    assert deploy.sync_env() == 1
    assert "no Render service named" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -v -k "model_override"`
Expected: FAIL — `AttributeError: module 'scripts.deploy' has no attribute
'_resolved_model_override'`

- [ ] **Step 3: Add the resolver and the guard**

In `scripts/deploy.py`, next to `_resolved_provider`:

```python
def _resolved_model_override(provider: str) -> str | None:
    """`provider`'s DB model override, or None when unset.

    Reads via a raw short-timeout connection rather than store.init_pool(), for
    the same reason _resolved_provider does. The column name comes from
    registry.MODEL_COLUMNS -- a hardcoded whitelist -- and is never built from
    `provider` directly.
    """
    column = registry.MODEL_COLUMNS[provider]
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    return (row[0] if row else None) or None
```

In `sync_env()`, extend the existing DB-override guard block (lines 718-731) with the model
check, immediately after the provider check:

```python
        # Symmetric with the provider-override refusal above. An active model
        # override wins at runtime, so pushing a different model var would
        # report success while the service kept running the overridden model --
        # "what you pushed is what runs" has to stay true, not nearly true.
        try:
            model_override = _resolved_model_override(settings.llm_provider)
        # deliberate: the provider check reports DB trouble
        except Exception:  # noqa: BLE001
            model_override = None
        model_var = _PROVIDERS[settings.llm_provider][1]
        local_model = getattr(settings, model_var.lower(), "")
        if model_override and model_override != local_model:
            print(
                f"refusing to sync: a DB model override ({model_override}) is active for "
                f"{settings.llm_provider} and wins over the {model_var}={local_model} "
                "being pushed. Clear it first: uv run python -m scripts.set_override "
                f"{settings.llm_provider} --clear-model --no-activate",
                file=sys.stderr,
            )
            return 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: PASS

- [ ] **Step 5: Update `README.md`**

Add a section documenting the two new CLI surfaces and the file split:

```markdown
### Changing operational config

Operational settings — provider, model, usage caps, cooldown — live in
`.env.config` (see `.env.config.example`), never in `.env`. `.env` holds
credentials only, and nothing but a credential belongs there.

Two ways to change a setting:

- **Edit `.env.config`, then `uv run python -m scripts.deploy --sync-env`** —
  changes the baseline the service boots with. Costs a redeploy.
- **A DB override** — takes effect on the next claimed ticket, no restart, no
  redeploy:

  ```bash
  uv run python -m scripts.set_override vertex --model gemini-2.5-flash
  uv run python -m scripts.set_override --list
  uv run python -m scripts.set_usage_cap --tokens 20000 --reset 06:30
  uv run python -m scripts.set_cooldown --base 30 --factor 1.5
  ```

`--list` prints slot inventory, the active index, and the active model as names
and booleans only — never a credential value — so it is safe to run and paste
anywhere.
```

Also update the existing "Swapping API keys without a redeploy" section to mention
`--model` alongside `--index`.

- [ ] **Step 6: Update `SETUP.md`**

Add a subsection under the environment/config setup describing the two files, the migration
(create `.env.config` from the example, then remove those keys from `.env` — **in that
order**, because `.env.config` wins by precedence so there is never a window where a setting
is unset), and the fact that `tests/test_config.py` reports remaining misplaced keys by name.
Note that `KEY_USAGE_*` are declared in `render.yaml` but never pushed by `--sync-env` — a
dashboard-set baseline with `set_usage_cap.py` as the live-change path, exactly like the
cooldown vars.

- [ ] **Step 7: Remove the now-false parenthetical from `CLAUDE.md`**

In the `.env`-opening bullet of the "Secret handling" section, delete this sentence — the gap
it describes no longer exists:

> (Note: this is a known tooling gap, not a fully solved problem — this project's current
> CLI/scripts have no way to change some local config without an agent touching `.env`
> directly; that gap is tracked as a separate follow-up, not something this rule works
> around.)

Replace it with:

> (Operational config no longer requires this: non-secret settings live in `.env.config`,
> which is safe to open and edit, and provider/model/cap/cooldown changes also have
> redeploy-free CLI paths — see README's "Changing operational config".)

Then re-read the whole bullet for internal consistency, per `CLAUDE.md`'s own rule about
correcting one clause of a multi-sentence passage.

- [ ] **Step 8: Run the full suite and ruff**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS, except `test_no_operational_key_lives_in_the_secrets_file` if the user has
not yet migrated `.env`.

- [ ] **Step 9: Commit**

```bash
git add scripts/deploy.py README.md SETUP.md CLAUDE.md tests/test_deploy_script.py
git commit -m "feat: refuse a sync that a model override would override; document the split"
```

- [ ] **Step 10: Report the migration steps to the user**

Do **not** perform these; report them. Include the exact key names
`test_no_operational_key_lives_in_the_secrets_file` listed:

1. Copy `.env.config.example` to `.env.config` and fill in the values currently in `.env`.
2. Remove those same keys from `.env`.
3. Re-run `uv run pytest tests/test_config.py` — green means the migration is complete.
4. Run `uv run python -m scripts.deploy --sync-env` to push the renamed/new model vars, then
   set `KEY_USAGE_*` in the Render dashboard if the hosted service should have caps.

---

## Notes for the executor

- **Task 1's placement guard will fail until the user migrates `.env`.** That is expected and
  documented (spec §7). Do not "fix" it by editing `.env`, by weakening the assertion, or by
  removing keys from `OPERATIONAL_KEYS`. Report the names it prints.
- **Tasks 2 and 4 both touch `orchestrator._active_model()`.** Task 2 makes it registry-driven;
  Task 4 replaces the body with a delegation. That is deliberate sequencing, not duplicated
  work — Task 2 must be able to stand alone and pass its own tests.
- **Task 7 renames a function that `tests/conftest.py` monkeypatches.** If the rename is
  missed there, an autouse quarantine silently stops quarantining and `_wanted_env()` starts
  reading the contributor's real `.env`. Step 7's `grep` exists to catch exactly that.
- If any step's expectation does not match what you observe, **stop and report** rather than
  self-resolving — a controller cannot approve work already done (`CLAUDE.md`, "Plan-execution
  / multi-agent process hygiene").
