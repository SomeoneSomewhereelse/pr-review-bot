# Design — Validate `.env.config` model values against the pricing table

**Date:** 2026-08-16
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-08-15-operational-config-split-design.md` §9's
open item (this design closes it), `app/providers/pricing.py` (`is_known`, `models_for`),
`scripts/deploy.py` (`check_config`, `sync_env`, `_PROVIDERS`), `scripts/set_override.py`
(`--model`'s existing pricing refusal, the precedent this mirrors), `app/orchestrator.py`
(`estimate_cost_usd` call, the failure this prevents), `app/CLAUDE.md` (adapter/model
contracts, unaffected by this change).

## 1. Problem

`LLM_MODEL`, `GROQ_MODEL`, and `VERTEX_MODEL` are all `OPERATIONAL_KEYS` — freely
hand-editable in `.env.config` by an agent or operator, with no redeploy required to take
effect via `--sync-env`. `scripts/set_override.py --model` validates a DB-override model
against `app/providers/pricing.py`'s rate table and refuses an unpriced value (unless
`--force`). A value set directly in `.env.config` gets no such check: it reaches an
uncaught `KeyError` at `app/orchestrator.py`'s `estimate_cost_usd()` call — which runs
*after* the fan-out to all three specialists completes, not before it. One bad
`.env.config` edit can burn real, paid LLM calls on every PR review until someone notices
the exception in logs and fixes it, and `dispatcher.py`'s retry behavior means a single bad
model can repeat this across every retry attempt for a stuck ticket.

## 2. Decision

Two changes to `scripts/deploy.py`, both operating over all three `(provider, model_var)`
pairs from `registry.PROVIDERS` — not just the currently-active provider's:

1. **`check_config()` gains a diagnostic.** An unpriced model in any of the three vars
   becomes a `problems` entry (existing `FAIL`-row mechanism), naming the offending var,
   its value, and the known-good models for that provider.
2. **`sync_env()` gains an unconditional refusal.** Before any push, the same three-pair
   loop calls `pricing.is_known()`; the first unpriced one aborts with exit 2 and a message
   shaped like `set_override.py`'s, before any HTTP request — symmetric with the existing
   empty-value guard and the DB-override-disagreement guards already there.

**All three model vars, not just the active provider's.** This mirrors `_wanted_env()`'s
already-decided reasoning (§5 of the operational-config-split design): a DB provider
override can activate any of the three providers with no redeploy, so an unpriced value
sitting in a currently-*inactive* provider's model var is a live landmine, not a
harmless one — exactly the class of gap `_wanted_env()` was already changed to close for
model-var propagation itself.

**No `--force` on `sync_env()`'s refusal**, unlike `set_override.py --model`. That command
is a deliberate, interactive, one-shot operator action where `--force` is a considered
override. `sync_env()` is the automated push path (`deploy.py --sync-env`); forcing an
unpriced model past it is exactly the mistake this design exists to prevent. The correct
unblock is one of: fix `.env.config` to a known model, or add the new model's rate to
`pricing.py` first (the right move if it's a genuinely new, real model) — both already
possible today, neither requires new machinery.

## 3. Implementation shape

### 3.1 `check_config()`

Add a loop, after the existing provider/credential check, over `registry.PROVIDERS.items()`:

```python
for provider, (_credential, model_var) in _PROVIDERS.items():
    model = getattr(settings, model_var.lower(), "")
    if model and not pricing.is_known(provider, model):
        known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
        problems.append(
            f"{model_var}={model!r} has no pricing-table entry for {provider} "
            f"(known: {known})"
        )
```

Empty `model` is skipped deliberately — an empty model var is a distinct, pre-existing
failure mode (falls through to whatever downstream code does with an empty string) and
piling a second, confusing message onto it adds noise, not clarity. In practice this never
fires today: every `Settings` model field carries a non-empty, known-priced default.

This adds to `problems`, not `missing` — the var has a value, it's simply not one the
pricing table recognizes. Existing `detail_lines` assembly (`missing` then `problems`)
needs no change.

### 3.2 `sync_env()`

New guard, placed immediately after the `if settings.database_url:` block that holds the
provider-override and model-override-disagreement refusals, and before
`wanted = _wanted_env()`. Unlike that block, this new guard is **not** conditioned on
`settings.database_url` — it is a pure local pricing-table lookup, so it must run
regardless of whether a database is configured at all:

```python
for provider, (_credential, model_var) in _PROVIDERS.items():
    model = getattr(settings, model_var.lower(), "")
    if model and not pricing.is_known(provider, model):
        known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
        print(
            f"refusing to sync: {model_var}={model!r} has no pricing-table entry for "
            f"{provider} (known: {known}); fix .env.config or add a pricing.py entry first",
            file=sys.stderr,
        )
        return 2
