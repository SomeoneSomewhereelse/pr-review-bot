# Live provider-credential verification — design

**Date:** 2026-08-10
**Status:** Design approved; not yet planned or implemented.
**Extends:** `docs/superpowers/specs/2026-08-08-provider-agnostic-config-and-deploy-hardening-design.md`
§3.5 (the `provider` check this work sits next to) and §2.4's `_PROVIDERS` table
(the single source of truth this work reads, unchanged).
**Relates to:** `docs/2026-08-10-deploy-provider-credential-verification-gap.md`
(the handoff that surfaced this gap live, during a demo rehearsal).

## 1. Problem

`scripts/deploy.py`'s `provider` check resolves the actively-running provider
(DB override, or `LLM_PROVIDER`) and confirms its credential is set — but only
in the **local** `.env`. Nothing in the CLI asks the live Render service what
its environment actually contains. A DB override naming a provider whose
credential was never pushed reports a clean `provider PASS` and then fails
every real review with `"No API key was provided."` — confirmed live during a
demo rehearsal (`docs/2026-08-10-deploy-provider-credential-verification-gap.md`).

The same gap exists one step earlier: `scripts/set_provider.py` writes the
override itself and has no way to know whether the provider it just selected
even has a credential on the live service — it is a pure write today, and its
own docstring says exactly that ("cannot know whether that provider's
credential exists on the deployed service").

Both halves are closable with the same primitive: `GET
/v1/services/{id}/env-vars` via the existing `RENDER_API_KEY`, already used by
`render-service` and `--sync-env`.

## 2. Decision 1 — one new check, unified across both resolution paths

A new eighth check, `provider-live`, added to `run_checks()` immediately after
`provider` (same concept, extended to "is it live") and before
`render-service`.

It answers **both** open scope questions from the handoff doc with one
implementation, because provider resolution already falls back to
`settings.llm_provider` when no override exists:

- DB override active, credential never pushed → caught (the case that was hit
  live).
- No override, plain `LLM_PROVIDER`, credential never pushed → also caught —
  this is the "should `config` eventually get the same treatment" question
  from the handoff doc, answered by resolving it here rather than deferring
  it, since the same resolved-provider value covers it for free.

### 2.1 `_resolved_provider_or_env()`

```python
def _resolved_provider_or_env() -> tuple[str, str | None]:
    """Like _resolved_provider(), but usable without DATABASE_URL: without a
    database there is no override to check, so this falls back to the
    env-configured provider instead of requiring a connection.
    """
    if not settings.database_url:
        return settings.llm_provider, None
    return _resolved_provider()
```

`check_provider()` keeps using `_resolved_provider()` directly and keeps
SKIPping without `DATABASE_URL` — its whole purpose is override detection, and
that is unchanged. `provider-live` uses the new wrapper because its purpose is
broader: is *whatever is actually running* backed by a real credential,
override or not.

### 2.2 `check_provider_live()`

```
provider-live  SKIPPED  set RENDER_API_KEY to verify credentials against the live service
provider-live  PASS     groq (env) -- GROQ_API_KEY present on Render
provider-live  FAIL     gemini (DB override; env=groq) -- GEMINI_API_KEY not present on Render
```

- `SKIPPED` — no `RENDER_API_KEY`. Matches the existing convention (absence
  degrades to SKIPPED, never an error).
- `SKIPPED` — `_resolved_provider_or_env()` raises (a DB error while resolving
  the override) — mirrors `check_provider`'s own handling of the same failure.
- Otherwise: find the service, fetch its env-vars via `_render_env_vars()`
  (§3), and check the resolved provider's credential key for presence and
  non-emptiness only. The fetched value itself is discarded the instant that
  boolean is computed — it is never assigned to a variable that outlives the
  check, never printed, never part of `CheckResult.detail`.
- `FAIL` names the provider, its source (`env` or `DB override; env=X`, same
  format as `provider`), and the missing credential.
- `PASS` names the same, confirming presence.

No `--sync-env` auto-suggestion beyond naming it in the FAIL detail — matches
`render-service`'s existing style (`"push, or re-run --sync-env, to deploy
what you have"`), not an automated nudge.

## 3. Decision 2 — extract `_render_env_vars()`, reuse it three ways

`sync_env()` already fetches and unwraps the service's env-vars into a
`{key: value}` dict to compute what changed. Extract that block verbatim into:

```python
def _render_env_vars(service_id: str) -> dict[str, str]:
    resp = httpx.get(
        f"{_RENDER_API}/services/{service_id}/env-vars",
        headers=_render_headers(), timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return {
        (env_var := _unwrap(item, "envVar")).get("key"): env_var.get("value")
        for item in resp.json()
    }
```

`sync_env()` calls it instead of inlining the fetch — pure refactor, no
behavior change, one fewer copy of the unwrap logic. `check_provider_live()`
and `set_provider.py`'s new check (§4) both call it too. All three callers
hold the returned values only long enough to compute a boolean or an equality
check; none of them print or log a value. This is the same discipline
`sync_env()` already follows today (`pushed {key} (len {N})` — never the
value) — nothing new is introduced by holding a secret transiently in memory
for comparison, only by what happens to it afterward.

## 4. Decision 3 — `set_provider.py` verifies before it writes

Today `set_provider.py` is a pure write: validate the name, write the row,
done. It gains a pre-write check that can refuse.

### 4.1 `_verify_render_credential(provider: str) -> tuple[bool, str]`

Returns `(ok_to_proceed, message)`. Never returns, prints, or logs a fetched
value — only presence/absence and in-memory equality results.

```python
def _verify_render_credential(provider: str) -> tuple[bool, str]:
    if not settings.render_api_key:
        return True, ("could not verify against Render (no RENDER_API_KEY); "
                       "setting override without live verification")
    try:
        service_id = _find_render_service_id()
        if service_id is None:
            return True, (f"could not verify against Render (no service named "
                           f"{settings.render_service_name}); setting override "
                           "without live verification")
        env_vars = _render_env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return True, (f"could not verify against Render ({type(exc).__name__}); "
                       "setting override without live verification")

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, ("local DATABASE_URL does not match the Render service's; "
                       "this override has no effect on production -- skipping "
                       "live verification")

    credential, _ = _PROVIDERS[provider]
    live_value = env_vars.get(credential) or ""
    local_value = getattr(settings, credential.lower(), "")
    if not live_value:
        return False, (f"{credential} is missing on the Render service; the "
                        "override would fail every review immediately. Push it "
                        "first (uv run python -m scripts.deploy --sync-env) or "
                        "pass --force")
    if not local_value:
        return True, f"{credential} present on Render (no local value to compare)"
    if live_value != local_value:
        return False, (f"{credential} on Render differs from your local .env "
                        "value; the running service may use an unexpected key. "
                        "Sync first, or pass --force")
    return True, f"{credential} verified on Render (matches local .env)"
```

| condition | result |
| --- | --- |
| no `RENDER_API_KEY` | inability to verify — proceed with a warning |
| Render API/service-lookup error | inability to verify — proceed with a warning |
| local `DATABASE_URL` does not match Render's live `DATABASE_URL` | **not applicable** — this write cannot affect production, so verification is skipped outright (no warning tone; this is the expected shape of local testing, not a degraded state) |
| credential missing/empty on Render | real problem found — refuse by default |
| credential present, no local value to compare | fine — nothing to compare against |
| credential present, differs from local `.env` | real problem found — refuse by default |
| credential present, matches | verified — proceed |

The `DATABASE_URL` comparison costs no extra request: `_render_env_vars()`
already returns the service's *entire* env-var set, `DATABASE_URL` included,
so this reuses the one fetch that the credential check needs anyway. This is
what makes §5's "having a non-empty `RENDER_API_KEY` should not blindly use
it if not necessary" requirement possible without a second round trip.

**Only `set_provider.py` gets this DB-match gate.** `check_provider_live()` in
`deploy.py` (§2) does not — it is a read-only report, and `check_provider()`
already resolves an override from "whatever `DATABASE_URL` currently is"
without distinguishing local from production (an accepted property of the
existing, previously-shipped design, per the linked 2026-08-08 spec §3.5).
Gating the *write* guard here is what matters: a report row that's slightly
off when pointed at a local dev database is informational noise; a refused
write that blocks legitimate local testing is friction. `set_provider.py`
is where the friction lives, so that's where the fix goes.

### 4.2 `main()` changes

```
uv run python -m scripts.set_provider gemini
  # refuses, exit 2, if _verify_render_credential returns (False, ...)

uv run python -m scripts.set_provider gemini --force
  # prints the same message, then "proceeding anyway (--force)", writes the override
```

`--clear` skips `_verify_render_credential()` entirely — clearing an override
cannot cause the failure mode this exists to catch.

New `--force` flag on the parser (`allow_abbrev=False` already guards against
an accidental partial match, same as `--clear`).

## 5. Why `RENDER_API_KEY` stays optional here

Considered making it mandatory for every override write, so live verification
could never be silently skipped. Checked what that would break:

- `SETUP.md` §3.6 explicitly documents running `set_provider.py` against a
  **local** `DATABASE_URL` for local testing, with nothing else required
  ("nothing reaches production"). Mandating `RENDER_API_KEY` would force that
  workflow to configure a key it has no use for.
- Every existing test in `tests/test_set_provider_script.py` runs with no
  `RENDER_API_KEY` set. Mandating it would break the entire existing suite,
  not just require additions.

So it stays optional, degrading to a warning — consistent with `RENDER_API_KEY`,
`UPTIMEROBOT_API_KEY`, and `DATABASE_URL` all behaving the same way elsewhere
in this CLI.

**The local-testing edge case from the first draft of this design is closed,
not merely accepted:** an operator with `RENDER_API_KEY` set globally but
intentionally pointing `DATABASE_URL` at a local database for testing no
longer risks a refusal. `_verify_render_credential()` (§4.1) compares the
local `DATABASE_URL` against Render's live one — reusing the same env-var
fetch the credential check needs anyway, no extra request — and skips
verification outright when they differ, since a write to a different database
cannot affect production regardless of what Render's credentials look like. A
non-empty `RENDER_API_KEY` is therefore never used unnecessarily: it is
consulted only when the write is actually going to affect the service that
key belongs to.

## 6. Secrets hygiene invariant

Restated because it governs every line of this design, per the handoff doc's
explicit flag (a real "no secret is ever logged" violation happened live
during the session that produced it):

**A fetched Render env-var value is held in memory only long enough to
compute a boolean (presence/non-emptiness) or an equality check against a
local value. It is never assigned to a variable that outlives that
computation, never interpolated into a `CheckResult.detail` or a `print()`,
and never passed to a function that might log it.**

## 7. Documentation

- `README.md`'s check table gains a `provider-live` row, worded consistently
  with the existing seven.
- `README.md`'s "Switching providers without a redeploy" section already
  warns in prose about exactly this gap ("a provider whose key was never
  pushed to Render will report PASS here and then fail every real review...").
  That paragraph is updated to say the gap is now caught automatically by
  `provider-live`, rather than only by prose.
- `SETUP.md` §3.6 gains: the `--force` flag, and a note that pointing
  `DATABASE_URL` at a local database for testing automatically skips live
  verification (§5) — no key configuration needed to avoid it.

## 8. Testing

**`_render_env_vars()`:** respx-mocked GET, unwraps `{"envVar": {...}}` items
correctly (behavior already covered indirectly via `sync_env()`'s existing
tests, which now exercise it through the extracted function — no regression
expected, confirmed by running the existing `sync_env` test cases unchanged).

**`_resolved_provider_or_env()`:** no `DATABASE_URL` → env value, `None`;
`DATABASE_URL` set, no override → env value; override set → override wins;
DB error propagates (caller's job to catch).

**`check_provider_live()`:** SKIPPED without `RENDER_API_KEY`; SKIPPED on a DB
error resolving the override; PASS when the resolved provider's credential is
present on Render (env-sourced and override-sourced); FAIL when missing
(env-sourced and override-sourced); detail string in every case asserted to
never contain a fetched value (only the boolean-derived PASS/FAIL text).

**`set_provider.py`:** existing tests stay green unchanged (no
`RENDER_API_KEY` in their environment → skip-with-warning path, proceeds
exactly as today). New cases: `RENDER_API_KEY` set and local `DATABASE_URL`
matches Render's → credential missing on Render gives exit 2, override not
written; same, with `--force` → exit 0, override written, warning printed;
credential present and matches local `.env` → exit 0, no warning; credential
present and differs → exit 2 without `--force`, exit 0 with it. **`RENDER_API_KEY`
set but local `DATABASE_URL` does not match Render's** → proceeds without
refusal regardless of what Render's credentials look like (this is the case
that closes §5's edge case — assert the override is written and no `--force`
was needed). `--clear` never invokes `_verify_render_credential()` (assert via
a call-count check or monkeypatch spy).

**Mutation check:** on the `--force` bypass condition, on the
`DATABASE_URL`-match comparison, and on the credential value-equality
comparison in `_verify_render_credential()` — deliberately break each,
confirm the relevant test fails, revert.

## 9. Out of scope

- `check_config` itself gaining live verification — subsumed by
  `provider-live` answering both the override and plain-env cases already
  (§2).
- Any change to `--sync-env`'s own refusal logic beyond the `_render_env_vars`
  extraction, which is a pure refactor.
- Auto-triggering `--sync-env` from either script on a failed/refused check —
  both report and name the command; neither invokes it.
- Applying the `DATABASE_URL`-match gate (§4.1, §5) to `check_provider_live()`
  in `deploy.py` — that check is read-only and already inherits the same
  "whatever `DATABASE_URL` currently is" property from `check_provider()`
  (accepted in the 2026-08-08 spec); the gate is scoped to the write guard in
  `set_provider.py`, where a wrong refusal actually costs something.
- **Consolidating the Render-API and provider-resolution access code across
  scripts into a shared internal module.** This work adds a third consumer of
  a Render env-var fetch and a third path resolving the active provider,
  which is real, growing duplication — but the three planes involved (local
  `.env`, Render's live env, the DB override) encode genuinely different
  facts on purpose (§2.1 of the 2026-08-08 spec), so the fix is consolidating
  *access code*, not collapsing the data into one source of truth. Raised
  during review of this spec; deliberately deferred to its own follow-up
  design session rather than widening this diff.
