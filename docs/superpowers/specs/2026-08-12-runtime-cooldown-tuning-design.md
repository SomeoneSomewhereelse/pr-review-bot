# Design — Runtime-tunable re-review cooldown & escalation factor

**Date:** 2026-08-12
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-07-31-escalating-cooldown-design.md` (the
mechanism this tunes), `app/providers/active.py` + `scripts/set_provider.py` (the
DB-override pattern this mirrors), `render.yaml` (env var declarations).

## 1. Problem

The escalating cooldown (`store.effective_cooldown`) has three parameters:

| Parameter | Today | Configurable? |
|---|---|---|
| Base cooldown | `dispatcher_rereview_cooldown_seconds` = 300s | Env var, but not declared in `render.yaml` |
| Cap | `dispatcher_rereview_cooldown_max_seconds` = 3600s | Env var, but not declared in `render.yaml` |
| Escalation factor | hardcoded `2` in `base * 2**level` | **Not configurable at all** |

For a live demo, 300s/3600s is too slow to show escalation happening on stage,
and env-var changes require a Render redeploy (~1-2 min) even once declared in
`render.yaml`. Two gaps: the factor has no knob, and there's no way to retune
any of the three without a redeploy.

## 2. Decision

Two independent layers, matching how LLM-provider swapping already works
(`app/providers/active.py` + `scripts/set_provider.py`):

1. **Env var defaults** (redeploy required) — add the missing `_FACTOR` var and
   declare all three in `render.yaml` so they're visible/editable in the Render
   dashboard.
2. **DB-backed live override** (no redeploy; takes effect on the next claimed
   ticket) — a new `scripts/set_cooldown.py`, mirroring `set_provider.py`,
   writes to the existing `runtime_config` singleton row. This is the layer
   that makes live demo tuning possible.

Chosen over env-vars-only (correct but too slow to retune live) and over a new
bespoke config mechanism (the provider-override pattern already solves this
exact problem — reuse it).

## 3. Env var layer

- `app/config.py`: add `dispatcher_rereview_cooldown_factor: float = Field(default=2.0, ge=1.0)`.
  Uses `Field(...)` the same way `dispatcher_notice_sweep_batch_size` does — a
  guarded bound that fails config validation at startup rather than silently
  accepting a value that breaks an invariant. Here, a factor `< 1` would shrink
  the cooldown over escalation levels, defeating the point of escalation.
- `store.effective_cooldown(level)`'s hardcoded `2 **` becomes `factor **`
  (factor resolved via the cache below).
- `render.yaml`: add `DISPATCHER_REREVIEW_COOLDOWN_SECONDS`,
  `DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS`, `DISPATCHER_REREVIEW_COOLDOWN_FACTOR`,
  all `sync: false`, alongside the existing LLM vars.

## 4. DB-backed override layer

### 4.1 Schema

`runtime_config` (already a singleton row, `id = 1`, holding `provider`) gains
three nullable columns, migrated the same way `tickets.last_error` was:

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_base_seconds DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_max_seconds  DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_factor       DOUBLE PRECISION;
```

`NULL` in any column = "no override, fall back to the env var" — same
semantics as `provider IS NULL` today.

### 4.2 Store functions (`app/queue/store.py`)

- `get_cooldown_overrides() -> tuple[float | None, float | None, float | None]`
  — `(base, cap, factor)`, mirroring `get_provider_override`'s single-row read.
- `set_cooldown_override(base, cap, factor, now) -> None` — upserts all three
  columns at once (same `ON CONFLICT (id) DO UPDATE` shape as
  `set_provider_override`). The caller (the script) is responsible for
  read-modify-write logic — the store function just writes what it's given,
  same division of responsibility as today.

### 4.3 Cache module (`app/queue/cooldown_config.py`, new)

Mirrors `app/providers/active.py` exactly: module-level cache, empty until the
first refresh, degrades to env-var settings on any read failure or missing
value. Also the single place the fail-safe *validation* decided in §5 lives:

```python
def effective_config() -> tuple[float, float, float]:
    """(base, cap, factor) -- the DB override where valid, else the env default."""
    base = _base if _base is not None else settings.dispatcher_rereview_cooldown_seconds
    cap = _cap if _cap is not None else settings.dispatcher_rereview_cooldown_max_seconds
    factor = _factor if _factor is not None else settings.dispatcher_rereview_cooldown_factor
    if factor < 1.0 or base > cap:
        return (
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
        )
    return base, cap, factor

def set_override_cache(base, cap, factor) -> None: ...
def reset_override_cache() -> None: ...
```

