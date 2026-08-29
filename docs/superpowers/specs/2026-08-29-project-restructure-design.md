# Project restructure: bot / dashboard / onboarding as workspace sub-projects

## Goal

Today this repo is a single Python package (`app/` = bot + embedded dashboard,
`onboarding/` = the setup wizard, all sharing one root `pyproject.toml`/`uv.lock`).
`render.yaml` deploys `app/` as this repo's own Render service
(`pr-review-engine`); `onboarding/` exists as working code but has no deploy
path of its own.

The onboarding wizard (all 6 sub-projects, done as of 2026-08-27) is now the
product this repo delivers for itself: a self-service tool that provisions a
*visitor's own* bot+dashboard deployment on Render. This restructure makes the
repo's layout match that reality:

- `bot/` and `dashboard/` become sibling sub-projects — code that gets
  deployed to a *visitor's* Render service via the wizard, not run by this
  repo for itself anymore.
- `onboarding/` becomes the main project: what this repo's own
  `render.yaml`/Dockerfile actually deploys.

## Non-goals

- No behavior change to the bot, dashboard, or onboarding wizard themselves —
  this is a pure restructure (file moves, import path updates, deploy config
  updates). Any bug noticed along the way gets logged in `ISSUES.md`, not
  fixed inline, unless it blocks the move itself.
- Not splitting into separate git repos yet. The layout is chosen so that
  split (bot+dashboard → their own repo, onboarding stays here) is a short
  follow-up later, not something this restructure performs.
- Not splitting CI into per-package jobs. One combined `uv sync --all-extras
  --dev` + `uv run pytest -v` job continues to run everything in the
  workspace.

## A. New layout (uv workspace, 3 members)

```
bot/                      # was app/, minus dashboard/auth/static
  pyproject.toml            (workspace member; no declared dependency on
                             dashboard or onboarding)
  Dockerfile                 # what a visitor's Render service builds
  CLAUDE.md                  # was app/CLAUDE.md, updated for the new boundary
  __init__.py, main.py, config.py, webhook.py, orchestrator.py,
  github_app.py, diff_utils.py, formatting.py, hmac_verify.py
  providers/, queue/, specialists/
  scripts/                  # was root scripts/ — deploy.py, set_override.py,
                             # doctor.py, create_github_app.py, seed_demo_pr.py,
                             # encode_credential.py, init_env.py, etc. — all
                             # operate a bot instance
  fixtures/                 # was root fixtures/ — bad_code, webhook_payloads,
                             # llm_cassettes, demo_bulk_bad_code
  tests/                    # bot-only tests (see section C)
  SPEC.md                   # was root SPEC.md — the bot's own design
  cost.md                   # was root cost.md

dashboard/                 # was app/dashboard.py + app/auth.py + app/static/
  pyproject.toml            (workspace member)
  CLAUDE.md                  (new — dashboard-specific rules split out of
                             app/CLAUDE.md and the dashboard-authentication
                             design doc)
  __init__.py, router.py (was dashboard.py), auth.py, static/
  tests/                    # dashboard-only tests

onboarding/                # internals unchanged
  pyproject.toml            (workspace member)
  Dockerfile                  # NEW — this repo's own render.yaml builds this
  CLAUDE.md                   # unchanged content, path unchanged
  __init__.py, main.py, config.py, router.py, github_client.py,
  llm_client.py, render_client.py, supabase_client.py,
  uptimerobot_client.py, static/
  tests/                    # onboarding-only tests

pyproject.toml             # root: [tool.uv.workspace] members =
                             ["bot", "dashboard", "onboarding"]; shared
                             dev-dependency group (pytest, ruff, mkdocs-material,
                             cryptography, etc.) stays here
uv.lock                    # one shared lockfile across all 3 members
CLAUDE.md                  # stays — secret handling + cross-cutting process
                             rules apply to all three sub-projects
ISSUES.md                  # stays — project-wide incident log
README.md, render.yaml
guide/, mkdocs.yml         # stay at root; content updated for new paths
tests/                     # repo-meta tests only (see section C)
```

