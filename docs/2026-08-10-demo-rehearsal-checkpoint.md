# Zoom demo rehearsal — checkpoint

**Date:** 2026-08-10
**Status:** Rehearsal complete for all planned segments. Two follow-up threads
deliberately parked, not forgotten — see "Still open" below.
**Relates to:** `docs/superpowers/specs/2026-08-03-demo-plan-design.md` (the
rehearsed plan, now reflecting measured behavior throughout),
`docs/2026-08-10-deploy-provider-credential-verification-gap.md` (one of the
two parked threads, its own handoff doc).

## What happened this session

Rehearsed the full demo plan against the live Render + Supabase service —
happy path, Segment B (dead-vendor swap + cooldown heal), Segment C (quota
exhaustion + auto-recovery) — and fixed two real bugs found only by actually
running it:

1. **The deployed service was 61 commits stale.** `origin/main` (the
   Render-connected remote) hadn't been pushed to since Aug 7, so the entire
   DB-backed provider-override feature didn't exist in what was running.
   Fixed: pushed (fast-forward, `07348f7`), redeployed.
2. **`AsyncGroq`'s default `max_retries=2` silently retried 429s**, hiding
   them from the app's own `RateLimited` handling — Segment C's premise
   regardless of load sizing. Fixed: `app/providers/groq.py` now passes
   `max_retries=0` (commit `6397cfc`), pushed, redeployed, re-verified live.

Also re-verified Gemini works again (separate from this rehearsal — see
`README.md`/`SETUP.md`'s 2026-08-10 entries) and tried it for Segment C twice
(small and oversized fixtures); ruled out again, this time because the
account's real limits are high enough that neither attempt tripped a 429.
Groq remains the quota-exhaustion provider.

The demo plan doc (`docs/superpowers/specs/2026-08-03-demo-plan-design.md`)
is fully rewritten with measured numbers and a corrected Segment B narrative
(the "bonus" cooldown demo on a separate PR was cut — Segment B's own
re-review naturally demonstrates it).

## Cleanup done

- Closed 9 leftover open PRs (#1-9) from earlier hosting-migration
  rehearsals, plus 6 more opened and superseded during this session's own
  testing (#13, #14, #17, #18, closed as superseded when re-run after a fix).
- PRs left open on the testbed repo from this session (working demo
  material, not clutter): #10 (happy path), #12 (Segment B), #15/#16
  (Segment C, Groq), #19/#20/#21/#22 (Gemini exploration, not part of the
  final plan but real successful reviews, harmless to leave).
- DB provider override left **cleared** (falls back to `.env`'s
  `LLM_PROVIDER=groq`, the demo's actual resting state).

## Still open (deliberately parked, not forgotten)

### 1. More Groq testing + hardening

Finding 3 in the rewritten demo plan flagged that Groq's request-count
bucket refills far slower (~1 slot/86s) than its token bucket (~200 tok/s),
and that a rate-limited ticket's own retry can push its wait *later* under
sustained load (observed live: one ticket's ETA moved from 09:56 UTC to
10:41 UTC on its own scheduled retry). This is recorded as an accepted risk
in the demo plan, not yet investigated further or hardened against. Revisit
when picking this back up — no separate handoff doc written for this one
yet, the demo plan's "Rehearsal findings" §3 has the detail to resume from.

### 2. The deploy CLI's credential-verification gap

A real, live reproduction of a limitation the deploy-hardening spec already
named but arguably under-solved: switching to Gemini via the DB override
reported clean (`provider: PASS`) locally while `GEMINI_API_KEY` had never
actually been pushed to Render, so every real review failed regardless of
what the CLI reported. Full context, why it happens, and open questions for
a proper brainstorm live in
`docs/2026-08-10-deploy-provider-credential-verification-gap.md`.

## Resume by

Reading this file plus whichever of the two "still open" threads is being
picked up next. The demo plan itself needs no further work unless the
Groq-hardening thread changes provider behavior again.
