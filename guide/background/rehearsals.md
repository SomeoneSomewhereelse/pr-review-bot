# Live rehearsal history

This project has been run for real multiple times during development — not
just described — through the actual GitHub webhook delivery path (not a
direct function call or synthetic payload), against both a local tunnel and
the deployed Render service. This page is the log of those runs: which PR,
which path was exercised, and the measured result. It's the evidence that
the numbers quoted elsewhere in this guide (the 15-second review target, a
redeploy's real duration) are measured, not assumed.

| PR | Path exercised | Result |
|---|---|---|
| #2 | Direct `orchestrator.run_review()` call (step 5/6 milestone) | 8s, all 3 specialists ok, comment correct |
| #2 | `demo_provider_swap.py` (step 7) | groq ok → gemini fails gracefully (real error) → groq restored |
| **#3** | **Real GitHub webhook delivery** (quick tunnel + `PATCH /app/hook/config` JWT-updated URL + `seed_demo_pr.py`) | **8s** PR-created → comment-appeared, all 3 specialists found real issues |
| **#4** | **First hosted run** (Render + Supabase, 2026-08-07) — happy path, `seed_demo_pr.py` against the deployed service | **~9.2s** PR-created → comment-appeared, real findings via groq; `tickets` created by the app's own first boot against a real Supabase project (see `docs/2026-08-05-first-hosted-run-findings.md` for full detail) |
| **#5** | **Hosted Segment B** — `LLM_PROVIDER=github_models` redeploy (real 2026-07-30 retirement — see [Provider history](providers.md)) → all 3 specialists fail visibly → `groq` redeploy → follow-up commit → same comment updates in place | Redeploys **65.5s** / **56.7s** (not a 2s local restart); ticket survived both restarts intact |
| **#6-#9** | **Hosted Segment C** — 4 new PRs + a follow-up commit fired in quick succession under `groq` | **No 429 observed** (current Groq headroom exceeds the 2026-08-03 token-math measurement; not retried, per the testing-hygiene rules in [Provider history](providers.md)) |

**PR #3 is the definitive rehearsal.** It's the first run of the *actual*
webhook path end-to-end — GitHub → tunnel → HMAC verify → dedup →
background task → orchestrator → comment — not a direct function call or a
synthetic payload, and it's the run most often cited elsewhere as proof the
15-second review target holds: the comment appeared **8 seconds** after PR
creation, all three specialists found real issues, well under target.

PRs #6-#9 are the closest this project came to a sustained-volume test: four
new PRs plus a follow-up commit fired in quick succession, with no `429`
observed. That's a useful data point, not a guarantee — it wasn't retried at
higher volume, deliberately, per the same testing-hygiene discipline that
governs live LLM calls (see [Provider history](providers.md)).
