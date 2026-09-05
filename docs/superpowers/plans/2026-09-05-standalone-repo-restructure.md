# Standalone-repo Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flatten `bot/` up to the repo root, remove `onboarding/` entirely, and correct the handful of config/doc files that drifted during the monorepo period, so this repo's on-disk layout matches its actual status as a standalone, single-purpose project.

**Architecture:** Six sequential tasks, each ending in a state where the relevant verification commands are green. Tasks are ordered so nothing is left broken between commits longer than necessary: `onboarding/` removal and its workspace-config fallout come first (task 1), documentation cleanup that's independent of code is next (task 2), the big mechanical `bot/`-flatten-plus-import-rewrite is task 3, the small `render.yaml` fix is task 4, a full end-to-end verification pass is task 5, and the `brief.md` cleanup that must come last per the spec is task 6.

**Tech Stack:** Python ≥3.12, `uv` workspaces, FastAPI, pytest/pytest-asyncio (`-n 4 --dist=loadgroup --import-mode=importlib`), ruff, Docker, MkDocs.

**Spec:** `docs/superpowers/specs/2026-09-05-standalone-repo-restructure-design.md`

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest -v`; lint: `uv run ruff check .`.
- `ruff` line-length **100**; lint select is `["E4", "E7", "E9", "F", "E501"]`.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **Every `git mv` must be a real `git mv` invocation** (not a manual delete+recreate) so per-file history survives, per the spec's explicit goal.
- **Never run an unscoped `grep`/`Read`/`cat` against `.env` or `.env.config`.** When sweeping for `bot.`/`bot/` references, exclude both files explicitly. If either ever turns out to contain a real hit, do not open it — tell the user and ask them to check/fix it themselves. This is root `CLAUDE.md`'s absolute rule, not a per-task judgment call.
- **Never commit on anyone else's behalf** and never skip hooks (`--no-verify`) or lint/test failures to "get past" a red step.
- Before pushing/deploying anything (not required by this plan, but binding if a later session picks up deploy work from here): full suite + ruff + Docker build/boot + `mkdocs build --strict` all green, per root `CLAUDE.md`.
- Full verification bar for **this plan's completion**: `uv run pytest -v`, `uv run ruff check .`, `docker build -f Dockerfile .` + a boot smoke test, and `mkdocs build --strict` — all green, confirmed in task 5.

---

### Task 1: Remove `onboarding/` and fix the workspace fallout

**Files:**
- Delete: `onboarding/` (entire directory, via `git rm -r`)
- Delete: 11 onboarding-only design docs under `docs/superpowers/{specs,plans}/` (listed below)
- Modify: `pyproject.toml` (workspace members, testpaths)
- Modify: any file found by the stray-reference sweep (README.md, CLAUDE.md, mkdocs.yml, guide/**)

**Interfaces:**
- Produces: a workspace with members `["bot", "dashboard"]` (task 3 will later drop `"bot"` too, once it flattens) and `testpaths = ["tests", "bot/tests", "dashboard/tests"]` (onboarding's testpath entry removed). This is the state task 3 builds on.

- [ ] **Step 1: Remove the `onboarding/` directory**

```bash
git status  # confirm no uncommitted work under onboarding/ or elsewhere first
git rm -r onboarding/
```

- [ ] **Step 2: Remove the 11 onboarding-only design docs**

```bash
git rm \
  docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md \
  docs/superpowers/plans/2026-08-26-onboarding-github-app-frame.md \
  docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md \
  docs/superpowers/plans/2026-08-26-onboarding-supabase-provisioning-frame.md \
  docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md \
  docs/superpowers/plans/2026-08-26-onboarding-wizard-render-frame.md \
  docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md \
  docs/superpowers/plans/2026-08-27-onboarding-llm-provider-frame.md \
  docs/superpowers/specs/2026-08-27-onboarding-render-service-frame-design.md \
  docs/superpowers/plans/2026-08-27-onboarding-render-service-frame.md \
  docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md \
  docs/superpowers/plans/2026-08-27-onboarding-uptimerobot-frame.md \
  docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md \
  docs/superpowers/plans/2026-09-01-onboarding-github-app-manual-validation.md \
  docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md \
  docs/superpowers/plans/2026-09-02-onboarding-server-side-session.md \
  docs/superpowers/specs/2026-09-03-supabase-oauth-abuse-mitigation-design.md \
  docs/superpowers/specs/2026-09-04-supabase-pat-frame-design.md \
  docs/superpowers/plans/2026-09-04-supabase-pat-frame.md
