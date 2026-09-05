# Standalone-repo restructure: flatten `bot/`, drop `onboarding/`

**Date:** 2026-09-05
**Status:** Approved, ready for implementation planning
**Relates to:** `brief.md` (now deleted per its own instructions once this
work lands), `docs/superpowers/specs/2026-08-29-project-restructure-design.md`
(the prior monorepo-era restructure this supersedes the structural parts of),
`ISSUES.md`'s Design Gaps section (the `deploy.py --sync-env` /
`set_override.py` entry this closes out).

## Context

This repo was split off from a monorepo it shared with a sibling project,
the "onboarding wizard" (now `~/onboarding-wizard`, separate history by
design). This copy kept the monorepo's full git history. The repo still
looks like a monorepo (`bot/`, `dashboard/`, `onboarding/` as siblings under
a 3-member `uv` workspace) even though it's now a single-purpose,
standalone project. This restructure makes the on-disk layout match that
reality: `onboarding/` is removed, `bot/`'s content flattens up to repo
root, and a handful of config/doc files that drifted during the monorepo
period get corrected.

## Goals

- Root of the repo *is* the bot project — no `bot/` indirection.
- `dashboard/` remains a distinct, independently-reasoned-about package
  nested under root (unchanged from today).
- No content loss: every file's git history survives moves (`git mv`), and
  every piece of *unique* information in files being deleted is ported
  somewhere durable first.
- Full verification bar stays green throughout: `uv run pytest -v`,
  `uv run ruff check .`, a Docker build + boot smoke test, and
  `mkdocs build --strict`.

## Non-goals

- No behavior change to the bot, dashboard, or their APIs.
- No new abstractions, no dependency upgrades beyond what the workspace
  reshape mechanically requires.
- Not reopening the `dashboard/` nesting question, the tests-merge
  question, or the `deploy.py`/`set_override.py` retire-vs-keep question —
  all four were decided during brainstorming (see Decisions Log) and are
  inputs to this design, not open questions for the implementation plan.

## Design

### 1. Remove `onboarding/`

- `git rm -r onboarding/`.
- Remove these design docs from `docs/superpowers/{specs,plans}/` (all
  onboarding-only, no bot/dashboard content mixed in — confirmed by name):
  - `specs/2026-08-26-onboarding-github-app-frame-design.md` (+ matching
    `plans/2026-08-26-onboarding-github-app-frame.md`)
  - `specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md` (+
    matching plan)
  - `specs/2026-08-26-onboarding-wizard-render-frame-design.md` (+ matching
    plan)
  - `specs/2026-08-27-onboarding-llm-provider-frame-design.md` (+ matching
    plan)
  - `specs/2026-08-27-onboarding-render-service-frame-design.md` (+ matching
    plan)
  - `specs/2026-08-27-onboarding-uptimerobot-frame-design.md` (+ matching
    plan)
  - `specs/2026-09-01-onboarding-github-app-manual-validation-design.md` (+
    matching plan)
  - `specs/2026-09-01-onboarding-server-side-session-design.md` (+
    `plans/2026-09-02-onboarding-server-side-session.md` — note the date
    mismatch between spec and plan filenames, both still get removed)
  - `specs/2026-09-03-supabase-oauth-abuse-mitigation-design.md` (superseded
    predecessor to the PAT redesign, no matching plan file exists)
  - `specs/2026-09-04-supabase-pat-frame-design.md` (+
    `plans/2026-09-04-supabase-pat-frame.md`)
- The implementer should grep `docs/superpowers/` for `onboarding` after
  these removals to confirm no stray reference or leftover file remains,
  and separately grep for any *other* file (README, CLAUDE.md, mkdocs.yml
  nav, guide/) that still points at `onboarding/`.

### 2. Flatten `bot/` to repo root

Use `git mv` file-by-file (or directory-by-directory where a whole
subtree moves as a unit) so per-file history is preserved:

| From | To |
|---|---|
| `bot/main.py`, `config.py`, `config_deps.py`, `orchestrator.py`, `webhook.py`, `diff_utils.py`, `formatting.py`, `github_app.py`, `hmac_verify.py`, `render_client.py` | same names at root |
| `bot/providers/`, `bot/specialists/`, `bot/queue/`, `bot/scripts/`, `bot/fixtures/` | same names at root |
| `bot/Dockerfile` | `Dockerfile` |
| `bot/SPEC.md`, `bot/cost.md` | root |
| `bot/__init__.py` (empty) | root `__init__.py` (root has no `__init__.py` today; `[tool.uv] package = false` means it's not strictly required, but `git mv` it over anyway for consistency with "every file flattens") |
| `bot/tests/*` | merged into root `tests/` (flat; zero filename collisions, confirmed) |
| `bot/pyproject.toml` | its `[project]` dependency list merges into root `pyproject.toml` (see §5); the file itself is deleted, not moved |
| `bot/CLAUDE.md` | **not moved as a file** — its two sections (Layering constraints, Contracts) merge into root `CLAUDE.md` as a new `## Module boundaries and contracts` section, since this content now describes the root project directly rather than a subdirectory needing its own auto-loaded file |

### 3. Rewrite every `bot.`-qualified reference

Every `from bot.X import ...` / `import bot.X` in application code, tests,
and scripts loses the `bot.` prefix. This includes:

- `dashboard/router.py`, `dashboard/environment.py`, `dashboard/auth.py`
  (currently import `bot.queue.store`, `bot.queue.dispatcher`,
  `bot.providers.base`, `bot.render_client`, `bot.config`).
- `dashboard/CLAUDE.md` — prose references to `bot/Dockerfile`,
  `bot.queue.store`, `bot.render_client`, `bot.config.settings`,
  `bot.providers.base` all need the `bot`/`bot.` prefix dropped (this is a
  doc, not code, so it won't show up in an import-statement grep — check
  it explicitly).
- Every test file under the newly-merged `tests/`.
- `mkdocs.yml`, `guide/**`, `README.md`, root `CLAUDE.md` — any prose path
  reference to `bot/...` (e.g. `bot/scripts/deploy.py`,
  `bot/SPEC.md`, `guide/operations/overrides.md`'s references to the
  override scripts).
- `conftest.py` at root, if it references `bot/` paths for fixture
  resolution.

Mechanical process: `grep -rn 'bot\.' --include='*.py'` and
`grep -rn 'bot/' --include='*.md'` (plus `.yml`/`.yaml`) across the whole
tree, fix each hit, then re-run both greps to confirm zero remaining
matches (excluding this design doc's own history references, `ISSUES.md`'s
incident history, and `docs/superpowers/{specs,plans}/*` filenames/dates
that predate the flatten — those are historical record, not live
references, and must not be rewritten).

### 4. Loose `docs/*.md` handling — no new subfolder

All 13 files at `docs/*.md` (not under `docs/superpowers/`) are removed,
not relocated. Verified by cross-reference grep: every file's substantive
content already exists in a spec/plan/ISSUES.md entry that's being kept,
**except** one file with content found nowhere else. Handling:

**Delete outright (11 files)** — confirmed duplicate of kept specs/plans:
`2026-07-28-dispatcher-followups.md`,
`2026-07-29-comment-visibility-final-review-fixes.md`,
`2026-07-29-comment-visibility-followups.md`,
`2026-07-29-cooldown-review-invocation-followup.md`,
`2026-07-31-comment-lifecycle-followups.md`,
`2026-08-03-supabase-hosting-migration-handoff.md`,
`2026-08-05-first-hosted-run-findings.md`,
`2026-08-05-supabase-first-deploy-provisioning-handoff.md`,
`2026-08-07-deploy-cli-checkpoint.md`,
`2026-08-10-deploy-provider-credential-verification-gap.md`,
`2026-08-11-full-project-review-security-performance-quality.md`.

**Delete after porting (1 file):**
`2026-08-10-demo-rehearsal-checkpoint.md` — its one open thread (Groq's
slow request-count-bucket refill) is already preserved in
`docs/superpowers/specs/2026-08-03-demo-plan-design.md` §3, so nothing to
port; just confirm that cross-reference still reads coherently, then
delete.

**Port then delete (1 file):**
`2026-07-31-escalating-cooldown-final-review-fixes.md` has two sections
with no other home:
- Its "Parked (non-blocking) minor findings" list (5 items: the
  `cap < base` docstring-clamping omission, `mark_failed`'s stale
  docstring, the untested nonzero-seeded-level Site-B branch, the
  108-char docstring line, `enqueue_or_update`'s docstring omission) —
  port verbatim into `ISSUES.md`'s Parked Issues section, one entry per
  finding (or one combined entry covering all five, since they share one
  source review) using that section's existing format, retroactively
  honoring `CLAUDE.md`'s "every parked Minor finding must be logged in
  ISSUES.md" rule that was never applied to this review round at the
  time.
- Its "Unrelated discovery: working-tree CRLF drift" section — port as a
  closed-incident entry in `ISSUES.md` (root-caused: WSL/Windows mount
  checkout drift, not a git-history issue; already fixed via
  `.gitattributes`, so this is a closed record, not an open item).

Then delete the file.

### 5. `pyproject.toml` — 2-member workspace

Root `pyproject.toml` gains a `[project]` block (currently absent — the
root has no `[project]` today, only `[tool.uv.workspace]` and shared dev
tooling) carrying `bot/pyproject.toml`'s current dependency list verbatim
(fastapi, uvicorn, pydantic, pydantic-settings, pygithub, google-genai,
httpx, groq, psycopg[binary], psycopg-pool, python-dotenv, google-auth,
pyjwt, requests, python-multipart) plus `requires-python = ">=3.12"` and a
project name/description (e.g. `name = "pr-review-bot"`, description
carried over from `bot/pyproject.toml`'s). `[tool.uv.workspace].members`
becomes `["dashboard"]` — root is no longer a workspace *member* pointed
at from outside, it *is* the workspace root project. `bot/pyproject.toml`
is deleted (its content merged in, not moved). `[tool.pytest.ini_options].testpaths`
drops `"bot/tests"` and `"onboarding/tests"`, becomes
`["tests", "dashboard/tests"]`. `dashboard/pyproject.toml` is untouched.
The dev dependency group, ruff config, and pytest addopts/markers carry
over unchanged (they're already workspace-root-scoped, not per-member).

### 6. `render.yaml`

`dockerfilePath: ./onboarding/Dockerfile` → `dockerfilePath: ./Dockerfile`.
`buildFilter.ignoredPaths` stays `["**/*.md"]` (no cross-repo scoping to
remove — there wasn't any beyond the comment explaining why it used to
matter; that comment should be trimmed since the "why this drifted" story
is monorepo-specific and now lives in `ISSUES.md` history instead).
`envVars` list is untouched — brief and prior audit both confirm it's
already correctly bot-shaped.

### 7. `deploy.py --sync-env` / `set_override.py` — keep, close the gap

No code change. Close out `ISSUES.md`'s Design Gaps entry for this
(currently deferred to "when the bot sub-project eventually moves to its
own repo" — that's now) with a resolution note: kept as a CLI bootstrap
fallback (first-deploy env-var push before the dashboard is reachable at
all), day-to-day operational path is the dashboard Environment tab. No doc
changes needed to `guide/operations/overrides.md` since both tools remain
live and documented as-is — just the path prefixes from §3's sweep.

### 8. Verification

Same bar as every prior restructuring pass: `uv run pytest -v`,
`uv run ruff check .`, `docker build -f Dockerfile .` +
`docker run --rm <image> python -c "import main"` (or equivalent
post-flatten import smoke test), and `mkdocs build --strict`. All four
green before considering this done. Given the scale of the import rewrite
(§3), expect at least one full pytest run to surface a missed rewrite as
an `ImportError` or `ModuleNotFoundError` — that's the intended
tripwire, not a sign something else is wrong.

## Decisions Log

Resolved during brainstorming, recorded here so the implementation plan
doesn't need to re-litigate them:

1. **`dashboard/` stays nested**, not flattened — it's a genuinely
   separate package.
2. **`bot/tests/` merges flat into root `tests/`** — zero collisions,
   mechanically clean.
3. **`render.yaml`** gets the mechanical `dockerfilePath` fix only.
4. **`deploy.py --sync-env`/`set_override.py` are kept**, not retired —
   they're the fresh-deploy bootstrap path before the dashboard is
   reachable.
5. **Loose `docs/*.md` files are deleted, not relocated** — no new
   `docs/superpowers/` subfolder. 11 are pure duplicates of kept
   specs/plans; 1 (`demo-rehearsal-checkpoint`) is fully redundant; 1
   (`escalating-cooldown-final-review-fixes`) has content ported to
   `ISSUES.md` first.
6. **`pyproject.toml` becomes a 2-member workspace** (root + `dashboard`),
   following directly from decision 1.

## Out of scope

- Any further reorganization of `docs/superpowers/specs/`/`plans/`
  themselves (their existing flat, dated-filename scheme is unchanged).
- Renaming any bot module or restructuring its internal package layout
  beyond the mechanical `bot/` prefix removal.
- `brief.md` itself — per its own header, gets deleted once this
  restructure is done and reviewed, but that's a follow-up step after
  implementation, not part of this design.
