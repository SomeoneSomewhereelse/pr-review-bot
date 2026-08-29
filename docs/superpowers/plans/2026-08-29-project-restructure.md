# Project Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn this single-package repo into a 3-member uv workspace — `bot/`,
`dashboard/`, `onboarding/` — where `onboarding/` is what this repo's own
`render.yaml` deploys, and `bot/`+`dashboard/` are code deployed to a
*visitor's* own Render service via the onboarding wizard.

**Architecture:** Bulk `git mv app bot`, then extract `dashboard.py`/`auth.py`/
`static/` out into a sibling `dashboard/` package. A single blanket
`from app.` → `from bot.`/`from dashboard.` sweep keeps every import (app
code, scripts, tests, docs, error-message strings) correct at every commit
boundary. `onboarding/` moves into the same uv workspace unchanged
internally. Deploy config flips: `render.yaml` now builds
`onboarding/Dockerfile`; a new `bot/Dockerfile` (which COPYs both `bot/` and
`dashboard/`, since they share one runtime process) is what the wizard's
`render_client.py` points a visitor's new service at instead.

**Tech Stack:** Python 3.12, FastAPI, `uv` workspaces (`[tool.uv.workspace]`,
per-member `package = false`), pytest/pytest-asyncio/pytest-xdist, ruff,
Docker, Render, mkdocs.

**Spec:** `docs/superpowers/specs/2026-08-29-project-restructure-design.md`

## Global Constraints

- **Never touch `.env`, `.env.config`, `github-app-private-key.pem`, or
  `vertex-ai-private-key.json` with any tool** (Read, Edit, Bash — including
  `grep`/`ls`/`cat` against them, even a narrow-looking pattern). None of
  these files move or change in this plan — they are gitignored, and every
  task that needs to reference their *names* does so only inside error
  strings/docs, never by opening them. If a step seems to require reading
  one, stop and ask the user instead.
- **Never modify `.claude/hooks/check_env_access.py`** for any reason in
  this plan, even if it looks stale after the move (e.g. still assuming a
  root-level `app/`). Flag it as a follow-up in the final task's report
  instead of editing it.
- Every task ends with `uv run ruff check .` and `uv run pytest -v` green
  before committing. Never commit on a red suite.
- No behavior change to bot/dashboard/onboarding logic in this plan — every
  edit is a file move, an import-path rename, or a deploy-config path
  update. If you notice an actual bug while moving code, do not fix it
  inline — log it in `ISSUES.md`'s Parked Issues section instead and move
  on.
- Work happens in a git worktree per this project's standing convention.
  The **first** `uv sync`/`uv run` in a fresh worktree can take 5+ minutes
  (rebuilding `.venv` from scratch, no progress output) — this is expected,
  not a hang; give it a long timeout.
- Commit after every task with a descriptive message; do not squash tasks
  together.

---

### Task 1: Scaffold the uv workspace; move `onboarding/` in as the first member

**Files:**
- Modify: `pyproject.toml` (root)
- Create: `onboarding/pyproject.toml`
- Test: existing `tests/test_onboarding_*.py` (run as-is, no content changes yet)

**Interfaces:**
- Produces: a working `[tool.uv.workspace]` root with one member
  (`onboarding`), proven by a green `uv sync && uv run pytest`. Later tasks
  add `bot` and `dashboard` to the same `members` list.

- [ ] **Step 1: Rewrite the root `pyproject.toml`**

Replace its entire contents with:

```toml
[project]
name = "pr-review-engine"
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
    "openai>=2.48.0",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "python-dotenv>=1.0",
    "google-auth>=2.35",
    "pyjwt>=2.13",
    "requests>=2.32",
]

[tool.uv.workspace]
members = ["onboarding"]

[dependency-groups]
dev = [
    # Imported directly by tests/test_onboarding_github_client.py to build a
    # real RSA key; transitive via pygithub[crypto] otherwise.
    "cryptography>=44.0",
    "mkdocs-material>=9.5",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-xdist>=3.6",
    "respx>=0.21",
    "ruff>=0.7",
    "testcontainers[postgres]>=4.0",
]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "E501"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]
# -n is a PINNED 4, deliberately not `auto`. Each xdist worker pays a full
# app-import startup cost (~1.25s here), which only pays for itself if the
# worker then has enough tests to run. On a 24-core machine `auto` spins 24
# workers and that fixed cost dominates a 20-45s workload: measured on the
# fast-iteration subset, `auto` took 45.2s versus 30.3s serial -- i.e. it made
# the design's own primary target metric SLOWER than no parallelism at all.
# `-n 4` measured 20.2s, the sweep's minimum (2 -> 20.4s, 8 -> 22.1s). See the
# 2026-08-19 test-suite-performance design doc's section 8 for the full table.
# Revisit the number if the suite grows by a large multiple; do not "restore"
# it to auto.
addopts = "-n 4 --dist=loadgroup"
markers = [
    "db: transitively touches the shared Postgres via the db_url fixture (auto-applied by tests/conftest.py's pytest_collection_modifyitems hook, not meant to be added by hand)",
    "xdist_meta: exercises real xdist worker subprocess scheduling via pytester -- slow, excluded from the fast-iteration `-m` filter",
]
```

