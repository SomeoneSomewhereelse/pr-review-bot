# Design — Test suite execution time: root causes and approved fixes

**Date:** 2026-08-19
**Status:** Approved for planning
**Relates to:** `tests/conftest.py` (`db_url`, `db` fixtures), `tests/test_github_app.py`
(`_throwaway_app_credentials`), `pyproject.toml` (`[tool.pytest.ini_options]`,
dev dependency group), `README.md`'s Testing section, a new `scripts/test_db.py`.

## 1. Problem

The full suite (821 tests as of this writing) takes **~54 seconds** per
`uv run pytest -q` invocation, run serially, on a 24-core machine with
`pytest-xdist` not installed. (Re-measured at ~38s on this machine as of the
final whole-branch review — see section 8b; the ~54s figure above is kept as
the historical number that motivated this design, not a claim about today.) This project's subagent-driven-development
pattern re-runs the full suite many times per plan (one fresh implementer +
one fresh reviewer subagent per task, each running `ruff` + full `pytest` +
`mkdocs build --strict` before reporting), so structurally-avoidable
per-invocation fixed costs get paid over and over. From Stage 3b's own
execution log (`tmp.md`, gitignored, not committed — 29 subagent dispatches
across 10 tasks): ~144 minutes of cumulative subagent wall-clock time for
that stage alone.

Five root causes, all measured directly this session (see section 8):

1. **No persistent test Postgres.** `tests/conftest.py:27`'s `db_url`
   fixture is `scope="session"`, but "session" means *per pytest process*
   (and, after this design lands, per xdist *worker* process — see section
   3c). With `DATABASE_URL` unset, every separate `pytest` invocation pays
   testcontainers' ~3.85s container-boot cost from cold. Across ~20-30
   separate subagent-run invocations in a stage, that's roughly 1.5-2
   minutes of pure, avoidable overhead.
2. **No `pytest-xdist` despite 24 idle CPU cores.** The large majority of
   the 821 tests are independent, mocked-boundary unit tests that
   parallelize cleanly. The hazard: tests that touch Postgres via the `db`
   fixture (`tests/conftest.py:58`) run `TRUNCATE tickets, runtime_config,
   reviews` against **one shared instance** — concurrent xdist workers would
   race.
3. **Confirmed, isolated inefficiency:** `tests/test_github_app.py:51`'s
   `_throwaway_app_credentials` fixture is `autouse=True` at function scope,
   generating a fresh 2048-bit RSA key (~45ms, measured) for every one of
   that file's 33 tests, though nothing depends on the key's value
   differing between tests (its own docstring: every HTTP call is mocked,
   nothing is ever sent with it). Costs ~1.4s for zero benefit.
4. **No test markers separating "fast/unit" from "needs Postgres."** No way
   to run "everything except DB-touching tests" without hand-picking files.
5. **Process habit, not a test-suite defect:** SDD dispatch briefs
   consistently run the full suite twice per task (baseline + pre-commit
   check) even when the immediately-prior task just confirmed green seconds
   earlier. Deliberately out of scope for this design — see section 6.

### Not investigated further (flagged, not root-caused)

`tests/test_github_app.py`'s slowest individual tests (0.78-1.03s in the
`call` phase, not fixture setup) were profiled enough to rule out RSA-keygen
and a real network stall as causes, but not enough to confirm the remaining
hypothesis (PyGithub client reconstruction per call in `app/github_app.py`).
**Out of scope** until someone profiles it correctly with `py-spy` or a
properly-scoped `--durations` pass outside single-file isolation.

## 2. Scope and build order

In scope, in this order:

1. **Fix RSA fixture scope** (section 3a) — isolated, zero-risk, do first.
2. **`scripts/test_db.py`** (section 3b) — no test-code changes, no
   dependency on anything else landing first.
3. **Unified `db` marker + xdist grouping** (section 3c) — the two require
   each other (the marker is what makes the grouping safe), so they land as
   one change.