```

Exit 2 (not 1): nothing has been pushed yet at this point in the function, consistent with
every other pre-push guard in `sync_env()` — this is "could not run", not a partial
failure.

### 3.3 No changes needed to `app/providers/pricing.py`

`is_known()` and `models_for()` already exist with exactly this contract (their docstrings
already name `set_override.py --model` as the reason they exist — this design is the second
caller they were always meant to serve).

## 4. Testing

New cases in `tests/test_deploy_script.py`, following the existing naming and fixture
conventions:

- `test_check_config_fails_on_an_unpriced_model` (parametrized or repeated per var: LLM_MODEL,
  GROQ_MODEL, VERTEX_MODEL) — value not in `pricing._RATES`, expect a `FAIL` row naming the
  var, value, and known models.
- `test_check_config_ignores_default_models` — confirms the three shipped defaults never
  trip this (regression guard against a future default/pricing-table drift).
- `test_check_config_reports_an_unpriced_model_alongside_other_missing_keys` — combined with
  an existing `missing`-producing case, mirroring
  `test_check_config_reports_a_bad_provider_alongside_other_missing_keys`.
- `test_sync_env_refuses_to_push_an_unpriced_model` (per var) — expect exit 2, stderr names
  the var/value/known models, and confirms no HTTP call was made (mirrors the assertion shape
  in `test_sync_env_refuses_when_a_model_override_disagrees`).
- `test_sync_env_refuses_on_a_non_active_providers_unpriced_model` — unpriced value sitting
  in an *inactive* provider's model var still blocks the push (mirrors
  `test_sync_env_refuses_when_a_non_active_providers_model_override_disagrees`).
- `test_sync_env_allows_a_priced_model` — sanity check the guard doesn't false-positive on
  the sync-ready fixture's defaults.

No live calls: `pricing.is_known()` is pure dict lookup, fully deterministic and mockable,
consistent with `SPEC.md` §8 and `CLAUDE.md`'s LLM-testing-hygiene rules — this feature
makes zero LLM calls itself.

## 5. Docs

- `docs/superpowers/specs/2026-08-15-operational-config-split-design.md` §9's open item gets
  a one-line addendum pointing at this doc as its resolution (no content there needs to
  change otherwise — it already correctly describes the gap).
- `README.md`'s `deploy` CLI section: one line noting `check_config`/`sync_env` now validate
  `.env.config` model values against the pricing table, alongside the existing description
  of the other checks.

## 6. Non-goals

- **A `--force` bypass for `sync_env()`** — decided against above; not reopened by a plan.
- **Runtime (app-process) validation of these settings at boot.** Out of scope: the
  operational-config-split design deliberately keeps its own placement-guard enforcement
  test-time, not runtime, so a drifted local file never bricks the deployed service. This
  design follows the same principle — enforcement lives in the deploy-time CLI checks an
  operator/agent actually runs before a push, not in `app/config.py` or service startup.
- **Auto-updating `pricing.py` from a live provider catalog.** `pricing.py`'s existing
  comments already document that rates are pinned, representative values verified manually
  at build time; this design does not change that maintenance model, only adds a consumer
  of `is_known()`.
- **Validating an active DB model override against the pricing table.** Identified at final
  review (2026-08-16), named as a known residual rather than fixed: `check_config()`'s
  pricing check reads `Settings`/`.env.config` only, matching this design's stated scope
  (§2, "a value set directly in `.env.config`"). An operator who forces an unpriced value
  past `set_override.py --model --force` produces a runtime-active unpriced model that
  `check_config` still reports `PASS` for — `check_config()` never resolves the DB override
  the way its sibling `check_provider()` does. The push path is not fully exposed —
  `sync_env()`'s pre-existing model-override-disagreement guard still blocks pushing a
  different, priced local value once such an override is active — but `check_config`'s
  local report can be misleadingly green for this one operator-opt-in case. Closing it would
  mean teaching `check_config()` to resolve the DB override before its pricing check, the
  same way `check_provider()` already does — a larger, separate change than this design's
  file-edit-path scope.

  **Resolved by** `docs/superpowers/specs/2026-08-16-credential-convention-design.md` §2e,
  which teaches `check_config()` to resolve `_resolved_model_overrides()` before checking
  pricing — closing this residual as part of that design's implementation.
