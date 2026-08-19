# Design — Test suite execution time: root causes and candidate fixes

**Date:** 2026-08-19
**Status:** Draft — for brainstorming, not yet approved for planning
**Relates to:** `tests/conftest.py` (`db_url`, `db` fixtures), `tests/test_github_app.py`
(`_throwaway_app_credentials`), `pyproject.toml` (`[tool.pytest.ini_options]`,
dev dependency group), the subagent-driven-development execution pattern used
for this project's staged plans (many separate `uv run pytest` process
invocations per plan, one per dispatched subagent).

## 1. Problem

The full suite (821 tests as of this writing) takes **~54 seconds** per
`uv run pytest -q` invocation, run serially, on a 24-core machine with
`pytest-xdist` not installed. That is not slow in isolation — but this
project's own development pattern (subagent-driven-development: one fresh
implementer + one fresh reviewer subagent per plan task, each independently
running `ruff check` + a full `pytest` pass + `mkdocs build --strict` before
reporting) means the full suite gets re-run **many times per plan**, each in
its own OS process, with no state shared between runs.

Concretely, from Stage 3b's own execution log (`tmp.md`, this session,
29 subagent dispatches across 10 tasks + review/fix rounds): the sum of
subagent-reported wall-clock time was **~8.6M ms (≈144 minutes)** across the
stage. A meaningful and fully avoidable fraction of that is redundant,
per-process fixed costs that have nothing to do with the actual work being
verified — found by directly profiling this session's own test runs, not
estimated:

1. **No persistent test Postgres.** `tests/conftest.py:27`'s `db_url`
   fixture is `scope="session"`, but "session" means *per pytest process*.
   `DATABASE_URL` is unset in this environment and no Postgres container is
   left running between invocations, so **every separate `pytest`
   invocation pays testcontainers' ~3.85s container-boot cost from cold**
   (measured directly this session — see the `setup` phase duration on
   whichever test happens to first request the `db` fixture). Across
   ~20-30 separate subagent-run `pytest` invocations in a single stage, that
   is **roughly 1.5-2 minutes of pure, structurally-avoidable container-boot
   overhead** — the same fixed cost paid over and over for no reason, since
   nothing about the container's state needs to differ between invocations.

