# Model Pricing Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/deploy.py` refuse to report a healthy config, or push to
Render, when any provider's model var in `.env.config` has no rate-table entry in
`app/providers/pricing.py`.

**Architecture:** One new private helper in `scripts/deploy.py`,
`_unpriced_models()`, loops over all three `(provider, model_var)` pairs from
`registry.PROVIDERS` and returns the ones whose locally-configured model fails
`pricing.is_known()`. `check_config()` turns each into a `problems` entry (an
existing `FAIL`-row mechanism); `sync_env()` refuses on the first with exit 2,
before any HTTP request. No changes to `app/`, no runtime/boot-time validation,
no `--force` bypass.

**Tech Stack:** Python 3.12, pydantic-settings (`app/config.py::Settings`),
pytest + respx, `uv` as the runner.

**Spec:** `docs/superpowers/specs/2026-08-16-model-pricing-validation-design.md`

## Global Constraints

- **All three model vars, always** — `LLM_MODEL`, `GROQ_MODEL`, `VERTEX_MODEL`
  are checked on every run, not just the currently-active provider's. A DB
  provider override can activate any provider with no redeploy, so an unpriced
  value in an inactive provider's var is a live landmine. This mirrors
  `_wanted_env()`, which already pushes every provider's model var for the same
  reason.
- **No `--force` bypass on `sync_env()`'s refusal.** The two supported unblocks
  are: fix `.env.config` to a known model, or add the model's rate to
  `app/providers/pricing.py` first. Do not add a flag.
- **Exit 2, not 1, for the `sync_env()` refusal** — nothing has been pushed at
  that point, consistent with every other pre-push guard in `sync_env()`.
- **An empty model var is skipped, never reported** — that is a distinct,
  pre-existing failure mode; a second message on top of it adds noise, not
  clarity.
- **No live LLM calls anywhere in this work.** `pricing.is_known()` is a pure
  dict lookup. Per `CLAUDE.md`'s LLM-testing-hygiene rules, nothing here may
  issue a generation request.
- **Never print a secret value.** `deploy.py`'s output contract is names,
  lengths, and equality results only. Model names are not secrets and are
  printed deliberately; nothing else new is printed.
- **Do not open `.env`.** Per `CLAUDE.md`'s Secret handling section, `.env` must
  not be read or edited by an agent for any reason. `.env.config` is non-secret
  and safe to open, but this work needs no edit to either file.

---

### Task 1: `check_config()` reports unpriced models

**Files:**
- Modify: `scripts/deploy.py` (imports at line 35; new `_unpriced_models()`
  helper immediately above `check_config()` at line 138; new loop inside
  `check_config()` after the provider/credential `if/else`, before
  `detail_lines`)
- Test: `tests/test_deploy_script.py` (new autouse fixture near the existing
  `_no_real_provider_credentials` at line 25; new tests after
  `test_check_config_reports_a_bad_provider_alongside_other_missing_keys` at
  line 165)

**Interfaces:**
- Consumes: `app.providers.pricing.is_known(provider, model) -> bool` and
  `app.providers.pricing.models_for(provider) -> tuple[str, ...]` (both already
  exist, unchanged); `deploy._PROVIDERS`, which is
  `registry.PROVIDERS: dict[str, tuple[str, str]]` mapping
  `provider -> (credential_var, model_var)`, e.g.
  `{"gemini": ("GEMINI_API_KEY", "LLM_MODEL"), "groq": ("GROQ_API_KEY", "GROQ_MODEL"), "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL")}`.
- Produces: `deploy._unpriced_models() -> list[tuple[str, str, str, str]]`,
  each element `(provider, model_var, model, known)` where `known` is a
  comma-joined string of the provider's priced models or the literal
  `"(none known for this provider)"`. Task 2 consumes this exact signature.

- [ ] **Step 1: Add the autouse fixture that pins model vars to their shipped defaults**

`app.config.settings` is a module-level singleton loaded from the real `.env`
and `.env.config` files, and the new guards read every model var — so without
this, a developer's locally-edited `.env.config` model value would change what
these tests assert. This is the same reason the existing
`_no_real_provider_credentials` autouse fixture exists.

In `tests/test_deploy_script.py`, change the import on line 18 from:

```python
from app.config import settings
```

to:

```python
from app.config import Settings, settings
from app.providers import pricing
```

Then add this fixture immediately after `_no_real_provider_credentials` (after
line 31):

```python
@pytest.fixture(autouse=True)
def _shipped_model_defaults(monkeypatch):
    """Pin every provider's model var to its Settings class default, so these
    tests describe the SHIPPED configuration rather than whatever the
    developer's local .env.config happens to say. Same reason
    _no_real_provider_credentials exists: settings is a module-level singleton
    loaded from real env files, and deploy.py's pricing guards read every
    model var -- so a locally-edited model value would otherwise silently
    change what these tests assert."""
    for _credential, model_var in deploy._PROVIDERS.values():
        field = model_var.lower()
        monkeypatch.setattr(settings, field, Settings.model_fields[field].default)
```

- [ ] **Step 2: Write the failing tests**

Add these after `test_check_config_reports_a_bad_provider_alongside_other_missing_keys`
(after line 175):

```python
@pytest.mark.parametrize("model_var", ["LLM_MODEL", "GROQ_MODEL", "VERTEX_MODEL"])
def test_check_config_fails_on_an_unpriced_model(complete_config, monkeypatch, model_var):
    """A model with no pricing.py rate entry reaches an uncaught KeyError in
    app/orchestrator.py's estimate_cost_usd() -- AFTER all three specialists
    already made real, paid calls. check_config is the local pre-flight that
    catches it first, for EVERY provider's var, not just the active one's."""
    monkeypatch.setattr(settings, model_var.lower(), "totally-made-up-model")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert model_var in result.detail
    assert "totally-made-up-model" in result.detail
    provider = next(p for p, (_c, mv) in deploy._PROVIDERS.items() if mv == model_var)
    assert provider in result.detail
    for known in pricing.models_for(provider):
        assert known in result.detail      # the fix is named, not just the fault


def test_check_config_ignores_default_models(complete_config):
    """Regression guard against default/pricing-table drift: if a shipped
    model default ever stops being priced, a fresh clone would FAIL config out
    of the box with nothing edited."""
    for provider, (_credential, model_var) in deploy._PROVIDERS.items():
        default = Settings.model_fields[model_var.lower()].default
        assert pricing.is_known(provider, default), (
            f"{model_var}'s shipped default {default!r} has no {provider} pricing entry"
        )
    assert deploy.check_config().status == "PASS"


def test_check_config_reports_an_unpriced_model_alongside_other_missing_keys(
    complete_config, monkeypatch
):
    """An unpriced model must not mask problems already collected -- one run
    surfaces every problem, per this module's own contract."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "groq_model", "totally-made-up-model")
    detail = deploy.check_config().detail
    assert "GITHUB_WEBHOOK_SECRET" in detail
    assert "GROQ_MODEL" in detail
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "unpriced_model or default_models" -v`

Expected: the two `unpriced` tests FAIL (`check_config()` returns `PASS`, so the
`assert result.status == "FAIL"` / missing-substring assertions fail);
`test_check_config_ignores_default_models` PASSES already (it asserts existing
correct behavior — that is fine, it is a drift guard, not a red test).

- [ ] **Step 4: Add the pricing import to `scripts/deploy.py`**

Change line 35 from:

```python
from app.providers import registry
```

to:

```python
from app.providers import pricing, registry
```

- [ ] **Step 5: Add the `_unpriced_models()` helper**

Insert immediately above `def check_config()` (i.e. after `_private_key_b64()`
ends at line 135, before line 138):

```python
def _unpriced_models() -> list[tuple[str, str, str, str]]:
    """Every provider whose locally-configured model has no rate-table entry,
    as (provider, model_var, model, known-models string).

    Checked for EVERY provider, not just the active one -- exactly as
    _wanted_env() pushes every provider's model var: a DB provider override
    can activate any of them with no redeploy, so an unpriced value sitting in
    a currently-inactive provider's var is a live landmine, not a harmless one.

    An empty model var is skipped deliberately: that is a distinct,
    pre-existing failure mode, and piling a second, confusing message onto it
    adds noise rather than clarity. In practice it never fires -- every
    Settings model field carries a non-empty, priced default.

    Shared by check_config() (which reports all of them) and sync_env() (which
    refuses on the first), so the two can never disagree about what counts as
    unpriced.
    """
    unpriced: list[tuple[str, str, str, str]] = []
    for provider, (_credential, model_var) in sorted(_PROVIDERS.items()):
        model = getattr(settings, model_var.lower(), "")
        if model and not pricing.is_known(provider, model):
            known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
            unpriced.append((provider, model_var, model, known))
    return unpriced
```

