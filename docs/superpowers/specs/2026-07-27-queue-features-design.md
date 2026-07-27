# Design — Durable Review Queue (RPM + daily-quota handling)

**Date:** 2026-07-27
**Status:** Approved for planning
**Relates to:** `SPEC.md` (architecture), `cost.md` (free-tier caps), `CLAUDE.md`
(LLM API testing hygiene), `SETUP.md` (live-provider deviations).

## 1. Problem

The engine currently runs every review in its own FastAPI `BackgroundTask`, and
each review fires **three LLM calls concurrently** (`asyncio.gather` in
`orchestrator.run_review`). There is no rate limiting and no persistence
anywhere; the only stateful thing is the in-memory delivery-dedup LRU, which is
lost on restart.

That breaks against the live providers' real free-tier ceilings:

- **Groq** ≈ 30 RPM / 14.4K per day.
- **GitHub Models** ≈ single-digit RPM / ~150 requests per day (≈ 50 reviews/day).

Two concrete failures follow:

1. **Per-minute bursts.** With N PRs arriving together, N independent background
   tasks fire `3 × N` calls at once with zero coordination. At, say, 5 RPM, two
   concurrent PRs (6 calls) already exceed the cap; some calls `429` and turn
   into visible "failed" specialist rows even though nothing is actually wrong.
2. **Daily wall.** Sustained use exhausts the daily quota; every subsequent call
   fails until the provider's quota resets hours later.

## 2. Goals / non-goals

**Goals**

- Absorb per-minute bursts so a temporary rate limit delays a review instead of
  failing it.
- When the daily quota is exhausted, **defer** the review durably and run it
  automatically once the provider allows it again — survives process restart.
- Always keep the PR author informed: any review that can't run immediately gets
  a visible placeholder comment explaining why and (when known) for how long.
- Preserve the existing 15-second target on the **normal** path (budget
  available). Missing it is acceptable **only** on a delayed path, and only with
  a notification.

**Non-goals (deliberately out of scope — YAGNI)**

- **No provider failover.** The multi-provider seam exists for
  provider-agnosticism, not as a drop-in failover. A daily wall on the active
  provider defers the review; it does **not** switch providers.
- **No proactive quota accounting.** We do not track usage counts, do not read
  `x-ratelimit-*` headers, and do not hardcode any per-provider RPM/RPD cap.
  Hardcoded caps go stale when providers change their limits; we avoid holding
  them at all (see §4).
- **No priority scheme.** FIFO is sufficient.
- **No horizontal scaling.** Single process, single dispatcher at demo scale.
  The ticket-claim is written atomically so multi-instance is *possible* later,
  but it is neither built for nor tested.

## 3. Core decisions (locked during brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Wall handling | **Durable deferred queue** (SQLite) + auto-retry | "Sustained production, survives restart" without a paid external DB service. |
| Detection | **Reactive, `Retry-After` only** | Providers tell us the truth; no cap can drift stale. One value handles both per-minute and daily limits. |
| Review granularity | **Atomic — whole review or nothing** | A comment that is half-filled-today / half-tomorrow is worse than one complete comment appearing once. |
| Concurrency model | **Producer/consumer: one serial dispatcher** | A single serialization point is the only way to coordinate a shared provider budget; serial dispatch is also anti-burst by design. |
| Notification | **Placeholder comment, edited in place** | Reuses the existing marker-based `upsert_comment`; every 15s miss is explained on the PR. |
| Provider seam change | One typed `RateLimited(retry_after)` exception | The single special signal the dispatcher needs; everything else stays a generic visible failure. |

## 4. Why reactive / `Retry-After`-only

We **do not predict** limits; we **react** to them. The only signal that we are
throttled is an HTTP `429` carrying a `Retry-After`. That single value works for
both time-scales:

- a **per-minute** limit yields a short `Retry-After` (seconds) → the review is
  deferred briefly and retried;
- a **daily** limit yields a long `Retry-After` (hours) → the review is deferred
  until the provider says it may run again.

Consequences:

- The **RPM-vs-daily distinction disappears** from our code. There is one
  mechanism at two magnitudes.
