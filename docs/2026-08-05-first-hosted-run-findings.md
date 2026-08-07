# First hosted Render + Supabase run — findings

**Date:** 2026-08-07
**Status:** Complete
**Relates to:** `docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md`
(resolved by this run), `docs/superpowers/specs/2026-08-05-deploy-command-design.md`
(unblocked by this run), `docs/superpowers/plans/2026-08-05-supabase-first-deploy-hardening-and-first-hosted-run.md`
(the plan this run executed, Tasks 6-13)

## Tooling lane

**Fast lane** (Render API key) for the whole run. `RENDER_API_KEY` was created
for this run and is revoked as part of closing it out (§8). No step's pass
criteria differ between lanes per the plan's design, but the fallback (manual
dashboard + pasted logs) was **not independently exercised** this run and
should be treated as unverified until someone actually runs it.

## The conclusive result

This is the empirical answer the whole investigation existed to produce:

```
SELECT to_regclass('public.tickets')
  before deploy (Task 7): None
  after deploy  (Task 9): 15 columns, set-equal to Ticket.__dataclass_fields__
```

`app/queue/store.py`'s `init_pool()` created the full `tickets` schema,
unattended, on the app's first boot against a real, freshly-provisioned
Supabase project — through the Session-mode pooler, with no manual SQL, no
privilege issue, no TLS issue, and Task 1's diagnostic `RuntimeError` wrapper
never fired because the connection succeeded on the first attempt (confirmed
by its total absence from two observed boot logs). All three of the original
handoff doc's mechanical unknowns (TLS, cold-project timing, pooler
privileges) are resolved as **no gap**, now by direct observation rather than
documentation research alone.

## Divergences from the corrected docs

1. **No remote GitHub repo existed for this project's own source.** The plan
   assumed one did, for Render's Blueprint deploy to point at — only the
   separate testbed repo (`GITHUB_TARGET_REPO`) was real. A genuine plan gap,
   not an execution mistake. Resolved by creating
   `https://github.com/SomeoneSomewhereelse/pr-review-bot` and pushing the
   worktree branch (`master` + all 6 Phase-1 hardening commits) as its `main`,
   so the hosted run tested exactly the hardening this run exists to validate.

