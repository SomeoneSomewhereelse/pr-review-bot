# Full-Project Review — Security, Performance, Code Quality

**Date:** 2026-08-11
**Scope:** Entire repository (`app/`, `tests/`, `scripts/`, `fixtures/`, `Dockerfile`, `render.yaml`, `pyproject.toml`), not a diff review.
**Method:** Three independent specialist scans (Security, Performance, Code Quality), mirroring this project's own review-engine architecture. Read-only analysis — no fixes applied.

---

## Summary

No high/critical findings. All three specialists confirmed the project's stated conventions in CLAUDE.md (partial-failure visibility, shared validate-repair layer, layering separation, HMAC-on-raw-body, no secret logging) hold up under direct code inspection, not just by documentation. Findings below are medium/low/info — mostly hardening and DRY opportunities, not defects.

| Severity | Security | Performance | Code Quality |
|---|---|---|---|
| High/Critical | 0 | 0 | 0 |
| Medium | 2 | 1 | 2 |
| Low | 1 | 1 | 2 |
| Info | 2 | 3 | 9 |

---

## 1. Security Findings

| # | File:Line | Severity | Issue | Suggested Fix |
|---|---|---|---|---|
| 1 | `app/formatting.py:90-99` (`_render_section`) | **Medium** | LLM-generated finding fields (`description`, `fix`, `issue`, `suggestion`, `type`, `category`, `file`) are interpolated into the Markdown table with no escaping of `\|`, backticks, or newlines. The PR diff is attacker-controlled (any PR author), so a crafted diff could get the LLM to emit a finding whose text breaks out of the table — e.g. injecting `\|` to add spoofed columns/rows, or a newline plus a fake `###` header — altering what reviewers actually see in the posted GitHub comment. Note this is distinct from the dashboard's `esc()` (commit 57b3cca), which only covers `dashboard.html`'s JS rendering, not the PR comment itself. | Escape `\|`, backticks, and newlines in each finding field before table insertion inside `_render_section`/`_file_line`. |
| 2 | `app/config.py:12`, `app/webhook.py:77` | **Medium** | `github_webhook_secret` defaults to `""` with no startup-time check that it's non-empty. If the env var is ever unset (misconfigured Render env, typo), `verify_signature` computes HMAC with an empty key — not a crash, but silent acceptance of any signature an attacker can also compute with the empty key: an effective auth bypass. | Fail fast at startup (in `lifespan` or a settings validator) if `github_webhook_secret` is empty. |
| 3 | `fixtures/bad_code/billing_report.py:14` | Info | Committed fixture contains a realistic-looking Stripe-style key (`sk_live_51Hj9aQqX7ZkTmvW2nP8sR3fA6bC0dE4gH`). Appears intentional — a planted issue for the specialists to catch, used by `scripts/seed_demo_pr.py` — but is shaped exactly like a real Stripe key and could trip GitHub's own secret-scanning. | If intentional, consider a value further from the real prefix format, or a code comment confirming it's synthetic (the existing "Rotated quarterly" comment reads as if real). |
| 4 | `Dockerfile` | Low | Container runs as root — no `USER` directive. Defense-in-depth gap, not a direct vuln given no untrusted file writes occur. | Add a non-root user before `CMD`. |
| 5 | `app/dashboard.py` (`/dashboard`, `/api/dashboard`) | Info | No auth on the ops dashboard; exposes repo names, PR numbers, provider/model, token counts, cost, and full LLM findings text. Documented, deliberate decision per `SPEC.md` and the latest commit — noted for completeness, not as a new issue. | Confirm the no-auth rationale explicitly considers that raw findings text (not just aggregate stats) is now exposed. |

**Checked and found clean:** secrets in tracked files (no `.pem`/`.env`/real credentials ever committed, `.gitignore` covers them); webhook HMAC verification uses raw body + `hmac.compare_digest` (constant-time); delivery dedup via bounded LRU; GitHub App JWT→installation-token flow correct, private key never logged, bot-comment spoofing mitigated; all SQL parameterized (`psycopg3` `%s` placeholders, no string-built SQL); no `shell=True` anywhere; no `pickle`/`eval`/`exec`/unsafe YAML; no secrets in any log/print call; exception text deliberately excluded from PR-comment failure footnotes; no user-controlled path construction; dependencies all pinned to recent versions; `render.yaml` marks all sensitive vars `sync: false` with no committed values; dashboard's own `esc()` is applied consistently to every interpolated field.

---

## 2. Performance Findings