(`app/`'s `[project]` name/dependencies are deliberately KEPT here, unchanged,
even though this is now also a workspace root — `app/` itself hasn't moved
yet (that's Task 2), and `tests/conftest.py` (loaded for every test,
including onboarding's, since `testpaths` still covers everything at this
point) imports `app.queue.store`, which needs `psycopg` etc. Verified
empirically: a workspace root can carry both a real `[project]` table with
its own dependencies AND `[tool.uv.workspace]` members — root's deps and a
member's deps both get installed into the one shared venv. Task 2, Step 10
is where this root `[project]` table gets removed (once `bot/pyproject.toml`
takes over providing these same dependencies) — do not remove it here.
`testpaths` stays `["tests"]` for now; it grows in Task 5 once tests are
redistributed.)

- [ ] **Step 2: Create `onboarding/pyproject.toml`**

```toml
[project]
name = "onboarding"
version = "0.1.0"
description = "Self-service setup wizard: provisions a visitor's own bot+dashboard deployment"
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
    "google-auth>=2.35",
    "pyjwt>=2.13",
    "requests>=2.32",
]

[tool.uv]
package = false
```

- [ ] **Step 3: Regenerate the lockfile and sync**

Run: `uv lock && uv sync --all-extras --dev`
Expected: resolves successfully, creates/updates `uv.lock` at the repo root.

- [ ] **Step 4: Run the full suite to confirm nothing broke**