```

If any path in this list errors as "did not match any files", check whether it was already removed or renamed — do not silently skip without checking why (it should not happen; the spec confirmed these exact filenames).

- [ ] **Step 2b: Confirm no onboarding doc remains**

```bash
find docs/superpowers -iname '*onboarding*'
```

Expected: no output. If anything remains, decide whether the spec missed it (check against the spec's list in `docs/superpowers/specs/2026-09-05-standalone-repo-restructure-design.md` §1) and remove it too.

- [ ] **Step 3: Fix `pyproject.toml`'s workspace members and testpaths**

Edit `pyproject.toml`:

```toml
[tool.uv.workspace]
members = ["bot", "dashboard"]
```

(drop `"onboarding"`), and in `[tool.pytest.ini_options]`:

```toml
testpaths = ["tests", "bot/tests", "dashboard/tests"]
```

(drop `"onboarding/tests"`).

- [ ] **Step 4: Sweep for stray `onboarding` references outside `docs/superpowers/`**

```bash
grep -rln 'onboarding' --include='*.md' --include='*.py' --include='*.yml' --include='*.yaml' \
  --exclude-dir='.git' --exclude='.env*' . | grep -v '^./ISSUES.md$'
```

`ISSUES.md` is excluded from the fix list because it's incident history — its mentions of the onboarding project are a historical record, not a live reference, and must not be edited. For every other file the grep finds (expect at minimum `README.md`, `CLAUDE.md`, `mkdocs.yml` if it has an onboarding nav entry, and possibly `guide/**`), open it and remove or rewrite the onboarding-specific content so it reads correctly for a repo that no longer contains `onboarding/`. Use judgment on wording — this is prose cleanup, not a mechanical rename.

- [ ] **Step 5: Verify**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: both green. (The bot/onboarding workspace member removal only affects collection paths; no test code changes yet, so this should already pass.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: remove onboarding/ — now a standalone project at ~/onboarding-wizard

onboarding/ split off into its own repo with fresh history. Removes the
directory, its 11 onboarding-only design docs, and the workspace/testpath
entries that referenced it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MgLyw7js8sK5RQFLw2LuZH
EOF
)"
```

---

### Task 2: Documentation cleanup — loose `docs/*.md` and the `deploy.py`/`set_override.py` Design Gap

**Files:**
- Delete: 13 files at `docs/*.md` (not under `docs/superpowers/`)
- Modify: `ISSUES.md` (port 2 sections in, close 1 Design Gap entry)

**Interfaces:**
- Produces: an `ISSUES.md` with (a) a new Parked Issues entry covering the 5 minor findings from the escalating-cooldown final review, (b) a new closed-incident entry for the CRLF working-tree drift, and (c) the existing `deploy.py --sync-env`/`set_override.py` Design Gap entry updated with a final resolution note (kept, not retired). No other task depends on this one's internals — it's independent of task 1 and task 3 and could run in any order, but is sequenced here as the natural "docs-only, no code" second step.

- [ ] **Step 1: Read the source file whose content needs porting**

Read `docs/2026-07-31-escalating-cooldown-final-review-fixes.md` in full (it's already been read once during brainstorming — re-read to work from the actual text, not memory).

- [ ] **Step 2: Port the 5 parked minor findings into `ISSUES.md`'s Parked Issues section**

Find `ISSUES.md`'s Parked Issues section (search for `## Parked Issues` or equivalent heading — check the file's actual section name before assuming; the Design Gaps section header format is documented at `ISSUES.md:292` but Parked Issues is a separate section elsewhere in the file). Add a new entry following that section's existing entry format, e.g.:

```markdown
### Escalating-cooldown final review — 5 parked minor findings (2026-07-31)

- **Found during:** final whole-branch review of the escalating re-review
  cooldown feature (commit range `ae732bb..0598b83`, fixed in `e5ec149`).
- **What:** five non-blocking Minor findings, none correctness bugs on any
  reachable default-config path:
  1. `effective_cooldown`'s docstring first line doesn't show the
     `min(level, _MAX_COOLDOWN_LEVEL)` clamping inline (`bot/queue/store.py`)
     — cosmetic; the brief's own docstring template omitted it too.
  2. `mark_failed`'s docstring may still narrate the old flat-cooldown
     re-arm behavior without mentioning the level escalate/reset
     (`bot/queue/store.py`) — pre-existing, not introduced by this work.
  3. The Site-B non-dirty ("latent level") branch is tested only at level
     `0 -> 0`; a nonzero seeded level (e.g. level `3` surviving a
     non-dirty finalize) isn't directly exercised, though the `CASE`'s
     `ELSE cooldown_level` clause is otherwise a one-line, low-risk read.
  4. Design's explicit "sustained churn 300->600->1200->2400->3600->3600,
     end-to-end through the store" sequence has no single composed test —
     the plateau and each step are covered piecewise, but nothing walks a
     ticket through the full ramp as one scenario.
  5. `bot/queue/store.py`'s `_due_after_cooldown` docstring line is 108
     chars, over the plan's stated 100-char guideline (ruff's default
     `select` doesn't flag `E501` at that column, and a similarly-long
     line already exists elsewhere in the file).
- **Why parked:** graded as optional polish by the final review, not
  merge-blocking; recorded here now (delayed from 2026-07-31) per
  `CLAUDE.md`'s Plan-execution rule that every parked Minor finding must
  be logged here before a branch is considered done — this entry was
  missed at the time and is being backfilled as part of the 2026-09-05
  standalone-repo restructure's documentation cleanup.
- **Status:** open | decided-non-issue — mark as **decided-non-issue** if,
  reading the code now, all five still hold as described; otherwise note
  what's changed.
- **Follow-up:** none required; revisit only if someone touches
  `bot/queue/store.py`'s cooldown logic again.
```

Adjust file paths in the entry from `bot/queue/store.py` to whatever path is correct *at the time this task runs* — if task 3 (the flatten) has already run, use `queue/store.py` instead. Check task order before writing this.

- [ ] **Step 3: Port the CRLF working-tree drift as a closed-incident entry in `ISSUES.md`**

Add near `ISSUES.md`'s other incident entries (match the file's existing incident-entry format — read a couple of existing entries first to match structure/tone):

```markdown
### Working-tree CRLF drift (2026-07-31, closed)

While staging a task commit during the escalating-cooldown implementation,
the controller found that `git add <exact-task-files>` swept in a large,
pre-existing, **uncommitted** CRLF conversion of those same files — the
repo's committed history was already pure LF; the working tree on a
WSL/Windows mount had drifted to CRLF before that session started. Fixed
per-task by normalizing back to LF before each commit landed. After the
branch merged, a full project-wide audit (`git ls-files` + `file`, plus an
untracked-file check) found the same drift on 59 tracked files — matching
the original `git status` dirty-file list from the start of that work.
Normalizing all 59 back to LF produced a byte-exact match with the
already-LF committed `HEAD`, confirming the CRLF was purely a
working-tree/checkout artifact, never actually committed to git history.
Closed by adding `.gitattributes` (`* text=auto eol=lf`, commit `802a9b8`)
so this can't silently recur on future checkouts. No further action
needed — recorded here (backfilled 2026-09-05) since it previously had no
home outside a since-deleted handoff doc.
```

- [ ] **Step 4: Close the `deploy.py --sync-env`/`set_override.py` Design Gap entry**

Find the existing entry (`ISSUES.md`, Design Gaps section, title `bot/scripts/deploy.py --sync-env` and `bot/scripts/set_override.py` are now redundant with the dashboard Environment tab`). Append a new `**Update (2026-09-05):**` line under its existing `**Update (2026-09-05):**` line (there's already one from the hosted-only-guide sweep — add this as a second, later update, or fold both under one bullet if they'd otherwise look like a duplicate date — use judgment reading the existing entry) resolving it:

```markdown
- **Update (2026-09-05), standalone-repo restructure:** resolved — kept as
  a CLI bootstrap fallback. `--sync-env` is what makes a fresh deploy's
  env vars non-empty before the dashboard is reachable at all; the
  dashboard Environment tab is the normal day-to-day path once it's up.
  No script changes needed, no doc changes needed beyond the path-prefix
  sweep in task 3 of `docs/superpowers/plans/
  2026-09-05-standalone-repo-restructure.md`.
```

- [ ] **Step 5: Delete the 13 loose `docs/*.md` files**

```bash
git rm \
  docs/2026-07-28-dispatcher-followups.md \
  docs/2026-07-29-comment-visibility-final-review-fixes.md \
  docs/2026-07-29-comment-visibility-followups.md \
  docs/2026-07-29-cooldown-review-invocation-followup.md \
  docs/2026-07-31-comment-lifecycle-followups.md \
  docs/2026-07-31-escalating-cooldown-final-review-fixes.md \
  docs/2026-08-03-supabase-hosting-migration-handoff.md \
  docs/2026-08-05-first-hosted-run-findings.md \
  docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md \
  docs/2026-08-07-deploy-cli-checkpoint.md \
  docs/2026-08-10-demo-rehearsal-checkpoint.md \
  docs/2026-08-10-deploy-provider-credential-verification-gap.md \
  docs/2026-08-11-full-project-review-security-performance-quality.md
```

- [ ] **Step 6: Confirm `docs/` has no more loose `.md` files**

```bash
find docs -maxdepth 1 -name '*.md'
```

Expected: no output (everything else lives under `docs/superpowers/`).

- [ ] **Step 7: Verify**

```bash
uv run pytest -v
uv run ruff check .
mkdocs build --strict
```

Expected: all green. `mkdocs build --strict` matters here specifically — if `mkdocs.yml`'s nav references any of the 13 deleted files, this is what catches it (fix `mkdocs.yml` if so).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: remove loose docs/*.md handoff notes, port orphaned content to ISSUES.md

11 of 13 files' content already lives in a kept spec/plan (confirmed by
cross-reference grep during brainstorming); the escalating-cooldown final-
review-fixes doc's 5 parked minor findings and CRLF-drift discovery — the
only content with no other home — are ported into ISSUES.md first. Also
closes the deploy.py/set_override.py Design Gap entry that was explicitly
deferred to this restructure.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MgLyw7js8sK5RQFLw2LuZH
EOF
)"
```

---

### Task 3: Flatten `bot/` to the repo root

**Files:**
- Move (via `git mv`): every file/dir directly under `bot/` except `bot/CLAUDE.md`, `bot/pyproject.toml`, `bot/tests/`
- Modify: root `CLAUDE.md` (merge in `bot/CLAUDE.md`'s two sections, then delete `bot/CLAUDE.md`)
- Modify: root `pyproject.toml` (add `[project]` block from `bot/pyproject.toml`, drop `"bot"` from workspace members, fix `testpaths`)
- Modify: every file containing `bot.`-qualified imports or `bot/`-path prose (`dashboard/*.py`, `dashboard/CLAUDE.md`, test files, `mkdocs.yml`, `guide/**`, `README.md`, root `CLAUDE.md`, `conftest.py`)
- Delete: `bot/pyproject.toml`, `bot/CLAUDE.md` (content merged elsewhere, not moved as files), `bot/` (now-empty directory)

**Interfaces:**
- Produces: a repo with no `bot/` directory; all bot application code, scripts, and docs live at root; `tests/` contains the union of the old root 7 files and the old `bot/tests/` 55 files; `pyproject.toml` is a 2-member workspace (`["dashboard"]` under `[tool.uv.workspace]`, root itself carrying the merged `[project]` block). This is the state task 4 and task 5 build on.
- **This is the largest task in the plan.** Do not stop partway — a half-completed flatten (some files moved, imports not yet fixed) will not pass verification and should not be committed in that state. Complete every step through Step 8 (verify) before committing.

- [ ] **Step 1: Move every top-level `bot/` file and directory except `CLAUDE.md`, `pyproject.toml`, `tests/`**

```bash
git mv bot/Dockerfile Dockerfile
git mv bot/SPEC.md SPEC.md
git mv bot/__init__.py __init__.py
git mv bot/config.py config.py
git mv bot/config_deps.py config_deps.py
git mv bot/cost.md cost.md
git mv bot/diff_utils.py diff_utils.py
git mv bot/fixtures fixtures
git mv bot/formatting.py formatting.py
git mv bot/github_app.py github_app.py
git mv bot/hmac_verify.py hmac_verify.py
git mv bot/main.py main.py
git mv bot/orchestrator.py orchestrator.py
git mv bot/providers providers
git mv bot/queue queue
git mv bot/render_client.py render_client.py
git mv bot/scripts scripts
git mv bot/specialists specialists
git mv bot/webhook.py webhook.py
```

- [ ] **Step 2: Move `bot/tests/*` into root `tests/`**

```bash
git mv bot/tests/*.py tests/
rmdir bot/tests  # should now be empty
```

- [ ] **Step 3: Merge `bot/CLAUDE.md` into root `CLAUDE.md`, then delete it**

Read `bot/CLAUDE.md` (it has two sections: `## Layering constraints` and `## Contracts`, under a `# bot/ — module boundaries and contracts` heading). Add a new `## Module boundaries and contracts` section to root `CLAUDE.md` (a sensible placement is right after the existing `## Project` section, before `## Conventions` — but read root `CLAUDE.md`'s current structure first and pick a placement that reads coherently) containing those same two subsections verbatim, with one adjustment: `bot/providers/active_model.py` in the Contracts section becomes `providers/active_model.py` (drop the `bot/` prefix — this file is being moved in Step 1). Then:

```bash
git rm bot/CLAUDE.md
```

- [ ] **Step 4: Merge `bot/pyproject.toml`'s dependencies into root `pyproject.toml`, fix workspace members and testpaths**

Read `bot/pyproject.toml` for its exact current dependency list and any inline comments explaining specific pins (e.g. the comment about `requests`/`pyjwt` being explicit for `bot/tests/test_github_app.py`) — carry those comments over too, updating the file path in the comment from `bot/tests/test_github_app.py` to `tests/test_github_app.py`.

Edit root `pyproject.toml` to add, above `[tool.uv.workspace]`:

```toml
[project]
name = "pr-review-bot"
version = "0.1.0"
description = "Autonomous code-review engine — GitHub PR webhook -> LLM specialists -> PR comment"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "pygithub>=2.4",
    "google-genai>=0.3",
    "httpx>=0.27",
    "groq>=1.5.0",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "python-dotenv>=1.0",
    "google-auth>=2.35",
    # Neither is imported directly by application code at root -- both are
    # transitive via pygithub. Declared explicitly anyway because
    # tests/test_github_app.py imports `requests` directly (to patch
    # PyGithub's own HTTP transport, per that file's own module docstring),
    # and pyjwt is kept pinned alongside it for the same reason PyGithub
    # needs it.
    "pyjwt>=2.13",
    "requests>=2.32",
    "python-multipart>=0.0.20",
]

[tool.uv]
package = false
```

(Verify this dependency list against what you actually read from `bot/pyproject.toml` in this step — copy the real current list, don't trust this plan's snapshot if it's drifted since the spec was written.)

Then change:

```toml
[tool.uv.workspace]
members = ["dashboard"]
```

and:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "dashboard/tests"]
```

Then:

```bash
git rm bot/pyproject.toml
```

- [ ] **Step 5: Confirm `bot/` is now empty and remove it**

```bash
find bot -type f
```

Expected: no output. If anything remains, it was missed in Steps 1-4 — go back and move/merge it per the spec before proceeding.

```bash
rmdir bot
```

- [ ] **Step 6: Rewrite every `bot.`-qualified Python import**

```bash
grep -rln 'from bot\.\|import bot\.' --include='*.py' --exclude='.env*' .
```

For every file found (expect at minimum `dashboard/router.py`, `dashboard/environment.py`, `dashboard/auth.py`, and possibly others in `dashboard/tests/`), open it and drop the `bot.` prefix from each import (`from bot.queue.store import X` → `from queue.store import X`, `from bot.providers.base import Y` → `from providers.base import Y`, etc.). Do not touch anything under `.git/`.

Re-run the grep to confirm zero remaining matches:

```bash
grep -rln 'from bot\.\|import bot\.' --include='*.py' --exclude='.env*' .
```

Expected: no output.

- [ ] **Step 7: Rewrite every `bot/`-path prose reference**

```bash
grep -rln 'bot/' --include='*.md' --include='*.yml' --include='*.yaml' --exclude='.env*' . \
  | grep -v '^\./ISSUES\.md$' \
  | grep -v '^\./docs/superpowers/'
```

(`ISSUES.md` and everything under `docs/superpowers/` are excluded — they're historical record: incident history and dated design/plan docs that describe what was true *at the time*, not live references to fix. The one exception inside `docs/superpowers/` would be this plan and its own spec, which correctly describe the migration itself — leave those alone too, they're not stale.)

For every file found (expect at minimum `dashboard/CLAUDE.md`, `README.md`, root `CLAUDE.md`, `mkdocs.yml`, files under `guide/`), fix each `bot/`-prefixed path reference to drop the prefix (`bot/Dockerfile` → `Dockerfile`, `bot/scripts/deploy.py` → `scripts/deploy.py`, `bot/SPEC.md` → `SPEC.md`, `bot.queue.store` in prose → `queue.store`, etc.). This includes `dashboard/CLAUDE.md`'s several prose references documented in the spec (`bot/Dockerfile`, `bot.queue.store`, `bot.render_client`, `bot.config.settings`, `bot.providers.base`).

Re-run the grep (same exclusions) to confirm the fix list is empty or down to only genuinely-remaining, correctly-still-referring-to-something-real hits.

- [ ] **Step 8: Verify**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: both green. If pytest reports an `ImportError`/`ModuleNotFoundError`, that's Step 6 or 7 catching a missed rewrite — find and fix it, don't work around it.

```bash
docker build -f Dockerfile . -t pr-review-bot-flatten-check
docker run --rm pr-review-bot-flatten-check python -c "import main"
```

Expected: build succeeds, import succeeds with no output/error.

```bash
mkdocs build --strict
```

Expected: succeeds (this catches any `bot/`-path reference inside `guide/` or an `mkdocs.yml` nav entry that Step 7's grep scope might have missed, since MkDocs's own link-checking is stricter than a grep).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: flatten bot/ to repo root

bot/'s content moves up to root via git mv (history-preserving): app
code, scripts, fixtures, SPEC.md/cost.md, Dockerfile. bot/CLAUDE.md's
content merges into root CLAUDE.md as a new Module boundaries section.
bot/pyproject.toml's dependencies merge into a new root [project] block;
the workspace shrinks to just dashboard. bot/tests/ merges flat into
root tests/ (zero collisions). Every bot.-qualified import and bot/-path
doc reference is rewritten to match.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MgLyw7js8sK5RQFLw2LuZH
EOF
)"
```

---

### Task 4: Fix `render.yaml`

**Files:**
- Modify: `render.yaml`

**Interfaces:**
- Consumes: the flattened `Dockerfile` at repo root, produced by task 3.
- Produces: a `render.yaml` with a correct `dockerfilePath` for this repo's actual current layout. Nothing later in this plan depends on this task's internals, but it must run after task 3 (the `Dockerfile` it points at doesn't exist at the new path until then).

- [ ] **Step 1: Update `dockerfilePath`**

In `render.yaml`, change:

```yaml
dockerfilePath: ./onboarding/Dockerfile
```

to:

```yaml
dockerfilePath: ./Dockerfile
```

- [ ] **Step 2: Trim the now-stale `buildFilter` comment**

Read the comment block above `buildFilter:` in `render.yaml` — it explains a cross-repo path-scoping trick that only ever mattered during the monorepo period. Rewrite it to reflect the current, simpler reality (an ignore-list that skips docs-only changes, nothing more), e.g.:

```yaml
    # Docs-only changes (README, SPEC, CLAUDE.md, ISSUES.md, docs/**)
    # never affect what's actually deployed. An ignore-list (not an
    # include-list) is deliberate: anything NOT explicitly excluded here
    # still triggers a deploy by default, so a forgotten update here costs
    # one avoidable redeploy, never a real change silently failing to
    # deploy.
    buildFilter:
      ignoredPaths:
        - "**/*.md"