| # | File:Line | Severity | Impact | Suggested Fix |
|---|---|---|---|---|
| 1 | `app/providers/google_genai.py:79`, `app/providers/groq.py:71`, `app/providers/github_models.py:80` | **Medium** | No explicit `timeout=` passed to `genai.Client`/`AsyncGroq`/`AsyncOpenAI`. Since `app/queue/dispatcher.py` is the sole serial consumer of the review queue, one hung LLM call stalls the whole queue (every pending PR), not just one ticket, for up to the SDK's default timeout (minutes). | Pass an explicit short `timeout=` (e.g. 30-60s) so a hang fails fast into the existing backoff path. |
| 2 | `app/providers/factory.py:16-24` (via `app/specialists/base.py:55`) | Low | A brand-new provider client is constructed on every specialist call — no connection/session reuse, so every LLM call pays a fresh TCP/TLS handshake. Not urgent at 20 PRs/day. | Cache one client instance per provider (lazy module-level singleton) if volume grows. |
| 3 | `app/dashboard.py:65-67` | Info | `dashboard.html` (~477 lines) is read from disk on every `GET /dashboard` request instead of being cached in memory. Harmless at current traffic. | Read once at startup into a module-level string. |
| 4 | `app/queue/dispatcher.py:280-307`, `app/static/dashboard.html:314` | Info | Dispatcher idle-polls Postgres every ~1s; dashboard polls `/api/dashboard` every 4s (3 queries per poll). Fine at free-tier scale but is the dominant steady-state DB load. | No change needed now; `dispatcher_idle_sleep_seconds` / `POLL_INTERVAL_MS` are the knobs if Supabase limits become a concern. |
| 5 | `app/queue/dispatcher.py` (design) | Info | Reviews process strictly one ticket at a time by design, to avoid concurrent LLM bursts that risk the Trust & Safety flag CLAUDE.md documents. | None — correct tradeoff, not a defect. |

**Checked and found clean:** all sync PyGithub/psycopg calls wrapped in `asyncio.to_thread`; specialist fan-out genuinely parallel via `asyncio.gather(..., return_exceptions=True)` with no intervening await; diff token cap (`annotate_and_cap`) is real and applied before every specialist call; dispatcher has real exponential backoff with jitter; DB has an index backing its one `ORDER BY` query, and all result sets are explicitly bounded; connection pooling is real (`ConnectionPool`, opened once in `lifespan`); startup work is minimal, no heavy synchronous imports at module scope.

---

## 3. Code Quality Findings

| # | File:Line | Severity | Issue | Suggested Fix |
|---|---|---|---|---|
| 1 | `app/providers/google_genai.py:29-38`, `app/providers/groq.py:32-41`, `app/providers/github_models.py:30-39` | **Medium** | The `_parse(raw_text, schema)` helper is byte-for-byte identical across all three provider adapters — exactly the duplication the project's own "narrow interfaces" convention warns against. | Move `_parse` into `app/providers/base.py` (or a small shared module) and import it. |
| 2 | `app/providers/google_genai.py:49-60`, `app/providers/groq.py:84-91`, `app/providers/github_models.py:93-100` | **Medium** | The rate-limit-translation `try/except` block is duplicated verbatim across all three adapters, differing only in the awaited call. | Factor into a shared async context manager/decorator in `app/providers/base.py`. |
| 3 | `app/queue/store.py:338-353` | Low | `mark_failed(ticket_id, now, error=None)` accepts `error` but never persists it — docstring admits it's unused, yet `dispatcher.py:236` passes `error=str(exc)` as if it mattered. | Either add an `error` column now, or drop the parameter until needed. |
| 4 | `app/dashboard.py:22-25` | Low | `_KNOWN_PROVIDERS` is a third hand-maintained copy of the provider list (alongside `factory.py` and `config.py`) — a fourth provider added to `factory.py` won't show up on the dashboard until this list is also updated. | Add one shared constant (e.g. in `app/providers/base.py`) instead of a third copy. |

**Checked and found clean:** no dead code, no commented-out blocks, no `TODO`/`FIXME`/`XXX` anywhere in `app/`, `scripts/`, or `tests/`; every broad `except Exception` is annotated `# noqa: BLE001` with a rationale, never a silent swallow; the shared validate-and-repair layer (`app/providers/validate.py`) is genuinely shared, not duplicated per provider; layering matches CLAUDE.md exactly (`formatting.py` has no LLM knowledge, `orchestrator.py` never reaches into provider internals, specialists know nothing about GitHub); partial-failure visibility is enforced in code, not just documented, and covered by tests; provider adapters normalize `tokens_in`/`tokens_out` identically across all three; the three specialists and their schemas are structurally parallel with no asymmetry; every `app/` module has a corresponding test file, including edge cases (malformed JSON, off-schema JSON, rate-limit retry-after parsing, dispatcher backoff, dashboard degrade-on-error); naming and docstrings are consistently clear about *why*, not just *what*.

---

## Recommended priority order

1. Escape LLM-generated finding text before Markdown table insertion (`app/formatting.py`) — the one finding with a real attacker-controlled path into the posted PR comment.
2. Fail fast at startup if `GITHUB_WEBHOOK_SECRET` is empty.
3. Add explicit timeouts to the three LLM provider clients — protects the single-serial dispatcher from a full-queue stall.
4. De-duplicate `_parse` and rate-limit-translation logic across the three provider adapters.
5. Everything else (Dockerfile non-root user, dashboard HTML caching, `mark_failed`'s unused `error` param, provider-list triplication) — low-priority hardening/cleanup, no urgency.