Run: `uv run ruff check . && uv run pytest -v`
Expected: identical pass count to the pre-change baseline (`onboarding/`
hasn't moved on disk or changed a single import — only its dependency
declarations moved into its own `pyproject.toml`, so every existing
`tests/test_onboarding_*.py` must still pass unmodified).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock onboarding/pyproject.toml
git commit -m "build: scaffold uv workspace, move onboarding in as first member"
```

---

### Task 2: Split `app/` into `bot/` + `dashboard/`

**Files:**
- Move: `app/` → `bot/` (bulk), then `bot/dashboard.py` → `dashboard/router.py`,
  `bot/auth.py` → `dashboard/auth.py`, `bot/static/` → `dashboard/static/`
- Create: `bot/pyproject.toml`, `dashboard/pyproject.toml`, `dashboard/__init__.py`
- Modify: `bot/main.py` (3 import lines), root `pyproject.toml` (workspace members)
- Rename-in-place (content only, not moved yet): every `tests/test_*.py`,
  every `scripts/*.py`, `.claude/commands/setup.md`, `app/CLAUDE.md` → moves
  to `bot/CLAUDE.md`

**Interfaces:**
- Produces: `bot.config.settings`, `bot.queue.store`, `bot.providers.base`,
  etc. (every name `app.X` used to export, now under `bot.X`).
  `dashboard.router.router` (was `app.dashboard.router`), `dashboard.auth.router`
  / `dashboard.auth.SessionRequired` / `dashboard.auth.require_session` (was
  `app.auth.*`).
- Consumes: nothing new — this is a pure rename of Task 1's baseline.

- [ ] **Step 1: Bulk-move `app/` to `bot/`**

```bash
git mv app bot
```

- [ ] **Step 2: Repo-wide blanket import rename**

This single sweep is what keeps the suite green immediately after this
task, even though `dashboard.py`/`auth.py` haven't been carved out yet and
`scripts/`/`fixtures/`/`tests/` haven't physically moved yet (that's Tasks
4-5) — only their *content* changes here.

```bash
grep -rlZ --include='*.py' -E '^from app\.|^from app import|^import app\b' . \
  --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__} \
  | xargs -0 sed -i \
    -e 's/^from app\./from bot./' \
    -e 's/^from app import/from bot import/' \
    -e 's/^import app\b/import bot/'
```

Also fix the one non-import string reference (an error message in
`bot/github_app.py`, unrelated to the scripts/bot.scripts rename that
happens in Task 4 — leave the `scripts.encode_credential` part of that
string alone for now):

Run: `grep -rn "app\.main:app" . --include='*.py' --include='*.md' --include='*.yml' --include='Dockerfile' 2>/dev/null | grep -v .venv`
Expected: hits in `Dockerfile` (`CMD`) and possibly `README.md`/`tests/test_skeleton.py`-adjacent docs — leave `Dockerfile`'s hit alone (Task 3 replaces the whole Dockerfile); for any `.md` hit, replace `app.main:app` → `bot.main:app` by hand.

- [ ] **Step 3: Verify the blanket sed didn't touch anything under `dashboard`'s future files incorrectly**

Run: `grep -rn "^from bot\." bot/dashboard.py bot/auth.py`
Expected:
```
bot/dashboard.py:from bot.providers.base import KNOWN_PROVIDERS
bot/dashboard.py:from bot.queue import dispatcher, store
bot/auth.py:from bot.config import settings
```
These are correct as-is — dashboard reading bot's internals is the intended
one-directional dependency (see the spec's section A). No edit needed here.

- [ ] **Step 4: Extract `dashboard/` as a sibling package**

```bash
mkdir -p dashboard
git mv bot/dashboard.py dashboard/router.py
git mv bot/auth.py dashboard/auth.py
git mv bot/static dashboard/static
touch dashboard/__init__.py
git add dashboard/__init__.py
```

- [ ] **Step 5: Fix `bot/main.py`'s 3 import lines**

The blanket sed in Step 2 turned these into `from bot.auth import ...` /
`from bot.dashboard import ...`, which are now wrong since those modules no
longer live under `bot/`. Find the current lines (around line 10-13 of
`bot/main.py`):

```python
from bot.auth import SessionRequired, require_session
from bot.auth import router as auth_router
from bot.config import settings
from bot.dashboard import router as dashboard_router
```

Replace with:

```python
from bot.config import settings
from dashboard.auth import SessionRequired, require_session
from dashboard.auth import router as auth_router
from dashboard.router import router as dashboard_router
```

(Keep the rest of `bot/main.py`'s imports as the blanket sed left them —
`from bot import github_app`, `from bot.providers import registry`, `from
bot.queue import dispatcher, store`, `from bot.webhook import router as
webhook_router` are all correct.)

- [ ] **Step 6: Sweep for any other stray `bot.dashboard`/`bot.auth` reference**

```bash
grep -rln --include='*.py' -E 'from bot\.dashboard|from bot\.auth|import bot\.dashboard|import bot\.auth' . --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__}
```

For every file this lists (expect `tests/test_dashboard_api.py`,
`tests/test_dashboard_page.py`, `tests/test_auth.py`, possibly others),
apply the same replacement as Step 5: `bot.dashboard` → `dashboard.router`,
`bot.auth` → `dashboard.auth`.

- [ ] **Step 7: Update `bot/CLAUDE.md` and add `dashboard/CLAUDE.md`**

`app/CLAUDE.md` already became `bot/CLAUDE.md` via Step 1's directory move —
no further move needed, just update its opening line.

Edit `bot/CLAUDE.md`'s first line from:
```
# app/ — module boundaries and contracts

Loaded when working with files under `app/`. Project-wide conventions,
```
to:
```
# bot/ — module boundaries and contracts

Loaded when working with files under `bot/`. Project-wide conventions,
```

Create `dashboard/CLAUDE.md`:

```markdown
# dashboard/ — module boundaries and contracts

Loaded when working with files under `dashboard/`. Project-wide conventions
live in the root `CLAUDE.md`.

## What this package is

The ops/demo dashboard: `GET /` (static page) + `GET /api/dashboard` (JSON),
plus the session-cookie login flow that gates both. Deployed in the same
process as `bot/` (one Render service, one Dockerfile — see
`bot/Dockerfile`), not as its own service.

## Layering

- `dashboard/router.py` reads `bot.queue.store`, `bot.queue.dispatcher`, and
  `bot.providers.base.KNOWN_PROVIDERS` directly — this is the one place
  `dashboard` depends on `bot`'s internals, and it's read-only (never
  enqueues, never mutates provider state).
- `dashboard/auth.py` reads only `bot.config.settings` (the three
  `DASHBOARD_*` credential fields) — no queue/provider access.
- `bot/main.py` mounts `dashboard.router.router` and `dashboard.auth.router`
  — the one place `bot` depends on `dashboard`. Neither package declares
  the other in its own `pyproject.toml` `dependencies` (see the
  2026-08-29 project-restructure design spec, section A) — they coexist as
  workspace members sharing one venv.

## Contracts

- Every dashboard/auth route assumes it runs behind `bot/main.py`'s
  `SessionRequired` exception handler and `require_session` dependency —
  this package does not re-implement that wiring itself.
- `dashboard/static/dashboard.html` and `dashboard/static/login.html` are
  read once at import time (`_STATIC_DIR = Path(__file__).parent /
  "static"`) — moving either file requires keeping them siblings of
  `router.py`/`auth.py`.
```

- [ ] **Step 8: Create `bot/pyproject.toml`**

```toml
[project]
name = "bot"
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
    "openai>=2.48.0",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "python-dotenv>=1.0",
    "google-auth>=2.35",
    "pyjwt>=2.13",
    "requests>=2.32",
]

[tool.uv]
package = false
```

- [ ] **Step 9: Create `dashboard/pyproject.toml`**

```toml
[project]
name = "dashboard"
version = "0.1.0"
description = "Ops/demo dashboard for the bot — deployed in-process with bot/, not standalone"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "pydantic>=2.9",
    "pyjwt>=2.13",
]

[tool.uv]
package = false
```

(`dashboard` does not declare `bot` as a dependency — see the design spec's
note on this. It works because both are workspace members in the same
venv, and this package is never synced/deployed standalone.)

- [ ] **Step 10: Add both to the workspace, remove root's now-redundant `[project]` table, and re-lock**

Task 1 deliberately kept root `pyproject.toml`'s `[project]` table (name
`pr-review-engine` + all of `app/`'s former dependencies) because `app/`
hadn't moved yet and `tests/conftest.py` needed those dependencies
importable. Now that `bot/pyproject.toml` (Step 8, same dependency list)
exists and provides them instead, remove root's `[project]` table entirely
— root becomes a "virtual" workspace root that ships no code of its own,
matching the design spec's intent. Root `pyproject.toml` should end up
with no `[project]` table at all, just:

```toml
[tool.uv.workspace]
members = ["bot", "dashboard", "onboarding"]
```

followed by the existing `[dependency-groups]`, `[tool.ruff]`,
`[tool.ruff.lint]`, and `[tool.pytest.ini_options]` sections, unchanged.

Run: `uv lock && uv sync --all-extras --dev`

- [ ] **Step 11: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: same pass count as Task 1's baseline. `tests/` hasn't moved yet
(Task 5), but every test's *content* now imports `bot.*`/`dashboard.*`
correctly, so nothing should fail or be skipped differently.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "refactor: split app/ into bot/ (review engine) + dashboard/ (ops dashboard)"
```

---

### Task 3: Rewire deploy config

**Files:**
- Delete: `Dockerfile` (root)
- Create: `bot/Dockerfile`, `onboarding/Dockerfile`
- Modify: `render.yaml`, `onboarding/render_client.py`

**Interfaces:**
- Consumes: `bot.main:app` and `dashboard` (Task 2), `onboarding.main:app`
  (Task 1).
- Produces: this repo's own `render.yaml` deploy now builds
  `onboarding/Dockerfile`; a visitor's wizard-created Render service builds
  `bot/Dockerfile` at `./bot/Dockerfile` in the same repo.

- [ ] **Step 1: Create `bot/Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY bot/pyproject.toml ./bot/pyproject.toml
RUN uv sync --frozen --no-dev --package bot

COPY bot ./bot
COPY dashboard ./dashboard

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

(`dashboard/pyproject.toml` is deliberately NOT copied/synced — `dashboard`
has no declared dependency edge from `bot`, so `--package bot` never tries
to resolve it; `dashboard/`'s *code* still needs to be physically present
for `bot.main`'s `from dashboard.router import ...` to resolve at import
time, which the plain `COPY dashboard ./dashboard` provides. `--no-sync` on
the `CMD` is load-bearing: without it, `uv run` re-syncs the whole
workspace on container start, which would need `dashboard/pyproject.toml`
and every other member's lockfile entry present — verified empirically
against a real 2-member workspace scratch test before writing this task.)

- [ ] **Step 2: Create `onboarding/Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY onboarding/pyproject.toml ./onboarding/pyproject.toml
RUN uv sync --frozen --no-dev --package onboarding

COPY onboarding ./onboarding

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "onboarding.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Delete the root Dockerfile**

```bash
git rm Dockerfile
```

- [ ] **Step 4: Update `render.yaml`**

Modify the `dockerfilePath` line:
```yaml
    dockerfilePath: ./Dockerfile
```
→
```yaml
    dockerfilePath: ./onboarding/Dockerfile
```

Everything else in `render.yaml` (the `envVars` list, `buildFilter`,
`healthCheckPath`) is unrelated to this change — leave it as-is for now.
(A later, separate task — not in this plan — is to actually swap
`render.yaml`'s `envVars` list to onboarding's own env vars; this plan only
repoints the build path, since the design's non-goals section scopes this
restructure to file/deploy-path moves, not a re-audit of which env vars
onboarding vs. bot needs on Render.)

- [ ] **Step 5: Update `onboarding/render_client.py`**

Find the `create_service` function's request body (around line 145):
```python
                    "serviceDetails": {
                        "runtime": "docker",
                        "plan": "free",
                        "healthCheckPath": "/healthz",
                        "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
                    },
```
Change `"./Dockerfile"` to `"./bot/Dockerfile"`.

Also fix the function's docstring, which currently claims parity with
`render.yaml`'s own deploy (no longer true — `render.yaml` now builds
`onboarding/Dockerfile`, not `bot/Dockerfile`):

Find:
```
    """Create a free-plan Docker web service from repo_url, with every env
    var left blank -- the visitor's later frames fill them in via
    push_env_vars(). Mirrors render.yaml's own buildFilter/healthCheckPath/
    dockerfilePath exactly, so a wizard-created service behaves identically
    to this project's own production deploy. Never derives the returned
```
Replace with:
```
    """Create a free-plan Docker web service from repo_url, with every env
    var left blank -- the visitor's later frames fill them in via
    push_env_vars(). Mirrors bot/Dockerfile's own build shape
    (buildFilter/healthCheckPath match render.yaml's conventions; the
    dockerfilePath points at bot/Dockerfile specifically, since this
    project's own render.yaml builds onboarding/Dockerfile instead -- see
    the 2026-08-29 project-restructure design spec). Never derives the returned
```

- [ ] **Step 6: Check for any test asserting the old `dockerfilePath` string**

Run: `grep -rln '"./Dockerfile"' tests/ --include='*.py'`
Expected: likely `tests/test_onboarding_render_client.py`. Update its
asserted string from `"./Dockerfile"` to `"./bot/Dockerfile"`.

- [ ] **Step 7: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: same pass count as Task 2's end state (no test builds a real
Docker image, so this task's changes are exercised only by the render_client
test's string assertion).

- [ ] **Step 8: Sanity-build both new Dockerfiles locally (not part of the test suite, but cheap to verify before committing)**

Run: `docker build -f bot/Dockerfile -t bot-restructure-check .`
Run: `docker build -f onboarding/Dockerfile -t onboarding-restructure-check .`
Expected: both build successfully. If Docker isn't available in this
environment, skip this step and note it in the task's final report instead
of guessing.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "build: repoint deploy config -- render.yaml builds onboarding/Dockerfile, add bot/Dockerfile for wizard-created services"
```

---

### Task 4: Move `scripts/`, `fixtures/`, `SPEC.md`, `cost.md` under `bot/`

**Files:**
- Move: `scripts/` → `bot/scripts/`, `fixtures/` → `bot/fixtures/`,
  `SPEC.md` → `bot/SPEC.md`, `cost.md` → `bot/cost.md`
- Modify: every `.py`/`.md` file referencing `scripts.`/`scripts/` as a
  module path, `.claude/commands/setup.md`, `.github/workflows/ci.yml`,
  `README.md` (SPEC.md/cost.md links only — the full rewrite is Task 6)

**Interfaces:**
- Produces: `bot.scripts.deploy`, `bot.scripts.set_override`,
  `bot.scripts.doctor`, etc. (every `scripts.X` module, now under
  `bot.scripts.X`).

- [ ] **Step 1: Bulk-move**

```bash
git mv scripts bot/scripts
git mv fixtures bot/fixtures
git mv SPEC.md bot/SPEC.md
git mv cost.md bot/cost.md
```

- [ ] **Step 2: Repo-wide `scripts.` module-path rename**

```bash
grep -rlZ -E 'scripts\.[a-z_]+' . \
  --include='*.py' --include='*.md' --include='*.yml' \
  --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__} \
  | xargs -0 sed -i -E 's/\bscripts\.([a-z_]+)/bot.scripts.\1/g'
```

Run: `grep -rn '\bscripts\.[a-z_]\+' . --include='*.py' --include='*.md' --include='*.yml' --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__} | grep -v 'bot\.scripts\.'`
Expected: no output (everything already prefixed `bot.`).

- [ ] **Step 3: Fix `from scripts import X` import lines specifically**

The sweep above only rewrites the `scripts.module` attribute-access shape.
`from scripts import X` lines need a different substitution:

```bash
grep -rlZ --include='*.py' '^from scripts import' . \
  --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__} \
  | xargs -0 sed -i 's/^from scripts import/from bot.scripts import/'
```

- [ ] **Step 4: Fix any bare `scripts/` path reference in docs/CI**

Run: `grep -rn '\bscripts/' . --include='*.md' --include='*.yml' --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__} | grep -v 'bot/scripts/'`
Expected: a handful of hits in `guide/operations/*.md`, `README.md`,
`bot/CLAUDE.md` (via the "moved from `app/CLAUDE.md`" step in Task 2 — that
file doesn't reference scripts, skip if not found). For each hit, replace
`scripts/` with `bot/scripts/`.

- [ ] **Step 5: Fix `.github/workflows/ci.yml`'s `docs` job**

Find:
```yaml
      - name: Regenerate reference docs
        run: uv run python -m scripts.gen_docs
```
Confirm Step 2's sweep already turned this into
`uv run python -m bot.scripts.gen_docs` — if not (workflow YAML files were
included in the sweep's `--include='*.yml'`, so this should already be
correct), fix it by hand now.

- [ ] **Step 6: Fix `.claude/commands/setup.md`**

Confirm Step 2/3's sweep already updated `scripts.doctor` → `bot.scripts.doctor`,
`scripts.init_env` → `bot.scripts.init_env`, `scripts.create_github_app` →
`bot.scripts.create_github_app`, and `scripts/deploy.py` → `bot/scripts/deploy.py`,
`scripts/doctor.py` → `bot/scripts/doctor.py` throughout the file. Read it
back and fix any the sweep missed (the sweep's regex requires a
lowercase-and-underscore module name right after `scripts.`; verify none of
the file's prose sentences slipped past that).

- [ ] **Step 7: Update `tests/test_setup_command.py`'s assertions**

Find:
```python
    for tool in ("scripts.init_env", "scripts.create_github_app"):
```
and
```python
    assert "scripts.doctor" in text
```
Replace `"scripts.init_env"` → `"bot.scripts.init_env"`,
`"scripts.create_github_app"` → `"bot.scripts.create_github_app"`,
`"scripts.doctor"` → `"bot.scripts.doctor"`.

- [ ] **Step 8: Fix README.md's SPEC.md/cost.md links**

Find:
```markdown
Full design: [`SPEC.md`](SPEC.md). Stack/conventions: [`CLAUDE.md`](CLAUDE.md).
Cost model: [`cost.md`](cost.md).
```
Replace with:
```markdown
Full design: [`bot/SPEC.md`](bot/SPEC.md). Stack/conventions: [`CLAUDE.md`](CLAUDE.md).
Cost model: [`bot/cost.md`](bot/cost.md).
```

- [ ] **Step 9: Verify `seed_demo_pr.py`/`seed_bulk_demo_pr.py`'s fixtures path still resolves**

These use `Path(__file__).resolve().parent.parent / "fixtures" / ...` —
module-relative, and since both `scripts/` and `fixtures/` moved one level
deeper together (into `bot/`), this needs no code change. Confirm:

Run: `uv run python -c "from pathlib import Path; import bot.scripts.seed_demo_pr as m; print(m.FIXTURES_DIR, m.FIXTURES_DIR.exists())"`
Expected: prints a path ending in `bot/fixtures/bad_code` and `True`.

- [ ] **Step 10: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: same pass count as Task 3's end state (`tests/` still physically
at root, unmoved — only content/assertions changed).

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: move scripts/, fixtures/, SPEC.md, cost.md under bot/"
```

---

### Task 5: Redistribute `tests/`

**Files:**
- Move: the files listed below from `tests/` into `bot/tests/`,
  `dashboard/tests/`, `onboarding/tests/`, or leave at root `tests/`
- Modify: root `pyproject.toml`'s `testpaths`

**Interfaces:**
- Consumes: `bot.*`, `dashboard.*`, `onboarding.*` (all prior tasks —
  imports are already correct from Task 2's sweep, this task only moves
  files on disk).

- [ ] **Step 1: Create the new test directories**

```bash
mkdir -p bot/tests dashboard/tests onboarding/tests
```

- [ ] **Step 2: Move dashboard tests**

```bash
git mv tests/test_auth.py dashboard/tests/test_auth.py
git mv tests/test_dashboard.py dashboard/tests/test_dashboard.py
git mv tests/test_dashboard_api.py dashboard/tests/test_dashboard_api.py
git mv tests/test_dashboard_page.py dashboard/tests/test_dashboard_page.py
git mv tests/test_login_page.py dashboard/tests/test_login_page.py
```

`tests/test_dashboard.py` asserts against
`Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"`
— fix this path now that both the test and the static file moved under
`dashboard/`:

Find: `_DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"`
Replace: `_DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"`

- [ ] **Step 3: Move onboarding tests**

```bash
git mv tests/test_onboarding_config.py onboarding/tests/test_onboarding_config.py
git mv tests/test_onboarding_github_client.py onboarding/tests/test_onboarding_github_client.py
git mv tests/test_onboarding_i18n.py onboarding/tests/test_onboarding_i18n.py
git mv tests/test_onboarding_llm_client.py onboarding/tests/test_onboarding_llm_client.py
git mv tests/test_onboarding_main.py onboarding/tests/test_onboarding_main.py
git mv tests/test_onboarding_page.py onboarding/tests/test_onboarding_page.py
git mv tests/test_onboarding_render_client.py onboarding/tests/test_onboarding_render_client.py
git mv tests/test_onboarding_router.py onboarding/tests/test_onboarding_router.py
git mv tests/test_onboarding_supabase_client.py onboarding/tests/test_onboarding_supabase_client.py
git mv tests/test_onboarding_uptimerobot_client.py onboarding/tests/test_onboarding_uptimerobot_client.py
```

- [ ] **Step 4: Move bot tests**

```bash
for f in \
  test_active_model.py test_config.py test_conftest_db_fixture.py \
  test_cooldown_config.py test_create_github_app.py test_credentials.py \
  test_deploy_script.py test_diff_utils.py test_dispatcher.py \
  test_dispatcher_backoff.py test_doctor.py test_doctor_steps.py \
  test_encode_credential.py test_formatting.py test_gen_docs.py \
  test_github_app.py test_groq_provider.py test_hmac_verify.py \
  test_init_env.py test_key_index_override.py test_key_usage_cap.py \
  test_main_lifespan.py test_orchestrator.py test_orchestrator_rate_limited.py \
  test_override_helpers.py test_performance_specialist.py \
  test_placeholder_formatting.py test_prereqs.py test_pricing.py \
  test_pricing_check.py test_probes.py test_provider_override.py \
  test_provider_rate_limited.py test_provider_registry.py test_providers.py \
  test_quality_specialist.py test_queue_store.py test_render_client.py \
  test_review_draft_config.py test_reviews_store.py \
  test_schedule_notice_formatting.py test_schemas.py \
  test_security_specialist.py test_set_override_script.py test_skeleton.py \
  test_specialists_base.py test_store_init.py test_store_schema.py \
  test_test_db_script.py test_usage_cap_config.py test_vertex_credentials.py \
  test_webhook.py
do
  git mv "tests/$f" "bot/tests/$f"
done
```

That's the complete bot-test list (52 files) — cross-check against
`tests/` after this step:

Run: `ls tests/*.py`
Expected: only `conftest.py`, `test_check_env_access_hook.py`,
`test_ci_workflow.py`, `test_conftest_db_marker_hook.py`,
`test_conftest_guards.py`, `test_guide_site.py`, `test_setup_command.py`,
`test_xdist_group_ordering.py` remain (the root-meta set — these stay,
Step 5 explains why).

- [ ] **Step 5: Leave root-meta tests where they are**

`conftest.py`, `test_conftest_db_marker_hook.py`, `test_conftest_guards.py`,
`test_check_env_access_hook.py`, `test_ci_workflow.py`, `test_guide_site.py`,
`test_setup_command.py`, `test_xdist_group_ordering.py` all stay in root
`tests/` — none of them is "about" one specific sub-project's code (they
test the workspace/CI/tooling itself), and `conftest.py` in particular must
stay at an ancestor directory of *all four* test directories (root
`tests/`, `bot/tests/`, `dashboard/tests/`, `onboarding/tests/`) for its
`db_url`/`db` fixtures to be visible to `bot/tests/` and `dashboard/tests/`
both — pytest's conftest discovery is directory-hierarchy-based, not
sibling-based, so moving `conftest.py` into `bot/tests/` would silently
break fixture visibility for `dashboard/tests/`. No file changes needed
here beyond what Task 2's sweep already did to `conftest.py`'s own imports.

- [ ] **Step 6: Update `pyproject.toml`'s `testpaths`**

```toml
testpaths = ["tests", "bot/tests", "dashboard/tests", "onboarding/tests"]
```

- [ ] **Step 7: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: same total pass count as Task 4's end state — every test now
lives in its new location, `conftest.py`'s fixtures still reach
`bot/tests/`+`dashboard/tests/` via directory-hierarchy inheritance from
root `tests/`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "test: redistribute tests/ into bot/tests, dashboard/tests, onboarding/tests"
```

---

### Task 6: CI, mkdocs/guide, README, final sweep, cleanup

**Files:**
- Modify: `.github/workflows/ci.yml` (spot-check, likely no change needed),
  `mkdocs.yml`, `guide/**/*.md` (path references), `README.md` (rewrite)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing new — this is the final consistency pass.

- [ ] **Step 1: Confirm CI workflow needs no further edits**

Run: `cat .github/workflows/ci.yml`
Expected: `uv sync --all-extras --dev` and `uv run ruff check .`/`uv run
pytest -v` are unchanged shape (they still operate workspace-wide with no
`--package` flag) and already work correctly across all 3 members without
edits — Tasks 1-5 never introduced a `--package`-scoped step here. The
`docs` job's `uv run python -m bot.scripts.gen_docs` line was already fixed
in Task 4, Step 5. If anything else references `app.`/`scripts.` (not
`bot.scripts.`), fix it now.

- [ ] **Step 2: Sweep `guide/**/*.md` for stale `app/`/`scripts/`/`Dockerfile` references**

```bash
grep -rln -E '\bapp[./]|uvicorn app\.main|scripts/(?!doctor\.py")' guide --include='*.md'
```

For each hit found, read the surrounding context and fix:
- `uv run uvicorn app.main:app` → `uv run uvicorn bot.main:app`
- `scripts.X` / `scripts/X.py` → `bot.scripts.X` / `bot/scripts/X.py` (should
  already be handled by Task 4's sweep if the file matched `*.md` there —
  spot-check `guide/operations/overrides.md`, `guide/operations/tuning.md`,
  `guide/operations/deploy.md` specifically, since those were named in this
  plan's earlier investigation as having `scripts.` references)
- A bare `docker build -t pr-review-engine .` / `docker run ... pr-review-engine`
  (root Dockerfile no longer exists) → `docker build -f bot/Dockerfile -t
  bot .` / `docker run -p 8000:8000 --env-file .env bot`

`mkdocs.yml`'s `nav:` structure and `guide/`'s directory layout itself do
not need to move — this repo's published docs site stays at
`https://tovtechorg.github.io/pr-review-bot/` documenting the bot's setup
flow regardless of the internal code reorg.

