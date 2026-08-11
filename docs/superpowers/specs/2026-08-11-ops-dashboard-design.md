# Design: Ops/Demo Dashboard

Date: 2026-08-11

## Purpose

A single web page, served by the existing FastAPI app, that serves two
audiences at once:

- **Demo/presentation**: pull it up live while triggering a real PR review
  and watch it update — proof the system works, without narrating raw logs.
- **Ops**: at any other time, a glance at queue depth, recent review outcomes,
  and provider health (including rate-limit backoff).

No auth (nothing secret is shown — no credentials, tokens, or internal URLs;
it does surface repo/PR identifiers, timing, cost, and the reviewed findings
themselves — file paths, line numbers, and LLM-written descriptions — so this
assumes either a public target repo or that the dashboard's own exposure is
acceptable for the demo). No new Python dependency and no CDN script — plain
server-rendered HTML with a small inline vanilla-JS polling loop.

## Problem: review results aren't persisted today

`orchestrator.attempt_review()` builds a `ReviewResult` (per-specialist
findings, timing, tokens, `est_cost_usd`), renders it to Markdown via
`format_comment()`, posts it to GitHub, and then discards it — nothing is
kept server-side. The `tickets` table (`app/queue/store.py`) tracks queue
*lifecycle* (status, attempts, `last_reviewed_at`) but not review *content*
or *cost*. A dashboard needs both, so this design adds persistence for the
first time.

## Architecture

```
orchestrator.attempt_review()
  └─ after upsert_comment() succeeds:
       store.record_review(repo_full_name, pr_number, review_result, comment_id)
            └─ INSERT into new `reviews` table (fire-and-forget-safe: failure
               to record must never fail the review itself — see Error handling)

app/dashboard.py  (new module)
  GET /dashboard       → HTML shell + inline <script> (polling loop)
  GET /api/dashboard   → JSON: { stats, queue, reviews[] }
       stats:  totals across `reviews` (count, sum est_cost_usd, avg elapsed_ms)
       queue:  counts from `tickets` grouped by status + per-provider
               backoff read from app.queue.dispatcher's in-memory
               `_blocked_until` (exposed via a small `dispatcher.backoff_status()`
               getter — no new persistence needed for this part)
       reviews: last 50 rows from `reviews`, newest first, findings included
```

`app/dashboard.py` knows nothing about LLM providers or GitHub — it only
reads `store` and `dispatcher`, matching the existing "formatting knows
nothing about LLMs" separation.

## Data model

New table in `app/queue/store.py`'s `_SCHEMA` (same file that owns `tickets`,
since both are queue-adjacent persistence, both created via the same
`CREATE TABLE IF NOT EXISTS` migration-on-startup pattern):

```sql
CREATE TABLE IF NOT EXISTS reviews (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name  TEXT    NOT NULL,
    pr_number       INTEGER NOT NULL,
    provider        TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    comment_id      BIGINT,
    created_at      TEXT    NOT NULL,   -- ISO-8601 UTC, same convention as tickets
    total_elapsed_ms   INTEGER NOT NULL,
    total_tokens_in    INTEGER NOT NULL,
    total_tokens_out   INTEGER NOT NULL,
    est_cost_usd       DOUBLE PRECISION NOT NULL,
    results         JSONB   NOT NULL    -- serialized list[SpecialistResult], findings included
);
CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews (created_at DESC);
```

`store.record_review(repo_full_name, pr_number, review: ReviewResult, comment_id: int | None) -> None`
— synchronous like the rest of `store.py`; the caller wraps it in
`asyncio.to_thread`, same convention as every other store call.

`store.dashboard_reviews(limit: int = 50) -> list[dict]` and
`store.dashboard_queue_counts() -> dict[str, int]` — read helpers, plain SQL,
no new abstraction beyond what `store.py` already does for `tickets`.

No retention/pruning logic. At brief scale (20 PRs/day) `reviews` grows by
~20 rows/day; not worth the complexity of a cleanup job for a demo project.

## API response shape

```json
{
  "stats": {
    "total_reviews": 42,
    "total_cost_usd": 0.0834,
    "avg_elapsed_ms": 8120
  },
  "queue": {
    "by_status": {"queued": 1, "running": 0, "done": 40, "deferred": 1},
    "backoff": {"gemini": null, "groq": "2026-08-11T14:32:00Z"}
  },
  "reviews": [
    {
      "repo": "org/repo", "pr_number": 57, "provider": "groq",
      "model": "llama-3.3-70b-versatile",
      "created_at": "2026-08-11T14:20:03Z",
      "elapsed_ms": 7900, "tokens_in": 3200, "tokens_out": 540,
      "est_cost_usd": 0.0021,
      "comment_url": "https://github.com/org/repo/pull/57#issuecomment-123",
      "specialists": [
        {"name": "Security", "status": "ok", "findings": [ /* SecurityFinding dicts */ ]},
        {"name": "Performance", "status": "ok", "findings": [...]},
        {"name": "Code Quality", "status": "failed", "error": "..."}
      ]
    }
  ]
}
```

`comment_url` is constructed from `repo_full_name` + `pr_number` +
`comment_id` (`.../pull/{n}#issuecomment-{id}`) — no extra GitHub API call.

## Page layout

- **Stat tiles** (top row): total reviews, total est. cost, avg review time,
  queue depth by status, active provider + backoff-until (if any).
