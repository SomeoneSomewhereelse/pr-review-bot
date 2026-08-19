# Setup Experience — Stage 3b (Guide Site) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn 1,266 lines of README + SETUP prose into a published MkDocs guide a stranger can follow from `git clone` to a first review, and cut README to the ~150 lines that answer "what is this and does it work?"

**Architecture:** Tasks 2–6 are **purely additive** — they build the guide alongside the existing docs, so the original content still exists at every point. Only Task 7 deletes anything, and it updates the three tests coupled to `SETUP.md` in the same commit. Guide setup pages map 1:1 onto `scripts/doctor.py`'s eight steps per track, so the tool and the guide can never disagree about where an operator is.

**Tech Stack:** MkDocs + Material theme · GitHub Actions (Pages) · pytest · Python 3.12.

**Spec:** `docs/superpowers/specs/2026-08-18-setup-experience-design.md` — §3 (3a–3d), §4a/§4a-i (the two tracks the setup pages mirror), §5 (OS idioms), and §11's non-goals. Read §3 before starting.

## Global Constraints

- **`CLAUDE.md`'s "Secret handling" section overrides everything here.** Read it first. **Never open `.env`.** No example in any guide page may show a real value — placeholders only (`<your-service>`, `<your-user>`).
- **NO TEST IN THIS PLAN MAY REACH LIVE INFRASTRUCTURE.** Stage 3a's plan shipped a test that called `deploy.run_checks(...)` unmocked; `check_installation_and_webhook` PATCHed the **real production GitHub App webhook**, and six task-scoped reviews missed it because the test only asserted row *names* (see `ISSUES.md`, 2026-08-18). Every test here reads files or parses YAML — nothing else. If you find yourself calling a `scripts/deploy.py` check function, stop: that is the exact shape of that incident. `tests/conftest.py`'s autouse `_quarantine_operator_apis` fixture now blanks the App credentials by default; do **not** request `live_operator_apis_allowed` anywhere in this stage.
- **Tasks 2–6 add files only. They must not delete or edit `README.md` or `SETUP.md`.** That ordering is what makes the migration safe — the source text remains intact until Task 7 has the guide to replace it with. Do not "tidy up as you go".
- **`guide/reference/` is generated output (Stage 3a).** Never hand-edit those four files; never add a fifth by hand. Link to them.
- **Every file write passes `encoding="utf-8"` and `newline="\n"`** (spec §5a).
- **Every documented command routes through `uv run python -m scripts.*` where one exists** (spec §5). Specifically: `base64 -w0` → `scripts.encode_credential` (`-w` is GNU-only; macOS/BSD errors), and `curl .../healthz` → `scripts.deploy --health-only` (on Windows PowerShell `curl` aliases `Invoke-WebRequest`, which takes different arguments and *looks* like it works).
- **Python 3.12.** ruff selects `E4, E7, E9, F, E501`, `line-length = 100`.
- **Lint and test before every commit:** `uv run ruff check .` then `uv run pytest -v`. Baseline entering this stage: **788 passing**, ruff clean, HEAD `8b8a5ad`.
- **No changes to `app/`.**

---

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `mkdocs.yml` | Site config, Material theme, nav | 1 |
| `guide/index.md` | Landing: what this is, what you'll need, ~30 min | 1 |
| `guide/setup/index.md` | The shared prefix, then "choose your track" | 2 |
| `guide/setup/01..04-*.md` | Steps 1–4, shared by both tracks | 2 |
| `guide/setup/local/05..08-*.md` | Local track: Postgres, tunnel, webhook, run | 3 |
| `guide/setup/hosted/05..08-*.md` | Hosted track: Supabase, Render, sync, pinger | 4 |
| `guide/operations/*.md` | Deploy CLI, overrides, tuning, image deploys, config files | 5 |
| `guide/background/*.md` | Provider history, rehearsals, repo history, redo notes | 6 |
| `guide/reference/*.md` | **Generated in Stage 3a — do not touch** | — |
| `README.md` | Cut 516 → ~150 | 7 |
| `SETUP.md` | Deleted; content lives in the guide | 7 |
| `.github/workflows/ci.yml` | Gains a Pages build+deploy job | 9 |

## Source Migration Map

Every line of the two source documents has a destination. Nothing is dropped without being named here.

**`README.md` (516 lines):**

| Lines | Section | Destination |
|---|---|---|
| 1–13 | Intro + doc links | **Keep** (rewrite links to the guide) |
| 14–44 | Architecture | **Keep** |
| 45–50 | Tech stack | **Keep** |
| 51–72 | Running locally + Docker | **Keep** (trim) |
| 73–110 | Changing operational config | → `guide/operations/config-files.md` |
| 111–137 | Deploying to production + one-time setup | → `guide/setup/hosted/` (Task 4) |
| 138–203 | Verifying a deployment + check table | → `guide/operations/deploy.md`; **the table itself is now generated** — link `../reference/checks.md`, do not restate it |
| 204–261 | Deploying (`--sync-env`) | → `guide/operations/deploy.md`; **push set is generated** — link `../reference/sync-env.md` |
| 262–316 | Switching providers/keys | → `guide/operations/overrides.md` |
| 317–341 | Re-review cooldown | → `guide/operations/tuning.md` |
| 342–390 | Per-key usage cap | → `guide/operations/tuning.md` |
| 391–408 | Image-registry deploys | → `guide/operations/image-deploys.md` |
| 409–442 | Testing + live verification scripts | **Keep** (trim) |
| 443–460 | Live E2E rehearsal | → `guide/background/rehearsals.md` |
| 461–511 | Known limitations | **Keep** — this is the graded deviations record |
| 512–516 | Cost | **Keep** |

