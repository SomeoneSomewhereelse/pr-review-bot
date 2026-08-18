# Setup Experience — Stage 3a (Doc Generation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it structurally impossible for the project's reference documentation to disagree with its code, by generating it from the code and failing CI when the two drift.

**Architecture:** One `scripts/gen_docs.py` emits four Markdown fragments into `guide/reference/` from class-level metadata and module constants — **never** from the instantiated `settings` singleton, which holds real credentials. `scripts/deploy.py`'s eleven checks gain a registry so the check table has a source at all, and `run_checks` consumes the same registry so the two can never diverge. A CI job regenerates and fails on any diff.

**Tech Stack:** Python 3.12 · pydantic v2 (`model_fields` introspection) · pytest · GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-18-setup-experience-design.md` — §7 in full, plus §5a's encoding rule and §8h/§8k. Read §7 before starting.

## Global Constraints

- **`CLAUDE.md`'s "Secret handling" section overrides everything here.** Read it first. Never open `.env`.
- **THE RULE THIS STAGE TURNS ON: `gen_docs.py` must never read `app.config.settings`.** It reads `Settings.model_fields` (class-level metadata — declared *defaults*, not configured values) and module-level constants. The singleton holds the developer's real `DATABASE_URL`, API keys, and service-account material; a generator that touched it would write those into a file that is **committed and published**. Import `Settings` (the class) and never `settings` (the instance). Task 2 pins this with a sentinel test, and it applies to every generator added afterwards.
- **Generated output must be deterministic.** No timestamps, no `datetime.now()`, no set iteration order, no absolute paths. The CI drift check compares byte-for-byte, so any run-to-run variation turns into a permanently red build.
- **Every file read/write passes `encoding="utf-8"`; every write also passes `newline="\n"`** (spec §5a). On Windows a missing `newline=` writes CRLF, and `.gitattributes` (`* text=auto eol=lf`) pins the working tree to LF — so the drift check would fail on that operator's machine and nowhere else.
- **`gen_docs.py` writes only into `guide/reference/`, and only files it names itself.** It is a *replacing* writer by design. Task 5 confines it by test. Do not add a flag that lets it write elsewhere.
- **Do not author any hand-written guide page in this stage.** `guide/reference/` is generated output only; the rest of the site is Stage 3b. A hand-written page placed under `guide/reference/` would be silently destroyed on the next generate — this ordering is the protection, not care.
- **Python 3.12.** Use `X | None`, not `Optional[X]`.
- **Lint and test before every commit:** `uv run ruff check .` then `uv run pytest -v`. Baseline entering this stage: **752 passing**, ruff clean, HEAD `99bac95`.
- **No changes to `app/`.** If a task appears to need one, stop and report.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/deploy.py` | Gains a `CheckSpec` registry (`CHECKS`) that both `run_checks` and the docs generator read. No behavior change. |
| `scripts/gen_docs.py` | **New.** Four pure `render_*()` functions plus a confined `write_all()` and a CLI. |
| `guide/reference/config.md` | **Generated.** Every `Settings` field: name, type, declared default, and which file it belongs in. |
| `guide/reference/pricing.md` | **Generated.** The rate table with each entry's source URL, verified date, and any provenance caveat. |
| `guide/reference/checks.md` | **Generated.** The eleven deploy checks: what each verifies, and whether it is required. |
| `guide/reference/sync-env.md` | **Generated.** Exactly which env vars `--sync-env` pushes, and which are deliberately never pushed. |
| `.github/workflows/ci.yml` | Gains a `docs` job: regenerate, then `git diff --exit-code guide/reference/`. |

---

### Task 1: Give `scripts/deploy.py`'s checks a registry

Spec §7. Today the check table exists **only** as README prose, so there is nothing to generate from. This adds the single source and makes `run_checks` consume it — removing a hand-maintained list rather than adding one. Behavior-preserving: the same eleven checks run in the same order with the same arguments.

**Files:**
- Modify: `scripts/deploy.py` (add `CheckSpec` and `CHECKS`; rewrite `run_checks`)
- Test: `tests/test_deploy_script.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `CheckSpec` (`NamedTuple`: `name: str`, `func: Callable[..., CheckResult]`, `verifies: str`, `required: bool`, `args: tuple[str, ...]`) and `CHECKS: tuple[CheckSpec, ...]`. `args` entries are drawn from `{"repos", "base"}` only. Task 4 renders `CHECKS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_script.py (append)
def test_checks_registry_matches_what_run_checks_actually_runs():
    """The registry is the single source: if run_checks stops consuming it,
    the generated checks.md silently starts describing a different tool."""
    names = [spec.name for spec in deploy.CHECKS]
    assert names == [
        "config", "pricing", "boot-creds-live", "github-app", "health",
        "database", "provider", "provider-live", "api-key-live",
        "render-service", "uptime-pinger",
    ]
    results = deploy.run_checks(frozenset(), "https://example.invalid")
    assert [r.name for r in results] == names


