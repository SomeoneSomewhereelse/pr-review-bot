# Design — Runtime-swappable API keys (no secret leaves env vars)

**Date:** 2026-08-12
**Status:** Approved for planning
**Relates to:** `app/providers/active.py` + `scripts/set_provider.py` (the
DB-override pattern this mirrors), `docs/superpowers/specs/2026-08-12-runtime-cooldown-tuning-design.md`
(the second application of the same pattern), `app/providers/factory.py`
(client cache — the one place this feature needs new behavior beyond the
existing pattern), `scripts/deploy.py`'s `_PROVIDERS` / `check_provider_live`,
`CLAUDE.md`'s "secrets only via env vars; no secret is ever logged" rule
(this design's storage choice exists specifically to keep that rule intact).

## 1. Problem

Each provider's API key is a single env var (`GEMINI_API_KEY`, `GROQ_API_KEY`,
`GITHUB_MODELS_TOKEN`), read once into the `Settings` singleton and baked into
a provider client cached for the process lifetime
(`app/providers/factory.py`). Rotating a key — because it's rate-limited,
revoked, or being swapped for a demo — today means editing the env var on
Render, which restarts the service. That's the same class of problem the
provider and cooldown overrides already solved for *which provider* and *how
long to wait*; nothing yet solves it for *which credential*.

A naive mirror of the existing pattern would store the key value itself in
the `runtime_config` Postgres row. That works mechanically but is a real
regression against `CLAUDE.md`'s "secrets only via env vars" rule — it would
make Supabase DB access equivalent to key access, for no offsetting benefit.

## 2. Decision

Store an **index**, not a secret. Each provider's credential env var gets
numbered siblings, provisioned ahead of time on Render exactly like any other
env var (one redeploy to add a slot):

```
GROQ_API_KEY      # index 0
GROQ_API_KEY_1    # index 1
GROQ_API_KEY_2    # index 2
```

`runtime_config` gains one nullable integer column per provider recording
*which index is active*. Swapping keys becomes flipping that integer — same
no-redeploy, "takes effect on the next claimed ticket" mechanics as the
provider and cooldown overrides, and no secret value is ever written to,
read from, or logged by the database layer.

This buys back exactly the redeploy the provider/cooldown overrides already
buy back for their own dimensions — and no more: **provisioning a slot for
the first time still needs a redeploy** (to add the new env var to Render).
This feature only removes the redeploy for switching among slots that are
already deployed.

## 3. Shared provider registry (prerequisite refactor)

Resolving `GROQ_API_KEY` → base name requires the provider→credential-env-var
mapping. That mapping exists today as `_PROVIDERS` inside `scripts/deploy.py`,
whose own docstring calls it "the single source of truth... so a provider
cannot be known to one and unknown to another." App-side code (the factory,
building a client at runtime) now needs the same mapping, and `app/` must not
import from `scripts/` (dependency direction runs the other way everywhere
else in this codebase).

Move the mapping into `app/providers/registry.py`:

```python
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
}
```

`scripts/deploy.py` and `scripts/set_provider.py` change their one import
(`from app.providers.registry import PROVIDERS as _PROVIDERS`) and are
otherwise untouched — every existing call site (`_PROVIDERS[provider]`,
`_PROVIDERS.get(provider)`, `sorted(_PROVIDERS)`) keeps working unchanged.
This is the only touch to pre-existing code the feature requires, and it's
required by the codebase's own single-source-of-truth invariant, not
optional cleanup.

## 4. Resolving the active credential

New `app/providers/credentials.py`:

```python
def resolve(provider: str, index: int) -> tuple[str, str]:
    """(env_var_name, value) for `provider` at `index`. Reads os.environ
    directly -- Settings can't declare an unbounded family of numbered
    fields, and Render env vars don't change within a process lifetime
    (a redeploy that adds/edits one restarts the process anyway), so a
    plain os.environ.get at resolve-time is equivalent to reading it at
    startup, with no need to guess an upper bound on how many slots exist."""
    base, _ = registry.PROVIDERS[provider]
    env_name = base if index == 0 else f"{base}_{index}"
    return env_name, os.environ.get(env_name, "")
```

An empty `value` (slot not provisioned) is not specially handled — the
provider client gets built with an empty credential exactly as it does today
when the base env var is unset, and the failure surfaces at call time as a
failed specialist row, per the existing `asyncio.gather(...,
return_exceptions=True)` / "partial failure always visible" convention. No
new error-handling path is needed; this is the same failure shape the
codebase already has for a missing base key.

## 5. DB-backed override layer

