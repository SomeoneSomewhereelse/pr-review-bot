# Checkpoint — deploy verification CLI (paused mid fix-wave)

**Date:** 2026-08-07
**Status:** Paused at a token-budget boundary. Implementation complete and
reviewed; one fix wave was in flight when work stopped.
**Branch:** `feat/deploy-verification-cli` — 12 commits ahead of `master`
(branch point `5addf5d`), HEAD `350eaa4`.
**Relates to:** `docs/superpowers/specs/2026-08-05-deploy-command-design.md`
(the spec), `docs/superpowers/plans/2026-08-07-deploy-verification-cli.md`
(the plan), `.superpowers/sdd/2026-08-07-deploy-verification-cli/progress.md`
(the SDD ledger — **git-ignored**, so treat this file as the durable record).

## Where things stand

All **11 tasks are implemented, individually reviewed, and committed.** The
full suite was green at HEAD: **254 passed**, `ruff check .` clean, verified
directly rather than taken from a subagent's report.

The final whole-branch review then ran and returned **1 Critical, 3 Important,
and 12 Minor** findings. A single fix wave covering six of them (the Critical,
all three Important, and two one-line docs/anchor items) was dispatched and was
**still running when work stopped**.

### The working tree is not clean — this is expected

`scripts/deploy.py` and `tests/test_deploy_script.py` carry **uncommitted
changes from the in-flight fix wave**. Do not revert them without reading them
first; they are partial work toward the six items below, not stray edits. On
resume, either let that agent finish, or read the diff and complete the wave by
hand.

## What the fix wave was asked to do

1. **CRITICAL — `--sync-env` can push a self-inconsistent provider config while
   every check reports green.** `_wanted_env()` pushes `LLM_PROVIDER`, but
   `_SYNCED_ENV_VARS` never includes `GEMINI_API_KEY`, and
   `settings.llm_provider` defaults to `"gemini"`. An operator who fills in
   `GROQ_API_KEY` + `GITHUB_MODELS_TOKEN` to satisfy the clobber guard (exactly
   what README instructs) can overwrite a live `LLM_PROVIDER=groq` with
   `gemini` while never pushing that provider's key. The service then boots,
   answers `/healthz`, and fails every review — with all six checks PASS. Fix:
   a guard in `sync_env()`, before any HTTP request, refusing to sync when the
   selected provider's key is not in the synced set.
2. **IMPORTANT —** the webhook *write* (`set_webhook_url`) sits outside any
   `try`, so a failing PATCH renders `unexpected GithubException` with no
   status and no next action, breaking the "every FAIL detail actionable on its
   own" contract. Also, the generic non-404 lookup branch discards the
   underlying status, so a 401 and a 502 read identically.
3. **IMPORTANT —** `main()`'s `--sync-env` wiring is entirely untested. Nothing
   exercises `main(["--sync-env"])`: not flag detection, not the early return
   without printing the table, not the fall-through to the checklist.
4. **IMPORTANT —** non-`httpx` exceptions escape `sync_env()` as a traceback
   and exit 1, which the contract defines as "a check failed" — misleading any
   wrapper script. Should become exit 2 with a terse message.
5. **MINOR —** `_README_ANCHOR` is `README.md#deploying-to-production`, but the
   real GitHub anchor is `#deploying-to-production-render--supabase`. It is the
   only pointer the failure output offers.
6. **MINOR —** the exit-code caption in `README.md`/`SETUP.md` names one cause
   for exit 2; there are now four.

## Resume from here

1. **Check whether the fix wave landed.** `git log --oneline 350eaa4..HEAD` and
   `git status`. If it committed, note the SHAs; if the tree is still dirty,
   read the diff and finish the six items.
2. **Run exactly one scoped re-review of the fix wave** —
   `FIX_BASE = 350eaa4`, using superpowers:subagent-driven-development's
   `scripts/review-package` and `re-review-prompt.md`. The process allows one
   fix wave and one scoped re-review; there is no second wave.
3. **Adjudicate any residual findings** — park with a written ruling, or stop
   on anything load-bearing.
4. **Then** use superpowers:finishing-a-development-branch. The base branch is
   `master` (no `main` exists in this repo, despite git's default-branch
   label). The user's standing preference: implementation work goes on a
   feature branch, never committed directly to `master`.
5. Delete `.superpowers/sdd/2026-08-07-deploy-verification-cli/` once the
   branch is finished — git history becomes the record.

## Test environment (needed on every run)

Docker **is** available, but this WSL2 + Docker Desktop setup hangs on
testcontainers' Ryuk reaper sidecar. Always run:

```bash
TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q
```

Plain `uv run pytest` may hang forever. Baseline at `350eaa4` is 254 passed.

## A spec defect that still needs a decision

The Critical above is **not** an implementation slip — the implementation
followed the spec faithfully. The spec contradicts itself:

- §6.1 makes the required provider key a *function* of `LLM_PROVIDER`,
  including `GEMINI_API_KEY`.
- §8 hardcodes a fixed eight-variable push list that **includes**
  `LLM_PROVIDER` but **excludes** `GEMINI_API_KEY`.

The dispatched fix is the minimal code-level reconciliation (refuse to sync a
provider whose key is not in the set). The spec still needs a real decision,
which is the user's to make:

- **(a)** drop `LLM_PROVIDER` from the synced set, leaving it to `render.yaml`; or
- **(b)** make the selected provider's key part of the synced set.

Two smaller spec issues found by the same review, also unresolved:

- §7.2 lists three causes for exit 2, but §10 never requires the docs to carry
  all three — so Task 11 documented one and no test caught it. The docs-parity
  test already reads both files; it is the natural place to pin exit-code
  causes too.
- §11's "no `detail` exceeding the §7.4 length budget" is unimplementable as
  literally written, since `check_config`'s missing-key enumeration legitimately
  exceeds it. Needs a stated exemption for enumerations before it can be a test.

## Deferred minors carried to merge

Recorded in the ledger and triaged by the final review as non-blocking: six
over-length test lines (102-108 chars; ruff's E501 is unselected so nothing
catches them), duplicated PEM-path resolution across two helpers, the health
URL built independently in two checks, `test_env_var_names_match_the_docs`
reading CWD-relative paths, unbounded monitor-URL enumeration in
`check_uptime_pinger`, a stale `discover_installation_id` docstring, no
`--help`/unknown-flag handling, and no pagination on the Render list endpoints.