def test_every_check_spec_is_documented_and_well_formed():
    for spec in deploy.CHECKS:
        assert spec.verifies.strip(), f"{spec.name} has no description to generate from"
        assert callable(spec.func)
        assert isinstance(spec.required, bool)
        assert set(spec.args) <= {"repos", "base"}, f"{spec.name} wants an unknown argument"


def test_required_checks_are_the_ones_that_need_no_operator_key():
    """`required` drives the generated table's "Always runs?" column, so it has
    to mean something precise: a check that needs no operator-local key
    (RENDER_API_KEY, UPTIMEROBOT_API_KEY, DATABASE_URL).

    Deliberately NOT "can fail the run" -- `pricing` always runs but only ever
    WARNs, so conflating the two would misdescribe it in the published table.
    """
    required = {spec.name for spec in deploy.CHECKS if spec.required}
    assert required == {"config", "pricing", "github-app", "health"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "registry or check_spec or required_checks" -v`
Expected: FAIL with `AttributeError: module 'scripts.deploy' has no attribute 'CHECKS'`.

- [ ] **Step 3: Add the registry and rewrite `run_checks`**

Add above `run_checks` in `scripts/deploy.py`:

```python
class CheckSpec(NamedTuple):
    """One row of the checklist, and the single source for its documentation.

    `verifies` is written for an operator reading the generated table, not for
    a maintainer reading this file -- scripts/gen_docs.py renders it verbatim.
    `required` means "always runs -- needs no operator-local key". It is NOT
    "can fail the run": `pricing` always runs but only ever WARNs. The others
    degrade to SKIPPED without RENDER_API_KEY / UPTIMEROBOT_API_KEY /
    DATABASE_URL rather than failing.
    """

    name: str
    func: Callable[..., CheckResult]
    verifies: str
    required: bool
    args: tuple[str, ...] = ()


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("config", check_config,
              "Every setting the service needs is resolvable locally", True),
    CheckSpec("pricing", check_pricing,
              "Every provider's effective model has a rate-table entry "
              "(a warning only -- an unpriced model runs, without a cost estimate)",
              True),
    CheckSpec("boot-creds-live", check_boot_credentials_live,
              "The vars the service reads at every boot are present on the deployed "
              "Render service under their current names -- not just locally", False),
    CheckSpec("github-app", check_installation_and_webhook,
              "The App has exactly one installation, every repo in GITHUB_TARGET_REPO "
              "is covered by it, and its webhook points here (set only if wrong)",
              True, ("repos", "base")),
    CheckSpec("health", check_health_endpoint,
              "/healthz answers BOTH GET and HEAD -- UptimeRobot's free tier sends "
              "HEAD, so a GET-only endpoint lets the instance sleep", True, ("base",)),
    CheckSpec("database", check_database,
              "Postgres is reachable and the app has provisioned its tickets table",
              False),
    CheckSpec("provider", check_provider,
              "The provider that will actually run -- LLM_PROVIDER, or an active DB "
              "override -- has its credential set", False),
    CheckSpec("provider-live", check_provider_live,
              "The actively-resolved provider's credential is present on the deployed "
              "Render service, not just locally", False),
    CheckSpec("api-key-live", check_api_key_live,
              "The actively-resolved provider's actively-resolved key slot is present "
              "on the deployed Render service", False),
    CheckSpec("render-service", check_render_service,
              "The latest Render deploy is live, and matches local HEAD when a commit "
              "is comparable", False),
    CheckSpec("uptime-pinger", check_uptime_pinger,
              "A monitor targets /healthz exactly, is active, and polls at most every "
              "10 minutes", False, ("base",)),
)
```

`scripts/deploy.py` already imports `Literal` from `typing` and uses `NamedTuple`/`dataclass`; add `Callable` and `NamedTuple` to that same `typing` import. The project's ruff config selects `E4, E7, E9, F, E501` with `line-length = 100` and no `UP` rules, so `typing.Callable` is not flagged — but every snippet in this plan must still fit 100 columns. Then replace `run_checks`'s body:

```python
def run_checks(repos: frozenset[str], base: str) -> list[CheckResult]:
    """All eleven, foundational (and cheap, where possible) first, so a
    misconfiguration is reported before the checks that would fail as a
    consequence of it. Order and content come from CHECKS, which
    scripts/gen_docs.py also renders -- so the table an operator reads can
    never describe a different set than the one that runs.
    """
    available = {"repos": repos, "base": base}
    return [
        _safe(spec.name, spec.func, *(available[name] for name in spec.args))
        for spec in CHECKS
    ]