**Out of scope:** the SDD double-run habit (was "2e" in the draft) — not a
test-suite change, and revisiting it only makes sense once the above are
measured and the per-invocation cost has actually dropped. Not tracked as a
follow-up item; if it's still worth revisiting later, that's a fresh,
separate conversation informed by the after-numbers this design produces.
The unconfirmed `test_github_app.py` per-call cost (above) stays out for the
same "don't guess at an unconfirmed cause" reason.

## 3. The fixes

### 3a. Fix `test_github_app.py`'s per-test RSA keygen

`_throwaway_app_credentials` (`tests/test_github_app.py:51`) can't simply
become `scope="module"` — it depends on the `monkeypatch` fixture, which
pytest only provides at function scope; a module-scoped fixture requesting
it raises `ScopeMismatch` at collection, before any test runs. Split it in
two instead:

- A new `scope="module"` fixture (no `monkeypatch` dependency) that does the
  actual `rsa.generate_private_key(...)` call once per file and returns the
  base64-encoded PEM (and the fixed `github_app_id`/`installation_id`
  values) as plain data.
- `_throwaway_app_credentials` stays `autouse=True` at function scope (so
  every test still gets `monkeypatch.setattr` applied and automatically
  undone, same as today — the safety property nothing changes here), but
  now just reads that cached data and calls `monkeypatch.setattr` three
  times — no keygen. `monkeypatch.setattr` itself is cheap; the RSA
  generation is the ~45ms cost, and it now runs once per file instead of
  once per test.

Confirmed safe to share the key across tests: the fixture's own docstring
already states the key's only purpose is local JWT-signing round-trips with
every HTTP call mocked — no test depends on the key's value differing from
another test's. Saves ~1.4s in this file.

Also do a repo-wide grep for the same `autouse` + implicit-function-scope +
expensive-setup pattern (crypto keygen, anything else non-trivial), in case
it recurs elsewhere. Not confirmed to recur — this was the only instance
found so far, and the grep wasn't exhaustive — but cheap to check while
already in this area.

### 3b. `scripts/test_db.py` — persistent local test Postgres

A new script, not a guide change (existing local-track docs at
`guide/setup/local/05-postgres.md` cover the *app's* runtime Postgres for
the local-hosting track; this is a separate, test-iteration-only concern
that a hosted-track contributor — app running on Render+Supabase, no local
Postgres otherwise — would not get from that page).

- `uv run python -m bot.scripts.test_db` (default `up` behavior): idempotent —
  checks for a running, healthy container named `pr-review-test-pg`; starts
  one via `docker run` if absent or unhealthy. Uses **port 5433**, not 5432,
  specifically so it never collides with the local track's `pr-review-pg`
  container (which uses 5432) if a contributor has both running. Prints
  `export DATABASE_URL=postgresql://postgres:x@localhost:5433/postgres` to
  stdout and nothing else, for `eval "$(uv run python -m bot.scripts.test_db)"`.
- `uv run python -m bot.scripts.test_db down`: stops and removes the container.
- The printed connection string's password is a fixed, throwaway,
  script-generated local value that authenticates nothing real — this is
  not the kind of secret CLAUDE.md's "Secret handling" section is about
  (that section concerns credentials that authenticate against real
  infrastructure: Supabase, Render, GitHub, GCP). Printing it to stdout for
  the `eval` pattern is not a violation of that section. The script must
  still never accept, read, or echo a **real** `DATABASE_URL` — it only ever
  constructs its own throwaway local one.
- Reuses `scripts/_prereqs.py::_looks_like_local_test_db` (already
  documented there as mirroring `tests/conftest.py`) as a sanity check that
  the URL it's about to print is in fact local before printing it — belt
  and suspenders, since the URL is script-constructed and inherently local
  already.
- `tests/conftest.py:29`'s existing `if env_url:` branch (and its
  `_looks_like_local_test_db` guard, unchanged) already handles consuming
  this exported `DATABASE_URL` — **no test code changes needed** for this
  part.
- README's existing Testing section (`README.md:78-96`, within its 180-line
  budget and required-headings list, verified by
  `test_readme_is_a_landing_page_not_a_manual`) gets one line pointing to
  the script as the fast-iteration path; the zero-config testcontainers
  path stays documented as the default for a fresh clone.

### 3c. Unified `db` marker + xdist grouping