### 5.1 Schema

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS gemini_key_index INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS groq_key_index INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS github_models_key_index INTEGER;
```

`NULL` = index 0 (base env var) — zero migration risk for existing rows,
same semantics as `provider IS NULL` today. Each provider gets its own
column (not one shared "current index") so switching *which provider* is
active never disturbs the key slot chosen for the other two.

### 5.2 Store functions (`app/queue/store.py`)

```python
_KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "github_models": "github_models_key_index",
}

def get_key_index_override(provider: str) -> int | None: ...
def set_key_index_override(provider: str, index: int | None, now: str) -> None: ...
```

The column name is looked up through `_KEY_INDEX_COLUMNS`, never
interpolated from the caller's `provider` string directly — psycopg can
parameterize values but not column identifiers, so the whitelist dict *is*
the injection guard, not an optimization. `index < 0` is rejected the same
way a bad cooldown factor is: defensively, at read time, in the cache layer
below (§5.3), independent of whatever wrote it.

### 5.3 Cache module (`app/providers/key_index.py`, new)

Mirrors `app/providers/active.py` exactly, generalized from one provider to
per-provider dict:

```python
_overrides: dict[str, int] = {}

def active_key_index(provider: str) -> int:
    value = _overrides.get(provider)
    return value if value is not None and value >= 0 else 0

def set_override_cache(overrides: dict[str, int]) -> None:
    global _overrides
    _overrides = overrides

def reset_override_cache() -> None:
    set_override_cache({})
```

Fail-safe by construction, same as every other override cache here: empty
dict (before first refresh, or after a failed refresh) degrades every
provider to index 0, never to a crash or a stale value.

### 5.4 Dispatcher refresh (`app/queue/dispatcher.py::process_next_due`)

Same cadence and shape as the existing provider/cooldown refreshes — once
per claimed ticket, degrade-on-exception:

```python
try:
    overrides = await asyncio.to_thread(store.get_all_key_index_overrides)
    key_index.set_override_cache(overrides)
except Exception:  # noqa: BLE001
    logger.exception("failed to refresh key-index overrides; using index 0")
    key_index.reset_override_cache()
```

(`get_all_key_index_overrides` reads all three columns in the one existing
singleton-row query and returns `{provider: index}` for non-null columns
only — one query, not three.)

## 6. Client cache invalidation (`app/providers/factory.py`)

The one place this feature needs behavior the provider/cooldown overrides
never had to have: `_instances` is keyed only by provider name, and a
provider's SDK client is built once with its credential baked in
(`GeminiProvider.__init__` reads `settings.gemini_api_key` a single time).
Swapping the active index for the currently-active provider must not keep
serving requests through the old client.

Fix: key the cache by `(provider, index)`:

```python
_instances: dict[tuple[str, int], LLMProvider] = {}

def _build(provider: str, index: int) -> LLMProvider:
    env_name, value = credentials.resolve(provider, index)
    if provider == "gemini":
        return GeminiProvider(api_key=value)
    if provider == "groq":
        return GroqProvider(api_key=value)
    if provider == "github_models":
        return GitHubModelsProvider(api_key=value)
    raise ValueError(f"Unknown provider: {provider!r}")

def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    cache_key = (provider, index)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index)
    return _instances[cache_key]