```

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS, including every pre-existing `deploy.py` test — this task changes no behavior. A failure means the registry's order or arguments differ from the original `run_checks`; fix the registry, not the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "refactor: give deploy.py's checks a registry that run_checks consumes"
```

---

### Task 2: `scripts/gen_docs.py` and the config table — with the safety rule pinned

Spec §7 and §8k. **This task establishes the rule the whole stage depends on**, so its test suite matters more than its output.

`Settings.model_fields` exposes each field's **declared default** — the class-level value, not whatever is configured on this machine. `app.config.settings` is the opposite: a live instance holding the developer's real `DATABASE_URL`, API keys, and service-account key. Reading the instance would write those into a file that is committed and published.

**Files:**
- Create: `scripts/gen_docs.py`
- Test: `tests/test_gen_docs.py`

**Interfaces:**
- Consumes: `app.config.Settings` (the **class**), `app.config.OPERATIONAL_KEYS`.
- Produces: `GENERATED_HEADER: str`, `render_config() -> str`. Tasks 3-5 add siblings and follow the same rule.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gen_docs.py
"""gen_docs reads CLASS metadata, never the live settings instance.

That distinction is the whole safety story: Settings.model_fields carries
declared defaults, while app.config.settings carries this machine's real
credentials -- and everything generated here is committed and published.
"""
from __future__ import annotations

from app.config import OPERATIONAL_KEYS, settings
from scripts import gen_docs

SENTINEL = "SENTINEL-6d21fa48c093be75-MUST-NOT-BE-PUBLISHED"


def test_config_table_lists_every_settings_field():
    from app.config import Settings

    table = gen_docs.render_config()
    for name in Settings.model_fields:
        assert name.upper() in table, f"{name} missing from the generated table"


def test_config_table_never_contains_a_configured_value(monkeypatch):
    """The regression guard for the rule this stage turns on. If a generator is
    ever changed to read `settings` instead of `Settings`, this fails."""
    for field in ("database_url", "github_webhook_secret", "groq_api_key",
                  "gemini_api_key", "gcp_service_account_key", "github_app_private_key"):
        monkeypatch.setattr(settings, field, SENTINEL, raising=False)
    assert SENTINEL not in gen_docs.render_config()