**Cross-package dependency shape:** `bot/main.py` imports
`dashboard.router` to mount it (bot → dashboard, at composition time only).
`dashboard/router.py` and `dashboard/auth.py` import `bot.config.settings`,
`bot.queue.store`, `bot.queue.dispatcher`, and `bot.providers.base` directly
(dashboard → bot, for reading operational state). Neither package declares
the other in its own `dependencies = [...]` — they coexist as workspace
members sharing one venv, so the cross-imports work without a formal
dependency edge, and there's no circular-dependency-resolution problem for
`uv` to solve. This also matches the eventual repo split: bot+dashboard would
move together (they're one deployed process today); onboarding would stay
behind.

`onboarding/` has zero import relationship with `bot/` or `dashboard/`,
matching its existing CLAUDE.md rule that it never shares credential-handling
code paths with the review engine.

## B. Deploy config

- `render.yaml`: single service, `dockerfilePath: ./onboarding/Dockerfile`.
  The root `Dockerfile` is deleted — nothing else needs it once onboarding
  has its own.
- `onboarding/Dockerfile`: near-identical to today's root `Dockerfile`, but
  `uv sync --package onboarding` (workspace-scoped install) and `CMD` runs
  `onboarding.main:app` instead of `app.main:app`.
- `bot/Dockerfile`: COPYs root `pyproject.toml`/`uv.lock` + `bot/` +
  `dashboard/`, runs `uv sync --package bot` (which, since dashboard has no
  declared dependency edge to install, also needs `dashboard/` physically
  present for the `bot → dashboard` import to resolve at runtime — the
  Dockerfile COPYs both directories even though only one is "the package"
  being synced), `CMD` runs `bot.main:app` (unchanged from today's
  `app.main:app` other than the module rename).
- `onboarding/render_client.py`: the wizard's create-service call changes
  `"envSpecificDetails": {"dockerfilePath": "./Dockerfile"}` →
  `"./bot/Dockerfile"`.

## C. Docs, scripts, fixtures, tests, CI

**Test classification rule:** a test file moves to the sub-project whose
module it imports as its primary subject.

- `tests/test_dashboard*.py`, `tests/test_auth.py`, `tests/test_login_page.py`
  → `dashboard/tests/`
- `tests/test_onboarding_*.py` → `onboarding/tests/`
- Everything else that imports `app.*` today (orchestrator, specialists,
  providers, queue, webhook, github_app, diff_utils, formatting, config,
  hmac_verify, the `scripts/`-adjacent tests, dispatcher/store tests, the
  provider/credential tests) → `bot/tests/`
- Repo-meta tests that exercise the workspace/tooling itself rather than one
  package's code — `test_ci_workflow.py`, `test_guide_site.py`,
  `test_gen_docs.py`, `test_skeleton.py`, `test_check_env_access_hook.py` —
  stay at root `tests/`.

The exact per-file list is enumerated during implementation (there are ~70
files in `tests/` today); this rule is what the implementation plan applies
mechanically, not a fixed list frozen here.

- `.github/workflows/ci.yml`: unchanged in shape — `uv sync --all-extras
  --dev` installs the whole workspace, `uv run ruff check .` and `uv run
  pytest -v` still run against everything. `scripts.gen_docs` in the `docs`
  job needs its import path(s) updated for whatever moved under `bot/`.
- `guide/`, `mkdocs.yml`: stay at root (published GitHub Pages site).
  Content updated wherever it references `app/`, the root `Dockerfile`, or
  `uv run uvicorn app.main:app`.
- `README.md`: updated to describe the new three-part structure and that
  onboarding is what this repo itself deploys.

## D. Migration mechanics

This touches most of the repo (~150+ file moves plus import rewrites), so:

1. Work happens in a git worktree (this project's standing convention).
2. Sequenced so the suite stays green between phases, each phase its own
   commit:
   1. Scaffold the uv workspace (`[tool.uv.workspace]`, three member
      `pyproject.toml` files) and move `onboarding/` into it as-is — it's
      already self-contained, so this proves the workspace mechanics
      (`uv sync`, `uv run pytest`) work before anything riskier happens.
   2. Split `app/` into `bot/` + `dashboard/`, rewriting internal imports
      (`app.` → `bot.`/`dashboard.`) as they move.
   3. Rewire deploy config: `onboarding/Dockerfile` (new), `bot/Dockerfile`
      (moved + package-scoped), root `Dockerfile` deleted, `render.yaml`
      updated, `onboarding/render_client.py`'s `dockerfilePath` updated.
   4. Move `scripts/`, `fixtures/`, `SPEC.md`, `cost.md` under `bot/`.
   5. Redistribute `tests/` per the classification rule in section C.
   6. Update CI, `mkdocs.yml`/`guide/`, `README.md`, and any remaining
      `app.`/root-path references repo-wide (a final grep pass for
      `app\.` / `from app` / `"app/` catches stragglers).
3. Each phase ends with `uv run ruff check .` and `uv run pytest -v` green
   before starting the next.
4. After the branch merges into `main`, the primary worktree's working
   directory updates to match automatically (git handles the file deletions
   and additions) — no manual "apply the new structure" step. What *does*
   need manual cleanup, since git doesn't touch gitignored content: run
   `uv sync` to rebuild `.venv` for the new 3-package workspace, and clear
   stale `__pycache__`/`.pytest_cache`/`.ruff_cache` directories left over
   from the old `app/` layout. Remove the worktree promptly after merge
   (`git worktree remove`) per the existing
   `claude-code-resume-worktree-bug` memory.

## Risks / open items carried into the plan, not resolved here

- The exact per-file test classification (section C) is mechanical but
  large; the implementation plan enumerates it rather than this spec.
- `bot/Dockerfile` needing `dashboard/` physically COPYed in even though only
  `bot` is the `uv sync --package` target is a slightly unusual shape worth
  the implementer double-checking works as expected (workspace-scoped sync
  pulling in a sibling member's *code* via plain filesystem presence, not a
  declared dependency) before trusting it silently.