- **No reset-model decision** (UTC-midnight vs rolling-24h). The daily-limit
  `Retry-After` *is* the reset signal.
- **No stale caps.** We hold no per-provider RPM/RPD numbers, so none can drift.

**Accepted costs of going reactive (all confirmed):**

1. We do not *prevent* `429`s; we incur one per block period and back off. This
   is bounded — once `blocked_until` (§6) is set, we stop firing against that
   provider until it clears, so a block period produces at most one burst of
   `429`s (from the single review that discovers the block), then silence. This
   is the ecosystem-correct, hygiene-compliant behavior (honor `Retry-After`,
   never hammer — see `CLAUDE.md`).
2. **"Never partial" wastes a little quota at the daily boundary.** If the real
   remaining daily budget is 1–2 calls, the atomic review fires 3, the 1–2 that
   succeed are discarded, and the whole review defers to reset. A couple of
   wasted calls per block period is the price of a simple, atomic pipeline.
3. **Serial dispatch adds latency under load.** A slow review (LLM latency, a
   few seconds) makes the next PR wait even when the provider is healthy. Fine
   at demo scale and anti-burst by design.

**Fallback for a missing header.** If a provider returns a `429` without a
usable `Retry-After`, we back off by a single, **non-provider-specific**
`DEFAULT_RETRY_AFTER_SECONDS` (default `60`). This is a backoff default, not a
cap — worst case we retry slightly early and receive another honest `429`. Groq
is documented to send `retry-after`; GitHub Models must be **verified once,
live** (one deliberate call, per `CLAUDE.md` hygiene) before we rely on it.

## 5. Architecture — producer / consumer

```
GitHub PR ──▶ POST /webhook                         (PRODUCER)
                verify HMAC → delivery-dedup
                → upsert ticket in SQLite → 202      (no LLM work; returns instantly)
                                                        │
                                                        ▼
              Dispatcher loop            (CONSUMER — one per process, SERIAL)
                repeat:
                  ticket = store.claim_next_due()          # FIFO, honors not_before
                  if none: idle briefly; continue
                  provider = active LLM_PROVIDER
                  if now < blocked_until[provider]:        # soft, in-memory gate
                      defer(ticket, not_before = blocked_until[provider])
                      ensure_placeholder(ticket, kind="blocked")
                      continue
                  outcome = attempt_review(ticket)         # whole review, atomic
                  match outcome:
                    RateLimited(retry_after):
                      blocked_until[provider] = now + retry_after
                      defer(ticket, not_before = blocked_until[provider])
                      ensure_placeholder(ticket, kind="rate_limited", until=…)
                    Completed(review_result):
                      upsert_comment(real result)          # replaces any placeholder
                      ticket.done()
```

The dispatcher is the **only** caller of the review path, so all pacing/quota
decisions are serialized and the multi-PR race is gone by construction.
`orchestrator.run_review` is essentially unchanged internally — it is simply
invoked *by the dispatcher* instead of by a per-request background task, and it
gains the ability to surface `RateLimited` (§8).

## 6. State

### 6.1 Durable — the ticket (SQLite; embedded, $0, survives restart)

```sql
CREATE TABLE tickets (
  id              INTEGER PRIMARY KEY,
  repo_full_name  TEXT    NOT NULL,
  pr_number       INTEGER NOT NULL,
  head_sha        TEXT,                    -- latest commit; updated on re-trigger
  status          TEXT    NOT NULL,        -- pending | running | deferred | done | failed
  provider        TEXT    NOT NULL,        -- active provider at enqueue time
  not_before      TEXT,                    -- ISO-8601; set when deferred
  attempts        INTEGER NOT NULL DEFAULT 0,
  comment_id      INTEGER,                 -- so placeholder → result edits one comment
  enqueued_at     TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL,
  UNIQUE(repo_full_name, pr_number)        -- one live ticket per PR (§7)
);
```

Claiming a ticket is an atomic
`UPDATE tickets SET status='running', updated_at=? WHERE id=? AND status='pending'`
(and the analogous check for a due `deferred` ticket). SQLite's single-writer
semantics make this safe; the atomicity also future-proofs a multi-instance
deployment without our building one.