A bad row (hand-edited, or a future bug) can never produce a broken effective
cooldown — the whole override set is discarded for that read, not just the
offending field, so a bad `factor` can't silently pair with a stale `base`/`cap`.

- `store.effective_cooldown(level)` calls `cooldown_config.effective_config()`
  instead of reading `settings` directly.
- `app/queue/dispatcher.py::process_next_due` refreshes the cache once per
  claimed ticket, right next to the existing provider-override refresh, with
  the same try/except-degrade-to-None pattern:

```python
try:
    overrides = await asyncio.to_thread(store.get_cooldown_overrides)
    cooldown_config.set_override_cache(*overrides)
except Exception:  # noqa: BLE001
    logger.exception("failed to refresh cooldown override; using env defaults")
    cooldown_config.reset_override_cache()
```

### 4.4 `scripts/set_cooldown.py` (new)

Shaped like `scripts/set_provider.py`:

```
uv run python -m scripts.set_cooldown --base 30 --factor 1.5
uv run python -m scripts.set_cooldown --cap 600
uv run python -m scripts.set_cooldown --clear
```

- `--base`, `--cap`, `--factor`: any subset; each given value overwrites just
  that field, unspecified fields keep their current override (read-modify-write:
  read `get_cooldown_overrides()`, apply the given flags, write all three back).
- `--clear`: resets all three to `None` in one call (matches `set_provider.py`'s
  single `--clear`) — re-set individual fields afterward with `--base`/etc. if
  only one should reset.
- `--factor` values `< 1` are rejected client-side with a clear error (the
  write-time guard), independent of the read-time fail-safe in §4.3 which
  covers rows that got bad some other way.
- Before writing, reuses the same live-verification shape as
  `set_provider.py._verify_render_credential`: if `RENDER_API_KEY` is set and
  the local `DATABASE_URL` matches the live Render service's, proceeds; if it
  can't confirm (no key, no matching service, mismatched `DATABASE_URL`),
  degrades to a warning and proceeds anyway — never a hard block, since there's
  no credential correctness at stake here, only "are you sure this write reaches
  production." No `--force` needed since nothing here is ever refused, only
  warned about.
- Prints old → new for whichever fields changed.

## 5. Surface

- `app/config.py` — new `dispatcher_rereview_cooldown_factor` field.
- `app/queue/store.py` — `_SCHEMA` migration (3 columns), `get_cooldown_overrides`,
  `set_cooldown_override`, `effective_cooldown` calls `cooldown_config.effective_config()`.
- `app/queue/cooldown_config.py` — new, mirrors `app/providers/active.py`.
- `app/queue/dispatcher.py` — refresh cooldown cache once per claimed ticket.
- `scripts/set_cooldown.py` — new CLI.
- `render.yaml` — 3 new `sync: false` entries.
- `SPEC.md` — document the factor param + the two-layer override story.

## 6. Testing (deterministic-first)

- `cooldown_config.effective_config()`: no override → env defaults; partial
  override → mixed; invalid override (`factor < 1`, `base > cap`) → falls back
  to env defaults entirely, not a partial mix.
- `store.get_cooldown_overrides` / `set_cooldown_override`: round-trip; schema
  migration adds columns to a pre-existing table (mirrors the `last_error`
  migration test).
- `store.effective_cooldown`: factor other than 2 changes the escalation curve
  correctly; still capped at `cap`; still floors at `base` for level 0.
- Dispatcher: cache refreshed once per claimed ticket, not per idle tick (mirrors
  the existing provider-override test); a refresh failure degrades to env
  defaults, never raises into the review path.
- `scripts/set_cooldown.py`: partial-flag read-modify-write; `--clear`; rejects
  `--factor < 1`; Render-verification warning path (mirrors `test_set_provider_script.py`).

## 7. Non-goals

- No dashboard UI surface for the override (script-only, matching provider
  override's current state — no dashboard exposure either).
- No per-field `--clear-base`/`--clear-cap`/`--clear-factor` — one `--clear`
  for all three, matching `set_provider.py`.
- No change to escalation *semantics* (still silent, still never abandons a
  churning PR, still resets on quiet) — only the three numbers feeding the
  existing formula become tunable.