Add `pytest-xdist` to `pyproject.toml`'s dev dependency group. Add
`addopts = "-n auto --dist=loadgroup"` to `[tool.pytest.ini_options]` —
applies to every invocation, local and CI, with nothing to remember. `-n
auto` detects CPU count fresh per invocation (cheap syscall); no caching
mechanism or CLAUDE.md rule is needed, and none is added.

A `pytest_collection_modifyitems` hook in `tests/conftest.py` auto-applies
both `pytest.mark.db` and `pytest.mark.xdist_group(name="db")` to any
collected test item where **`"db_url" in item.fixturenames`**. This is
deliberately checked against `db_url` — the root fixture — rather than
against `db`/`db_exec`/`db_query` individually: `fixturenames` is pytest's
fully-resolved transitive closure, so any test using `db`, `db_exec`, or
`db_query` (all three depend on `db_url`) is already covered by this one
check, as is a test that requests `db_url` directly (several do — e.g.
`tests/test_override_helpers.py`, `tests/test_set_override_script.py`).
Checking the three derived names instead of the root would have missed the
direct-`db_url` tests, leaving them free to land on a different xdist
worker than the grouped ones — which would both double-pay the
testcontainers boot cost (session-scoped `db_url` re-triggering on a second
worker) and reopen exactly the cross-worker race the grouping exists to
prevent. One marker registered in `pyproject.toml`'s pytest config drives
both `-m "not db"` (fast-iteration selection) and the `xdist_group` (safe
parallelization) — nothing to hand-annotate per file, and a newly added
DB-touching test is covered automatically because it's detected by fixture
usage, not by a maintained list.

**Zero-config fresh-clone path is unaffected by any of this.** With no
`DATABASE_URL` set, exactly one xdist worker ends up owning the entire `db`
group (by construction — the hook guarantees every `db_url`-touching test
lands in that one group), so exactly one worker ever requests the
session-scoped `db_url` fixture and testcontainers boots exactly once for
the whole run — the same cost paid today, not multiplied by worker count.
The DB-touching subset does not itself parallelize (it's serialized onto
one worker by design, to protect the one shared Postgres instance,
regardless of whether that instance is testcontainers-managed or a real
`DATABASE_URL`) — the win is that the non-DB majority of the suite fans out
across the remaining workers *concurrently* with that one worker's DB work,
instead of paying both costs serially back to back.

**CI:** `.github/workflows/ci.yml`'s `lint-and-test` job already sets
`DATABASE_URL` via its `services: postgres` container before running `uv
run pytest -v` — same fixture code path as local, so the same one-worker
grouping guarantee applies there too. Confirm this behaves as expected
against the actual job definition as a plan verification task (run CI on
the branch and check for `TRUNCATE`-related failures or connection errors)
rather than assuming it from code-reading alone.

## 4. Safety net: guard test coverage

There is currently no test exercising
`tests/conftest.py::_looks_like_local_test_db` or the `db_url` fixture's
`AssertionError`-raising refusal path at all — the guard that stands between
an accidentally-exported production `DATABASE_URL` and a `TRUNCATE` is
itself untested. This design does not change that guard's logic, but it
does make setting `DATABASE_URL` locally more common (that's 3b's whole
point), which raises the cost of the guard silently regressing. Add a test
proving a production-shaped URL (e.g. a Supabase pooler hostname) still
raises `AssertionError` and is not bypassed absent `ALLOW_REMOTE_TEST_DB`.
This is new test coverage for existing, unchanged behavior — not a change to
the guard itself.

## 5. Non-goals

- No change to the mocked-SDK-boundary testing philosophy (`CLAUDE.md`'s
  "LLM API testing hygiene" section) — this is about execution speed of the
  existing test architecture, not what gets tested or how.
- No migration away from `testcontainers` as the zero-config default path
  for a fresh clone with no `DATABASE_URL` set. Section 3b adds a faster
  **opt-in** path; it does not remove or alter the zero-config fallback
  Stage 3b's guide promises a first-time contributor.
- No attempt to eliminate the RSA-keygen or JWT-signing cost from
  `app/github_app.py`'s actual runtime behavior — section 3a only touches
  how often the *test* fixture regenerates a throwaway key.
