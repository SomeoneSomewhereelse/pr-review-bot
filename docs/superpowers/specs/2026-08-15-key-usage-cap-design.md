# Design — Proactive per-key daily usage cap

**Date:** 2026-08-15
**Status:** Approved for planning
**Relates to:** `SPEC.md` §12 (review queue — RPM + daily-quota handling; this
design reopens one item its own "out of scope" list named explicitly:
"proactive quota accounting (no `x-ratelimit-*` tracking, no hardcoded
caps)"), `docs/superpowers/specs/2026-08-12-api-key-index-override-design.md`
(the per-slot key-index override this design tracks usage *per*), `cost.md`
(the token/cost math this reuses), `CLAUDE.md`'s Trust & Safety section (a
different, already-mitigated risk — this design's motivation is cost
overrun, not abuse-flag avoidance; see §1).

## 1. Problem

Today the app has only **reactive** protection against runaway spend: it
waits for a provider to actually return a 429, then backs off (SPEC.md §12).
Nothing stops the app from making calls at all if, say, a bug fans out
reviews faster than expected, or PR volume spikes — the free-tier credit or
a provider's paid tier could be burned through before anyone notices. There
is no proactive ceiling on how much a given credential is allowed to spend
or consume in a day.

## 2. Decision

A per-key/slot daily cap, checked **before** a review starts (not predicted
in advance — see §4), enforced via the existing deferred-ticket queue
machinery so it costs no new state machine.

### 2.1 Config (`app/config.py`)

```python
key_usage_token_cap: int | None = None
key_usage_cost_cap_usd: float | None = None
key_usage_reset_hour_utc: int = Field(default=4, ge=0, le=23)
```

Env vars: `KEY_USAGE_TOKEN_CAP`, `KEY_USAGE_COST_CAP_USD`,
`KEY_USAGE_RESET_HOUR_UTC` — same `Settings`-field-name-uppercased
convention as every other tunable here. Both caps default to `None`
(feature off): an existing deployment that never sets either env var sees
no behavior change. **`KEY_USAGE_TOKEN_CAP` takes precedence when both are
set** — the cost cap is not consulted at all in that case, not merely a
tiebreak. Test/demo value: `KEY_USAGE_TOKEN_CAP=20000`.

### 2.2 Scope

One independent running total per **(provider, active key/slot index)**,
mirroring the granularity `key_index.active_key_index()` already tracks.
Swapping to a sibling slot via `scripts/set_api_key.py` immediately grants a
fresh budget — no special-case code, it falls out of the query being scoped
to `key_index`.

### 2.3 Enforcement timing (check-before, not predict-before)

Actual token/cost usage for a review is only known after it completes (from
provider usage metadata). Rather than estimate an upcoming review's cost in
advance, the gate checks whether the slot's cumulative usage *so far today*
already meets or exceeds the cap; if not, the review proceeds normally, even
though its own usage may push the total slightly past the cap. The cap
bounds when the *next* review is allowed to start, not the exact daily
total — the same shape the existing reactive-429 backoff already has (it
also only blocks *future* calls, never the one already in flight).

## 3. Data model — extend `reviews`, no new table

`app/queue/store.py`'s `_SCHEMA` gains one nullable column:

```sql
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS key_index INTEGER;
```

`record_review` gains a `key_index: int` parameter, populated by
`orchestrator.attempt_review` from `key_index.active_key_index(provider)` —
the same value `factory.py` already resolved to build that review's client
— at the point it calls `store.record_review` (`app/orchestrator.py:127-135`).
`NULL` (rows written before this migration) is treated as index 0 when
querying, consistent with every other nullable-means-index-0 column here.

New read helper:

```python
def get_key_usage(provider: str, key_index: int, since: str) -> tuple[int, float]:
    """(tokens_total, cost_total_usd) from `reviews` for this (provider,
    key_index) since `since` (inclusive), treating a NULL key_index column
    as index 0."""
```

```sql
SELECT COALESCE(SUM(total_tokens_in + total_tokens_out), 0) AS tokens,
       COALESCE(SUM(est_cost_usd), 0) AS cost
FROM reviews
WHERE provider = %s
  AND COALESCE(key_index, 0) = %s
  AND created_at >= %s
```