- [ ] **Step 3: Rewrite `README.md`**

Update the following, keeping everything else (badges, the review-comment
example, "Known limitations", "Cost") as-is:

- "Running locally" section: change
  ```bash
  uv sync
  cp .env.example .env   # fill in real values -- see the guide's setup section
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
  ```
  to
  ```bash
  uv sync
  cp .env.example .env   # fill in real values -- see the guide's setup section
  uv run uvicorn bot.main:app --host 0.0.0.0 --port 8000
  ```
- "Docker" section: change
  ```bash
  docker build -t pr-review-engine .
  docker run -p 8000:8000 --env-file .env pr-review-engine
  ```
  to
  ```bash
  docker build -f bot/Dockerfile -t pr-review-engine .
  docker run -p 8000:8000 --env-file .env pr-review-engine
  ```
- Add a short paragraph right after the "Architecture" section explaining
  the 3-project structure:
  ```markdown
  ## Repo structure

  This is a 3-member uv workspace:

  - **`onboarding/`** — a self-service setup wizard. **This is what this
    repo's own `render.yaml` deploys** — it provisions a *visitor's own*
    bot+dashboard deployment on Render.
  - **`bot/`** — the review engine described above (webhook, orchestrator,
    specialists, providers, queue). Deployed to a visitor's own Render
    service by the onboarding wizard, not by this repo's own deploy.
  - **`dashboard/`** — the ops dashboard below, deployed in the same
    process as `bot/` (one Render service, one Dockerfile:
    `bot/Dockerfile`), organized as its own package for a clear module
    boundary.
  ```