- The unconfirmed `test_github_app.py` per-call overhead stays out of scope
  until someone profiles it correctly (see section 1).
- The SDD double-run process habit is out of scope entirely (see section 2)
  — not tracked as a follow-up in this document.
- No change to `_looks_like_local_test_db`'s logic or the
  `ALLOW_REMOTE_TEST_DB` escape hatch — section 4 adds test coverage for the
  existing behavior, not a behavior change.

## 6. Verification plan (for the implementation plan to execute)

Record a before number, an after number, and the exact command for each, per
this project's "measure, don't assert" convention:

- Before: `uv run pytest -q --durations=25` (current: ~54s serial, no
  xdist) — already captured in section 1, re-confirm at plan-execution time
  in case drift occurred. (Re-confirmed at ~38s on this machine — see 8b.)
- After 3a alone: same command, expect ~1.4s improvement.
- After 3c lands: same command (now parallel by default via `addopts`);
  also run `uv run pytest -q -m "not db"` and record its time separately as
  the fast-iteration number.
- Confirm the zero-config path explicitly: run the full suite with
  `DATABASE_URL` unset and Docker available, confirm exactly one
  testcontainers container is created (not one per worker) — e.g. via
  `docker ps` during the run, or a log line — and the suite still passes.
- Confirm 3b's script: `up` twice in a row is a no-op the second time
  (idempotency), `down` actually removes the container, and the printed
  `DATABASE_URL` is structurally valid (never assert on the value itself,
  per CLAUDE.md's secret-handling conventions, even though this one isn't a
  real secret — match the existing convention anyway for consistency).
- Confirm CI: push the branch, confirm `lint-and-test` still passes with no
  `TRUNCATE`/connection-related failures.