- [ ] **Step 6: Report unpriced models from `check_config()`**

In `check_config()`, insert after the provider `if entry is None: ... else: ...`
block ends (after line 167, `missing.append(credential)`) and before the blank
line preceding `detail_lines = []`:

```python
    # A problem, not a missing key: the var HAS a value, it is simply not one
    # the pricing table recognizes. Existing detail_lines assembly (missing,
    # then problems) needs no change.
    for provider, model_var, model, known in _unpriced_models():
        problems.append(
            f"{model_var}={model!r} has no pricing-table entry for {provider} "
            f"(known: {known})"
        )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -k "unpriced_model or default_models" -v`
Expected: PASS (4 parametrized/plain cases from the new tests, all green)

- [ ] **Step 8: Run the whole deploy-script suite for regressions**

Run: `uv run pytest tests/test_deploy_script.py -q`
Expected: all pass. The new autouse fixture changes model vars for every test in
the file; if any existing test breaks, it is because it depended on a
locally-configured model value — report it rather than loosening the fixture.

- [ ] **Step 9: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat: check_config reports a model with no pricing-table entry"
```

---

### Task 2: `sync_env()` refuses to push an unpriced model

**Files:**
- Modify: `scripts/deploy.py` (`sync_env()`, new guard after the
  `if settings.database_url:` block ends at line 785, before
  `wanted = _wanted_env()` on line 786)
- Test: `tests/test_deploy_script.py` (new tests after
  `test_sync_env_refuses_when_a_non_active_providers_model_override_disagrees`,
  at the end of the file)

**Interfaces:**
- Consumes: `deploy._unpriced_models() -> list[tuple[str, str, str, str]]` from
  Task 1, each element `(provider, model_var, model, known)`.
- Produces: nothing new — `sync_env()` keeps its `int` exit-code return.

- [ ] **Step 1: Write the failing tests**

Append to the end of `tests/test_deploy_script.py`:

```python
@pytest.mark.parametrize("model_var", ["LLM_MODEL", "GROQ_MODEL", "VERTEX_MODEL"])
def test_sync_env_refuses_to_push_an_unpriced_model(
    sync_ready, monkeypatch, capsys, model_var
):
    """Pushing an unpriced model would burn real, paid specialist calls on
    every PR review until someone noticed the KeyError in the logs -- and the
    dispatcher retries a hard failure, so one bad value repeats it. The
    refusal must fire before any HTTP request, so nothing is half-pushed."""
    monkeypatch.setattr(settings, model_var.lower(), "totally-made-up-model")
    called = []
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2                       # "could not run", not a partial failure
    err = capsys.readouterr().err
    assert model_var in err
    assert "totally-made-up-model" in err
    assert ".env.config" in err            # names the file to fix
    assert called == []                    # refused before any HTTP


def test_sync_env_refuses_on_a_non_active_providers_unpriced_model(
    sync_ready, monkeypatch, capsys
):
    """sync_ready selects groq, whose own model is fine -- but VERTEX_MODEL is
    pushed by _wanted_env() too, and a DB provider flip can activate vertex
    with no redeploy. An unpriced value there must block the push exactly as
    the active provider's would; the refusal must name vertex specifically."""
    assert settings.llm_provider == "groq"
    monkeypatch.setattr(settings, "vertex_model", "totally-made-up-model")
    called = []
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: called.append(1))
    assert deploy.sync_env() == 2
    err = capsys.readouterr().err
    assert "VERTEX_MODEL" in err
    assert "vertex" in err
    assert called == []