- [ ] **Step 4: Final repo-wide grep sweep for stragglers**

```bash
grep -rn -E '\bapp\.(config|main|webhook|orchestrator|github_app|diff_utils|formatting|hmac_verify|dashboard|auth|providers|queue|specialists)\b' . \
  --include='*.py' --include='*.md' --include='*.yml' --include='Dockerfile' \
  --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__}
grep -rn -E '"\./Dockerfile"|uvicorn app\.main|from app import|^import app\b' . \
  --include='*.py' --include='*.md' --include='*.yml' \
  --exclude-dir={.git,.venv,.pytest_cache,.ruff_cache,__pycache__}
```
Expected: no output from either command. Fix anything that does show up
by hand, using the same `app.` → `bot.`/`dashboard.` mapping as Task 2.

- [ ] **Step 5: Run the full suite one final time**

Run: `uv run ruff check . && uv run pytest -v`
Expected: full green, same pass count as Task 5's end state.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: update CI/guide/README for the bot+dashboard+onboarding workspace split"
```

- [ ] **Step 7: Note follow-ups for the human, don't act on them**

In this task's final report (not a file edit), flag:
- `.claude/hooks/check_env_access.py` may reference `app/`-relative
  assumptions — per the Global Constraints, this plan never touches that
  file; the user should review it separately if needed.
- `render.yaml`'s `envVars` list still lists bot-specific keys
  (`GITHUB_APP_ID`, `LLM_PROVIDER`, etc.) even though it now deploys
  `onboarding/Dockerfile` — swapping that list to onboarding's own env vars
  is out of scope for this plan (see Task 3, Step 4) and needs its own
  follow-up.
- After this branch merges to `main`, remind the user to run `uv sync` in
  the primary worktree (rebuilds `.venv` for the new 3-package layout) and
  clear stale `__pycache__`/`.pytest_cache`/`.ruff_cache` left over from the
  old `app/` layout, then remove this worktree promptly (`git worktree
  remove`) per the existing `claude-code-resume-worktree-bug` memory.