```

Leave `envVars` untouched — it's already correct.

- [ ] **Step 3: Verify**

```bash
cat render.yaml  # visually confirm the two edits look right
```

There's no automated linter for `render.yaml` in this repo's verification bar; a careful read is the check here. Confirm `dockerfilePath: ./Dockerfile` matches the file that actually exists at repo root now (`ls Dockerfile`).

- [ ] **Step 4: Commit**

```bash
git add render.yaml
git commit -m "$(cat <<'EOF'
chore: fix render.yaml for the standalone-repo flatten

dockerfilePath now points at the flattened root Dockerfile instead of
the removed onboarding/Dockerfile. Trims the buildFilter comment's
monorepo-era cross-repo-scoping explanation, which no longer applies now
that this is a real standalone repo.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MgLyw7js8sK5RQFLw2LuZH
EOF
)"
```

---

### Task 5: Full verification pass

**Files:** none (verification only — fix forward in whichever file is implicated if something fails)

**Interfaces:**
- Consumes: the fully-restructured repo produced by tasks 1-4.
- Produces: confirmation that the spec's verification bar (§8) is met end-to-end, immediately before the final `brief.md` cleanup in task 6.

- [ ] **Step 1: Full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass. Note the pass count and compare informally against the pre-restructure baseline (7 + 55 root/bot tests, plus dashboard's own — nothing should have vanished from collection).

- [ ] **Step 2: Lint**

```bash
uv run ruff check .
```

Expected: clean, no errors.

- [ ] **Step 3: Docker build + boot smoke test**

```bash
docker build -f Dockerfile . -t pr-review-bot-verify
docker run --rm pr-review-bot-verify python -c "import main"
```

Expected: build succeeds; import succeeds with no output/error. This is the check that specifically catches a workspace-boundary dependency gap that `pytest`/`ruff` (running against the full dev venv) can't see, per root `CLAUDE.md`'s standing rule about this exact failure mode.

- [ ] **Step 4: Docs build**

```bash
mkdocs build --strict
```

Expected: succeeds with no warnings/errors.

- [ ] **Step 5: Confirm no leftover `bot/`/`onboarding/` artifacts**

```bash
find . -maxdepth 1 -iname 'bot' -o -maxdepth 1 -iname 'onboarding'
grep -rln 'from bot\.\|import bot\.' --include='*.py' --exclude='.env*' .
```

Expected: no output from either command.

- [ ] **Step 6: If everything above is green, no commit needed for this task**

This task is pure verification — if a check fails, fix the underlying file (it belongs to whichever earlier task's scope it falls under) and re-run this task's steps from the top before moving on. Do not proceed to task 6 with any check red.

---

### Task 6: Final cleanup — delete `brief.md` and its `.gitignore` entry

**Files:**
- Delete: `brief.md`
- Modify: `.gitignore` (remove the `brief.md` entry, currently at line 48)

**Interfaces:**
- Consumes: a fully green verification pass from task 5. Do not run this task before task 5 passes — per the spec, `brief.md` is this restructure's own source material and should stay available to reference until the work it describes is fully verified.

- [ ] **Step 1: Delete `brief.md`**

```bash
git rm brief.md
```

- [ ] **Step 2: Remove its `.gitignore` entry**

Open `.gitignore`, find the `brief.md` line, remove it.

- [ ] **Step 3: Verify**

```bash
git status
```

Confirm `brief.md` is gone and `.gitignore` shows only the intended one-line removal as a diff.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: remove brief.md now that the standalone-repo restructure is verified

brief.md was local-only kickoff context for this restructure (gitignored,
never meant to be committed), per its own header. The restructure it
describes is complete and passed the full verification bar in task 5, so
it and its now-unused .gitignore entry are removed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MgLyw7js8sK5RQFLw2LuZH
EOF
)"
```