No dedicated aggregate table. *(Considered and rejected: a running-total
`key_usage` table incremented on each `finalize_review` — extra state to
keep consistent with `reviews` for no benefit at free-tier volume; a `SUM`
over `reviews` costs nothing meaningful at 20 PRs/day.)*

### 3.1 Usage-day bucket

```python
def usage_bucket_start(now: datetime, reset_hour: int) -> datetime:
    """UTC instant the current usage window began. If now's UTC
    time-of-day is before reset_hour, the window started at *yesterday's*
    reset_hour; otherwise today's."""
```

Pure function of `(now, reset_hour)` — no state, trivially unit-testable.
Lives in `app/queue/store.py` alongside `effective_cooldown`/
`next_cooldown_level`, the existing precedent for small pure helpers
colocated with the module that calls them despite touching no DB state.

## 4. Enforcement point — `app/queue/dispatcher.py::process_next_due`

Inserted after the existing key-index-override refresh
(`dispatcher.py:157-167`) and **before** the existing provider-blocked check
(`dispatcher.py:180-198`) — same position in the per-claimed-ticket
sequence, same "must never abort a review on a refresh failure" fail-safe
shape as the three refreshes already there, except this one's failure mode
is "fail open" (a usage-check error must never block reviews — cost-cap
enforcement degrading to off-by-default is the same posture as every other
override here degrading to its safe default):

```python
if settings.key_usage_token_cap is not None or settings.key_usage_cost_cap_usd is not None:
    try:
        bucket_start = usage_bucket_start(now, settings.key_usage_reset_hour_utc)
        tokens, cost = await asyncio.to_thread(
            store.get_key_usage, provider, key_index.active_key_index(provider),
            bucket_start.isoformat(),
        )
        over_cap = (
            tokens >= settings.key_usage_token_cap
            if settings.key_usage_token_cap is not None
            else cost >= settings.key_usage_cost_cap_usd
        )
    except Exception:  # noqa: BLE001
        logger.exception("failed to check key usage cap; proceeding without it")
        over_cap = False
    if over_cap:
        reset_at = bucket_start + timedelta(hours=24)
        await asyncio.to_thread(
            store.defer_usage_capped, ticket.id, not_before=reset_at.isoformat(), now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number,
                (reset_at - now).total_seconds(), now, ticket.comment_id, reason="usage_cap",
            )
        return StepResult(action="deferred", ticket_id=ticket.id)
```

`provider = active_provider()` is resolved once, above this block, exactly
as it already is today for the existing blocked-provider check right below
it (`dispatcher.py:184`) — this new block moves that one line earlier so
both checks share it.

### 4.1 New `tickets.defer_reason` column, and a distinguishable notice

```sql
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS defer_reason TEXT;
```

`NULL` (or `'provider'`) = today's existing meaning (rate limit or cooldown
wait). `'usage_cap'` = this app's own self-imposed limit, set only by the
new `store.defer_usage_capped`. Every other code path that sets
`status = 'deferred'` (`defer_rate_limited`, the cooldown re-arm branches in
`enqueue_or_update` and `finalize_review`) must explicitly write
`defer_reason = NULL` — a stale `'usage_cap'` must never survive into a
later, unrelated deferral of the same ticket row, the same "never leave a
stale flag" discipline `rereview_requested`/`cooldown_level` already follow
elsewhere in this file.

`format_schedule_notice` and `format_placeholder` (`app/formatting.py`) each
gain a `reason: Literal["provider", "usage_cap"] = "provider"` parameter
(default preserves today's exact wording/behavior for every existing call
site):

- `format_placeholder`'s existing magnitude-based branch
  ("rate limit" / "provider's daily quota") only applies when
  `reason == "provider"`; `reason == "usage_cap"` always renders a third,
  explicit message regardless of wait length — e.g. *"⏳ Bot's own daily
  usage limit reached for this key — review queued, will post automatically
  after the limit resets (~HH:MM UTC). This is not a provider rate limit."*
- `format_schedule_notice` similarly branches:
  *"🔄 Re-review scheduled ~HH:MM UTC (usage limit reached — resets
  automatically, not a provider quota issue)"* vs. today's unchanged
  `"🔄 Re-review scheduled ~HH:MM UTC"`.

