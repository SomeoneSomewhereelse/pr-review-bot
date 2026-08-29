# Design — Unify the provider/key-index override CLIs; fix the --sync-env numbered-slot gap

**Date:** 2026-08-12
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-08-12-api-key-index-override-design.md` (the
feature this unifies), `scripts/set_provider.py` / `scripts/set_api_key.py` (superseded,
kept temporarily), `scripts/deploy.py` (`_wanted_env`, the `--sync-env` gap),
`app/providers/factory.py` (the fail-fast check), `CLAUDE.md`'s "no secret is ever
logged" rule.

## 1. Problem

Two independent gaps surfaced while provisioning `GROQ_API_KEY_1`/`GROQ_API_KEY_2` for a
live demo:

1. **`--sync-env` cannot push a numbered key slot at all.** `scripts/deploy.py`'s
   `_wanted_env()` only knows about each provider's *base* credential (via
   `registry.PROVIDERS`) — it has no logic for `BASE_1`, `BASE_2`, etc. README's
   "Swapping API keys without a redeploy" section claims a new slot can be provisioned
   "via `--sync-env` or the Render dashboard" — false for the `--sync-env` half. Provisioning
   the two slots above required a one-off manual Render API call outside any scripted path.
2. **Setting both which provider is active and which key slot it uses takes two
   separate commands** (`scripts/set_provider.py` then `scripts/set_api_key.py`), each with
   its own Render-verification round trip. The two scripts are also near-duplicates of each
   other: same argparse shape (`allow_abbrev=False`, `--clear`, `--force`), same
   "verify-against-Render, degrade-to-warning-if-unverifiable, refuse-unless---force" logic,
   same `store.init_pool()` → write → print shape. Their tests mirror each other line-for-line.

A third, related concern raised during design: **the system should fail fast and clearly
on a locally-detectable invalid configuration** (e.g. activating `gemini` at index 1 when
only `GEMINI_API_KEY` — index 0 — is provisioned anywhere), as distinct from a **live
provider failure** (e.g. GitHub Models' real retirement) — the latter is a normal, already
well-handled runtime failure mode; the former is knowable in advance without a network call
and today isn't checked until three separate specialist calls each independently discover it
via a wasted, doomed network round trip.

## 2. Decision

Four independent pieces, landing together:

1. Fix `--sync-env` to discover and push numbered slots automatically, by scanning local
   `.env` for the naming pattern already established by the feature itself.
2. Extract the shared verification/local-value logic into a new module,
   `scripts/_override.py`.
3. Build one new script, `scripts/set_override.py`, that is a complete, standalone
   replacement for everything both `scripts/set_provider.py` and `scripts/set_api_key.py` do
   today, **plus** a new combined "activate a provider and set its key index together"
   operation.
4. Add a fail-fast check in `app/providers/factory.py` for a locally-detectable missing
   credential, distinct from (and not weakening) the existing live-failure path.

**`scripts/set_provider.py` and `scripts/set_api_key.py` are explicitly temporary** —
left completely untouched by this design (not even refactored internally) because they are
already written into tonight's rehearsed demo plan and are slated for deletion once the
presentation is over. Refactoring throwaway code is wasted effort; the new script is built
standalone against the new shared module instead.

## 3. Fix 1 — `--sync-env` discovers numbered slots

**File:** `scripts/_override.py` (new)

```python
import re
from dotenv import dotenv_values