2. **Git/GitHub auth took several attempts in this sandbox**, worth recording
   for anyone repeating this setup:
   - Plain HTTPS push failed outright (no stored credential).
   - The SSH remote failed (`Host key verification failed`,
     `ssh_askpass: exec(...): No such file or directory`) — this sandbox
     cannot interactively accept a new SSH host key.
   - `gh auth setup-git` is config-mutating and was blocked by the session's
     auto-mode permission classifier when run from the assistant's own tool
     calls; had to be run by the operator directly.
   - Even then, HTTPS still failed (`could not read Username`) — `gh`
     resolved to a **symlink to the Windows `gh.exe`** binary
     (`/home/emanresu/.local/bin/gh -> /mnt/c/Program Files/GitHub CLI/gh.exe`),
     whose config lived at a Windows path
     (`C:\Users\Home\AppData\Roaming\GitHub CLI\hosts.yml`), never synced to
     the WSL-side git config this shell's `git` actually reads.
   - This same Windows/WSL split later caused `gh repo clone` (used by
     `scripts/seed_demo_pr.py`) to fail in two different ways: an SSH
     host-key failure, then — after switching `gh`'s protocol to HTTPS — a
     silent path-translation failure (the Windows binary's child `git`
     process couldn't resolve a WSL-style temp path passed to it).
   - **Root fix**: installed native Linux `gh` in this WSL distro (the
     official apt-based install) and removed the old symlink from `PATH`.
     This resolved the whole class of bug at once — credential helper,
     protocol config, and path translation all became WSL-native and
     consistent. A native WSL git identity (`user.name`/`user.email`) also
     had to be configured from scratch, since it had never been needed
     before this run.
   - Intermediate fix (superseded by the native install, but useful if that
     path isn't available): `git config --local credential.helper
     '!gh auth git-credential'`, scoped to the worktree only.

3. **GitHub's secret-scanning push protection correctly fired** on
   `fixtures/bad_code/billing_report.py:14`'s fake Stripe-shaped key
   (`sk_live_51Hj9aQqX7ZkTmvW2nP8sR3fA6bC0dE4gH`) — this is the project's own
   intentional planted-bad-code fixture (SPEC.md/README.md/the demo plan bait
   for the Security specialist), not a real credential. The operator allowed
   it via GitHub's one-time unblock-secret URL after confirming its purpose.
   No code change; a one-time, per-secret GitHub-side allow.

4. **`GITHUB_TARGET_REPO` was missing from `.env` entirely** going into this
   run (never previously needed) and had to be added. `.env` and the App's
   private-key PEM also needed symlinking into the worktree, since both are
   gitignored and `git worktree add` never copies untracked files.

5. **Docker Desktop's WSL integration** needed enabling for this distro
   before any DB-backed local test could run (group membership only takes
   effect in a fresh login shell — an already-running shell doesn't pick it
   up), and Docker itself dropped/needed restarting twice more during the
   session, unrelated to anything in this repo.

6. **`tests/conftest.py`'s local-Postgres fallback had a pre-existing,
   previously-invisible bug**, found and fixed (commit `a6b5b5b`, its own
   commit, outside any plan task's scope, user-approved): `PostgresContainer
   .get_connection_url(driver="psycopg")` always builds a SQLAlchemy-style
   `postgresql+psycopg://` URL, which raw psycopg3 cannot parse at all. This
   was invisible until Docker actually became reachable in this session — CI
   sets `DATABASE_URL` directly and never calls this method, and every local
   run before this session always failed earlier on
   `docker.errors.DockerException`, before this code path ever ran. Fixed
   with `driver=None`.

7. **A Docker Desktop + WSL2 testcontainers quirk**: the Ryuk reaper
   sidecar can hang indefinitely waiting for a handshake ack that never
   arrives. Worked around locally via `TESTCONTAINERS_RYUK_DISABLED=1` — not
   applicable to the hosted run itself, since Supabase is a real Postgres,
   not testcontainers.

8. **The worktree's own checkout directory was found deleted mid-session**
   (branch and all commits survived intact in git; only the working tree was
   gone) — recovered via `git worktree add` from the existing branch. Its
   gitignored `.superpowers/sdd/` ledger was lost with it and reconstructed
   from `git log` plus the ledger's own prior entries (see that skill's
   documented recovery path). Separately, creating this worktree broke
   `claude --resume`'s session listing again, a previously-documented and
   now-reconfirmed recurring issue in this project's dual WSL/Windows
   filesystem setup.

## Measured numbers

- **Happy-path latency** (PR open → bot comment, real hosted deploy, warm
  instance): **~9.2s**, comfortably under the 15s target.
- **Render redeploy duration** (env-var change → `live`, via the API's
  explicit deploy trigger — env changes do not auto-deploy):
  - `groq` → `github_models`: **65.5s**
  - `github_models` → `groq`: **56.7s**

  This replaces the demo plan's assumption of a ~2-second local `uvicorn`
  restart. **The demo plan's Segment B narration timing needs updating** to
  account for a ~60-90s redeploy window per provider swap on the hosted
  stack.
- **Segment C (quota exhaustion) did not reproduce.** Fired 4 new PRs plus a
  follow-up commit on the happy-path PR in quick succession, per the plan,
  exactly once (CLAUDE.md's LLM hygiene rules — not retried). All 5 reviews
  in that burst (plus the earlier one) completed successfully within ~90
  seconds — roughly 26.5K tokens, exceeding the demo plan's `2026-08-03`-dated
  12K TPM measurement — with zero `429`s, zero `deferred`/`retrying` tickets.
  This is a genuine finding, not an execution failure: Groq's actual current
  rate-limit headroom for this account exceeds what was measured four days
  earlier. **The demo plan's Segment C choreography should be re-validated
  against Groq's current limits** before being relied on for a live
  presentation — it may need more simultaneous PRs, a tighter firing window,
  or updated token math to reliably reproduce a `429` today.
- **Persistence across restarts is now a real, verified claim.** Ticket
  `id=1` (the happy-path PR) was byte-for-byte unchanged across both Segment B
  redeploys — not meaningful under the old local-SQLite setup, genuinely true
  against Supabase.
- **The re-review escalating cooldown fired correctly** when a follow-up
  commit was pushed to a just-reviewed PR's branch: the ticket went
  `deferred` with `not_before` ~5 minutes out (`dispatcher_rereview_cooldown_seconds`),
  rather than instantly re-reviewing — expected behavior, not a stall. Once
  due, the same comment (`comment_id` unchanged) updated in place with real
  findings from the restored provider.

## A genuine code-level finding: `/healthz` didn't support `HEAD` — fixed

The keep-warm pinger (UptimeRobot) never successfully verified the service,
for two stacked reasons:

1. **Operator config mistake** (real, but not the root cause): the monitor
   URL had a trailing comma (`/healthz,`), producing 14 straight `404`s over
   a 71-minute window despite firing exactly on its 5-minute schedule.
   Fixed by the operator.
2. **The actual root cause, found after the URL fix**: UptimeRobot's
   free-tier HTTP(s) monitor sends `HEAD` requests by default. Tested
   directly against the live endpoint:

   ```
   GET     /healthz -> 200
   HEAD    /healthz -> 405
   OPTIONS /healthz -> 405
   POST    /healthz -> 405
   Allow: [GET]
   ```

   `app/main.py`'s `/healthz` route only registered `GET`. This was a
   **code-level finding**, not an operator mistake or a docs gap — no
   dashboard setting could fix it.

   **Fixed after the run closed** (commit `ed4ec55`, TDD: a failing
   `test_healthz_supports_head` in `tests/test_skeleton.py` asserting
   `client.head("/healthz")` returns 200, then `@app.head("/healthz")` added
   alongside the existing `@app.get`). Pushed to the Render-connected
   remote's `main`; the Blueprint service auto-deployed on push (no manual
   redeploy needed, no `RENDER_API_KEY` required — it had already been
   revoked by this point). Reverified directly against the live URL
   afterward: `GET` and `HEAD` both return `200 {"status":"ok"}`.

   This sub-check was recorded as unverified at the time the run itself
   closed (per the plan's own "label unexercised paths unverified"
   principle, since no repo changes were allowed mid-run) — it is now
   resolved and verified live, just outside the run's own boundary. Every
   other Task 9 check (boot log, `/healthz` via `GET`, schema creation,
   webhook registration) had already passed conclusively during the run
   itself.

## `check_database` recommendation for `/deploy`

Confirmed empirically in this run: a plain `psycopg.connect(...)` against a
correctly-formed Supabase pooler URL succeeds in well under a second once the
project is ready. Recommend `check_database` use a short-timeout
`psycopg.connect` + `SELECT 1` directly — **not** the app's
`ConnectionPool`/`init_pool()` — so a real connection failure reports
immediately with the driver's own error, rather than waiting out the pool's
30-second timeout to surface Task 1's (correct, but startup-oriented)
diagnostic message. Optionally, `check_database` could also assert `tickets`
exists via `to_regclass`, now that this run has shown that's a meaningful,
fast, read-only check — worth deciding when `/deploy`'s design resumes,
rather than prescribing it here.

## Recommended follow-ups

1. ~~Add `HEAD` support to `/healthz`~~ — **done**, commit `ed4ec55`, verified
   live.
2. Re-validate the demo plan's Segment B timing (real ~60-90s redeploys, not
   ~2s local restarts) and Segment C token math (current Groq headroom
   exceeds the `2026-08-03` measurement) before relying on it for a live
   presentation — this was flagged as pending in Task 5 and remains pending.
3. Consider whether the fast lane's Render API key should become a documented,
   first-class part of `/deploy`'s design (it materially reduced setup risk
   in this run — see the plan's own tooling rationale) now that it has been
   exercised end-to-end.

## Post-run addendum (same day)

After the run and its findings were committed, the branch was merged to
`master` locally (fast-forward, `69802b9..3aaddc7`) and the worktree removed.
The `/healthz` `HEAD` fix above was then implemented via TDD directly on
`master` (commit `ed4ec55`) and pushed to the Render-connected remote, which
auto-deployed it without any manual redeploy step or API key. The pinger
issue is now fully resolved, not merely diagnosed.