- If README's "821 deterministic tests" count changes (section 4 adds at
  least one test), update README's Testing section to match — `pytest
  --collect-only -q | tail -1` gives the authoritative count.
- If any candidate lands and the before/after numbers don't move, say so in
  the plan's closing report and consider reverting rather than keeping a
  change that only added configuration surface.

## 7. Evidence trail

All numbers in section 1 were measured directly in the session that
produced the original draft of this document, not estimated: `uv run
pytest -q --durations=25` for the full-suite breakdown; `uv run pytest
tests/test_github_app.py -q --durations=0` for the isolated file; a
20-iteration microbenchmark of `rsa.generate_private_key` for the keygen
cost; `nproc` and `python -c "import xdist"` (`ModuleNotFoundError`) for the
parallelization headroom; `docker ps` and `echo $DATABASE_URL` for
confirming no persistent Postgres was in use. `tmp.md` (gitignored, not
committed) holds the full per-subagent timing log the stage-level numbers
in section 1 are drawn from, for anyone who wants the raw per-dispatch
breakdown.

## 8. Results

All numbers below were re-measured on the final branch state (856 collected
tests) on the same machine section 1 used — WSL2, `nproc` = 24 — after the
final whole-branch review found that the originally-shipped `-n auto` had made
this design's own primary target metric *slower*. Each configuration was run at
least once, the chosen one twice.

### 8a. Worker count: `-n auto` was actively harmful; `-n 4` ships

`addopts` originally shipped `-n auto`. On a 24-core machine that spins **24
workers**, and each worker pays a full app-import startup cost (~1.25s
inferred). On a workload whose *total* serial time is ~31-57s, that fixed cost
dominates and never amortizes:

Fast-iteration subset, `uv run pytest -q -m "not db and not xdist_meta"`
(621 tests):

| `-n` | Wall time | vs serial |
|---|---|---|
| `0` (serial) | 30.96s | — |
| `2` | 21.66s | −30% |
| **`4`** | **19.95s / 20.04s** | **−36%** |
| `6` | 21.01s | −32% |
| `8` | 22.26s | −28% |
| `auto` (= 24) | 45.42s | **+47% (slower than no parallelism at all)** |

Full suite, `uv run pytest -q --durations=25` (856 tests, testcontainers path,
no `DATABASE_URL` set):

| `-n` | Wall time | vs serial |
|---|---|---|
| `0` (serial) | 57.15s | — |
| **`4`** | **35.21s / 35.58s** | **−38%** |
| `auto` (= 24) | 65.82s | **+15% (slower than serial)** |

The curve is flat-bottomed between 2 and 8 and falls off a cliff past that, so
`-n 4` is picked as the measured minimum rather than a knife-edge optimum —
anything in 2-8 would have been defensible, and the guard test in
`tests/test_conftest_db_marker_hook.py` asserts that range rather than the
literal 4. `pyproject.toml` now pins `-n 4 --dist=loadgroup`, with a comment
recording why it must not be "restored" to `auto`.

**Why CI never caught this.** GitHub's hosted runners have 2-4 cores, so
`-n auto` *there* already resolved to a small, sane worker count — CI timed at
41s and 44s and looked fine. The pathology only appears on a high-core
developer machine, which is exactly where the fast-iteration loop this design
exists to speed up actually runs. Pinning an integer also makes local and CI
behaviour identical instead of silently core-count-dependent.

**The design was sound; the configuration was wrong.** Section 6 says to
consider reverting a change whose numbers don't move. With the worker count
corrected the numbers move substantially and in the right direction (−36% on
the primary target, −38% on the full suite), so the mechanism is kept.

### 8b. Backfilling section 6's "after 3a alone" measurement

Section 6 asked for this and Task 5 never performed it (it measured only after
all four tasks had landed). Backfilled here against the two real commits, in
throwaway worktrees, both of which predate xdist and therefore ran serially —
matching section 6's original intent. Two runs each after a discarded warmup,
`uv run pytest -q --durations=25`:

| Commit | What | Runs | Mean |
|---|---|---|---|
| `f3d301b` | pre-plan baseline (821 tests) | 38.57s, 38.12s | 38.35s |
| `4288152` | Task 1 / section 3a alone (823 tests) | 37.40s, 37.12s | 37.26s |

**~1.1s saved**, against section 3a's ~1.4s estimate — and the after-commit
runs 2 *more* tests than the baseline, so the per-test saving is slightly
larger than the raw delta shows. Close enough to call the estimate confirmed;
reported as measured rather than rounded up to the predicted figure.

This backfill also corrects a stale number in section 1: the baseline full
suite measures **~38s serially on this machine today**, not the "~54 seconds"
section 1 records. Section 1's figure was captured in an earlier session and
has drifted (machine state, warm caches, dependency versions); it is left in
place as the historical record of what prompted this design, but ~38s is the
honest present-day serial baseline, and it is the number the −38% full-suite
improvement above should be read against.

### 8c. Test counts and marker selection

`--collect-only -q`: **856** total, **621** with
`-m "not db and not xdist_meta"` (235 deselected), **234** with `-m db` (this
project's authoritative "how many tests touch Postgres" count), **1** with
`-m xdist_meta`. The two marker sets are disjoint (`-m "db and xdist_meta"`
collects 0) and their union matches exactly: 235 = 234 + 1.

`xdist_meta` marks exactly **one** test —
`test_hook_applied_xdist_group_reaches_the_loadgroup_scheduler` (5.34s call; it
spins real xdist worker subprocesses via `pytester`). It was previously applied
module-wide via `pytestmark`, which also swept in that file's second test,
`test_this_projects_conftest_hook_declares_tryfirst` — a sub-5ms assertion, and
the only direct check that the real `tests/conftest.py` hook still declares
`tryfirst`. Excluding it bought nothing and removed a real guard from the fast
loop, so the marker is now applied per-test and the `tryfirst` check runs in
`-m "not db and not xdist_meta"`.

### 8d. Zero-config path (section 6, step 4)

Confirmed exactly one distinct testcontainers-managed Postgres container over a
whole run, on two independent runs (container names `interesting_goldstine` and
`dazzling_curie`), polled continuously via
`docker ps --filter ancestor=postgres:16-alpine` every second for each run's
full duration rather than sampled once. Max concurrent count was 1 on both
runs. Both runs exited 0.

### 8e. `scripts/test_db.py` against real Docker (section 6)

Section 6 required this and it was never done — every test in
`tests/test_test_db_script.py` mocks `shutil.which` and `subprocess.run`, so a
script whose entire purpose is real Docker orchestration had never once been
executed against real Docker. Performed here against Docker 29.7.2:

- **`up` when absent** → printed exactly
  `export DATABASE_URL=postgresql://postgres:x@localhost:5433/postgres`, exit 0;
  `docker ps` showed `bcd1d7f05b01 pr-review-test-pg Up 2 seconds
  0.0.0.0:5433->5432/tcp`.