_SLOT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def local_numbered_slots(base: str, env_path: str = ".env") -> dict[str, str]:
    """Every {base}_{N} key with a non-empty value in the local .env file.

    N >= 1 only -- index 0 is the base var itself, read through Settings
    elsewhere, never through this scan. Returns {} if env_path doesn't exist
    (dotenv_values degrades gracefully) or nothing matches.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    values = dotenv_values(env_path)
    return {
        key: value
        for key, value in values.items()
        if value and pattern.match(key)
    }
```

**File:** `scripts/deploy.py`, `_wanted_env()`

After the existing "any other provider's credential is included only when it has a local
value" loop, add:

```python
for credential, _ in _PROVIDERS.values():
    for slot_name, slot_value in _override.local_numbered_slots(credential).items():
        wanted[slot_name] = slot_value
```

This resolves the README/code mismatch by **implementing** the missing behavior, not by
weakening the doc's claim — once this lands, "provisioning a slot via `--sync-env`" is
actually true. No change to `_wanted_env()`'s existing base-credential/model-var/always-synced
logic.

## 4. Fix 2 — shared module `scripts/_override.py`

Beyond `local_numbered_slots` above:

```python
def local_value(provider: str, index: int) -> str:
    """The local value for (provider, index) -- index 0 via Settings (matching
    every other index-0 read in this codebase), index >= 1 via the .env scan."""
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return getattr(settings, base.lower(), "")
    return local_numbered_slots(base).get(f"{base}_{index}", "")


def verify_render_slot(provider: str, index: int) -> tuple[bool, str]:
    """(ok_to_proceed, message). Replaces both set_provider.py's
    _verify_render_credential and set_api_key.py's _verify_render_key_slot.

    Same degrade-to-warning shape as both predecessors (no RENDER_API_KEY, no
    matching service, DATABASE_URL mismatch -- all "can't verify, proceed with
    a warning, never a hard block"). Where it differs: equality-against-the-
    local-value is now attempted for ANY index, not just 0 -- index >= 1 slots
    routinely have a real local counterpart now that Fix 1 exists, so treating
    them as "no local value to compare" (the old set_api_key.py's assumption)
    is no longer accurate. Never returns, prints, or logs a fetched Render
    value -- only presence/absence and in-memory equality results, per
    CLAUDE.md and docs/superpowers/specs/
    2026-08-10-provider-live-credential-verification-design.md section 6.
    """
    base, _ = registry.PROVIDERS[provider]
    env_name = base if index == 0 else f"{base}_{index}"
    if not settings.render_api_key:
        return True, "could not verify against Render (no RENDER_API_KEY); proceeding"
    service_id = _render.find_service_id()
    if service_id is None:
        return True, (
            f"could not verify against Render (no service named "
            f"{settings.render_service_name}); proceeding"
        )
    env_vars = _render.env_vars(service_id)
    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, "could not confirm this DATABASE_URL is the one Render reads -- skipping"
    live_value = env_vars.get(env_name) or ""
    if not live_value:
        return False, f"{env_name} is missing on the Render service"
    local = local_value(provider, index)
    if not local:
        return True, f"{env_name} present on Render (no local value to compare)"
    if live_value != local:
        return False, f"{env_name} on Render differs from your local .env value"
    return True, f"{env_name} verified on Render (matches local .env)"
```

(Exception handling around the two `_render` calls mirrors both predecessors' `except
Exception as exc: return True, "could not verify... (type)"` shape — omitted above for
brevity, kept verbatim in implementation.)

`scripts/set_provider.py` and `scripts/set_api_key.py` are **not** changed to call this —
per §2, they stay untouched. Only the new script (§5) and `deploy.py`'s sync-env fix use it.

## 5. Fix 3 — `scripts/set_override.py` (new, full replacement)

```
uv run python -m bot.scripts.set_override PROVIDER                            # activate PROVIDER only
uv run python -m bot.scripts.set_override --clear                              # clear provider override only
uv run python -m bot.scripts.set_override PROVIDER --index N --no-activate      # set PROVIDER's index only
uv run python -m bot.scripts.set_override PROVIDER --clear-index --no-activate   # clear PROVIDER's index only
uv run python -m bot.scripts.set_override PROVIDER --index N                   # activate PROVIDER + set its index, together (NEW)
uv run python -m bot.scripts.set_override PROVIDER --clear-index               # activate PROVIDER + clear its index, together
uv run python -m bot.scripts.set_override PROVIDER --index N --force           # bypass a failed live-verification refusal
```

**Mapping to the two scripts it replaces** (both remain functionally reachable, this is a
superset):

| Old invocation | New equivalent |
|---|---|
| `set_provider.py PROVIDER` | `set_override.py PROVIDER` |
| `set_provider.py --clear` | `set_override.py --clear` |
| `set_api_key.py PROVIDER N` | `set_override.py PROVIDER --index N --no-activate` |
| `set_api_key.py PROVIDER --clear` | `set_override.py PROVIDER --clear-index --no-activate` |
| *(none — new)* | `set_override.py PROVIDER --index N` |

**Validation rules:**

- `--clear` must be used alone — no `PROVIDER`, `--index`, `--clear-index`, or
  `--no-activate` may accompany it (mirrors `set_provider.py --clear`'s existing simplicity).
- `PROVIDER` is required unless `--clear` is given.
- `--index` and `--clear-index` are mutually exclusive.
- `--no-activate` requires `--index` or `--clear-index` (otherwise there is nothing to
  suppress activation of).
- `--force` is meaningful only when a write requiring verification happens; harmless
  no-op otherwise.

**Verification against the *effective* index, always** — a real gap-fix versus today's
`set_provider.py`, which unconditionally verifies index 0 regardless of any existing
key-index override for that provider (a pre-existing latent gap, not introduced here). The
new script resolves the index that will actually be active *after* this write:

```python
if args.index is not None:
    effective_index = args.index
elif args.clear_index:
    effective_index = 0
else:
    effective_index = store.get_key_index_override(args.provider) or 0
```

...then calls `_override.verify_render_slot(args.provider, effective_index)` exactly once.
This whole verification step is skipped when `--clear` is given — clearing the provider
override needs no credential check, matching `set_provider.py --clear`'s existing behavior.
Verification is also skipped when `--clear-index` is paired with `--no-activate` (added in a
later fix round), matching old `set_api_key.py PROVIDER --clear`'s behavior — nothing is being
activated and the target is the documented default, so there is no credential at stake.
`--clear-index` *without* `--no-activate` still verifies, against index 0, since a provider is
about to become active there.

**Persistence — the "simple version"** (per explicit decision: an atomic combined-column
UPSERT was considered and set aside): the script's `main()` calls the two existing granular
store functions as needed, in sequence:

```python
if args.clear:
    store.set_provider_override(None, now)
else:
    if not args.no_activate:
        store.set_provider_override(args.provider, now)
    if args.index is not None:
        store.set_key_index_override(args.provider, args.index, now)
    elif args.clear_index:
        store.set_key_index_override(args.provider, None, now)
```

The top-level `if args.clear` branch mirrors §5's own validation rule that `--clear` is used
alone — by the time this runs, `--clear` and a `PROVIDER`/`--index`/`--clear-index` can never
both be present, so the two branches are exhaustive and never overlap. No new store function. The two UPSERTs target the same singleton row but are not wrapped in
a single transaction — an acceptable, tiny window for a human-run, retry-friendly ops script
(matches this codebase's existing granularity: `set_cooldown.py` already does read-modify-write
against the same table without a single-statement guarantee across its three fields).

## 6. Fix 4 — fail-fast on a locally-detectable missing credential

**File:** `app/providers/factory.py`, `_build()`

```python
def _build(provider: str, index: int) -> LLMProvider:
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} "
            "(expected 'gemini', 'groq', or 'github_models')"
        )
    env_name, api_key = credentials.resolve(provider, index)
    if not api_key:
        raise ValueError(
            f"no credential configured for provider={provider!r} index={index} "
            f"({env_name} not set)"
        )
    if provider == "gemini":
        return GeminiProvider(api_key=api_key)
    if provider == "groq":
        return GroqProvider(api_key=api_key)
    if provider == "github_models":
        return GitHubModelsProvider(api_key=api_key)
    raise ValueError(f"registry lists {provider!r} but _build cannot construct it")
```

Caught by `run_specialist`'s existing broad `except Exception` (`app/specialists/base.py`)
— unchanged. All three specialists fail with this exact message, immediately, with **zero
network calls** — versus today, where an empty credential reaches the SDK client
construction, and the failure only surfaces after a real (doomed) network round trip per
specialist.

**Explicitly not affected:** a *dead-but-configured* provider (real credential present,
vendor down or retired — e.g. GitHub Models) is untouched by this check — `credentials.resolve()`
returns a non-empty value, `_build()` proceeds normally, and the existing live-call failure
path handles it exactly as before. This check only catches the class of problem that is
knowable without ever making a network call.

## 7. `.env.example` update

Each provider's base credential gains two commented-out numbered-sibling lines and a short
explanation of the convention, so a new developer discovers the feature by reading the file
rather than by finding this design doc:

```
# Each credential below can have numbered siblings -- BASE_1, BASE_2, ... --
# swappable at runtime with no redeploy via `scripts/set_override.py`'s
# key-index override (see README "Swapping API keys without a redeploy").
...
GEMINI_API_KEY=
# GEMINI_API_KEY_1=
# GEMINI_API_KEY_2=
```

(Already implemented ahead of the rest of this design, since it was a pure documentation
addition with no behavioral risk — same treatment for `GROQ_API_KEY` and
`GITHUB_MODELS_TOKEN`.)

## 8. Surface

- `scripts/_override.py` — new: `local_numbered_slots`, `local_value`, `verify_render_slot`.
- `scripts/deploy.py` — `_wanted_env()` gains the numbered-slot scan (§3); no other change.
- `scripts/set_override.py` — new CLI (§5).
- `app/providers/factory.py` — `_build()` gains the fail-fast check (§6).
- `.env.example` — numbered-slot examples for all three providers (§7, already done).
- `scripts/set_provider.py`, `scripts/set_api_key.py` — **untouched**, explicitly temporary.
- `README.md` — new section documenting `scripts/set_override.py`, alongside (not yet
  replacing) the existing `set_provider.py`/`set_api_key.py` sections; a note marking the
  latter two as superseded/pending removal.

## 9. Testing (deterministic-first)

- `scripts/_override.py`:
  - `local_numbered_slots`: matches `BASE_1`, `BASE_2`, ...; ignores non-matching keys;
    ignores empty-valued entries; returns `{}` for a missing/empty env file.
  - `local_value`: index 0 reads through a monkeypatched `settings` attribute; index ≥ 1
    reads through the scan (temp `.env` file fixture, using `env_path` for injection).
  - `verify_render_slot`: every branch — no `RENDER_API_KEY` (degrade), no matching service
    (degrade), `DATABASE_URL` mismatch (degrade), env var absent on Render (refuse), local
    value known and differs (refuse), local value known and matches (pass), local value
    unknown and Render has a value (pass, "no local value to compare").
- `scripts/set_override.py` (`tests/test_set_override_script.py`, mirroring the existing two
  scripts' test shape): every row of §5's mapping table, both mutual-exclusion validation
  errors, `--force`, the effective-index resolution in all three cases (explicit `--index`,
  `--clear-index` → 0, neither given → read the existing override), and a `--help` subprocess
  test (catching the `python scripts/x.py` vs `python -m bot.scripts.x` pitfall the other two
  test files already guard against).
- `scripts/deploy.py`: extend (not rewrite) the existing sync-env tests — a numbered slot
  with a local value gets pushed; one without a local value is left alone; existing
  base-credential/always-synced-var behavior is unchanged.
- `app/providers/factory.py`: one new case — `_build()` raises with the specific
  missing-env-var message when `credentials.resolve()` returns an empty value; existing
  "unknown provider" test is unaffected (ordering preserved: unknown-provider check still
  runs before the credential-presence check).
- `scripts/set_provider.py`, `scripts/set_api_key.py`: **zero test changes** — both scripts
  and their existing test files are untouched, and stay exercised by CI until deleted.

## 10. Non-goals / Open items

- No atomic single-UPSERT combined write — the "simple version" per §5, explicitly chosen
  over a fancier single-statement alternative.
- No change to `scripts/set_provider.py` or `scripts/set_api_key.py` — they are temporary,
  not touched by this design.
- **Deleting `scripts/set_provider.py`, `scripts/set_api_key.py`, their test files, and the
  demo plan doc's references to them is an explicit fast-follow after the presentation, not
  part of this design's deliverable.** Doing so tonight would touch the exact CLI the
  rehearsed demo plan depends on, hours before presenting — an accepted, deliberate deferral,
  not an oversight.
- No dashboard UI surface for any of this (matches the provider/cooldown-override precedent).
- No live LLM authentication call anywhere in this design — all verification is
  presence/equality against Render's env-var API, per `CLAUDE.md`'s testing-hygiene rule.