**`SETUP.md` (750 lines):**

| Lines | Section | Destination |
|---|---|---|
| 1–5 | Header ("Step 0 prerequisites (completed)") | **Drop** — it is a build-journal title |
| 6–64 | §1 GitHub App | Instructions → `guide/setup/02-github-app.md`; the App-ID/installation-ID/client-ID confusion note → same page |
| 65–211 | §2 LLM provider (Groq live, Vertex reinstated, Gemini blocked→resolved) | Key-acquisition steps → `guide/setup/04-llm-provider.md`; **all history** → `guide/background/providers.md` |
| 212–266 | §2b GitHub Models (retired 2026-07-30) | → `guide/background/providers.md` |
| 267–279 | §2a Docker | → `guide/setup/01-prerequisites.md` (**OS fix**: `winget` line becomes three tabs) |
| 280–295 | §2c Running tests locally | → `guide/setup/01-prerequisites.md` |
| 296–302 | §3 intro | → `guide/setup/index.md` |
| 303–324 | §3.1 Supabase | → `guide/setup/hosted/05-supabase.md` |
| 325–371 | §3.2 Render web service | → `guide/setup/hosted/06-render.md` |
| 372–425 | §3.3 Secrets encoding | → `guide/setup/02-github-app.md` (**OS fix**: `base64 -w0` → `scripts.encode_credential`) |
| 426–541 | §3.4 App install, webhook, verification | → `guide/setup/03-install-app.md` + both tracks' step 7 (**OS fix**: `curl` → `--health-only`) |
| 542–560 | §3.5 Keep-warm pinger | → `guide/setup/hosted/08-pinger.md` |
| 561–601 | §3.6 `set_override.py` | → `guide/operations/overrides.md` (merge with README 262–316; **do not keep both**) |
| 602–640 | §3.7 `--sync-config-db` | → `guide/operations/tuning.md` (merge with README 317–341) |
| 641–659 | §3.8 Image deploys | → `guide/operations/image-deploys.md` (merge with README 391–408) |
| 660–719 | §4 Secrets hygiene + two config files | → `guide/operations/config-files.md` |
| 720–727 | Repo history note | → `guide/background/history.md` |
| 728–744 | Live rehearsal history table | → `guide/background/rehearsals.md` |
| 745–750 | Redo-from-scratch notes | → `guide/background/history.md` |

---

### Task 1: MkDocs scaffolding and the landing page

Spec §3b.

**Files:**
- Create: `mkdocs.yml`, `guide/index.md`
- Modify: `pyproject.toml` (dev dependency group)
- Test: `tests/test_guide_site.py`

**Interfaces:**
- Consumes: `guide/reference/*.md` (Stage 3a output) for nav entries.
- Produces: `mkdocs.yml`'s `nav` structure, which Tasks 2–6 extend and Task 9's workflow builds.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py
"""The site config is the one thing that makes the guide navigable, so its
shape is asserted rather than assumed. Everything here reads files -- no test
in Stage 3b touches the network (see the plan's Global Constraints)."""
from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_MKDOCS = _ROOT / "mkdocs.yml"