def test_sync_env_allows_a_priced_model(sync_ready, monkeypatch, capsys):
    """The guard must not false-positive on the shipped defaults. Returning
    None from find_service_id stops the run at "no such service" -- which
    proves it got PAST the pricing guard without needing a full push."""
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    assert deploy.sync_env() == 1
    assert "no Render service named" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "unpriced_model or priced_model" -v`

Expected: the four refusal cases FAIL (`sync_env()` proceeds past the missing
guard and returns 1 from `find_service_id`, so `assert code == 2` fails);
`test_sync_env_allows_a_priced_model` PASSES already.

- [ ] **Step 3: Add the refusal guard to `sync_env()`**

In `scripts/deploy.py`, insert after the `if settings.database_url:` block ends
(after the model-override `return 2` on line 785) and before
`wanted = _wanted_env()`:

```python
    # Deliberately NOT inside the `if settings.database_url:` block above: this
    # is a pure local pricing-table lookup, so it must run whether or not a
    # database is configured. Like every other pre-push guard here it runs
    # before any HTTP request, so refusing can never leave a partial push
    # behind. No --force escape hatch, unlike set_override.py --model: that is
    # a deliberate one-shot operator action, this is the automated push path,
    # and forcing an unpriced model past it is the exact mistake this guard
    # exists to prevent.
    for provider, model_var, model, known in _unpriced_models():
        print(
            f"refusing to sync: {model_var}={model!r} has no pricing-table entry for "
            f"{provider} (known: {known}); fix .env.config or add a pricing.py entry first",
            file=sys.stderr,
        )
        return 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -k "unpriced_model or priced_model" -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. Pay attention to `tests/test_deploy_script.py`'s existing
`test_sync_env_allows_an_agreeing_model_override` and the two model-override
refusal tests — the new guard sits directly after the block they exercise.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat: --sync-env refuses to push a model with no pricing-table entry"
```

---

### Task 3: Documentation

**Files:**
- Modify: `README.md:163` (exit-2 row), `README.md:143` (the `config` check-table
  row), and the "Deploying" section after the DB-override refusal paragraph
  (`README.md:211-214`)
- Modify: `docs/superpowers/specs/2026-08-15-operational-config-split-design.md`
  (§9's "A model set directly in `.env.config` still has no pricing-table
  guard" bullet, ending at line 417)

**Interfaces:**
- Consumes: the behavior implemented in Tasks 1 and 2. Nothing produces code
  here.

- [ ] **Step 1: Widen the README exit-2 row**

Replace line 163's cell text:

```
| 2 | the run could not proceed: `GITHUB_TARGET_REPO` or a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values, an unsupported `LLM_PROVIDER`, a model with no pricing-table entry, or an active DB override that would mask the push) |
```

- [ ] **Step 2: Widen the `config` check-table row**

Replace line 143's cell text:

```
| `config` | Every setting the service needs is resolvable locally, and every provider's model var has a pricing-table entry | yes |
```

- [ ] **Step 3: Add the deploy-section paragraph**

Insert immediately after the DB-override refusal paragraph that ends
`uv run python -m scripts.set_override --clear.` (line 214), as its own
paragraph:

```markdown
`--sync-env` also refuses (exit 2, before any request) if **any** provider's
model var — `LLM_MODEL`, `GROQ_MODEL`, or `VERTEX_MODEL`, not just the active
provider's — names a model with no entry in `app/providers/pricing.py`'s rate
table; `config` reports the same thing as a `FAIL` row. An unpriced model only
fails at cost-estimation time, *after* all three specialists have already made
real, paid calls, so it is caught here instead. There is no `--force`: either
fix `.env.config` to a model the table knows, or add the new model's rate to
`pricing.py` first.
```

- [ ] **Step 4: Add the resolution addendum to the older design doc**

Append to the end of the §9 bullet in
`docs/superpowers/specs/2026-08-15-operational-config-split-design.md` (after
line 417, `reaches a live dispatcher run.`), as an indented continuation of that
bullet:

```markdown
  **Resolved** by `docs/superpowers/specs/2026-08-16-model-pricing-validation-design.md`
  — `check_config()` reports an unpriced model as a `FAIL`-row problem and
  `sync_env()` refuses the push (exit 2), for all three model vars.
```

- [ ] **Step 5: Verify the docs tests still pass**

Run: `uv run pytest tests/test_deploy_script.py -k "docs or exit_codes" -v`
Expected: PASS (`test_env_var_names_match_the_docs`,
`test_exit_codes_are_documented`)

- [ ] **Step 6: Run the whole suite once more**

Run: `uv run pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-15-operational-config-split-design.md
git commit -m "docs: record the .env.config model pricing validation in README and the config-split spec"
```