def test_gen_docs_module_does_not_import_the_settings_instance():
    """Static guard complementing the behavioural one above: importing the
    singleton at all is the mistake.

    Parsed with ast rather than grepped for a substring. A source grep would
    also match the module's own docstring explaining the rule -- a false
    positive that has already bitten this project once (ISSUES.md, Stage 2
    Task 4, where a docstring naming a forbidden function failed that task's
    own read-only source check).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gen_docs))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            assert "settings" not in imported, (
                f"line {node.lineno} imports the settings instance from {node.module}"
            )


def test_config_table_marks_where_each_key_belongs():
    table = gen_docs.render_config()
    for line in table.splitlines():
        if line.startswith("| `LLM_PROVIDER`"):
            assert ".env.config" in line
        if line.startswith("| `GROQ_API_KEY`"):
            assert ".env" in line and ".env.config" not in line
    assert "LLM_PROVIDER" in OPERATIONAL_KEYS


def test_generated_output_carries_the_do_not_edit_header():
    assert gen_docs.render_config().startswith(gen_docs.GENERATED_HEADER)
    assert "do not edit" in gen_docs.GENERATED_HEADER.lower()
    assert "scripts.gen_docs" in gen_docs.GENERATED_HEADER


def test_render_config_is_deterministic():
    """CI compares byte-for-byte, so any run-to-run variation is a red build."""
    assert gen_docs.render_config() == gen_docs.render_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gen_docs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.gen_docs'`.

- [ ] **Step 3: Write the module and the config generator**

```python
"""Generate guide/reference/*.md from the code, so documentation cannot drift.

    uv run python -m scripts.gen_docs

THE ONE RULE. Every generator here reads CLASS-LEVEL metadata and module
constants -- Settings.model_fields, OPERATIONAL_KEYS, pricing._RATES,
deploy.CHECKS -- and NEVER app.config.settings. model_fields carries each
field's DECLARED DEFAULT; the settings instance carries this machine's real
DATABASE_URL, API keys, and service-account material. Everything written here
is committed and published, so reading the instance would publish those
values. Import the Settings CLASS only; the module-level instance in
app/config.py must never be imported here. tests/test_gen_docs.py pins this
both behaviourally and by parsing this module's own import statements.

Output must be DETERMINISTIC -- no timestamps, no unordered iteration, no
absolute paths. The CI drift job compares byte-for-byte, so any run-to-run
variation is a permanently red build rather than a useful signal.
"""

from __future__ import annotations

from app.config import OPERATIONAL_KEYS, Settings

GENERATED_HEADER = (
    "<!-- generated by scripts.gen_docs -- do not edit by hand.\n"
    "     Change the code it reads, then run: uv run python -m scripts.gen_docs -->\n"
)


def _type_name(annotation: object) -> str:
    """A readable type for the table. str(annotation) renders unions as
    'float | None' already; the getattr picks the friendlier name for plain
    classes ('int', not \"<class 'int'>\")."""
    return getattr(annotation, "__name__", None) or str(annotation)


def _default_text(field) -> str:
    """The DECLARED default -- class-level, never a configured value."""
    default = field.default
    if default is None:
        return "`None`"
    if default == "":
        return "*(empty)*"
    return f"`{default}`"


def render_config() -> str:
    """Every Settings field, and which of the two config files it belongs in.

    Secret-by-default, exactly as app/config.py's OPERATIONAL_KEYS defines it:
    a name on that list is operational and lives in .env.config; everything
    else is a credential and lives in .env.
    """
    lines = [
        GENERATED_HEADER,
        "# Configuration reference\n",
        "Every setting this service reads. `.env.config` holds operational "
        "settings and is safe to open and edit; `.env` holds credentials and "
        "nothing else.\n",
        "| Setting | Type | Default | Lives in |",
        "| --- | --- | --- | --- |",
    ]
    for name, field in Settings.model_fields.items():
        env_name = name.upper()
        home = ".env.config" if env_name in OPERATIONAL_KEYS else ".env"
        lines.append(
            f"| `{env_name}` | `{_type_name(field.annotation)}` | "
            f"{_default_text(field)} | `{home}` |"
        )
    return "\n".join(lines) + "\n"
```

`Settings.model_fields` preserves declaration order, so no sorting is needed and none should be added — the file's own order is more useful to a reader than alphabetical.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_gen_docs.py -v`
Expected: all PASS. If `test_config_table_never_contains_a_configured_value` fails, a generator is reading the instance — fix the generator, never the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_docs.py tests/test_gen_docs.py
git commit -m "feat: generate the config reference from Settings class metadata"
```

---

### Task 3: The pricing and sync-env tables

Spec §7. Two small constant-table renders, both names-and-numbers only.

**Files:**
- Modify: `scripts/gen_docs.py`
- Test: `tests/test_gen_docs.py` (append)

**Interfaces:**
- Consumes: Task 2's `GENERATED_HEADER`; `app.providers.pricing._RATES` and `Rate`; `app.providers.registry.PROVIDERS`; `scripts.deploy`'s `_ALWAYS_SYNCED`, `_GENERIC_OPERATIONAL_ENV_ATTRS`, `_DB_SYNCED_OPERATIONAL_KEYS`, `_NEVER_SYNCED_OPERATIONAL_KEYS`.
- Produces: `render_pricing() -> str`, `render_sync_env() -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gen_docs.py (append)
def test_pricing_table_carries_every_rate_with_its_provenance():
    from app.providers import pricing

    table = gen_docs.render_pricing()
    for (provider, model), rate in pricing._RATES.items():
        assert model in table
        assert provider in table
        assert rate.verified in table
        assert rate.source_url in table


def test_pricing_table_surfaces_an_inherited_rates_caveat():
    """A `verified` date that records no independent check must not be
    presented as though it did -- the note exists precisely to say so."""
    table = gen_docs.render_pricing()
    assert "not independently checked" in table


def test_pricing_table_explains_that_an_unpriced_model_still_runs():
    table = gen_docs.render_pricing()
    assert "without a cost estimate" in table


def test_sync_env_table_separates_pushed_from_never_pushed():
    from scripts import deploy

    table = gen_docs.render_sync_env()
    for name in deploy._ALWAYS_SYNCED:
        assert name in table
    for name in deploy._DB_SYNCED_OPERATIONAL_KEYS:
        assert name in table, "the DB-only keys must be listed as deliberately never pushed"
    for name in deploy._NEVER_SYNCED_OPERATIONAL_KEYS:
        assert name in table
    assert "runtime_config" in table, "must say WHERE the DB-only keys actually live"


def test_sync_env_table_lists_every_providers_model_var():
    from app.providers import registry

    table = gen_docs.render_sync_env()
    for _credential, model_var in registry.PROVIDERS.values():
        assert model_var in table


def test_the_new_renderers_are_deterministic():
    assert gen_docs.render_pricing() == gen_docs.render_pricing()
    assert gen_docs.render_sync_env() == gen_docs.render_sync_env()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gen_docs.py -k "pricing or sync_env" -v`
Expected: FAIL with `AttributeError: module 'scripts.gen_docs' has no attribute 'render_pricing'`.

- [ ] **Step 3: Write both generators**

Append to `scripts/gen_docs.py` (and extend the imports):

```python
from app.providers import pricing, registry
from scripts import deploy


def render_pricing() -> str:
    """The rate table, with each entry's provenance.

    Sorted explicitly: dict order is stable in practice, but the drift check
    compares byte-for-byte and a reordered _RATES literal should not read as a
    documentation change.
    """
    lines = [
        GENERATED_HEADER,
        "# Model pricing\n",
        "Rates are USD per 1M tokens. Pricing is **optional**: a model with no "
        "entry here still runs, the PR comment simply appears without a cost "
        "estimate, and `scripts/deploy.py` reports it as a warning rather than "
        "blocking the deploy.\n",
        "| Provider | Model | In | Out | Verified | Source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    caveats: list[str] = []
    for (provider, model), rate in sorted(pricing._RATES.items()):
        marker = " †" if rate.note else ""
        lines.append(
            f"| `{provider}` | `{model}` | {rate.rate_in} | {rate.rate_out} | "
            f"{rate.verified}{marker} | [source]({rate.source_url}) |"
        )
        if rate.note:
            caveats.append(f"† `{provider}`/`{model}`: {rate.note}")
    if caveats:
        lines.append("")
        lines.extend(caveats)
    return "\n".join(lines) + "\n"


def render_sync_env() -> str:
    """What `--sync-env` pushes to Render, and what it deliberately never does.

    The second half matters more than the first: a setting that looks like an
    env var but lives only in the database is exactly the thing an operator
    edits, redeploys, and then cannot explain the non-effect of.
    """
    always = sorted(deploy._ALWAYS_SYNCED)
    credentials = sorted({c for c, _m in registry.PROVIDERS.values()})
    models = sorted({m for _c, m in registry.PROVIDERS.values()})
    generic = sorted(deploy._GENERIC_OPERATIONAL_ENV_ATTRS)
    db_only = sorted(deploy._DB_SYNCED_OPERATIONAL_KEYS)
    never = sorted(deploy._NEVER_SYNCED_OPERATIONAL_KEYS)

    def bullets(names: list[str]) -> list[str]:
        return [f"- `{name}`" for name in names]

    return "\n".join(
        [
            GENERATED_HEADER,
            "# What `--sync-env` pushes\n",
            "## Always pushed\n",
            *bullets(always),
            "",
            "Plus `LLM_PROVIDER`, and `GITHUB_APP_INSTALLATION_ID` once it is set "
            "locally (it is optional, so an empty value is not an error).\n",
            "## Every provider's model var\n",
            *bullets(models),
            "",
            "All of them, not just the active provider's: a database override can "
            "activate any provider with no redeploy, so a provider whose model var "
            "was never pushed would read a missing value on the service.\n",
            "## Provider credentials\n",
            *bullets(credentials),
            "",
            "The selected provider's is always pushed. Another provider's is pushed "
            "only if you happen to have it set locally -- an unselected provider's "
            "key is never demanded.\n",
            "## Operational settings\n",
            *bullets(generic),
            "",
            "## Never pushed: database-only settings\n",
            *bullets(db_only),
            "",
            "These live only in the `runtime_config` table, never as a Render env "
            "var, because the dispatcher must be able to change them with no "
            "redeploy. `uv run python -m scripts.deploy --sync-config-db` pushes "
            "them there. Editing one and redeploying does nothing on its own.\n",
            "## Never pushed: operator-machine settings\n",
            *bullets(never),
            "",
            "These configure your local tooling, not the service. Setting them on "
            "Render would create dead env vars.",
        ]
    ) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_gen_docs.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_docs.py tests/test_gen_docs.py
git commit -m "feat: generate the pricing and sync-env references from code"
```

---

### Task 4: The deploy-checks table

Spec §7. Renders Task 1's registry, so the table an operator reads and the checks that actually run come from one place.

**Files:**
- Modify: `scripts/gen_docs.py`
- Test: `tests/test_gen_docs.py` (append)

**Interfaces:**
- Consumes: `deploy.CHECKS` and `deploy.CheckSpec` (Task 1); `GENERATED_HEADER` (Task 2).
- Produces: `render_checks() -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gen_docs.py (append)
def test_checks_table_renders_every_registered_check_in_order():
    from scripts import deploy

    table = gen_docs.render_checks()
    positions = [table.index(f"`{spec.name}`") for spec in deploy.CHECKS]
    assert positions == sorted(positions), "table order must match run order"
    for spec in deploy.CHECKS:
        assert spec.verifies in table, f"{spec.name}'s description was not rendered"


def test_checks_table_distinguishes_required_from_optional():
    from scripts import deploy

    table = gen_docs.render_checks()
    for line in table.splitlines():
        for spec in deploy.CHECKS:
            if line.startswith(f"| `{spec.name}`"):
                assert ("yes" in line) is spec.required, f"{spec.name} marked wrongly"


def test_checks_table_names_the_keys_that_unlock_the_optional_ones():
    """An operator seeing SKIPPED needs to know which key would unskip it."""
    table = gen_docs.render_checks()
    for key in ("RENDER_API_KEY", "UPTIMEROBOT_API_KEY", "DATABASE_URL"):
        assert key in table


def test_render_checks_is_deterministic():
    assert gen_docs.render_checks() == gen_docs.render_checks()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gen_docs.py -k checks -v`
Expected: FAIL with `AttributeError: module 'scripts.gen_docs' has no attribute 'render_checks'`.

- [ ] **Step 3: Write the generator**

Append to `scripts/gen_docs.py`:

```python
def render_checks() -> str:
    """The deploy checklist, rendered from deploy.CHECKS in run order.

    Order is the registry's order, not sorted: the checks run foundational
    first so a misconfiguration is reported before the checks that would fail
    as a consequence of it, and the table is more useful read the same way.
    """
    lines = [
        GENERATED_HEADER,
        "# Deployment checks\n",
        "`uv run python -m scripts.deploy` runs all of these and prints one line "
        "each, so a single run surfaces every problem rather than only the "
        "first.\n",
        "| Check | Verifies | Always runs? |",
        "| --- | --- | --- |",
    ]
    for spec in deploy.CHECKS:
        lines.append(
            f"| `{spec.name}` | {spec.verifies} | "
            f"{'yes' if spec.required else 'needs an operator key'} |"
        )
    lines.extend(
        [
            "",
            "## Unskipping the optional checks\n",
            "An optional check degrades to `SKIPPED` with a hint, never to a "
            "failure, when its operator-local key is unset. None of these keys is "
            "ever set on the Render service itself.\n",
            "- `RENDER_API_KEY` (Render → Account Settings → API Keys) enables "
            "`boot-creds-live`, `render-service`, `provider-live`, `api-key-live`, "
            "and `--sync-env`.",
            "- `UPTIMEROBOT_API_KEY` (a read-only key) enables `uptime-pinger`.",
            "- `DATABASE_URL` enables `database` and `provider` — the provider "
            "override lives in the same database.",
        ]
    )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_gen_docs.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_docs.py tests/test_gen_docs.py
git commit -m "feat: generate the deploy-checks table from the check registry"
```

---

### Task 5: `write_all()`, the CLI, and the generated files themselves

Spec §7, §8h, §5a.

**`write_all()` is a replacing writer, deliberately.** Four things protect the content it overwrites, and all four must be in place — none is optional:

1. It writes **only** into `guide/reference/`, and only the four filenames it names itself. Confined by test.
2. Every file it writes opens with `GENERATED_HEADER`, so a human who opens one is told not to edit it.
3. The CI job (Task 6) fails on any diff, so a hand edit is caught rather than silently reverted later.
4. This plan forbids authoring any hand-written page under `guide/reference/` — the rest of the site is Stage 3b.

This is the same shape as the bug that cost Stage 2 its Critical finding (`ISSUES.md`, 2026-08-18): a replacing writer is safe only when something explicit protects what it replaces. Here that something is the four points above; do not weaken any of them.

**Files:**
- Modify: `scripts/gen_docs.py`
- Create (generated, then committed): `guide/reference/config.md`, `guide/reference/pricing.md`, `guide/reference/checks.md`, `guide/reference/sync-env.md`
- Test: `tests/test_gen_docs.py` (append)

**Interfaces:**
- Consumes: all four `render_*()` functions.
- Produces: `REFERENCE_DIR: str` (`"guide/reference"`), `GENERATED_FILES: dict[str, Callable[[], str]]`, `write_all(root: Path) -> list[Path]`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gen_docs.py (append)
from pathlib import Path


def test_write_all_creates_exactly_the_four_generated_files(tmp_path):
    written = gen_docs.write_all(tmp_path)
    assert {p.name for p in written} == {
        "config.md", "pricing.md", "checks.md", "sync-env.md"
    }
    for path in written:
        assert path.read_text(encoding="utf-8").startswith(gen_docs.GENERATED_HEADER)


def test_write_all_touches_nothing_outside_guide_reference(tmp_path):
    """The confinement guarantee. A generator that can write anywhere is one
    misplaced argument away from destroying hand-written content."""
    (tmp_path / "guide").mkdir()
    keeper = tmp_path / "guide" / "index.md"
    keeper.write_text("hand-written, must survive\n", encoding="utf-8")

    gen_docs.write_all(tmp_path)

    assert keeper.read_text(encoding="utf-8") == "hand-written, must survive\n"
    produced = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert produced == {
        "guide/index.md",
        "guide/reference/config.md",
        "guide/reference/pricing.md",
        "guide/reference/checks.md",
        "guide/reference/sync-env.md",
    }


def test_write_all_is_idempotent_byte_for_byte(tmp_path):
    """Spec section 8h. The CI drift job compares bytes, so a second run that
    differs at all -- a timestamp, a reordered set -- is a permanent red build."""
    first = {p: p.read_bytes() for p in gen_docs.write_all(tmp_path)}
    second = {p: p.read_bytes() for p in gen_docs.write_all(tmp_path)}
    assert first == second


def test_written_files_use_lf_endings(tmp_path):
    """.gitattributes pins the working tree to LF. A CRLF write on Windows
    would fail the drift check on that operator's machine and nowhere else."""
    for path in gen_docs.write_all(tmp_path):
        assert b"\r\n" not in path.read_bytes()


def test_committed_reference_files_are_up_to_date():
    """The repo's committed output must match what the code generates now --
    the same invariant CI enforces, checked here so a local run catches it."""
    root = Path(__file__).resolve().parent.parent
    for name, render in gen_docs.GENERATED_FILES.items():
        committed = (root / gen_docs.REFERENCE_DIR / name).read_text(encoding="utf-8")
        assert committed == render(), f"{name} is stale -- run scripts.gen_docs"


def test_main_writes_and_reports(tmp_path, capsys):
    assert gen_docs.main(["--root", str(tmp_path)]) == 0
    assert "config.md" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gen_docs.py -k "write_all or committed or main" -v`
Expected: FAIL with `AttributeError: module 'scripts.gen_docs' has no attribute 'write_all'`.

- [ ] **Step 3: Write the writer and CLI**

Append to `scripts/gen_docs.py` (add `argparse`, `sys`, and `from pathlib import Path` to the imports):

```python
# The only directory this script may write to, and the only files it may
# create. Both are fixed, not configurable: a generator that can be pointed
# anywhere is one wrong argument away from replacing hand-written content.
REFERENCE_DIR = "guide/reference"

GENERATED_FILES = {
    "config.md": render_config,
    "pricing.md": render_pricing,
    "checks.md": render_checks,
    "sync-env.md": render_sync_env,
}


def write_all(root: Path) -> list[Path]:
    """Write every generated file under `root`/guide/reference; return them.

    A REPLACING writer by design -- these files have no hand-written content to
    preserve. What makes that safe is that it is confined to REFERENCE_DIR and
    to GENERATED_FILES' names, every output carries a do-not-edit header, CI
    fails on drift, and no hand-authored page is ever placed in this directory.
    """
    target = root / REFERENCE_DIR
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, render in GENERATED_FILES.items():
        path = target / name
        path.write_text(render(), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate guide/reference/ from the code"
    )
    parser.add_argument("--root", default=".", help="repository root to write under")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    for path in write_all(Path(args.root)):
        print(f"wrote {path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Generate the files for real**

Run: `uv run python -m scripts.gen_docs`
Expected: four `wrote guide/reference/*.md` lines.

Then read each of the four generated files and sanity-check them as a human would: the config table lists every setting with plausible types and defaults, the pricing table's four rows carry their source links and the vertex caveat, the checks table has eleven rows, and the sync-env page's "never pushed" sections are present. **Confirm no line contains anything resembling a credential value** — this is the first time generated output becomes a committed file, so read it rather than trusting the sentinel test alone.

- [ ] **Step 5: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS, including `test_committed_reference_files_are_up_to_date`.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_docs.py tests/test_gen_docs.py guide/reference/
git commit -m "feat: write the generated reference docs and commit their first output"
```

---

### Task 6: The CI drift check

Spec §7. Regenerating and diffing is what turns "generated from the code" from a convention into a guarantee.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_ci_workflow.py`

**Interfaces:**
- Consumes: `scripts.gen_docs` (Task 5).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ci_workflow.py
"""The drift job is what makes generated docs a guarantee rather than a habit.

Asserted structurally rather than by running CI: the job must exist, must
regenerate, and must fail on a diff.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_a_docs_job_exists_alongside_lint_and_test():
    jobs = _workflow()["jobs"]
    assert "docs" in jobs
    assert "lint-and-test" in jobs, "the existing job must not be replaced"


def test_the_docs_job_regenerates_and_fails_on_drift():
    steps = _workflow()["jobs"]["docs"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)
    assert "scripts.gen_docs" in commands
    assert "git diff --exit-code" in commands
    assert "guide/reference" in commands


def test_the_docs_job_needs_no_database():
    """gen_docs reads class metadata and module constants only, so wiring a
    Postgres service to this job would be pure noise."""
    assert "services" not in _workflow()["jobs"]["docs"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ci_workflow.py -v`
Expected: FAIL with `KeyError: 'docs'`.

If `yaml` is not importable, add `pyyaml` to the dev dependency group — `tests/test_deploy_script.py` already parses `render.yaml` with it, so it should already be present.

- [ ] **Step 3: Add the job**

Append to `.github/workflows/ci.yml`, as a sibling of `lint-and-test` (same two-space indentation under `jobs:`):

```yaml
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --all-extras --dev

      # No Postgres service: gen_docs reads class metadata and module
      # constants only, never the database and never a configured value.
      - name: Regenerate reference docs
        run: uv run python -m scripts.gen_docs

      - name: Fail if the generated docs are stale
        run: git diff --exit-code guide/reference/
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_ci_workflow.py -v`
Expected: all PASS.

- [ ] **Step 5: Prove the drift check actually catches drift**

Run:
```bash
printf '\nDRIFT\n' >> guide/reference/checks.md
git diff --exit-code guide/reference/ ; echo "exit=$?"
git checkout -- guide/reference/checks.md
git diff --exit-code guide/reference/ ; echo "exit=$?"
```
Expected: `exit=1` then `exit=0`. A drift check that cannot fail is worth nothing, so demonstrate it rather than assuming it. Confirm `git status --porcelain` is empty afterwards.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml tests/test_ci_workflow.py
git commit -m "ci: fail the build when generated reference docs drift from the code"
```

---

### Task 7: Full-suite verification and stage close-out

Not a code change — the gate before Stage 3b.

- [ ] **Step 1: Run the whole suite clean**

Run: `uv run ruff check . && uv run pytest -q`
Expected: zero failures. Baseline entering the stage was **752 passing**; report the new count.

- [ ] **Step 2: Confirm generation is stable across a clean run**

Run:
```bash
uv run python -m scripts.gen_docs
git status --porcelain
```
Expected: the second command prints nothing. A non-empty result means generation is not deterministic and CI would be permanently red — stop and report rather than committing the churn.

- [ ] **Step 3: Confirm nothing outside `guide/reference/` was created**

Run: `git status --porcelain --untracked-files=all guide/`
Expected: empty. Only the four generated files should exist under `guide/` at the end of this stage; the rest of the site is Stage 3b.

- [ ] **Step 4: Report completion**

Summarise: tasks completed, test count before and after, the four generated files' sizes, confirmation that the drift check was demonstrated to fail on real drift, and any deviation from this plan with its reason.

---

## Out of Scope for Stage 3a

Everything reader-facing. Stage 3b covers it:

- **The rest of the `guide/` site** — `index.md`, `setup/01..08.md`, `operations/`, `background/`. Only `guide/reference/` exists after this stage.
- **`README.md`'s reduction** from 516 lines to ~150, and the `SETUP.md` split into `guide/setup/` plus `guide/background/`.
- **The OS-idiom doc fixes** (spec §5): `base64 -w0` → `scripts/encode_credential.py`, `curl` → `deploy.py --health-only`, the Windows-only Docker install line, and the bash/PowerShell content tabs.
- **`scripts/deploy.py:39`'s `_README_ANCHOR`**, which must become a guide URL (spec §3d). It cannot be repointed until the guide page it would target exists, so it stays as-is here.
- **MkDocs itself** — `mkdocs.yml`, the Material theme dependency, and the Pages deploy workflow. Stage 3a's output is plain Markdown that renders fine on github.com in the meantime.
- **`CLAUDE.md`'s `SETUP.md` §-references** (spec §3d), which move with the content in 3b.
- **No changes to `app/`.**
