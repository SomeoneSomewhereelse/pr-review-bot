# Design — Hosted-only setup guide, mandatory Render/UptimeRobot keys, doctor simplification

**Date:** 2026-09-05
**Status:** Approved for planning
**Relates to:** `guide/setup/index.md`, `guide/setup/local/*.md` (deleted),
`guide/setup/hosted/*.md` (moved), `guide/index.md`,
`guide/setup/{02-github-app,03-install-app,04-llm-provider}.md`,
`bot/scripts/doctor.py`, `bot/scripts/deploy.py`, `bot/config.py`,
`bot/.env.example`, `bot/scripts/gen_docs.py`-generated
`guide/reference/checks.md`, `guide/operations/deploy.md`, `ISSUES.md`,
`README.md`.

## 1. Problem and context

The setup guide currently documents two parallel tracks — **Local** (run on
your own machine behind a `cloudflared` tunnel) and **Hosted** (deploy to
Render + Supabase) — sharing steps 1–4 and diverging at step 5.
`bot/scripts/doctor.py` mirrors this fork: it auto-detects which track an
operator is on and grades their setup against a different 4-step tail for
each. `bot/scripts/deploy.py`, by contrast, is already hosted-only in scope
and intent (its own module docstring: "Deploy verification CLI for the
hosted Render + Supabase deployment").

Two independent simplifications are wanted:

1. **Drop the Local track outright.** It's the fork the guide, and doctor,
   don't need to carry going forward — the project only demos/operates the
   hosted path in practice. Removing it collapses a whole axis of
   conditional logic in `doctor.py` and one dimension of the guide's own
   step numbering.
2. **Make `RENDER_API_KEY` and `UPTIMEROBOT_API_KEY` mandatory** rather than
   "operator-local tooling that degrades checks to `SKIPPED` when absent."
   Several `deploy.py` checks (`boot-creds-live`, `provider-live`,
   `api-key-live`, `render-service`, `uptime-pinger`) currently treat a
   missing key as a soft `SKIPPED`; with the Local track gone, there is no
   longer a legitimate reason for an operator following this guide to ever
   run without them, so the soft path should become a hard `FAIL`.

These two land together because #2 is what lets `doctor.py`'s "hosted"
step list actually gate step 8 (the keep-warm pinger) on real completion,
rather than accepting `SKIPPED` as satisfied. `DATABASE_URL`-gated checks
(`database`, `provider`) are explicitly **out of scope** — this only
touches Render/UptimeRobot mandatoriness.

**Explicitly out of scope (per prior discussion):** the ISSUES.md Design
Gap about `deploy.py --sync-env`/`set_override.py` being redundant with the
dashboard's Environment tab is *not* being resolved here. See section 6.

## 2. Guide restructuring

- **Delete** `guide/setup/local/{05-postgres,06-tunnel,07-webhook,08-run}.md`
  outright.
- **Move** `guide/setup/hosted/{05-supabase,06-render,07-sync,08-pinger}.md`
  to `guide/setup/{05-supabase,06-render,07-sync,08-pinger}.md` (drop the
  `hosted/` path segment — it's the only path now). Update their prose to
  drop "the hosted track's"/"this track" framing (e.g. 05-supabase.md's
  opening sentence; 08-pinger.md's "Optional: let doctor verify it" section
  becomes a plain required step, and its closing "or SKIPPED where a
  credential ... was left unset by choice" caveat is deleted).
- **Rewrite `guide/setup/index.md`**: no more "choose a track" section or
  table. One linear description of all 8 steps — steps 5–8 introduced the
  same way as 1–4, just naming what they cover (Supabase, Render, sync,
  pinger) instead of branching on a track choice. The closing auto-detection
  paragraph ("`doctor.py` re-detects which track you're on...") is deleted
  since there is only one track to detect.
- **`guide/index.md`**: delete the "## Two tracks" section entirely; change
  the opening "**Needs:**" line's "a Postgres you can reach (local or a free
  Supabase project)" to "a free Supabase project."
- **`guide/setup/02-github-app.md`, `03-install-app.md`,
  `04-llm-provider.md`**: drop each file's "(Local track)"/"(Hosted
  track)"/"either track"/"both tracks" phrasing, and repoint
  `04-llm-provider.md`'s links from `local/05-postgres.md` /
  `hosted/05-supabase.md` to the single new `05-supabase.md`.

## 3. `doctor.py` — remove track branching

- Remove `TRACKS`, `resolve_track()`, the `--track` CLI argument, `_LOCAL`,
  and `steps_for()`'s branch — replaced by one flat step tuple (`_SHARED +
  _HOSTED`'s current content, now unconditional and simply named `STEPS`).
- `check_prereqs()` drops its `track` parameter and the
  `if track == "local": tools.append(_prereqs.TUNNEL_TOOL)` branch —
  `cloudflared` is no longer a prerequisite this project's guide asks for.
- `build_state()`: drop the `if track == "local":` tunnel-row block and the
  `if track == "hosted":` guard around the five Render/UptimeRobot-derived
  check calls — the latter five always run now.
- `State.public_url` becomes `ok("health")` unconditionally (was
  track-conditional between `ok("tunnel")` and `ok("health")`).
- `State.keepalive` becomes `ok("uptime-pinger")` outright. The existing
  "two steps can't share one signal" comment block and the "treat SKIPPED
  as satisfied" carve-out both go away: once `check_uptime_pinger` can no
  longer return `SKIPPED` for a missing key (section 4), the existing `ok()`
  helper's plain PASS/WARN rule already does the right thing with no
  special-casing.
- `main()`/`resolve_track` callers, `render()`, and `as_json()` drop the
  `track: hosted --` prefix from their output — nothing left to disambiguate.
- `current_step()` and `steps_for()` lose their `track` parameter.

## 4. Mandatory Render/UptimeRobot keys

- `check_boot_credentials_live`, `check_provider_live`, `check_api_key_live`,
  `check_render_service` (all currently `SKIPPED` without `RENDER_API_KEY`)
  and `check_uptime_pinger` (currently `SKIPPED` without
  `UPTIMEROBOT_API_KEY`) change their missing-key branch from `SKIPPED` to
  `FAIL`, with a detail message naming the missing var directly (mirroring
  the phrasing of an existing hard-required check like `check_config`'s
  missing-var lines).
- Their `CheckSpec.required` flags (in `deploy.CHECKS`) flip from `False` to
  `True` to match — this is what makes `gen_docs.py`'s generated
  `guide/reference/checks.md` describe them correctly (section 5).
- `bot/config.py`: the `render_api_key`/`uptimerobot_api_key` field comment
  block ("Optional operator tooling ... Absence degrades a check to
  SKIPPED, never to an error") is rewritten to describe them as required.
- **Unchanged:** `DATABASE_URL`-gated checks (`database`, `provider`) keep
  their existing `SKIPPED`-without-key behavior — explicitly out of scope.
- **Unchanged:** `sync_env()`'s existing hard requirement for
  `RENDER_API_KEY` (`--sync-env requires RENDER_API_KEY`, already an exit-2
  refusal, not a skip) needs no code change; it's already mandatory.

## 5. Generated docs and hand-written prose

- Re-run `uv run python -m bot.scripts.gen_docs` after the `CHECKS` change
  above, so `guide/reference/checks.md` regenerates: the "Always runs?"
  column flips to "yes" for the five affected rows, and the "Unskipping the
  optional checks" section shrinks to just its `DATABASE_URL` bullet (the
  `RENDER_API_KEY` and `UPTIMEROBOT_API_KEY` bullets are deleted from
  `gen_docs.py`'s `render_checks()` since there's nothing left to unskip).
- `guide/operations/deploy.md`'s hand-written prose ("Three operator-local
  keys unskip the optional checks, and none of them is...") is rewritten by
  hand to describe only the remaining `DATABASE_URL` case — `gen_docs.py`'s
  own docstring warns generated tables and surrounding hand-written prose
  can drift independently, so this file needs an explicit pass, not just a
  regeneration.

## 6. `bot/.env.example`

- The "`--- Optional operator tooling (NOT used by the deployed service)
  ---`" section header covering `RENDER_API_KEY`/`UPTIMEROBOT_API_KEY` is
  rewritten to describe them as required, e.g.:

  ```
  # --- Render & UptimeRobot (required) ---
  # Render API key (Account Settings -> API Keys): required for the
  # boot-creds-live/render-service/provider-live/api-key-live checks,
  # `--sync-env`, and the deployed service's own dashboard Environment tab.
  RENDER_API_KEY=
  # UptimeRobot read-only API key: required for the uptime-pinger check.
  UPTIMEROBOT_API_KEY=
  ```

  This also fixes a pre-existing inaccuracy: the old header claims
  `RENDER_API_KEY` is "NOT used by the deployed service," which
  `deploy.py`'s `_ALWAYS_SYNCED` comment already contradicts (the service
  needs its own copy for the dashboard's Environment tab).
- `bot/.env.config.example` needs no changes — its "Optional" mentions
  (`GITHUB_TARGET_REPO`, `PUBLIC_BASE_URL`) are unrelated.

## 7. `ISSUES.md`

Add a note to the existing "`bot/scripts/deploy.py --sync-env` and
`bot/scripts/set_override.py` are now redundant with the dashboard
Environment tab" Design Gap entry (still open, no `Status:` line — see
section 581 area) recording this session's scoping decision: that
redundancy is deliberately **not** being resolved as part of this sweep.
Record that when the bot sub-project eventually moves to its own repo, the
GitHub Pages guide and the operator scripts (`deploy.py`, `doctor.py`,
`set_override.py`) move with it — so the retire-vs-keep question for
`set_override.py` (and the rest of that gap) is deferred to that future
move rather than decided now.

## 8. Testing

- `bot/tests/` almost certainly has coverage for `doctor.py`'s
  `resolve_track`/`steps_for`/track-conditional `build_state` behavior, and
  for the SKIPPED-without-key branches of the five `deploy.py` checks
  touched in section 4 — both need updating to match (track tests deleted
  outright; SKIPPED-branch tests become FAIL-branch tests). `gen_docs.py`
  has a drift-check test (byte-for-byte comparison against committed
  `guide/reference/*.md`) that will fail until the generated files are
  regenerated and committed alongside the code change.
- Per root `CLAUDE.md`: full `uv run pytest -v` and `uv run ruff check .`
  must both be green before pushing, and the deploy Docker image
  (`docker build -f bot/Dockerfile .` + boot smoke-test) must be rebuilt
  and confirmed after merging to `main`.

## 9. `README.md`

Folded into this sweep: drop the Local/Hosted framing, add a link to this
repo's own deployed onboarding wizard, drop the hardcoded test count (it
changes too often to keep in sync), and generally keep the README
introductory — pushing narrative/technical detail behind existing
`<details>` blocks or links for readers who want to dig further, while
keeping directions (setup links, running-locally commands) plainly visible.

- **Line 13–17** ("Deploy your own"): drop "covering both a local run and a
  hosted Render + Supabase deployment" (no more second track to describe).
  New text: "the full setup guide, from a fresh clone to a first posted
  review comment. The same pages live in `guide/` if you would rather read
  them in the repo."
- **New link directly below it**: this repo's own live deployment of the
  onboarding wizard (its `render.yaml` deploys `onboarding/`, not `bot/` —
  see the existing "Repo structure" section), so visitors can try
  provisioning without cloning anything first:

  ```
  **[Try the setup wizard →](https://pr-review-engine.onrender.com/)**
  — this repo's own live deployment; provision your own bot+dashboard
  Render service from your browser, no local clone needed.
  ```

  (URL confirmed by the user: `https://pr-review-engine.onrender.com/` —
  not derived from `render.yaml`'s service name, since Render's actual
  assigned subdomain isn't statically predictable from that file; see the
  discussion that ruled out CI-time discovery.)
- **"Known limitations" section**: wrap in a `<details>` block, mirroring
  the existing "Architecture" section's `<summary><strong>Implementation
  detail</strong></summary>` pattern — one plain sentence stays visible
  ("This project deliberately deviates from `SPEC.md`'s defaults in a few
  documented ways — most notably running Groq as the primary live
  provider."), with the full deviation narrative and the link to
  `guide/background/providers.md` moved inside the collapsed block.
- **"Testing" section**: delete the "856 deterministic tests" sentence's
  specific count — replace with "The full suite is deterministic and
  network-free: every GitHub, LLM-provider, and webhook interaction is
  mocked." (same claim, no number to go stale). The CI-runs-the-same-checks
  sentence, the `manual_verify_*.py` paragraph, and the rehearsal-history
  link are unchanged — they're pointers, not narrative, and already
  lightweight.
- **Unchanged**: badges, the one-line pitch, the Architecture diagram, the
  "What a review looks like" example, "Repo structure," "Tech stack,"
  "Running locally" (including Docker), and "Cost" — none of these
  reference the Local track or the test count, and all are either already
  short directions or a visual/illustrative aid rather than narrative
  technical detail.

## 10. Non-goals

- Not touching `set_override.py` or resolving the dashboard-overlap Design
  Gap (section 6/7 above documents the deferral instead).
- Not touching `DATABASE_URL`'s optional/SKIPPED behavior anywhere.
- Not renaming or restructuring `guide/operations/*.md`,
  `guide/background/*.md`, or `guide/reference/{config,pricing,sync-env}.md`
  beyond the one hand-written prose fix in section 5.