```

Each provider constructor (`GeminiProvider.__init__`, `GroqProvider.__init__`,
`GitHubModelsProvider.__init__`) gains an `api_key: str` parameter, replacing
its internal `settings.<x>_api_key` read — the value is resolved once, in
`factory._build`, and passed down, so the provider classes stay ignorant of
`Settings`/env vars/indices entirely (matches their existing "know nothing
about GitHub" narrowness, extended to "know nothing about credential
resolution either"). A swap becomes a cache miss on the new `(provider,
index)` tuple; the stale entry for the old index is simply never looked up
again — provider adapters hold no per-call mutable state (existing
invariant), so an unused cached client costs a little memory and nothing
else.

## 7. CLI: `scripts/set_api_key.py` (new)

Shaped exactly like `scripts/set_provider.py`:

```
uv run python -m scripts.set_api_key groq 2
uv run python -m scripts.set_api_key groq --clear   # back to index 0
```

- Positional `provider` (one of the three names) + non-negative integer
  index, or `--clear` to null the override for that provider.
- **Presence-only** live verification before writing (per explicit decision —
  no live LLM call, staying inside `CLAUDE.md`'s testing-hygiene rule):
  resolve the target env var name for `(provider, index)`, then check via
  `scripts/_render.py`'s `find_service_id()` / `env_vars()` that it exists
  and is non-empty on the live Render service. Unlike
  `set_provider.py`'s `_verify_render_credential`, this does **not** compare
  against a local `.env` value — a numbered slot typically has no local
  counterpart at all, so the only meaningful check is "does this slot exist
  on the service that will actually use it."
- Same refusal shape: missing/empty on Render → refuse unless `--force`;
  can't verify (no `RENDER_API_KEY`, no matching service) → proceed with a
  warning, never a hard block, matching `set_provider.py`'s degrade rule for
  "can't verify" vs. "verified and it's wrong."
- `--force` and no live-LLM-call requirement mean this script itself never
  makes a network call to Gemini/Groq/GitHub Models — only to Render's API.

## 8. Deploy-time check (`scripts/deploy.py`)

New `check_api_key_live`, mirroring `check_provider_live`: resolve the
*currently active* provider (DB override or env) and its *currently active*
key index (DB override or 0), compute the expected env var name, and confirm
it's present on the live Render service. This is the check that catches "the
DB says index 2 but nobody ever pushed `GROQ_API_KEY_2` to Render" — the
exact failure mode a redeploy-free index flip can introduce that a redeploy
would otherwise have caught via `--sync-env`. Only the active provider's
active index is checked (not all three providers × all provisioned slots),
matching `check_provider_live`'s existing scope of checking what's actually
in force, not everything that could theoretically be selected.

## 9. Surface

- `app/providers/registry.py` — new; `PROVIDERS` moved from `scripts/deploy.py`.
- `app/providers/credentials.py` — new; `resolve(provider, index)`.
- `app/providers/key_index.py` — new; mirrors `active.py`.
- `app/providers/factory.py` — cache key becomes `(provider, index)`;
  `_build` takes `index`, resolves the credential, passes it to the
  provider constructor.
- `app/providers/google_genai.py`, `groq.py`, `github_models.py` — each
  constructor's credential becomes an `api_key: str` parameter (resolved by
  the factory and passed in) instead of a `settings.<x>_api_key` read; the
  model attribute is unchanged, still read from `settings` inside the
  constructor.
- `app/queue/store.py` — 3-column migration; `get_key_index_override`,
  `set_key_index_override`, `get_all_key_index_overrides`.
- `app/queue/dispatcher.py` — refresh key-index cache once per claimed
  ticket, alongside the existing two refreshes.
- `scripts/set_api_key.py` — new CLI.
- `scripts/deploy.py` — import `PROVIDERS` from the new registry module
  instead of defining `_PROVIDERS` locally; new `check_api_key_live`.
- `SPEC.md` — extend the `runtime_config` override section to cover the
  three key-index columns alongside `provider` and the cooldown triple.
- `README.md` — new "Swapping API keys without a redeploy" section,
  mirroring the existing provider/cooldown sections, documenting the
  numbering convention and that adding a new slot still needs one redeploy.

## 10. Testing (deterministic-first)

- `app/providers/credentials.py::resolve` — index 0 → base env name;
  index ≥ 1 → suffixed name; missing env var → empty string, not an
  exception.
- `app/providers/key_index.py` — cache starts empty (index 0 for any
  provider); set/reset; a negative cached value defensively reads as 0.
- `app/queue/store.py` — round-trip get/set per provider column; singleton
  row semantics (mirrors `test_provider_override.py`'s constraint tests);
  schema migration adds the three columns to a pre-existing table.
- `app/queue/dispatcher.py` — key-index cache refreshed once per claimed
  ticket, not per idle tick; a refresh failure degrades to index 0 for
  every provider, never raises into the review path.
- `app/providers/factory.py` — **the genuinely new behavior**: calling
  `get_provider()` after `key_index.set_override_cache({"groq": 1})`
  returns a different instance than before the override, and a second call
  with the same override returns the same (cached) instance; switching back
  to index 0 returns the original index-0 instance rather than rebuilding
  it (still cached under its own key).
- `scripts/set_api_key.py` — mirrors `test_set_provider_script.py`:
  `--clear`, `--force`, the presence-only Render-verification refusal path,
  and the "can't verify, proceed with warning" path.
- `scripts/deploy.py::check_api_key_live` — mirrors the existing
  `check_provider_live` tests: PASS when the resolved slot is present on
  Render, FAIL when absent, SKIPPED when `RENDER_API_KEY` is unset or the
  active provider/index can't be resolved.

## 11. Non-goals

- No secret value is ever stored in, read from, or logged by Postgres —
  the override is an integer, full stop.
- No HTTP admin endpoint — CLI-only, matching the provider/cooldown
  precedent (no dashboard exposure either, per that same precedent).
- No live LLM authentication call as part of verification — presence-only,
  per `CLAUDE.md`'s "never loop/burst live calls... one deliberate call per
  real verification need" rule.
- No mechanism to provision a new numbered slot without a redeploy — this
  feature removes the redeploy for *switching among* already-provisioned
  slots, not for creating new ones.
- No automatic key rotation, expiry tracking, or scheduling — a human runs
  `scripts/set_api_key.py` when they decide to swap, exactly like
  `scripts/set_provider.py` today.