- **Review list** (reverse-chronological, capped at 50): one row per review —
  PR #, repo, provider/model, a status icon per specialist (✓ ok / ✗ failed),
  timing, tokens, cost, timestamp, a link to the GitHub comment. Clicking a
  row expands it to show that review's actual findings, grouped by
  specialist, each tagged with its severity/impact/category field.
- **Auto-refresh**: inline `<script>` polls `GET /api/dashboard` every 4s and
  re-renders the list + tiles. No diffing — just replace the DOM; at 50 rows
  this is cheap. An expanded row's open/closed state is tracked by PR number
  in a JS `Set` so a refresh doesn't collapse whatever the viewer had open.

## Theming, internationalization & responsive design

No frontend framework. Considered Tailwind v4 and rejected it: v4 dropped
the old CDN prototype script in favor of a real build (PostCSS/Vite), which
this repo has never needed (`uv`-only, no frontend tooling anywhere) and
would contradict the no-CDN, hand-rolled-JS decision already made for the
page itself. Plain CSS custom properties + logical properties cover
everything this page needs.

- **Theme (light / dark / system)**: two token sets defined as CSS custom
  properties on `:root` and `:root[data-theme="dark"]`; `system` is the
  absence of `data-theme` (falls through to a `prefers-color-scheme: dark`
  media query redefining the same variables). Palette is calm/professional
  in both modes — muted blues/grays, no saturated primaries; status colors
  (ok/failed, severity tags) are defined as tokens too, so they stay legible
  and consistent across both themes rather than being hardcoded per mode.
  Choice persists in `localStorage`; default is `system`.
- **Language (English / עברית)**: a small static JS object mapping string
  keys to `{en, he}` values for every piece of UI chrome this page owns —
  button labels, stat-tile labels, specialist names, status words, severity
  tags, column headers. Per the earlier decision, LLM-generated finding text
  (descriptions, fix suggestions) is never translated — it renders as-is
  regardless of language, since translating it would need extra LLM calls
  the demo doesn't need. Choice persists in `localStorage`; default is
  English.
- **RTL**: selecting עברית sets `dir="rtl"` on `<html>`; all layout CSS uses
  logical properties (`margin-inline-start/end`, `padding-inline`,
  `text-align: start`) instead of physical `left`/`right`, so the stat-tile
  grid, review rows, expand chevrons, and both popups mirror automatically
  with no separate RTL stylesheet.
- **Header controls**: theme button and language button sit side by side at
  the top of the page. Each always shows an icon plus the current selection
  (e.g. "🌙 Dark", "עברית 🇮🇱") and opens a small centered popup on click.
  Both popups use the same radio-group pattern — Light / Dark / System for
  theme, English 🇺🇸 / עברית 🇮🇱 for language — for visual consistency; only
  one popup is open at a time.
- **Mobile**: stat tiles go from a 4-across grid to 2-across (~900px) to
  1-across (~500px); the review list switches from table-like rows to
  stacked cards (label above value) below ~640px; both popups are centered
  modals sized to viewport at every breakpoint rather than a separate
  mobile-only sheet variant.

## Error handling

- `record_review()` is called from `attempt_review()` *after* `upsert_comment`
  succeeds, wrapped in try/except with `logger.exception` on failure — a
  failed insert must never fail the review or retry it (the PR comment is
  already posted; the review is done from the user's perspective). This
  mirrors the project's existing rule that one failure must never blank
  something that already succeeded.
- `GET /api/dashboard` wraps its store calls in try/except; on a Postgres
  error it returns `200` with an explicit `"error": "data unavailable"` field
  per section instead of a 500, and the page renders a visible "data
  unavailable" banner in place of that section — same partial-failure-visible
  philosophy as the PR comment itself, applied to the dashboard.

## Testing

Following the existing deterministic test-layer pattern (`pytest`, no live
network):

- `store.record_review()` + `store.dashboard_reviews()` / `dashboard_queue_counts()`
  round-trip against the `testcontainers`-backed Postgres already used for
  `tickets` tests.
- `orchestrator.attempt_review()` test asserting `record_review` is called
  once per completed review, and that a `record_review` exception doesn't
  propagate or affect the returned `ReviewCompleted`.
- `GET /api/dashboard` via `httpx.AsyncClient` against a seeded test DB,
  asserting the JSON shape above, including the degraded-`"error"` case with
  a monkeypatched store failure.
- `GET /dashboard` smoke test asserting 200 + the polling `<script>` is
  present, `dir="rtl"`/`dir="ltr"` toggling markup exists, and both the
  theme and language controls render.
- `dispatcher.backoff_status()` unit test.
- Theme/language logic (token switching, `localStorage` persistence, RTL
  attribute toggling) is plain DOM/JS on a static page with no server round
  trip — covered by manually exercising it in a browser (light/dark/system,
  English/Hebrew, mobile widths) rather than a pytest suite, consistent with
  "For UI or frontend changes ... use the feature in a browser" in this
  project's own conventions.

## Out of scope (YAGNI)

- Auth/access control (explicitly not needed per purpose).
- WebSocket/SSE push (polling is sufficient for a demo).
- Historical charts/time-series graphs beyond the current stat tiles.
- Pruning/retention policy for the `reviews` table.
- A separate frontend framework/build step (Tailwind or otherwise).
- Translating LLM-generated finding text (descriptions/fix suggestions) —
  UI chrome only.
- Languages beyond English/Hebrew; themes beyond light/dark/system.