The **durable ticket is the source of truth.** In particular, a deferred
ticket's `not_before` is what actually prevents an early run after a restart.

### 6.2 In-memory — `blocked_until[provider]`

A per-provider timestamp learned **only** from the last `Retry-After`. It is a
soft optimization so the dispatcher does not fire calls it knows will fail. It
is **not** authoritative and is intentionally **not** persisted: after a
restart it is empty, the first eligible ticket makes one honest attempt, and if
the provider is still blocked we re-learn `blocked_until` from that `429`.
Deferred tickets are protected across the restart by their persisted
`not_before`, so nothing runs early regardless.

## 7. Re-trigger collapsing

Two layers of de-duplication:

1. **Delivery-ID dedup** (existing, unchanged): the bounded in-memory LRU on
   `X-GitHub-Delivery` kills GitHub's exact webhook redeliveries.
2. **Per-PR collapsing** (new): the `UNIQUE(repo_full_name, pr_number)` upsert
   means a fresh push to a PR that already has a `pending`/`deferred` ticket
   **updates its `head_sha`** rather than stacking a second ticket. One review
   per PR, latest commit wins, comment edited in place. A ticket already
   `running` is allowed to finish; the newer push updates the row so the next
   claim reviews the latest commit.

## 8. Provider seam change

Adapters currently turn any failure into a `status="failed"` specialist row via
the validate-repair layer. We add exactly one typed signal:

```python
class RateLimited(Exception):
    def __init__(self, retry_after: float): ...   # seconds until retry is allowed
```

- Each adapter raises `RateLimited(retry_after)` when it receives a `429`,
  parsing `Retry-After` (seconds or HTTP-date), falling back to
  `DEFAULT_RETRY_AFTER_SECONDS` when the header is absent/unparseable.
- **Atomic propagation:** if *any* of a review's three calls raises
  `RateLimited`, `attempt_review` discards all partial results and returns
  `RateLimited(max(retry_after seen))`. It never posts a partial comment.
- Every **non-quota** failure keeps its current behavior: the specialist renders
  a visible `failed` row and the review still `Completed`s.

So `attempt_review` returns exactly one of: `Completed(ReviewResult)` |
`RateLimited(retry_after)`.

## 9. Comment lifecycle

Reuses the marker-based `upsert_comment(repo, pr, body)` and stores the returned
`comment_id` on the ticket so every later edit targets the same comment.

- **Fast path** (not blocked, `Completed`): post the real comment once — no
  placeholder, unchanged from today, well under 15s.
- **Delayed path**: post/keep a placeholder, later replaced in place by the real
  result:
  - rate-limited / blocked:
    `⏳ Queued behind rate limit — review will appear shortly.`
  - daily-scale wait (long `Retry-After`):
    `⏳ Daily model quota reached — queued, will post automatically after the provider's limit resets (~<local-time>).`

The placeholder is the notification that satisfies the "notify on any 15s miss"
rule. `formatting.py` gains a small renderer for these two placeholder states;
the existing result formatter is untouched.

## 10. Restart recovery

At app startup (lifespan), before the dispatcher begins:

1. Reset any `running` ticket (interrupted mid-review by a crash) back to
   `pending`.
2. Leave `deferred` tickets as-is; their `not_before` gates them.

The dispatcher then drains all `pending`, plus `deferred` tickets whose
`not_before` has passed. This is the "sustained production, survives restart"
property — free, because the queue is the durable source of truth.

## 11. Module / config surface

**New**

- `app/queue/store.py` — SQLite ticket store: `enqueue_or_update`,
  `claim_next_due`, `mark_done`, `mark_failed`, `defer`, `recover_on_startup`.
- `app/queue/dispatcher.py` — the serial consumer loop + `blocked_until` gate;
  started/stopped by the app lifespan.
- `app/queue/gate.py` (or kept inside the dispatcher if trivial) — the
  `blocked_until` map and the `now`-vs-`blocked_until` decision, kept behind an
  injectable clock so tests use no real sleeps.

**Changed**