def _config() -> dict:
    # mkdocs.yml uses !!python/name: tags for some Material extensions; the
    # base loader keeps them as plain strings instead of refusing to parse.
    return yaml.load(_MKDOCS.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_site_builds_from_the_guide_directory():
    config = _config()
    assert config["docs_dir"] == "guide", (
        "docs/ holds internal engineering notes and superpowers specs; pointing "
        "MkDocs at it would publish all of them"
    )
    assert config["site_name"]


def test_material_features_the_guide_relies_on_are_enabled():
    config = _config()
    assert config["theme"]["name"] == "material"
    extensions = config["markdown_extensions"]
    flat = [e if isinstance(e, str) else next(iter(e)) for e in extensions]
    # Admonitions carry the gotchas that must not read as body text; tabbed
    # content collapses the bash/PowerShell duplication README carries today.
    assert "admonition" in flat
    assert "pymdownx.tabbed" in flat
    assert "pymdownx.superfences" in flat


def test_every_nav_target_exists_on_disk():
    """A nav entry pointing at a missing file builds a broken site silently."""

    def targets(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from targets(value)
        elif isinstance(node, list):
            for item in node:
                yield from targets(item)

    for target in targets(_config()["nav"]):
        assert (_ROOT / "guide" / target).is_file(), f"nav points at missing {target}"


def test_the_generated_reference_pages_are_in_the_nav():
    flat = list(_MKDOCS.read_text(encoding="utf-8").splitlines())
    joined = "\n".join(flat)
    for name in ("config.md", "pricing.md", "checks.md", "sync-env.md"):
        assert f"reference/{name}" in joined


def test_landing_page_states_the_prerequisites_up_front():
    text = (_ROOT / "guide" / "index.md").read_text(encoding="utf-8")
    assert "3.12" in text, "the Python floor is in .python-version and nowhere a reader looks"
    assert "uv" in text
    assert "Postgres" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -v`
Expected: FAIL with `FileNotFoundError` on `mkdocs.yml`.

- [ ] **Step 3: Add the dependency and the config**

Add to `pyproject.toml`'s `[dependency-groups]` `dev` list:

```
    "mkdocs-material>=9.5",
```

Then run `uv sync --all-extras --dev` so the lockfile updates.

Create `mkdocs.yml`:

```yaml
site_name: PR Review Engine
site_description: Autonomous code-review engine — deploy your own
docs_dir: guide
theme:
  name: material
  features:
    - navigation.sections
    - navigation.top
    - content.code.copy
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      toggle: {icon: material/brightness-7, name: Switch to dark mode}
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      toggle: {icon: material/brightness-4, name: Switch to light mode}
markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
nav:
  - Home: index.md
  - Reference:
      - Configuration: reference/config.md
      - Model pricing: reference/pricing.md
      - Deployment checks: reference/checks.md
      - What --sync-env pushes: reference/sync-env.md
```

Tasks 2–6 each append their own `nav` entries. Keep `Reference` last in the nav; it is lookup material, not a reading path.

Create `guide/index.md` with this outline (write the prose; keep it under ~80 lines):

- **One-paragraph pitch** — adapted from `README.md:1-13`.
- **What you'll need** — a GitHub account, an LLM API key (Groq recommended: free tier, no card), and either a local Postgres or a free Supabase project. Say ~30 minutes.
- **Prerequisites** — Python **3.12** (from `.python-version`), `uv`, `git`, and **a Postgres you can reach** (Docker is one of three ways, not the requirement itself). Note that `uv` and Python install instructions live here because `scripts/doctor.py` runs *via* `uv` and so cannot advise on installing it.
- **Two tracks** — one sentence each, linking to `setup/index.md`.
- **The one command to remember** — `uv run python -m scripts.doctor`, which answers "where am I, what's missing, what's next" at any point.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v`
Expected: all PASS.

- [ ] **Step 5: Build the site locally**

Run: `uv run mkdocs build --strict`
Expected: exit 0, no warnings. `--strict` turns a broken internal link into a failure, which is the whole point of running it.

Then delete the build output so it is never committed: `rm -rf site/`, and add `site/` to `.gitignore`.

- [ ] **Step 6: Commit**

```bash
git add mkdocs.yml guide/index.md pyproject.toml uv.lock .gitignore tests/test_guide_site.py
git commit -m "feat: scaffold the MkDocs guide site with its landing page"
```

---

### Task 2: The shared setup prefix — steps 1–4

Spec §4a. These four pages must mirror `scripts/doctor.py`'s shared steps **exactly** — same numbers, same titles — or the tool and the guide disagree about where an operator is. Read `scripts/doctor.py`'s `_SHARED` tuple first and copy its titles verbatim.

**Files:**
- Create: `guide/setup/index.md`, `01-prerequisites.md`, `02-github-app.md`, `03-install-app.md`, `04-llm-provider.md`
- Modify: `mkdocs.yml` (nav)
- Test: `tests/test_guide_site.py` (append)

**Interfaces:**
- Consumes: `doctor._SHARED` step titles.
- Produces: pages Tasks 3 and 4 link to as "you have finished the shared steps".

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
_SETUP = _ROOT / "guide" / "setup"


def test_shared_pages_match_doctors_step_titles():
    """If the guide and doctor disagree about a step's name, an operator
    following one while running the other cannot tell where they are."""
    from scripts import doctor

    shared = [s for s in doctor.steps_for("local") if s.number <= 4]
    pages = {
        1: "01-prerequisites.md",
        2: "02-github-app.md",
        3: "03-install-app.md",
        4: "04-llm-provider.md",
    }
    for step in shared:
        text = (_SETUP / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text, f"step {step.number} page must carry doctor's title"


def test_prerequisites_page_uses_portable_commands_only():
    """spec section 5: `base64 -w0` is GNU-only and `curl` on Windows
    PowerShell aliases Invoke-WebRequest, which takes different arguments."""
    text = (_SETUP / "01-prerequisites.md").read_text(encoding="utf-8")
    assert "base64 -w0" not in text
    assert "winget install Docker" not in text or "=== " in text, (
        "a Windows-only install line must sit inside tabbed content"
    )


def test_github_app_page_encodes_the_pem_with_the_project_script():
    text = (_SETUP / "02-github-app.md").read_text(encoding="utf-8")
    assert "scripts.encode_credential" in text
    assert "base64 -w0" not in text


def test_setup_index_sends_the_reader_to_a_track():
    text = (_SETUP / "index.md").read_text(encoding="utf-8")
    assert "local/05" in text and "hosted/05" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k "shared or prerequisites or github_app_page or setup_index" -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the pages**

**`guide/setup/index.md`** — source: `SETUP.md:296-302`.
Outline: what the eight steps are; steps 1–4 are shared; then choose a track. A short comparison table (local: needs a tunnel, nothing to pay for, URL changes each restart / hosted: stable URL, free tiers, four browser steps). Links to `local/05-postgres.md` and `hosted/05-supabase.md`.

**`guide/setup/01-prerequisites.md`** — source: `SETUP.md:267-279` (Docker), `SETUP.md:280-295` (tests), `README.md:409-428` (test prerequisite prose).
Outline: `git clone` → `uv sync` → `uv run pytest` to prove the checkout. Then the tool table.
Must survive:
- Python **3.12** (`.python-version`).
- **Docker *or* a reachable `DATABASE_URL`**, stated as one conditional — that is what `tests/conftest.py`'s `db_url` fixture actually imposes. Without either, DB-touching tests fail with an opaque testcontainers error.
- The Docker install line becomes a **three-tab block** (Linux / macOS / Windows), each with the official URL as fallback. Get the exact commands from `scripts/_prereqs.py`'s `DOCKER.hints` — do not invent new ones.
- Close with: run `uv run python -m scripts.doctor` and it will tell you what is missing.

**`guide/setup/02-github-app.md`** — source: `SETUP.md:6-64`, `SETUP.md:372-425`.
Outline: the one-command path (`uv run python -m scripts.create_github_app`), what it does, then the manual fallback.
Must survive:
- **Permissions** `pull_requests: write`, `contents: read`, `issues: write`, `metadata: read`; **event** `pull_request`.
- **Keep the App private.** A public App lets any third party self-install and have their events accepted while `GITHUB_TARGET_REPO` is unset. Render this as a warning admonition.
- The **three IDs** confusion: App ID → `GITHUB_APP_ID`; Installation ID → `GITHUB_APP_INSTALLATION_ID` (optional but recommended — pinning it removes the private-key read from the unconditional boot path); Client ID → **unused here**, and sits on the same page.
- PEM encoding via `uv run python -m scripts.encode_credential github-app-private-key.pem`. **Do not mention `base64 -w0`** — `-w` is a GNU coreutils flag and macOS/BSD `base64` errors on it.
- The webhook URL is a **placeholder** at creation; step 7 corrects it.

**`guide/setup/03-install-app.md`** — source: `SETUP.md:426-470` (the install half).
Outline: browser-only, and why (GitHub does not permit an App to install itself). Choosing "All repositories" vs specific repos, and how `GITHUB_TARGET_REPO` is a *separate, optional* narrowing on top.
Must survive: leaving `GITHUB_TARGET_REPO` unset means the bot acts on every repo the installation covers — safe only because the App is private.

**`guide/setup/04-llm-provider.md`** — source: `SETUP.md:65-120` (the key-acquisition parts only; all history goes to Task 6).
Outline: pick a provider, get a key, set it.
Must survive:
- **`LLM_PROVIDER` has no default** and the service refuses to start without it — set it in `.env.config` to one of `gemini`, `groq`, `vertex`.
- **Groq is the recommended starting point**: free tier, no card, and it is what every live rehearsal used.
- Credentials go in `.env` via `uv run python -m scripts.init_env` (**run it yourself** — it prompts for real secrets).
- A model with no pricing entry still runs; it just produces no cost estimate. Link `../reference/pricing.md`.

Append the nav entries to `mkdocs.yml` under a `Setup` section.

- [ ] **Step 4: Run tests and build**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v && uv run mkdocs build --strict && rm -rf site/`
Expected: all PASS, strict build clean.

- [ ] **Step 5: Commit**

```bash
git add guide/setup/ mkdocs.yml tests/test_guide_site.py
git commit -m "docs: add the shared setup steps 1-4 to the guide"
```

---

### Task 3: The local track — steps 5–8

Spec §4a, §4a-i.

**Files:**
- Create: `guide/setup/local/05-postgres.md`, `06-tunnel.md`, `07-webhook.md`, `08-run.md`
- Modify: `mkdocs.yml`
- Test: `tests/test_guide_site.py` (append)

**Interfaces:**
- Consumes: `doctor.steps_for("local")` titles.
- Produces: nothing later tasks consume.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
def test_local_track_pages_match_doctors_titles():
    from scripts import doctor

    pages = {5: "05-postgres.md", 6: "06-tunnel.md", 7: "07-webhook.md", 8: "08-run.md"}
    for step in doctor.steps_for("local"):
        if step.number < 5:
            continue
        text = (_SETUP / "local" / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text


def test_tunnel_page_explains_the_ephemeral_url():
    text = (_SETUP / "local" / "06-tunnel.md").read_text(encoding="utf-8")
    assert "cloudflared" in text
    assert "changes" in text.lower(), "the URL changing each restart must be stated"


def test_local_verify_page_uses_the_project_health_check():
    """spec section 5: curl on Windows PowerShell aliases Invoke-WebRequest."""
    text = (_SETUP / "local" / "07-webhook.md").read_text(encoding="utf-8")
    assert "--health-only" in text
    assert "curl " not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k local -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the pages**

**`05-postgres.md`** — three ways to get one (Docker one-liner, native install, or a free Supabase project used purely as a remote Postgres), then set `DATABASE_URL`. Must survive: if you use Supabase here, copy the **Session-mode pooler** string, port **5432** not 6543 (`SETUP.md:303-324`).

**`06-tunnel.md`** — source: spec §4a-i.
Outline: why a tunnel is required at all (GitHub must reach you, or the trigger is never exercised); `cloudflared tunnel --url http://localhost:8000` in a second terminal; set `PUBLIC_BASE_URL` to the printed URL.
Must survive:
- **Cloudflare is the documented default, not a hard dependency** — it is the only option needing no account, no config, one binary, one command. ngrok now requires an account and authtoken. Anything yielding a public HTTPS URL works.
- **The URL changes on every restart**, so step 7 is re-run each session. Note the named-tunnel alternative (stable hostname, needs a Cloudflare account and DNS) as out of scope.
- An **optional milestone** before investing in the tunnel: `uv run python -m scripts.manual_verify_step3` has no public-URL dependency and proves App auth, diff fetch, and comment upsert against a real PR. It proves the *pipeline*, not the *trigger*. Mark it clearly optional.

**`07-webhook.md`** — `uv run python -m scripts.deploy` registers the webhook and verifies. Must survive: Render and pinger rows `SKIP` cleanly with no `RENDER_API_KEY` — that is expected here, not a problem. Use `--health-only` for a credential-free "is it up?" check; **never `curl`**.

**`08-run.md`** — `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`, then `uv run python -m scripts.seed_demo_pr` to open a real PR with planted issues. What a good result looks like: a comment within ~15s naming issues across all three sections, with a footer showing runtime, tokens, and (if the model is priced) an estimated cost.

- [ ] **Step 4: Run tests and build**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v && uv run mkdocs build --strict && rm -rf site/`

- [ ] **Step 5: Commit**

```bash
git add guide/setup/local/ mkdocs.yml tests/test_guide_site.py
git commit -m "docs: add the local setup track to the guide"
```

---

### Task 4: The hosted track — steps 5–8

Spec §4a, §4b.

**Files:**
- Create: `guide/setup/hosted/05-supabase.md`, `06-render.md`, `07-sync.md`, `08-pinger.md`
- Modify: `mkdocs.yml`
- Test: `tests/test_guide_site.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
def test_hosted_track_pages_match_doctors_titles():
    from scripts import doctor

    pages = {5: "05-supabase.md", 6: "06-render.md", 7: "07-sync.md", 8: "08-pinger.md"}
    for step in doctor.steps_for("hosted"):
        if step.number < 5:
            continue
        text = (_SETUP / "hosted" / pages[step.number]).read_text(encoding="utf-8")
        assert step.title in text


def test_supabase_page_pins_the_session_pooler_port():
    text = (_SETUP / "hosted" / "05-supabase.md").read_text(encoding="utf-8")
    assert "5432" in text and "6543" in text, "both ports named, so the wrong one is unmistakable"


def test_render_page_lists_exactly_the_four_boot_vars():
    """spec section 4b: SETUP.md walked through nine; only four are needed to
    boot, and deploy.py's own check already names which."""
    from scripts import deploy

    text = (_SETUP / "hosted" / "06-render.md").read_text(encoding="utf-8")
    for name in deploy._BOOT_CREDENTIAL_NAMES:
        assert name in text
    assert "RENDER_API_KEY" in text, "must say it is NOT a service env var"


def test_pinger_page_warns_about_the_exact_url():
    text = (_SETUP / "hosted" / "08-pinger.md").read_text(encoding="utf-8")
    assert "healthz" in text
    assert "HEAD" in text, "UptimeRobot's free tier sends HEAD, not GET"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k hosted -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the pages**

**`05-supabase.md`** — source `SETUP.md:303-324`.
Must survive, as a warning admonition: copy the **Session-mode pooler** string **verbatim**, port **5432 not 6543**; both the `postgres.<project-ref>` username and the region subdomain are project-specific, and either wrong gives `FATAL: Tenant or user not found`; percent-encode `@ # / ?` in the password. Also: **wait until the project reports ready** — Render does not retry a failed deploy.

**`06-render.md`** — source `SETUP.md:325-371`, `README.md:111-137`.
Outline: New + → Blueprint → point at `render.yaml`. Then set **exactly four** env vars.
Must survive:
- The four are `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `DATABASE_URL` — take them from `deploy._BOOT_CREDENTIAL_NAMES`, not from memory. Everything else `--sync-env` pushes in step 7.
- **`RENDER_API_KEY` is not a service env var.** It is operator-local tooling; never add it to `render.yaml` or the service.
- `render.yaml` sets `buildFilter.ignoredPaths: ["**/*.md"]`, so a docs-only push never triggers a deploy.
- Troubleshooting the first deploy: `error connecting in 'pool-1'` usually means the Supabase project was not ready or the pooler string is mistyped; fix and click **Manual Deploy**.

**`07-sync.md`** — source `README.md:204-261`.
Outline: `PUBLIC_BASE_URL=... uv run python -m scripts.deploy --sync-env` in bash and PowerShell **tabs**. Link `../../reference/sync-env.md` for the push set rather than restating it. Budget up to ~30 minutes worst case; a warm redeploy is well under a minute. Note the Claude Code `/deploy` command wraps the same CLI.

**`08-pinger.md`** — source `SETUP.md:542-560`.
Must survive: UptimeRobot monitor on `https://<your-service>.onrender.com/healthz`, **5-minute interval**, and the URL must match **exactly** — a stray trailing character 404s on every check while looking perfectly healthy in the dashboard. `/healthz` answers both `GET` and `HEAD` because the free tier sends `HEAD`. Set `UPTIMEROBOT_API_KEY` locally if you want `doctor`/`deploy` to verify it rather than report `SKIPPED`.

- [ ] **Step 4: Run tests and build**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v && uv run mkdocs build --strict && rm -rf site/`

- [ ] **Step 5: Commit**

```bash
git add guide/setup/hosted/ mkdocs.yml tests/test_guide_site.py
git commit -m "docs: add the hosted setup track to the guide"
```

---

### Task 5: `guide/operations/`

Spec §3a. Everything an operator does *after* setup. Where Stage 3a already generates a table, **link it — do not restate it**; a hand-copied table is exactly the drift the generation exists to prevent.

**Files:**
- Create: `guide/operations/deploy.md`, `overrides.md`, `tuning.md`, `image-deploys.md`, `config-files.md`
- Modify: `mkdocs.yml`
- Test: `tests/test_guide_site.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
_OPS = _ROOT / "guide" / "operations"


def test_operations_pages_link_generated_tables_instead_of_restating_them():
    """A hand-copied check table is precisely the drift Stage 3a's generation
    and CI job exist to make impossible."""
    deploy_page = (_OPS / "deploy.md").read_text(encoding="utf-8")
    assert "reference/checks" in deploy_page
    assert "reference/sync-env" in deploy_page
    assert "| Check | Verifies |" not in deploy_page, "do not restate the generated table"


def test_deploy_page_documents_the_exit_codes():
    text = (_OPS / "deploy.md").read_text(encoding="utf-8")
    for code in ("exit 0", "exit 1", "exit 2"):
        assert code in text


def test_config_files_page_states_the_two_file_split():
    text = (_OPS / "config-files.md").read_text(encoding="utf-8")
    assert ".env.config" in text and ".env" in text
    assert "OPERATIONAL_KEYS" in text


def test_tuning_page_says_the_db_only_settings_need_no_redeploy():
    text = (_OPS / "tuning.md").read_text(encoding="utf-8")
    assert "--sync-config-db" in text
    assert "runtime_config" in text


def test_no_operations_page_mentions_the_removed_cost_cap():
    """KEY_USAGE_COST_CAP_USD was removed in Stage 1; documenting it would
    invite someone to set a variable nothing reads."""
    for page in _OPS.glob("*.md"):
        assert "KEY_USAGE_COST_CAP_USD" not in page.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k operations -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the pages**

**`deploy.md`** — source `README.md:138-261`.
Outline: what `scripts/deploy.py` is, the three modes (`--sync-env`, `--health-only`, `--sync-config-db`, mutually exclusive), running it from your own machine (not inside the container — `scripts/` is not in the image, and `RENDER_EXTERNAL_URL` only exists inside Render), the exit-code table, and which operator-local keys unskip which checks.
Link `../reference/checks.md` and `../reference/sync-env.md` instead of reproducing either. bash/PowerShell **tabs** for the invocation. Use `--health-only`, never `curl`.

**`overrides.md`** — merge `README.md:262-316` **and** `SETUP.md:561-601` into one page; do not keep both versions.
Must survive: overrides take effect on the **next ticket the dispatcher claims** — no restart, no redeploy; they are written to whatever `DATABASE_URL` currently resolves to, so running against a local `.env` sets a **local** override only; each provider tracks its own key-index independently; **no secret value ever reaches the database — only the slot's integer index**; `--list` prints names and booleans only and is safe to paste anywhere.

**`tuning.md`** — merge `README.md:317-390` and `SETUP.md:602-640`.
Must survive: the cooldown trio and `KEY_USAGE_TOKEN_CAP`/`KEY_USAGE_RESET_TIME_UTC` are **never a Render env var** — they live only in `runtime_config`, and `.env.config` + `--sync-config-db` is the only path; the cap is **per key slot, not global**, so swapping slots grants a fresh budget; usage **survives restarts** because it is summed from the persisted `reviews` history; a usage-check failure **fails open**. **Do not mention `KEY_USAGE_COST_CAP_USD`** — it was removed in Stage 1.

**`image-deploys.md`** — merge `README.md:391-408` and `SETUP.md:641-659`. Must survive: Render always builds on Render; it never uploads your local tree. `render-service` reports a commit sha for a repo-connected service or an image ref for an image-backed one, and only compares against local `HEAD` when a commit is present.

**`config-files.md`** — source `README.md:73-110`, `SETUP.md:660-719`.
Must survive: `Settings` reads `env_file=(".env", ".env.config")` and the **last file wins**; `OPERATIONAL_KEYS` is an exhaustive literal list and **everything not on it is secret by default**; `.env.config` is safe for anyone — including an agent — to open and edit, `.env` is not; `tests/test_config.py` enforces the split in both directions. Link `../reference/config.md` for the field-by-field table.

- [ ] **Step 4: Run tests and build**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v && uv run mkdocs build --strict && rm -rf site/`

- [ ] **Step 5: Commit**

```bash
git add guide/operations/ mkdocs.yml tests/test_guide_site.py
git commit -m "docs: add the operations section to the guide"
```

---

### Task 6: `guide/background/`

Spec §3c. The journal half of `SETUP.md` — real evidence, kept and published, but out of a newcomer's path.

**Files:**
- Create: `guide/background/providers.md`, `rehearsals.md`, `history.md`
- Modify: `mkdocs.yml`
- Test: `tests/test_guide_site.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
_BG = _ROOT / "guide" / "background"


def test_provider_history_survives_the_migration():
    """This is the record of what was actually tried and what it cost --
    the thing most easily lost when a 750-line journal is restructured."""
    text = (_BG / "providers.md").read_text(encoding="utf-8")
    assert "PERMISSION_DENIED" in text, "the Gemini Trust & Safety block"
    assert "github_models" in text or "GitHub Models" in text
    assert "gemini-2.5-flash" in text, "the Vertex catalog finding"


def test_rehearsal_history_keeps_the_measured_timings():
    text = (_BG / "rehearsals.md").read_text(encoding="utf-8")
    assert "PR #3" in text or "#3" in text
    assert "8s" in text or "8 s" in text


def test_background_is_not_in_the_setup_reading_path():
    setup_pages = list((_ROOT / "guide" / "setup").rglob("*.md"))
    assert setup_pages
    for page in setup_pages:
        assert "background/" not in page.read_text(encoding="utf-8"), (
            "background is optional context; a setup step must not depend on it"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k "provider_history or rehearsal or background" -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the pages**

**`providers.md`** — source `SETUP.md:65-266` (all of §2, §2b), plus `README.md:461-511`'s provider bullets as context.
Must survive verbatim in substance: the Gemini **account-level `403 PERMISSION_DENIED`** Trust & Safety block, what triggered it (repeated 429s / many models back-to-back without backoff), that the documented fix is attaching GCP billing, and that a later key update resolved that specific block **without making the risk hypothetical**. The two Vertex bugs found by real live calls (missing OAuth scope on the service-account path; `gemini-flash-latest` not existing as a Vertex publisher model, hence `VERTEX_MODEL=gemini-2.5-flash`). GitHub Models' real 2026-07-30 retirement.

**`rehearsals.md`** — source `SETUP.md:728-744`, `README.md:443-460`. Keep the table intact: PRs #2–#9, path exercised, measured result. These are the project's evidence that it ran for real.

**`history.md`** — source `SETUP.md:720-727` (subtree split from the course repo) and `SETUP.md:745-750` (redo-from-scratch pointers).

- [ ] **Step 4: Run tests and build**

Run: `uv run ruff check . && uv run pytest tests/test_guide_site.py -v && uv run mkdocs build --strict && rm -rf site/`

- [ ] **Step 5: Commit**

```bash
git add guide/background/ mkdocs.yml tests/test_guide_site.py
git commit -m "docs: move the build journal into the guide's background section"
```

---

### Task 7: Cut README, delete SETUP — and fix the tests coupled to it

Spec §3a, §3c. **This is the only destructive task in the stage.** What protects the content is that Tasks 2–6 already migrated all of it; verify that before deleting anything.

Three tests currently assert content lives in `SETUP.md`. They must change **in this same commit**, or the branch lands red:

| Test | Line | Change |
|---|---|---|
| `test_env_var_names_match_the_docs` | `tests/test_deploy_script.py:1917` | **Delete.** It existed to catch prose drifting from the code; `guide/reference/sync-env.md` is now generated from the very constants it checked, and CI fails on drift. A prose test asserting against generated output is tautological. Record the reason in the deletion commit. |
| `test_exit_codes_are_documented` | `:1962` | Retarget from `("README.md", "SETUP.md")` to `guide/operations/deploy.md`. |
| `test_render_yaml_declares_every_synced_var` | `:1931` | Unaffected — it reads `render.yaml`, not the docs. Leave it. |

**Files:**
- Modify: `README.md` (516 → ~150), `tests/test_deploy_script.py`
- Delete: `SETUP.md`
- Test: `tests/test_guide_site.py` (append)

- [ ] **Step 1: Verify every section has a home before deleting anything**

Run:
```bash
grep -nE '^#{2,3} ' SETUP.md
ls -R guide/
```
Walk the plan's Source Migration Map and confirm each `SETUP.md` section's destination file exists and contains the migrated content. **If any row has no home, stop and report** — do not delete and reconstruct later.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_guide_site.py (append)
def test_setup_md_is_gone_and_the_guide_replaced_it():
    assert not (_ROOT / "SETUP.md").exists()
    assert (_ROOT / "guide" / "setup" / "index.md").is_file()


def test_readme_is_a_landing_page_not_a_manual():
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 180, "README should be a landing page, not an ops manual"
    assert "Deploy your own" in text, "the guide link must be prominent"
    for heading in ("Architecture", "Tech stack", "Testing", "Known limitations", "Cost"):
        assert heading in text, f"{heading} belongs in README, not the guide"


def test_readme_no_longer_carries_the_operations_manual():
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    for moved in ("--sync-config-db", "set_override", "uptime-pinger"):
        assert moved not in text, f"{moved} moved to guide/operations/"
```

- [ ] **Step 3: Cut README and delete SETUP**

`README.md` keeps: intro (rewrite the doc links to point at the guide), Architecture, Tech stack, Running locally, Docker, Testing (trimmed — drop the E2E rehearsal narrative, now in `guide/background/rehearsals.md`), Known limitations, Cost. Add a prominent **"Deploy your own →"** link near the top.

Delete lines 73–408 and 443–460 per the migration map. Then `git rm SETUP.md`.

Update the two tests as the table above specifies.

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS. A failure naming `SETUP.md` means a test was missed — fix the test, do not restore the file.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/ && git rm SETUP.md
git commit -m "docs: cut README to a landing page and retire SETUP.md into the guide"
```

---

### Task 8: Repoint every reference that named README or SETUP

Spec §3d. Tooling that prints a dead pointer is worse than printing nothing.

**Files:**
- Modify: `scripts/deploy.py` (`:14`, `:39`, `:48`, `:134`), `scripts/demo_provider_swap.py:9`, `scripts/create_github_app.py:14,72`, `tests/test_create_github_app.py:33`, `CLAUDE.md` (`:164`, `:188`, `:190`), `tests/test_deploy_script.py:99`
- Test: `tests/test_guide_site.py` (append)

**Watch for the self-reference trap.** A test that greps source for a forbidden
string must exclude the file that asserts it, or it fails on itself. That has
happened three times in this project already (Stage 2 Task 4's docstring,
Stage 3a's grep-vs-ast fix, and the scan below). Prefer `ast` parsing, or an
explicit directory scope, over a whole-repo substring search.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guide_site.py (append)
def test_deploy_points_at_a_guide_page_that_exists():
    """spec section 3d: the CLI prints this to a terminal user, so it must
    resolve to a real page."""
    from scripts import deploy

    assert deploy._GUIDE_URL.startswith("https://")
    path = deploy._GUIDE_URL.rstrip("/").split("/pr-review-bot/", 1)[1]
    assert (_ROOT / "guide" / f"{path}.md").is_file(), f"no guide page for {path}"


def test_no_script_still_points_at_setup_md():
    """Scans scripts/ ONLY.

    tests/ is deliberately excluded: this very file contains the literal
    "SETUP.md" in test_setup_md_is_gone_and_the_guide_replaced_it, so a scan
    including tests/ would fail on itself. That self-reference trap has now
    bitten this project three times -- see ISSUES.md (Stage 2 Task 4's
    docstring, and Stage 3a's ast-vs-grep note). Any test that scans source
    for a forbidden string must exclude the file asserting it.
    """
    for path in _ROOT.glob("scripts/*.py"):
        assert "SETUP.md" not in path.read_text(encoding="utf-8"), f"{path.name} is stale"


def test_claude_md_points_at_the_guide():
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "SETUP.md" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guide_site.py -k "guide_page or points_at_setup_md or claude_md" -v`
Expected: FAIL — `_GUIDE_URL` does not exist and `SETUP.md` references remain.

- [ ] **Step 3: Repoint everything**

In `scripts/deploy.py`, replace `_README_ANCHOR` with:

```python
_GUIDE_BASE = "https://someonesomewhereelse.github.io/pr-review-bot"
_GUIDE_URL = f"{_GUIDE_BASE}/operations/deploy/"
```

Update `render_report`'s failure line to print `_GUIDE_URL`, and update `tests/test_deploy_script.py:99`'s expected string to match. Fix the prose references at `deploy.py:14`, `:48`, `:134`, `demo_provider_swap.py:9`, `create_github_app.py:14,72`, and `test_create_github_app.py:33` to name guide pages instead of `SETUP.md` sections.

In `CLAUDE.md`, repoint `:164`, `:188`, `:190` at `guide/background/providers.md`.

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/ tests/ CLAUDE.md
git commit -m "docs: repoint every README/SETUP reference at the published guide"
```

---

### Task 9: Publish the site to GitHub Pages

Spec §3b, §7.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_ci_workflow.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_workflow.py (append)
def test_a_pages_job_builds_and_deploys_the_guide():
    jobs = _workflow()["jobs"]
    assert "pages" in jobs
    commands = " ".join(s.get("run", "") for s in jobs["pages"]["steps"])
    assert "mkdocs build" in commands
    assert "--strict" in commands, "a broken internal link must fail the build"


def test_the_pages_job_has_the_permissions_deployment_needs():
    job = _workflow()["jobs"]["pages"]
    assert job["permissions"]["pages"] == "write"
    assert job["permissions"]["id-token"] == "write"


def test_pages_deploys_only_from_the_default_branch():
    """A Pages deploy from a feature branch would publish unreviewed docs."""
    job = _workflow()["jobs"]["pages"]
    assert "refs/heads/main" in job["if"]


def test_the_docs_drift_job_still_exists():
    """Stage 3a's guarantee must survive this stage."""
    assert "docs" in _workflow()["jobs"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ci_workflow.py -k pages -v`
Expected: FAIL with `KeyError: 'pages'`.

- [ ] **Step 3: Add the job**

Append to `.github/workflows/ci.yml` as a sibling of `lint-and-test` and `docs`:

```yaml
  pages:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --all-extras --dev

      # --strict turns a broken internal link into a build failure, which is
      # the only thing that keeps a growing guide's cross-links honest.
      - name: Build the guide
        run: uv run mkdocs build --strict

      - uses: actions/configure-pages@v5

      - uses: actions/upload-pages-artifact@v3
        with:
          path: site

      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_ci_workflow.py -v`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml tests/test_ci_workflow.py
git commit -m "ci: build and publish the guide to GitHub Pages from main"
```

Note for the operator, not a code step: Pages must be set to **"GitHub Actions"** as its source in the repository's Settings → Pages, once. Report this in the close-out rather than attempting it.

---

### Task 10: Full-suite verification and stage close-out

- [ ] **Step 1: Run the whole suite clean**

Run: `uv run ruff check . && uv run pytest -q`
Expected: zero failures. Baseline entering the stage was **788 passing**; report the new count.

- [ ] **Step 2: Build the site strictly, one more time**

Run: `uv run mkdocs build --strict && rm -rf site/`
Expected: exit 0, no warnings. Every internal link resolves.

- [ ] **Step 3: Confirm the generated reference pages were never hand-edited**

Run:
```bash
uv run python -m scripts.gen_docs
git status --porcelain
```
Expected: empty. A diff here means someone edited generated output by hand during the migration.

- [ ] **Step 4: Read the guide end to end as a stranger would**

Follow `guide/index.md` → `setup/index.md` → both tracks, and confirm: every step's title matches what `uv run python -m scripts.doctor --track local` and `--track hosted` print; no page shows a real credential value; no command uses `base64 -w0` or `curl`. Report anything that reads as though it assumes prior knowledge.

- [ ] **Step 5: Report completion**

Summarise: tasks completed, test count before and after, README's final line count, the full `guide/` tree, confirmation that the strict build passes, the one manual step still outstanding (enabling Pages with the "GitHub Actions" source), and any deviation from this plan with its reason.

---

## Out of Scope for Stage 3b

- **No changes to `app/`.**
- **No edits to `guide/reference/`** — generated in Stage 3a; hand-editing is caught by CI.
- **No new tooling.** `doctor.py`, `init_env.py`, `create_github_app.py`, and `gen_docs.py` are finished; this stage documents them.
- **No custom domain, analytics, or versioned docs** on the Pages site.
- **No rewrite of `SPEC.md`, `cost.md`, or `ISSUES.md`.** `SPEC.md` was brought in line with the code during Stage 1.5 and is the design-of-record, not operator documentation.
- **Enabling Pages in repository settings** is an operator action (Settings → Pages → source "GitHub Actions"), reported in the close-out rather than automated.