- **`up` a second time** → byte-identical output, exit 0, and
  `docker ps -a --filter name=pr-review-test-pg` still showed **the same single
  container id** `bcd1d7f05b01` (count 1). Idempotency confirmed against real
  Docker, not a mock.
- **`eval "$(...)"`** → `DATABASE_URL` went from unset to the printed value in
  the calling shell, and a real `psycopg.connect` against it answered
  `SELECT 1 -> 1`, `server_version -> 16.14`. Structurally valid *and* live.
- **`down`** → exit 0, and `docker ps -a --filter name=pr-review-test-pg`
  returned **no rows**. Running `down` again on an already-absent container is
  also a clean exit 0.

### 8f. Safety gap found and closed during this fix wave

README documents `eval "$(uv run python -m bot.scripts.test_db)"` as the
fast-iteration path, and `Settings.database_url` reads the process environment
ahead of any `.env` file. `scripts/deploy.py::_wanted_env()` puts
`settings.database_url` into what `--sync-env` pushes, and `DATABASE_URL` is in
`_ALWAYS_SYNCED` — so running `--sync-env` from a shell where `test_db` had
been eval'd would have silently repointed the **live Render service** at
`postgresql://...@localhost:5433/postgres`. `sync_env()` already refused for an
active provider override and for disagreeing model overrides, but had no guard
for this. This project has had one real incident of exactly this shape (a live
Render service left holding a dummy test value — see `tests/conftest.py`'s
`_quarantine_operator_apis` comment).

`sync_env()` now refuses (exit 2, before any HTTP request or database
connection) when `scripts/_prereqs.py::_looks_like_local_test_db` — the same
predicate `scripts/test_db.py` itself consumes, unmodified — matches
`settings.database_url`, naming `scripts/test_db.py` as the likely cause and
telling the operator to `unset DATABASE_URL`. Verified end-to-end in a real
eval'd shell with every outbound call hard-stubbed to raise: the premise held
(`os.environ` did win over `.env`; host resolved to `localhost`) and
`sync_env()` returned 2 without attempting a single call.

### 8g. CI (section 6, step 5)

**Pass** — `lint-and-test` completed in 41s on push
(run [32282918106](https://github.com/TovTechOrg/pr-review-bot/actions/runs/32282918106)),
no `TRUNCATE`/connection-related failures. Measured while `addopts` still said
`-n auto`; as noted in 8a, CI runners are small enough that `auto` and a pinned
`4` resolve to comparable worker counts there, so this result was not expected
to be invalidated by the pin.

**Re-confirmed after the pin landed:** `lint-and-test` completed in 44s on the
push that included the `-n 4` config change and the final-review fix wave
(run [32296335505](https://github.com/TovTechOrg/pr-review-bot/actions/runs/32296335505)),
still no `TRUNCATE`/connection-related failures — comparable to the pre-pin
41s, as predicted.

### 8h. Bugs surfaced by measurement

Measurement itself surfaced three real bugs that would otherwise have shipped:
an order-dependent test pair in `tests/test_github_app.py` that broke when
split across xdist workers (`d491fa8`); a hook-ordering bug where
`tests/conftest.py`'s db-marker hook ran *after* xdist's worker-side
nodeid-stamping hook, silently defeating the `db` group's grouping guarantee
and allowing up to 24 concurrent testcontainers Postgres containers (`bec9b86`);
and the `-n auto` regression documented in 8a, caught only by the final
whole-branch review. The last of these is the reason section 6's
"if the numbers don't move, say so" instruction exists — and the reason it has
to be executed as a real measurement, not an assumption.