- `app/webhook.py` — replace `background_tasks.add_task(run_review, …)` with a
  ticket `enqueue_or_update`; still returns `202`. Delivery-dedup unchanged.
- `app/main.py` — lifespan: init/migrate the SQLite DB, run
  `recover_on_startup`, start the dispatcher task, cancel it on shutdown.
- `app/orchestrator.py` — expose `attempt_review` returning
  `Completed | RateLimited`; keep the existing `run_review` semantics for the
  `Completed` case.
- `app/providers/base.py` + `google_genai.py` / `groq.py` / `github_models.py` —
  raise `RateLimited(retry_after)` on `429`.
- `app/formatting.py` — render the two placeholder states.
- `app/config.py` — new env vars (below).
- `SPEC.md`, `cost.md`, `README.md` — document the feature and its limitations
  in the existing house style.

**Config added** (note: none are per-provider caps)

| Var | Default | Meaning |
|---|---|---|
| `QUEUE_DB_PATH` | `./queue.db` | SQLite file (gitignored). |
| `DEFAULT_RETRY_AFTER_SECONDS` | `60` | Backoff when a `429` omits `Retry-After`. |
| `DISPATCHER_IDLE_SLEEP_SECONDS` | `1` | Poll interval when the queue is empty. |

## 12. Testing (deterministic-first, matching SPEC §8's layers)

All time-dependent logic uses an **injected clock**; no real sleeps, no real
network. New layers:

1. **Ticket store** — enqueue; atomic claim (a claimed ticket can't be
   re-claimed); `UNIQUE(repo, pr)` upsert updates `head_sha` instead of stacking;
   `defer` sets `not_before`; `claim_next_due` honors `not_before` and FIFO.
2. **Provider `RateLimited`** — a mocked `429` with `Retry-After: 30` →
   `RateLimited(30)`; a `429` with no header → `RateLimited(60)`; an HTTP-date
   `Retry-After` → correct seconds.
3. **Atomic propagation** — one of three calls raises `RateLimited`; assert the
   whole review returns `RateLimited` and **no** comment is posted; the other
   calls' results are discarded.
4. **Dispatcher — burst** (the 5 RPM / 2 PR scenario): first ticket runs; second
   is `RateLimited`, gets a placeholder, and completes after the injected clock
   advances past `blocked_until`.
5. **Dispatcher — daily wall**: long `Retry-After` → ticket `deferred` +
   daily-scale placeholder → runs and replaces the placeholder after the clock
   passes `not_before`.
6. **Restart recovery**: a `running` ticket becomes `pending` on startup; a
   `deferred` ticket survives a store reopen and is not run before `not_before`.
7. **Non-quota failure still visible**: a generic specialist error still yields a
   `Completed` review with a visible `failed` row (regression guard on existing
   behavior).

**Live verification (one deliberate call each, per `CLAUDE.md` hygiene)**

- Confirm GitHub Models sends a usable `Retry-After` on a `429` (the one open
  assumption in §4). If it does not, the `DEFAULT_RETRY_AFTER_SECONDS` fallback
  is exercised instead and documented.

## 13. Future work (out of scope — its own planning session)

- **"Review updated" ping comment.** GitHub does not notify on comment *edits*,
  and this design edits the review comment in place — so an updated review
  (especially a deferred one that posts hours later) is easy to miss on a busy
  PR. A future feature could post a *new* lightweight comment linking to the
  review comment (`.../pull/{pr}#issuecomment-{comment_id}` — `comment_id` is
  already stored per §6.1). The mechanism is cheap; the real design work is the
  **notification policy** (which transitions ping, whether to replace/delete the
  prior ping, long-PR anti-spam), which deserves its own brainstorm and is
  deliberately not specified here.

## 14. Open items for the plan

- Exact SQLite migration/bootstrap approach (single `CREATE TABLE IF NOT
  EXISTS` at lifespan is likely enough; no migration framework).
- Whether `app/queue/gate.py` is a separate module or folded into the
  dispatcher — decide during implementation based on size.
- `.gitignore` must include `queue.db` (and `-wal`/`-shm` sidecars).