`post_pending_notices` (`dispatcher.py:99-122`) passes
`ticket.defer_reason or "provider"` through to `format_schedule_notice` —
the ticket row is the durable source for which wording a *later* sweep
iteration should use, since the sweep has no other memory of why a given
ticket is deferred.

## 5. Interplay with existing mechanics

- **Key-slot swap** (`scripts/set_api_key.py`) mid-day: immediately fresh
  budget for the new slot, no code change — the query is scoped to
  whatever `key_index.active_key_index(provider)` resolves to *now*.
- **Restart recovery** (`store.recover_on_startup`): unaffected. Usage is
  derived from persisted `reviews` rows, not in-memory state, so a
  crash/redeploy never resets or loses the count — unlike `_blocked_until`,
  which is deliberately in-memory and does reset on restart.
- **Dashboard**: not touched by this design. A "today's usage vs. cap per
  active slot" widget would be a natural follow-up but is out of scope
  here (§7).

## 6. Surface

- `app/config.py` — three new settings (§2.1).
- `app/queue/store.py` — `reviews.key_index` column; `tickets.defer_reason`
  column; `get_key_usage`; `usage_bucket_start`; `defer_usage_capped`;
  explicit `defer_reason = NULL` added to `defer_rate_limited` and the two
  cooldown re-arm statements.
- `app/orchestrator.py` — `attempt_review` resolves and passes `key_index`
  to `store.record_review`.
- `app/queue/dispatcher.py` — new pre-flight cap check in
  `process_next_due`; `_post_placeholder` gains the `reason` passthrough;
  `post_pending_notices` passes `ticket.defer_reason` through.
- `app/formatting.py` — `reason` parameter on `format_placeholder` and
  `format_schedule_notice`.
- `SPEC.md` — new subsection under §12 documenting the cap, alongside the
  existing RPM/daily-quota handling it complements (and a note that this
  fills the item §12's own "out of scope" list previously named).
- `README.md` — document the two env vars and the precedence rule, next to
  wherever `DISPATCHER_REREVIEW_COOLDOWN_*` is already documented.

## 7. Testing (deterministic-first, matches SPEC.md §8/§12's existing strategy)

- `usage_bucket_start` — just-before/just-after the reset hour, exact
  boundary instant, a reset hour of `0`.
- `store.get_key_usage` — sums only the matching `(provider, key_index)`
  rows within the window; a `NULL` `key_index` row counts as index 0;
  excludes rows before `since`.
- `store.defer_usage_capped` — sets `status='deferred'`, the given
  `not_before`, and `defer_reason='usage_cap'`; a subsequent
  `defer_rate_limited`/cooldown re-arm on the same ticket clears
  `defer_reason` back to `NULL`.
- `dispatcher.process_next_due` — extends `tests/test_dispatcher.py`: over
  cap → ticket deferred with `defer_reason='usage_cap'`,
  `attempt_review` never called (assert the monkeypatched review function
  wasn't invoked, not just that the ticket ended up deferred); under cap →
  runs normally; a `get_key_usage` exception degrades to "not capped"
  rather than blocking the review; only `KEY_USAGE_TOKEN_CAP` is consulted
  when both env vars are set.
- `format_placeholder` / `format_schedule_notice` — `reason="usage_cap"`
  renders the distinct wording at both a short and a long wait; the default
  (`reason="provider"`, and every pre-existing call site with no `reason`
  argument) renders byte-identical to today's output.

## 8. Non-goals

- No auto-swap to a sibling key slot on breach — a human decides that via
  `scripts/set_api_key.py`, exactly as key rotation works today.
- No global or per-provider pooling across slots — strictly per
  `(provider, key_index)`, per the explicit scope decision.
- No predictive/estimated pre-call check — check-before only (§2.3).
- No dashboard usage-vs-cap widget (§5) — a clean follow-up, not required
  for the cap to function.
- No change to the existing reactive-429 path (`RateLimited`,
  `ReviewRateLimited`, `_blocked_until`) — this is an independent,
  additional gate that runs strictly before it in `process_next_due`.
