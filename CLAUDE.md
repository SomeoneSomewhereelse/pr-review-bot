# CLAUDE.md — Autonomous Code Review Engine (Project ד)

## Project

Full design lives in `SPEC.md`; cost model in `cost.md`. Deployed as a Docker
container on Render (free tier) with the queue in Supabase Postgres, kept warm
by a free external pinger — see `cost.md` for the alternatives that were weighed.

Module boundaries and per-module contracts live in `app/CLAUDE.md`, which loads
when working under `app/`.

## Conventions

- Async throughout; one-purpose modules with narrow interfaces.
- Secrets only via env vars; **no secret is ever logged**. This also binds an
  agent's own ad hoc shell commands during manual/operational work, not just
  application code — never `cat`/`tail`/`head`/`echo` a file or variable known
  to hold secret material, even to check "just the last few characters" of a
  base64 blob. Verify a secret was written correctly structurally instead
  (length via `wc -c`, presence via `grep -c`, or a hash comparison), never by
  displaying any byte of the value. (A `tail -c 20` on a `.env` line holding a
  service-account key leaked a real fragment into a conversation transcript —
  see `ISSUES.md`.)
- **Never commit on someone else's behalf without being asked**, even to reach
  a clean working tree. If resolving a merge or other cleanup requires
  temporarily setting aside someone else's pre-existing uncommitted changes
  (e.g. via `git stash`), restore them **uncommitted**, exactly as found —
  committing them for tidiness is still an unrequested commit.
- **Partial failure is always visible** in the PR comment (a failed specialist
  renders a real row) — never silently dropped.

## Substitutions from the brief (and why)

- **`google-genai`** instead of the legacy `vertexai.generative_models` SDK —
  same Vertex backend, and it is what makes the one-env-var provider swap trivial.
- **`gemini-flash-latest`** instead of `gemini-2.5-flash` — the brief's model is
  deprecated/removed. The alias is pinnable to a dated version via env for demo
  reproducibility.
- **`vertex` adapter reinstated (2026-08-14)** — it was removed when Vertex AI's
  payment-card requirement collided with this project's no-card constraint (see
  SETUP.md §2), leaving it live-unrunnable and mock-only. GCP billing/ADC access
  later became available, so `vertex` is back as a real, live-runnable third
  provider, matching `SPEC.md`'s stated default. Its credential is a GCP
  service-account identity rather than an API-key string:
  `GCP_SERVICE_ACCOUNT_KEY_B64` (hosted, numbered slots) → a local key file →
  implicit ADC, resolved in `app/providers/vertex_credentials.py`. No secret
  reaches Postgres — only the slot index, exactly as for gemini/groq.

## Cost

Documented production total ≈ **$8–10/mo** at brief scale (20 PRs/day). The demo
runs at **$0** on free tiers + the $300 GCP trial credit. Cost is graded as a
documented calculation, not as actual spend — see `cost.md`.

## LLM API testing hygiene (avoid Trust & Safety flags)

Gemini AI-Studio access got **account-level blocked** (`403 PERMISSION_DENIED:
Your project has been denied access`) during this build, confirmed across
multiple models, multiple projects, and multiple separate Google accounts —
per Google's own AI Developer Forum, this is an automated Trust & Safety flag,
and one documented trigger is **hitting repeated 429s / testing many models
back-to-back without backoff**, which is exactly what happened during
troubleshooting here. The only documented fix is attaching GCP billing, which
this project's setup deliberately avoids (see SETUP.md) — so once flagged, a
provider is effectively lost for the rest of the demo. (**Update, 2026-08-10:**
a later API key update resolved this specific block — see SETUP.md §2 — but
that doesn't change the rule below; a flag is still a real risk that this
discipline exists to avoid, not something to rely on being reversible.)
**Rules to avoid repeating this:**

- **Never loop/burst live calls across many models or keys** to "see what
  sticks." One deliberate, single live call per real verification need.
- **Prefer mocked/cassette tests for exploration.** Reserve real network calls
  for the one live-verification step a build step actually requires (per
  SPEC.md section 8's testing strategy) — not for debugging or model-shopping.
- **If a provider starts returning 403/429, stop calling it immediately** and
  investigate via docs/support channels rather than retrying with different
  models/keys in quick succession — retrying does not help and each attempt
  is one more data point that can reinforce an abuse-pattern flag. This
  extends to OAuth/auth-layer failures too (e.g. `invalid_scope`,
  `RefreshError`) — same failure shape, same stop-and-diagnose principle,
  not a "try a different scope/key" situation.
- **The "one deliberate live call" limit is about generation/completion
  requests** — the ones that cost money and carry provider-abuse-flag risk.
  It does **not** apply to lightweight metadata/listing calls (e.g. checking
  whether a model ID exists in a provider's catalog). Checking several
  candidate values via a listing/existence endpoint in one pass is fine, and
  is the right way to narrow down configuration *before* making the one
  deliberate generation call — not a workaround for the rule above.
- This applies to **any** LLM provider's free tier, not just Gemini — Groq and
  future alternatives should get the same restraint.

## Plan-execution / multi-agent process hygiene

Lessons from running Superpowers-style plans through subagent-driven
development on this project (see `ISSUES.md` for the incidents these
generalize from):

- **A task brief's "stop and report" instruction is a hard stop, not a
  suggestion.** If an implementer hits an unpredicted failure a brief says to
  stop on, it must actually stop and return control — not self-resolve the
  problem and mention the deviation in its report afterward. A controller
  reviewing a report after the fact cannot approve or reject work that has
  already been done; by the time it reads "I deviated because...", the
  deviation has already happened.
- **When correcting or overriding part of a multi-sentence passage, re-read
  the whole passage afterward for internal consistency** — not just the
  clause that was changed. A targeted fix to one sentence is exactly the kind
  of edit that leaves a contradiction elsewhere in the same passage
  undetected by the person who made it.
- **Task-scoped review checks conformance to the brief, not correctness of
  the brief itself.** Code a plan hands an implementer verbatim — especially
  for external-API/auth integration (credential construction, OAuth scopes,
  client setup) — needs the same scrutiny as any other code. Matching the
  brief exactly does not mean the brief was right; a bug embedded in a plan's
  own provided snippet will sail through every task-scoped review that only
  checks "does this match what was asked." Flag this class of code for extra
  suspicion specifically at final/whole-branch review, and don't assume
  a whole-branch review that already had per-task reviews pass is redundant —
  it is often the first review that would even think to distrust the plan's
  own code.
- **Documentation describing the outcome of a live-verification step must be
  written after that step actually runs, not drafted in advance assuming
  success.** If a plan's task text describes what a doc should say about a
  pending live call's result, treat that text as a placeholder to revise
  based on the actual outcome, not as literal instructions to transcribe.
- **When a plan is authored in the same session that will execute it via a
  worktree-based flow, write or commit the plan file *inside* the worktree**
  (or commit it to the branch before creating the worktree). Writing a file
  to the main checkout and then branching off via `git worktree add` leaves
  that file invisible to the new worktree, since worktrees only materialize
  committed content.
- **Before merging a feature branch into any target branch, check the
  *target* branch for pre-existing uncommitted changes first** (`git status`
  there, not just on the branch being merged in) — a conflicting local edit
  or untracked file on the target can fail the merge in a way that's
  confusing to debug from the merge failure alone.