2. **No `pytest-xdist` despite 24 idle CPU cores.** The suite runs fully
   serial. The large majority of the 821 tests are independent, mocked-
   boundary unit tests (per this project's own stated testing philosophy —
   see `CLAUDE.md`'s "LLM API testing hygiene" section and the general
   mock-the-SDK-boundary pattern used throughout `tests/`), which should
   parallelize cleanly. The one real hazard: ~13 test files touch the `db`
   fixture (`tests/conftest.py:58`), which does `store.close_pool()` /
   `store.init_pool()` and a `TRUNCATE tickets, runtime_config, reviews` per
   test against **one shared Postgres instance** — running those across
   multiple `xdist` workers concurrently would race (one worker's `TRUNCATE`
   wiping another's fixture setup mid-test).

3. **A confirmed, isolated, zero-ambiguity inefficiency**:
   `tests/test_github_app.py:51`'s `_throwaway_app_credentials` fixture is
   `@pytest.fixture(autouse=True)` at **function scope**, and generates a
   fresh 2048-bit RSA key (`rsa.generate_private_key(...)`, directly
   measured at ~45ms on this machine) for **every one of that file's 33
   tests**, even though nothing about test correctness depends on the key's
   specific value — it is a throwaway signing key used only for local JWT
   round-trips, per the fixture's own docstring ("every HTTP call is mocked
   below, so nothing is ever sent anywhere with it"). Regenerating it 33
   times instead of once costs ~1.4s for zero benefit.

4. **No test markers separating "fast/unit" from "needs Postgres."**
   There is currently no way to run "everything except the DB-touching
   tests" without hand-picking files by name. This matters specifically for
   the subagent-driven-development iteration loop: a task that never touches
   `app/queue/store.py`-adjacent code still has no cheap way to skip the
   DB-fixture-touching subset during its own inner edit-test-edit loop, and
   ends up paying the DB-container-boot-plus-DB-test cost even when
   iterating on something unrelated (e.g. a pure documentation task, which
   is most of what this project's `guide/` stages actually do).

5. **Process-level, not a test-suite defect per se, but compounds all of the
   above**: task dispatch briefs in this project's SDD executions
   consistently ask each implementer/reviewer to run the full suite **twice**
   — once to "confirm baseline" at the start of a task, once as the final
   pre-commit check — even for narrow, single-file tasks where the
   immediately-prior task's own close-out had *just* confirmed a green
   baseline seconds earlier. This is not wrong (a defensive habit), but on a
   24-second-vs-2-minute test suite the calculus for "is re-confirming worth
   it" changes: right now it's cheap insurance; if the fixes below succeed
   in cutting single-run time to a few seconds, this doubling becomes
   genuinely free and not worth engineering around; if they don't, it stays
   worth revisiting.

### Not investigated further (flagged, not root-caused)

`tests/test_github_app.py`'s slowest individual tests (several around
0.78s-1.03s in the `call` phase specifically, i.e. not fixture setup) were
profiled far enough to rule out the RSA-keygen fixture as the primary cause
(setup phase for these tests measured only 0.03-0.07s) and to rule out a
real network stall (the file monkeypatches
`requests.adapters.HTTPAdapter.send` globally via its `fake_transport`
fixture, so no unmocked call can reach the network). The remaining
hypothesis — PyGithub's `Github(auth=Auth.AppAuth(...))` client being
reconstructed fresh per top-level call within `app/github_app.py`
(`_app_jwt_client()`, `get_installation_client()`), each doing its own
JWT-encode/PEM-parse — was not confirmed with a clean profile (a `cProfile`
run was dominated by cold-import/collection overhead specific to running one
test file in isolation, which is not representative of its cost inside a
warm full-suite run). Worth a proper `py-spy`/`pytest --durations` pass
scoped correctly if `test_github_app.py` specifically becomes a target.

## 2. Candidate fixes

Ranked by confidence and effort, not necessarily the order to build them —
that's for the planning session.

### 2a. Persistent local test Postgres (highest confidence, lowest effort)

Document (and/or script) starting one long-lived Postgres container once —
`docker run -d --name pr-review-test-pg -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16-alpine`
— and exporting `DATABASE_URL` to point at it for the duration of a
development/SDD session. `tests/conftest.py:29`'s existing `if env_url:`
branch already handles this path (with its existing
`_looks_like_local_test_db` safety guard, unchanged) — **no test code
changes needed**, only a documented/scripted operator step. Every
subsequent `pytest` invocation in that shell skips testcontainers' cold
boot entirely.

Open question for the planning session: where does this belong —
`guide/setup/01-prerequisites.md` (operator-facing, since it's not just an
SDD-session optimization but a general "fast local iteration" tip), a
`scripts/` helper that starts/verifies the container idempotently, or both?

### 2b. `pytest-xdist` with DB tests grouped to one worker

Add `pytest-xdist` to `pyproject.toml`'s dev dependency group. Mark every
test that (transitively) requests the `db` fixture with
`@pytest.mark.xdist_group(name="db")` so `xdist` schedules all of them onto
the same worker (avoiding cross-worker `TRUNCATE` races against the one
shared Postgres) while every other test fans out freely across the
remaining workers. Run with `-n auto` (or a fixed count — 24 logical cores
may not all be usable/desirable; the planning session should pick a
sensible default, e.g. `-n 8`).

Open questions: does `xdist_group` need to be applied per-test or can it be
applied at the module level for the ~13 affected files in one line each
(likely the latter, via `pytestmark`)? Does CI's own `services: postgres`
container (referenced in `tests/conftest.py`'s module docstring) tolerate
concurrent connections from a single `xdist`-grouped worker the same way
local testcontainers does — should be yes since it's the same fixture path,
but worth confirming against the CI job definition
(`.github/workflows/ci.yml`'s `lint-and-test` job) rather than assuming.

### 2c. Fix `test_github_app.py`'s per-test RSA keygen

Change `_throwaway_app_credentials` (`tests/test_github_app.py:51`) from
its current implicit function scope to `scope="module"`. Confirmed safe:
the fixture's own docstring already states the key's only purpose is local
JWT-signing round-trips with every HTTP call mocked — no test depends on
the key's value differing from another test's. Saves ~1.4s in this file
alone; worth a repo-wide grep for the same
`autouse` + function-scope + expensive-crypto-or-IO-setup pattern in case
it recurs elsewhere (not confirmed to recur — this was the only instance
found this session, but the grep wasn't exhaustive across every test file).

### 2d. Test markers for fast iteration

Introduce a `db` marker (or similar name — bikeshed in planning) applied to
every test that requests the `db` fixture (possibly the same marker used
for 2b's `xdist_group`, or a separate one if the two need to vary
independently). Document `pytest -m "not db"` as the fast-iteration command
in whatever this project's equivalent of a "how to run tests" doc is
(currently `README.md`'s trimmed Testing section, post-Stage-3b — see
`docs/superpowers/plans/2026-08-18-setup-experience-stage-3b-guide-site.md`'s
outcome). This is the most direct answer to "invoke the full suite only
when required": reserve the full run (ideally the now-parallelized, 2b
version) for pre-commit/final-review gates, and let iteration loops run a
markedly faster subset.

### 2e. SDD process guidance (not a test-suite change)

If 2a-2d land and meaningfully shrink single-run time, revisit whether
per-task implementer/reviewer dispatch briefs still need to instruct a
"confirm baseline, then confirm again at the end" double full-suite run for
narrow single-file tasks, versus trusting the SDD ledger's last-recorded
green state and running the full suite only once, at the end. Deliberately
sequenced last: this only becomes worth engineering around once the
underlying run is fast enough that a habit built around a slow suite stops
making sense on its own.

## 3. Non-goals (proposed — confirm in planning)

- No change to the mocked-SDK-boundary testing philosophy itself
  (`CLAUDE.md`'s "LLM API testing hygiene" section) — this is about
  execution speed of the existing test architecture, not what gets tested
  or how.
- No migration away from `testcontainers` as the zero-config default path
  for a fresh clone with no `DATABASE_URL` set — 2a adds a faster **opt-in**
  path for active development/SDD sessions, it doesn't remove the
  zero-config fallback a first-time contributor relies on (this directly
  serves Stage 3b's own goal of a stranger going from `git clone` to a
  first review with no extra setup).
- No attempt to eliminate the RSA-keygen or JWT-signing cost from
  `app/github_app.py`'s actual runtime behavior — 2c only touches how often
  the *test* fixture regenerates a throwaway key, not how the app itself
  authenticates.
- The unconfirmed `test_github_app.py` per-call overhead (see "Not
  investigated further" above) is explicitly out of this spec's scope until
  someone actually profiles it correctly — don't guess at a fix for a cause
  that wasn't confirmed.

## 4. Evidence trail

All numbers above were measured directly in this session, not estimated:
`uv run pytest -q --durations=25` for the full-suite breakdown;
`uv run pytest tests/test_github_app.py -q --durations=0` for the isolated
file; a 20-iteration microbenchmark of `rsa.generate_private_key` for the
keygen cost; `nproc` and `python -c "import xdist"` (`ModuleNotFoundError`)
for the parallelization headroom; `docker ps` and `echo $DATABASE_URL` for
confirming no persistent Postgres was in use. This spec's session also
produced `tmp.md` (gitignored, not committed) with the full per-subagent
timing log this problem statement's stage-level numbers are drawn from, if
whoever picks this up wants the raw per-dispatch breakdown.
