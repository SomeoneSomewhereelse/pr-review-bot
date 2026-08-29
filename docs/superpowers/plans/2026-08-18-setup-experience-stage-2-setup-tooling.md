# Setup Experience — Stage 2 (Setup Tooling) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a stranger cloning this repo one command that always answers "where am I, what's missing, what's the exact next command" — plus two one-shot tools that collapse the two most error-prone manual steps.

**Architecture:** A read-only, staged `scripts/doctor.py` composes `scripts/deploy.py`'s existing `CheckResult` functions rather than reimplementing them, and adds only the *backwards* probes deploy.py has no reason to own. Secret-safe probing is isolated in its own small module whose return types (`frozenset[str]`, `dict[str, int]`, `bool`) make a value leak structurally impossible. Two human-run writers (`init_env.py`, `create_github_app.py`) own all `.env` mutation; doctor never writes.

**Tech Stack:** Python 3.12 · `shutil.which` / `platform.system()` · `http.server` (stdlib, for the manifest redirect) · httpx · pydantic-settings · pytest + respx.

**Spec:** `docs/superpowers/specs/2026-08-18-setup-experience-design.md` — §4 in full (4a, 4a-i, 4b, 4c, 4d, 4d-i, 4e, 4f), plus §5's OS rules, §8a/§8b/§8c/§8g/§8j, and §11's non-goals. Read §4 before starting.

## Global Constraints

- **`CLAUDE.md`'s "Secret handling" section overrides everything in this plan.** Read it first. **Never open `.env`.** Never print a secret value or any fragment of one — names, lengths, and booleans only, mirroring `scripts/_render.py::env_vars()`'s contract.
- **`scripts/init_env.py` and `scripts/create_github_app.py` are HUMAN-RUN ONLY.** They write real credentials to `.env`. An agent must never invoke them, exactly as `scripts/encode_credential.py`'s own docstring already forbids for itself. Their tests must write only to `tmp_path` — a test that touches the repo's real `.env` would be a serious incident, so every writer function takes an explicit `Path` with no repo-root default.
- **`scripts/doctor.py` is read-only and idempotent.** It never writes a file, never starts a process, never mutates remote state. All writing belongs to the two tools above.
- **No live LLM calls.** No generation requests anywhere in this stage.
- **Python 3.12.** Use `X | None`, not `Optional[X]`.
- **Every file read/write passes `encoding="utf-8"`; every write also passes `newline="\n"`** (spec §5a).
- **Never branch on the OS for behavior — only for a printed hint.** `platform.system()` may select an install *message*; it must never select code paths. Every hint carries an official URL as a universal fallback (spec §4d).
- **Lint and test before every commit:** `uv run ruff check .` then `uv run pytest -v`. Baseline entering this stage: **675 passing**, ruff clean, HEAD `c024edf`.
- **DB-touching tests need Docker or a local `DATABASE_URL`** (`tests/conftest.py`'s `db_url` fixture).

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/_probes.py` | **New.** Secret-safe local state probes. Returns only `frozenset[str]`, `dict[str, int]`, `bool` — there is no field a secret value *could* occupy. Small and separate precisely so the guarantee is auditable in one place. |
| `scripts/_prereqs.py` | **New.** Tool presence via `shutil.which`, interpreter version, and per-platform install hints. Pure and OS-parameterizable. |
| `scripts/doctor.py` | **New.** Track resolution, the ordered step model, orchestration over `_probes` + `_prereqs` + `deploy.py`'s checks, table and `--json` rendering, CLI. |
| `scripts/create_github_app.py` | **New, human-run.** GitHub App Manifest flow → App ID, base64 PEM, webhook secret into `.env`. |
| `scripts/init_env.py` | **New, human-run.** Interactive scaffolding of `.env` / `.env.config` from the two `.example` files. |
| `.claude/commands/setup.md` | **New.** Claude Code wrapper. No logic of its own; drives `doctor --json` and hands off on any credential entry. |

Nothing in `app/` changes in this stage.

---

### Task 1: `scripts/_probes.py` — secret-safe local probes

Spec §4d-i and §8a. This is the security-critical foundation, so it goes first and gets the heaviest test suite in the stage.

**Files:**
- Create: `scripts/_probes.py`
- Test: `tests/test_probes.py`

**Interfaces:**
- Consumes: `app.config.settings`, `app.providers.registry.PROVIDERS`.
- Produces:
  - `PROBED_SECRETS: tuple[str, ...]` — the env-var names probed.
  - `present_secrets() -> frozenset[str]` — names whose value is non-empty.
  - `secret_lengths() -> dict[str, int]` — name → `len(value)`, only for present ones.
  - `private_key_decodes() -> bool` — whether `GITHUB_APP_PRIVATE_KEY` base64-decodes to something with a PEM header.
  - `llm_provider_state() -> tuple[str, bool]` — `(provider_name_or_empty, credential_present)`.
  Task 4 consumes all five (Task 3 is pure logic and touches no probes).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_probes.py
"""Every probe returns names, lengths, or booleans -- never a value.

The types are the primary guarantee (frozenset[str] / dict[str, int] / bool
have nowhere to put a secret). These tests defend the layer the types cannot:
that nothing stringifies a value into a message, a traceback, or JSON on the
way out. See CLAUDE.md's "Secret handling" section, which this module exists
to make structurally enforceable rather than a matter of discipline.
"""
from __future__ import annotations

import json

import pytest

from app.config import settings
from scripts import _probes

# Distinctive, high-entropy, and structurally unlike a length or a name, so a
# substring search cannot pass by accident.
SENTINEL = "SENTINEL-7f3a91c4e5b8d206-DO-NOT-LEAK"


@pytest.fixture
def seeded(monkeypatch):
    """Every probed secret set to a unique sentinel value."""
    values = {}
    for name in _probes.PROBED_SECRETS:
        value = f"{SENTINEL}-{name}"
        values[name] = value
        monkeypatch.setattr(settings, name.lower(), value, raising=False)
    return values


def test_present_secrets_returns_names_only(seeded):
    result = _probes.present_secrets()
    assert isinstance(result, frozenset)
    assert "GITHUB_WEBHOOK_SECRET" in result
    for name in result:
        assert name in _probes.PROBED_SECRETS
        assert SENTINEL not in name


def test_secret_lengths_returns_integers_only(seeded):
    lengths = _probes.secret_lengths()
    assert lengths, "negative control: the probe must return something"
    for name, length in lengths.items():
        assert isinstance(length, int)
        assert length == len(seeded[name])


def test_no_probe_output_contains_any_sentinel(seeded, capsys):
    """The whole surface at once: return values, their repr, a JSON dump, and
    anything printed. A leak through any one of these is a leak."""
    payload = {
        "present": sorted(_probes.present_secrets()),
        "lengths": _probes.secret_lengths(),
        "pem_ok": _probes.private_key_decodes(),
        "provider": _probes.llm_provider_state(),
    }
    surfaces = [repr(payload), json.dumps(payload), capsys.readouterr().out]
    for surface in surfaces:
        assert SENTINEL not in surface


def test_a_validation_failure_does_not_echo_the_value(monkeypatch):
    """pydantic's ValidationError echoes input_value, so a probe that lets one
    escape turns the error text itself into a secret leak (CLAUDE.md)."""
    monkeypatch.setattr(settings, "github_app_private_key", SENTINEL, raising=False)
    # A non-base64 value must be reported structurally, not by echoing it.
    assert _probes.private_key_decodes() is False
    try:
        _probes.secret_lengths()
    except Exception as exc:  # pragma: no cover -- must not raise at all
        pytest.fail(f"probe raised instead of degrading: {type(exc).__name__}")


def test_private_key_decodes_recognises_a_real_pem(monkeypatch):
    import base64

    pem = b"-----BEGIN RSA PRIVATE KEY-----\nZm9v\n-----END RSA PRIVATE KEY-----\n"
    monkeypatch.setattr(
        settings, "github_app_private_key", base64.b64encode(pem).decode(), raising=False
    )
    assert _probes.private_key_decodes() is True


def test_llm_provider_state_reports_name_and_credential_presence(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_" + SENTINEL, raising=False)
    provider, has_credential = _probes.llm_provider_state()
    assert provider == "groq"
    assert has_credential is True

    monkeypatch.setattr(settings, "llm_provider", "")
    assert _probes.llm_provider_state() == ("", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_probes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts._probes'`.

- [ ] **Step 3: Write the module**

```python
"""Secret-safe local state probes for scripts/doctor.py.

WHY THIS IS ITS OWN MODULE. Every function here returns a name, a count, or a
boolean, and the return TYPES (frozenset[str], dict[str, int], bool) leave
nowhere for a secret value to travel. That is the guarantee, and keeping it in
one small file is what makes it auditable -- see CLAUDE.md's "Secret handling"
section, and scripts/_render.py::env_vars()'s matching contract.

It reads individual Settings FIELDS and reduces each to a length or a boolean
at the point of access. It never reads .env as text: pydantic-settings already
parses both files, and text-parsing would reintroduce every case a regex gets
wrong (an '=' inside a DATABASE_URL, an unencoded multi-line PEM, 'export KEY=',
trailing comments, CRLF line endings from a Windows-authored file).

Nothing here raises. A probe's job is to report state; a probe that throws
would take out the very tool an operator is running to find out what is wrong.
"""

from __future__ import annotations

import base64
import binascii

from app.config import settings
from app.providers import registry

# Every secret-bearing env var doctor reports on. Enumerated literally rather
# than derived from a prefix, for the same reason app/config.py's
# OPERATIONAL_KEYS is: a pattern would silently pick up future names.
PROBED_SECRETS: tuple[str, ...] = (
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "DATABASE_URL",
    *sorted({credential for credential, _model in registry.PROVIDERS.values()}),
)


def _raw(name: str) -> str:
    """The value of one Settings field as text, or '' -- callers MUST reduce
    this to a length or boolean immediately and never let it escape."""
    value = getattr(settings, name.lower(), "")
    return "" if value is None else str(value)


def present_secrets() -> frozenset[str]:
    """Names of probed secrets that have a non-empty value. Names only."""
    return frozenset(name for name in PROBED_SECRETS if _raw(name))


def secret_lengths() -> dict[str, int]:
    """name -> character count, for present secrets only. Counts only."""
    return {name: len(_raw(name)) for name in PROBED_SECRETS if _raw(name)}


def private_key_decodes() -> bool:
    """Whether GITHUB_APP_PRIVATE_KEY base64-decodes to PEM-shaped bytes.

    A boolean, deliberately: this is the one probe that must look at decoded
    secret material, so the decoded bytes never leave this function's frame.
    The most common setup mistake is pasting the PEM verbatim instead of its
    base64 form, which this catches without anyone seeing either.
    """
    raw = _raw("GITHUB_APP_PRIVATE_KEY")
    if not raw:
        return False
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return False
    return decoded.lstrip().startswith(b"-----BEGIN")


def llm_provider_state() -> tuple[str, bool]:
    """(configured provider name or '', whether its credential is present).

    The provider name is NOT a secret and is deliberately reported -- naming it
    is how doctor can say which credential is missing.
    """
    provider = _raw("LLM_PROVIDER")
    entry = registry.PROVIDERS.get(provider)
    if entry is None:
        return (provider, False)
    return (provider, bool(_raw(entry[0])))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_probes.py -v`
Expected: all PASS. If `test_no_probe_output_contains_any_sentinel` fails, a value is escaping — fix the probe, never the test.

- [ ] **Step 5: Commit**

```bash
git add scripts/_probes.py tests/test_probes.py
git commit -m "feat: add secret-safe local state probes for the setup doctor"
```

---

### Task 2: `scripts/_prereqs.py` — tool detection and per-OS hints

Spec §4d's prerequisite stage and §8c. Note the requirement is **Docker *or* a reachable `DATABASE_URL`**, stated as one conditional — that is what `tests/conftest.py` actually imposes, and it is currently buried in a README prose note.

**Files:**
- Create: `scripts/_prereqs.py`
- Test: `tests/test_prereqs.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `Tool` (`NamedTuple`: `executable: str`, `label: str`, `hints: dict[str, str]`, `url: str`)
  - `REQUIRED_TOOLS: tuple[Tool, ...]`, `TUNNEL_TOOL: Tool`
  - `is_available(tool: Tool) -> bool`
  - `install_hint(tool: Tool, system: str | None = None) -> str`
  - `python_version_ok() -> bool` and `MINIMUM_PYTHON: tuple[int, int]`
  - `database_available() -> bool` — Docker present *or* `DATABASE_URL` set.
  - `GIT`, `DOCKER` — the `Tool` instances themselves (`DOCKER` is not in
    `REQUIRED_TOOLS`; it is one of two ways to satisfy the database prereq).
  Task 4 consumes all of these.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prereqs.py
"""Prerequisite detection must behave identically on every OS -- only the
printed install hint varies (design spec 2026-08-18 section 4d). These tests
parameterize the platform so all three hints are verified on any one machine.
"""
from __future__ import annotations

import pytest

from app.config import settings
from scripts import _prereqs


def test_every_required_tool_has_a_hint_for_all_three_platforms():
    for tool in (*_prereqs.REQUIRED_TOOLS, _prereqs.TUNNEL_TOOL):
        for system in ("Linux", "Darwin", "Windows"):
            hint = _prereqs.install_hint(tool, system)
            assert hint.strip(), f"{tool.executable} has no hint for {system}"
            assert tool.url in hint, "every hint carries the official URL as fallback"


def test_an_unknown_platform_still_gets_the_url_fallback():
    """Never leave an operator on a niche OS with nothing actionable."""
    hint = _prereqs.install_hint(_prereqs.REQUIRED_TOOLS[0], "Plan9")
    assert _prereqs.REQUIRED_TOOLS[0].url in hint


def test_is_available_uses_which(monkeypatch):
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: None)
    assert _prereqs.is_available(_prereqs.REQUIRED_TOOLS[0]) is False
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: "/usr/bin/" + name)
    assert _prereqs.is_available(_prereqs.REQUIRED_TOOLS[0]) is True


def test_database_available_accepts_docker_or_a_database_url(monkeypatch):
    """conftest's db_url fixture needs one or the other -- so the prereq is a
    single conditional, not two independent requirements."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: None)
    assert _prereqs.database_available() is False

    monkeypatch.setattr(settings, "database_url", "postgresql://localhost/x")
    assert _prereqs.database_available() is True

    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(_prereqs.shutil, "which", lambda name: "/usr/bin/docker")
    assert _prereqs.database_available() is True


def test_python_version_floor_matches_the_project():
    assert _prereqs.MINIMUM_PYTHON == (3, 12)
    assert _prereqs.python_version_ok() is True  # the suite runs on a supported one


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_install_hint_never_selects_behavior_only_text(system):
    """A regression guard on the rule: platform.system() may pick a MESSAGE,
    never a code path. Availability must not depend on the platform."""
    tool = _prereqs.REQUIRED_TOOLS[0]
    assert isinstance(_prereqs.install_hint(tool, system), str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_prereqs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts._prereqs'`.

- [ ] **Step 3: Write the module**

```python
"""Prerequisite detection for scripts/doctor.py's first stage.

THE OS RULE. platform.system() selects a printed install MESSAGE here and
nothing else -- never a code path, never an availability result. shutil.which
already handles Windows PATHEXT/.exe resolution, so detection itself is
uniform. Every hint ends with the official URL, so an operator on a platform
this module has never heard of still has something actionable.

Deliberately NOT covered: how to install Python or uv. doctor.py runs via
`uv run`, so it cannot advise on getting uv -- that lives in the guide's
prose (design spec 2026-08-18 section 4d).
"""

from __future__ import annotations

import platform
import shutil
import sys
from typing import NamedTuple

from app.config import settings

MINIMUM_PYTHON = (3, 12)


class Tool(NamedTuple):
    executable: str          # what shutil.which looks for
    label: str               # what an operator calls it
    hints: dict[str, str]    # platform.system() -> install command
    url: str                 # official install page; the universal fallback


GIT = Tool(
    "git", "Git",
    {
        "Linux": "your package manager, e.g. `sudo apt install git`",
        "Darwin": "`brew install git` (or Xcode Command Line Tools)",
        "Windows": "`winget install Git.Git`",
    },
    "https://git-scm.com/downloads",
)

DOCKER = Tool(
    "docker", "Docker",
    {
        "Linux": "`sudo apt install docker.io`, then add yourself to the `docker` group",
        "Darwin": "`brew install --cask docker`",
        "Windows": "`winget install Docker.DockerDesktop`",
    },
    "https://docs.docker.com/get-docker/",
)

TUNNEL_TOOL = Tool(
    "cloudflared", "cloudflared",
    {
        "Linux": "`sudo apt install cloudflared` (or download the binary)",
        "Darwin": "`brew install cloudflared`",
        "Windows": "`winget install Cloudflare.cloudflared`",
    },
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
)

# Docker is NOT here: it is only one of the ways to satisfy the database
# prerequisite -- see database_available().
REQUIRED_TOOLS: tuple[Tool, ...] = (GIT,)


def is_available(tool: Tool) -> bool:
    """Whether `tool` is on PATH. shutil.which resolves Windows PATHEXT for
    free, which is why there is no platform branch here."""
    return shutil.which(tool.executable) is not None


def install_hint(tool: Tool, system: str | None = None) -> str:
    """How to install `tool` on `system` (default: this machine).

    An unknown platform falls back to the URL alone -- never to nothing.
    """
    system = system or platform.system()
    command = tool.hints.get(system)
    if command:
        return f"install {tool.label}: {command} -- {tool.url}"
    return f"install {tool.label}: see {tool.url}"


def python_version_ok() -> bool:
    return sys.version_info[:2] >= MINIMUM_PYTHON


def database_available() -> bool:
    """Whether the test suite can get a Postgres: Docker present (testcontainers
    spins one up) OR DATABASE_URL already set. tests/conftest.py's db_url
    fixture needs exactly one of these, so it is one conditional prerequisite
    rather than two independent ones."""
    return bool(settings.database_url) or is_available(DOCKER)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_prereqs.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_prereqs.py tests/test_prereqs.py
git commit -m "feat: add prerequisite detection with per-platform install hints"
```

---

### Task 3: The step model and track resolution

Spec §4a (two tracks, shared prefix), §4d's `--track` rule, §8b, §8j. Pure logic — no I/O, no network, so the ordering is fully testable on its own and a reviewer can reject the step model without touching the CLI.

**Files:**
- Create: `scripts/doctor.py` (this task adds only the pure parts)
- Test: `tests/test_doctor_steps.py`

**Interfaces:**
- Consumes: `settings` (for `resolve_track` only).
- Produces:
  - `State` (`NamedTuple`, all fields `bool`): `prereqs`, `app_credentials`, `app_installed`, `llm_ready`, `database`, `public_url`, `webhook`, `keepalive`
  - `Step` (`NamedTuple`): `number: int`, `title: str`, `field: str`, `command: str`
  - `steps_for(track: str) -> tuple[Step, ...]`
  - `current_step(track: str, state: State) -> Step | None` — `None` means every step is satisfied
  - `resolve_track(explicit: str | None = None) -> str`
  - `TRACKS = ("local", "hosted")`
  Task 4 consumes all of these.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_doctor_steps.py
"""The step model is pure: given a state, it names where you are and what to
run next. Both tracks share steps 1-4 and diverge after (design spec
2026-08-18 section 4a)."""
from __future__ import annotations

import pytest

from app.config import settings
from scripts import doctor

ALL_SATISFIED = doctor.State(
    prereqs=True, app_credentials=True, app_installed=True, llm_ready=True,
    database=True, public_url=True, webhook=True, keepalive=True,
)


def _state(**overrides) -> doctor.State:
    return ALL_SATISFIED._replace(**overrides)


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_each_track_has_eight_consecutively_numbered_steps(track):
    steps = doctor.steps_for(track)
    assert [s.number for s in steps] == list(range(1, 9))
    for step in steps:
        assert step.title.strip()
        assert step.command.strip(), f"step {step.number} must name a next action"


def test_both_tracks_share_their_first_four_steps():
    local, hosted = doctor.steps_for("local"), doctor.steps_for("hosted")
    assert local[:4] == hosted[:4]
    assert local[4:] != hosted[4:], "tracks must actually diverge after step 4"


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_every_step_field_exists_on_state(track):
    """A typo'd field name would make a step silently never satisfiable."""
    for step in doctor.steps_for(track):
        assert step.field in doctor.State._fields


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_current_step_is_the_first_unsatisfied_one(track):
    assert doctor.current_step(track, _state(prereqs=False)).number == 1
    assert doctor.current_step(track, _state(app_credentials=False)).number == 2
    assert doctor.current_step(track, ALL_SATISFIED) is None


@pytest.mark.parametrize("track", doctor.TRACKS)
def test_the_earliest_unsatisfied_step_wins(track):
    """Reporting a later gap first would send an operator down the wrong path."""
    step = doctor.current_step(track, _state(prereqs=False, keepalive=False))
    assert step.number == 1


def test_resolve_track_prefers_the_explicit_flag(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    assert doctor.resolve_track("local") == "local"


def test_resolve_track_detects_hosted_from_render_signals(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    assert doctor.resolve_track(None) == "hosted"

    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    assert doctor.resolve_track(None) == "hosted"


def test_resolve_track_defaults_to_local(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://a-tunnel.example.com")
    assert doctor.resolve_track(None) == "local"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor_steps.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts.doctor'`.

- [ ] **Step 3: Write the pure core**

```python
"""Where am I in setup, what is missing, and what do I run next.

Read-only and idempotent: doctor never writes a file, starts a process, or
mutates remote state. Writing belongs to scripts/init_env.py and
scripts/create_github_app.py (design spec 2026-08-18 section 4d).

It COMPOSES scripts/deploy.py's checks rather than reimplementing them --
two check implementations that could drift is the thing most worth avoiding
here -- and adds only the backwards-looking probes deploy.py has no reason to
own (is .env populated, does the PEM decode, is LLM_PROVIDER set).
"""

from __future__ import annotations

from typing import NamedTuple

from app.config import settings

TRACKS = ("local", "hosted")


class State(NamedTuple):
    """Observable setup state. Every field is a plain bool: a step is either
    satisfied or it is not, and nothing here can carry a secret."""

    prereqs: bool
    app_credentials: bool
    app_installed: bool
    llm_ready: bool
    database: bool
    public_url: bool
    webhook: bool
    keepalive: bool


class Step(NamedTuple):
    number: int
    title: str
    field: str    # the State field that must be True for this step to be done
    command: str  # the exact next action, verbatim


_SHARED: tuple[Step, ...] = (
    Step(1, "Install prerequisites", "prereqs",
         "uv sync, then install anything the prereqs rows above name"),
    Step(2, "Create the GitHub App", "app_credentials",
         "uv run python -m bot.scripts.create_github_app   (run this yourself -- it writes secrets)"),
    Step(3, "Install the App on your repo(s)", "app_installed",
         "open https://github.com/settings/apps -> your app -> Install App"),
    Step(4, "Configure an LLM provider", "llm_ready",
         "set LLM_PROVIDER in .env.config and its API key via "
         "`uv run python -m bot.scripts.init_env` (run this yourself)"),
)

# Steps 5-8 diverge. 'keepalive' means something different per track: locally
# nothing needs to stay warm, so the running uvicorn process satisfies it;
# hosted, it is the UptimeRobot monitor that stops Render's free tier sleeping.
_LOCAL: tuple[Step, ...] = (
    Step(5, "Get a Postgres", "database",
         "start one (`docker run -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16`) "
         "and set DATABASE_URL"),
    Step(6, "Start a tunnel", "public_url",
         "cloudflared tunnel --url http://localhost:8000, then set PUBLIC_BASE_URL "
         "to the printed https URL"),
    Step(7, "Register the webhook", "webhook",
         "uv run python -m bot.scripts.deploy"),
    Step(8, "Run the service", "keepalive",
         "uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"),
)

_HOSTED: tuple[Step, ...] = (
    Step(5, "Create the Supabase project", "database",
         "create it at https://supabase.com, then set DATABASE_URL to the "
         "Session-mode pooler string (port 5432, NOT 6543)"),
    Step(6, "Create the Render service", "public_url",
         "Render dashboard -> New + -> Blueprint -> render.yaml, then set the four "
         "boot vars (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET, "
         "DATABASE_URL)"),
    Step(7, "Sync config and verify", "webhook",
         "uv run python -m bot.scripts.deploy --sync-env"),
    Step(8, "Add the keep-warm pinger", "keepalive",
         "create an UptimeRobot monitor on <your-service>/healthz at a 5-minute "
         "interval (the URL must match exactly); set UPTIMEROBOT_API_KEY locally "
         "if you want doctor to verify it rather than report SKIPPED"),
)


def steps_for(track: str) -> tuple[Step, ...]:
    if track not in TRACKS:
        raise ValueError(f"unknown track {track!r}; expected one of {TRACKS}")
    return _SHARED + (_LOCAL if track == "local" else _HOSTED)


def current_step(track: str, state: State) -> Step | None:
    """The EARLIEST unsatisfied step, or None when setup is complete.

    Earliest, not most-severe: a later gap is usually a consequence of an
    earlier one, so reporting it first would send an operator down the wrong
    path.
    """
    for step in steps_for(track):
        if not getattr(state, step.field):
            return step
    return None


def resolve_track(explicit: str | None = None) -> str:
    """Which track to grade against. An explicit --track always wins.

    Auto-detection is a documented rule, not a guess: a RENDER_API_KEY or an
    onrender.com base URL means hosted; anything else means local. Both tracks
    share steps 1-4, so a wrong guess early costs nothing.
    """
    if explicit:
        if explicit not in TRACKS:
            raise ValueError(f"unknown track {explicit!r}; expected one of {TRACKS}")
        return explicit
    if settings.render_api_key or "onrender.com" in settings.public_base_url:
        return "hosted"
    return "local"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_doctor_steps.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/doctor.py tests/test_doctor_steps.py
git commit -m "feat: add the setup doctor's step model and track resolution"
```

---

### Task 4: `scripts/doctor.py` — the CLI

Spec §4d. Composes `deploy.py`'s checks and adds only what deploy.py has no reason to own.

**Critical constraint:** doctor must **not** call `deploy.check_installation_and_webhook`. That check *sets* the App's webhook URL when it is wrong (`README.md`: "set only if wrong"), and doctor is read-only. Use `app.github_app.discover_installation_id_for_app()` and `app.github_app.get_webhook_url()` instead — both already exist and both are read-only. `app.github_app.set_webhook_url()` is the mutating one; never call it here.

**Files:**
- Modify: `scripts/doctor.py` (add the I/O and CLI layers under Task 3's pure core)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `_probes.present_secrets`, `_probes.private_key_decodes`, `_probes.llm_provider_state`, `_probes.secret_lengths`; `_prereqs.REQUIRED_TOOLS`, `_prereqs.TUNNEL_TOOL`, `_prereqs.is_available`, `_prereqs.install_hint`, `_prereqs.python_version_ok`, `_prereqs.database_available`; `deploy.CheckResult`, `deploy.render_report`, `deploy._safe`, `deploy.resolve_base_url`, `deploy.check_config`, `deploy.check_pricing`, `deploy.check_database`, `deploy.check_provider`, `deploy.check_health_endpoint`, `deploy.check_boot_credentials_live`, `deploy.check_provider_live`, `deploy.check_api_key_live`, `deploy.check_render_service`, `deploy.check_uptime_pinger`; Task 3's `State`/`Step`/`current_step`/`resolve_track`.
- Produces: `check_prereqs(track) -> CheckResult`, `check_local_config() -> CheckResult`, `check_github_install() -> CheckResult`, `check_webhook(base) -> CheckResult`, `build_state(track, base) -> tuple[State, list[CheckResult]]`, `render(track, step, results) -> str`, `as_json(track, step, results) -> str`, `main(argv=None) -> int`. Task 7's `.claude/commands/setup.md` consumes `--json`'s shape.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_doctor.py
"""doctor is read-only, degrades instead of erroring, and never leaks a value."""
from __future__ import annotations

import json

import pytest

from app.config import settings
from scripts import deploy, doctor

SENTINEL = "SENTINEL-2b6d40af91ce7385-DO-NOT-LEAK"


@pytest.fixture
def bare(monkeypatch):
    """A freshly-cloned checkout: nothing configured at all."""
    for field in (
        "github_app_id", "github_app_private_key", "github_webhook_secret",
        "database_url", "llm_provider", "groq_api_key", "gemini_api_key",
        "gcp_service_account_key", "public_base_url", "render_api_key",
        "uptimerobot_api_key",
    ):
        monkeypatch.setattr(settings, field, type(getattr(settings, field))(), raising=False)


def test_llm_provider_row_does_not_depend_on_a_database(bare, monkeypatch):
    """Step 4 must clear on local config alone. Gating it on
    deploy.check_provider (which SKIPs with no DATABASE_URL) would strand an
    operator who has a provider configured but no database yet."""
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x", raising=False)
    assert doctor.check_llm_provider().status == "PASS"
    state, _results = doctor.build_state("local", "")
    assert state.llm_ready is True


def test_a_bare_checkout_reports_step_two_not_a_crash(bare):
    """Render not existing yet is the NORMAL state at the start, not a failure."""
    track = doctor.resolve_track(None)
    assert track == "local"
    state, results = doctor.build_state(track, "")
    step = doctor.current_step(track, state)
    assert step is not None
    assert step.number == 2, "prereqs pass in this environment; the App comes next"
    assert results, "a table is the deliverable even with nothing configured"


def test_no_check_raises_even_with_nothing_configured(bare):
    """Every row must render. _safe guarantees it; this pins that doctor uses it."""
    _state, results = doctor.build_state("hosted", "")
    for result in results:
        assert result.status in ("PASS", "WARN", "FAIL", "SKIPPED")


def test_missing_operator_keys_skip_rather_than_fail(bare):
    _state, results = doctor.build_state("hosted", "https://x.onrender.com")
    by_name = {r.name: r for r in results}
    assert by_name["render-service"].status == "SKIPPED"
    assert "RENDER_API_KEY" in by_name["render-service"].detail


def test_local_track_reports_the_tunnel_and_hosted_does_not(bare):
    local_names = {r.name for r in doctor.build_state("local", "")[1]}
    hosted_names = {r.name for r in doctor.build_state("hosted", "")[1]}
    assert "tunnel" in local_names
    assert "tunnel" not in hosted_names
    assert "uptime-pinger" in hosted_names
    assert "uptime-pinger" not in local_names


def test_prereqs_row_names_the_install_hint_when_a_tool_is_missing(bare, monkeypatch):
    monkeypatch.setattr(doctor._prereqs.shutil, "which", lambda name: None)
    result = doctor.check_prereqs("local")
    assert result.status == "FAIL"
    assert "git-scm.com" in result.detail, "the hint's URL must reach the operator"


def test_local_config_row_reports_names_never_values(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    result = doctor.check_local_config()
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert SENTINEL not in result.detail


def test_json_output_is_machine_readable_and_leak_free(bare, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SENTINEL, raising=False)
    state, results = doctor.build_state("local", "")
    payload = doctor.as_json("local", doctor.current_step("local", state), results)
    assert SENTINEL not in payload
    parsed = json.loads(payload)
    assert parsed["track"] == "local"
    assert parsed["step"]["number"] >= 1
    assert parsed["step"]["command"]
    assert all({"name", "status", "detail"} <= set(c) for c in parsed["checks"])


def test_render_includes_the_you_are_here_line(bare):
    state, results = doctor.build_state("local", "")
    text = doctor.render("local", doctor.current_step("local", state), results)
    assert "step" in text.lower()
    assert "of 8" in text


def test_render_reports_completion_when_every_step_is_satisfied():
    text = doctor.render("local", None, [deploy.CheckResult("config", "PASS")])
    assert "complete" in text.lower()


def test_main_rejects_an_unknown_track(capsys):
    """argparse's choices= exits 2 itself, the same shape
    tests/test_deploy_script.py::test_main_rejects_an_unknown_flag asserts."""
    with pytest.raises(SystemExit) as exc:
        doctor.main(["--track", "nonsense"])
    assert exc.value.code == 2
    assert "nonsense" in capsys.readouterr().err


def test_doctor_never_calls_the_mutating_webhook_setter():
    """check_installation_and_webhook PATCHes the App's hook URL when wrong.
    doctor is read-only, so it must use get_webhook_url() instead."""
    import inspect

    source = inspect.getsource(doctor)
    assert "set_webhook_url" not in source
    assert "check_installation_and_webhook" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: FAIL — `AttributeError: module 'bot.scripts.doctor' has no attribute 'build_state'`.

- [ ] **Step 3: Write the I/O and CLI layers**

Append to `scripts/doctor.py` (Task 3's pure core stays at the top):

```python
import argparse
import json
import sys

from app import github_app
from scripts import _prereqs, _probes, deploy

_APP_CREDENTIALS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET")


def check_prereqs(track: str) -> deploy.CheckResult:
    """Python version and the tools that must be on PATH.

    cloudflared is required for the local track only -- it is what makes the
    service reachable by GitHub's webhook delivery (design spec section 4a-i).
    """
    problems: list[str] = []
    if not _prereqs.python_version_ok():
        major, minor = _prereqs.MINIMUM_PYTHON
        problems.append(
            f"Python {major}.{minor}+ required, running "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    tools = list(_prereqs.REQUIRED_TOOLS)
    if track == "local":
        tools.append(_prereqs.TUNNEL_TOOL)
    problems.extend(
        _prereqs.install_hint(tool) for tool in tools if not _prereqs.is_available(tool)
    )
    if problems:
        return deploy.CheckResult("prereqs", "FAIL", "\n".join(problems))
    return deploy.CheckResult("prereqs", "PASS", "")


def check_test_database() -> deploy.CheckResult:
    """Whether `uv run pytest` can get a Postgres: Docker present, or
    DATABASE_URL set. WARN, not FAIL -- it blocks the test suite, never the
    service itself, so it must not stop an operator from deploying."""
    if _prereqs.database_available():
        return deploy.CheckResult("test-db", "PASS", "")
    return deploy.CheckResult(
        "test-db", "WARN",
        "DB-touching tests need Docker running or DATABASE_URL set; "
        f"{_prereqs.install_hint(_prereqs.DOCKER)}",
    )


def check_local_config() -> deploy.CheckResult:
    """The App credentials present locally.

    Reports NAMES and a decode boolean only -- never a value, never a length in
    the failure path. Pasting the PEM verbatim instead of its base64 form is the
    single most common setup mistake, so it gets its own line.
    """
    present = _probes.present_secrets()
    missing = [name for name in _APP_CREDENTIALS if name not in present]
    problems = [f"missing: {', '.join(missing)}"] if missing else []
    if "GITHUB_APP_PRIVATE_KEY" in present and not _probes.private_key_decodes():
        problems.append(
            "GITHUB_APP_PRIVATE_KEY is set but does not base64-decode to a PEM "
            "-- it must be the base64 form, not the file's contents verbatim: "
            "uv run python -m bot.scripts.encode_credential github-app-private-key.pem"
        )
    if problems:
        return deploy.CheckResult("local-config", "FAIL", "\n".join(problems))
    return deploy.CheckResult("local-config", "PASS", "")


def check_llm_provider() -> deploy.CheckResult:
    """LLM_PROVIDER is set and its credential is present -- LOCALLY.

    Deliberately NOT deploy.check_provider, which resolves the DB override and
    therefore SKIPs without DATABASE_URL. Gating step 4 on that would leave an
    operator who has configured a provider but not yet a database stuck on
    step 4 forever, which is precisely the kind of dead end doctor exists to
    prevent. deploy.check_provider still runs as its own row, for the override
    resolution this cannot see.
    """
    provider, has_credential = _probes.llm_provider_state()
    if not provider:
        return deploy.CheckResult(
            "llm-provider", "FAIL",
            "LLM_PROVIDER is unset (there is no default) -- set it in .env.config",
        )
    if not has_credential:
        return deploy.CheckResult(
            "llm-provider", "FAIL", f"LLM_PROVIDER={provider} but its credential is not set"
        )
    return deploy.CheckResult("llm-provider", "PASS", f"provider={provider}")


def check_github_install() -> deploy.CheckResult:
    """Whether the App has exactly one installation. READ-ONLY."""
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult(
            "github-install", "SKIPPED", "needs the App credentials (see local-config)"
        )
    try:
        installation_id = github_app.discover_installation_id_for_app()
    except Exception as exc:  # noqa: BLE001 -- structural report, never the value
        return deploy.CheckResult(
            "github-install", "FAIL",
            f"could not resolve an installation ({type(exc).__name__}); "
            "install the App at https://github.com/settings/apps",
        )
    return deploy.CheckResult("github-install", "PASS", f"installation={installation_id}")


def check_webhook(base: str) -> deploy.CheckResult:
    """Whether the App's webhook points at `base`. READ-ONLY -- deliberately
    NOT deploy.check_installation_and_webhook, which PATCHes the URL when it is
    wrong. Fixing it is `uv run python -m bot.scripts.deploy`; reporting it is here."""
    if not base:
        return deploy.CheckResult("webhook", "SKIPPED", "no public base URL yet")
    if not all(name in _probes.present_secrets() for name in _APP_CREDENTIALS):
        return deploy.CheckResult("webhook", "SKIPPED", "needs the App credentials")
    wanted = base.rstrip("/") + "/webhook"
    try:
        current = github_app.get_webhook_url()
    except Exception as exc:  # noqa: BLE001
        return deploy.CheckResult("webhook", "FAIL", f"could not read it ({type(exc).__name__})")
    if current == wanted:
        return deploy.CheckResult("webhook", "PASS", "")
    return deploy.CheckResult(
        "webhook", "FAIL",
        f"points at {current or '(unset)'}, wanted {wanted} "
        "-- fix with: uv run python -m bot.scripts.deploy",
    )


def build_state(track: str, base: str) -> tuple[State, list[deploy.CheckResult]]:
    """Probe, staged: local first, then remote only for resources that exist.

    Ordering matters. Render not existing at step 1 is the normal state, so a
    remote probe is SKIPPED rather than failed until its precondition holds.
    """
    results = [
        deploy._safe("prereqs", check_prereqs, track),
        deploy._safe("test-db", check_test_database),
        deploy._safe("local-config", check_local_config),
        deploy._safe("llm-provider", check_llm_provider),
        deploy._safe("config", deploy.check_config),
        deploy._safe("pricing", deploy.check_pricing),
        deploy._safe("github-install", check_github_install),
        deploy._safe("database", deploy.check_database),
        deploy._safe("provider", deploy.check_provider),
    ]
    if track == "local":
        results.append(
            deploy.CheckResult(
                "tunnel", "PASS" if base else "FAIL",
                "" if base else "no PUBLIC_BASE_URL yet -- start a tunnel: "
                "cloudflared tunnel --url http://localhost:8000",
            )
        )
    results.append(deploy._safe("health", deploy.check_health_endpoint, base) if base
                   else deploy.CheckResult("health", "SKIPPED", "no public base URL yet"))
    results.append(deploy._safe("webhook", check_webhook, base))
    if track == "hosted":
        results.extend([
            deploy._safe("boot-creds-live", deploy.check_boot_credentials_live),
            deploy._safe("provider-live", deploy.check_provider_live),
            deploy._safe("api-key-live", deploy.check_api_key_live),
            deploy._safe("render-service", deploy.check_render_service),
            deploy._safe("uptime-pinger", deploy.check_uptime_pinger, base),
        ])

    by_name = {r.name: r for r in results}

    def ok(name: str) -> bool:
        return by_name.get(name, deploy.CheckResult(name, "SKIPPED")).status in ("PASS", "WARN")

    state = State(
        prereqs=ok("prereqs"),
        app_credentials=ok("local-config"),
        app_installed=ok("github-install"),
        llm_ready=ok("llm-provider"),
        database=ok("database"),
        # Gated on credential-FREE rows on purpose. render-service and
        # uptime-pinger both SKIP without an operator-local API key, and a
        # SKIPPED row counts as unsatisfied -- so gating on them would strand
        # an operator who never sets RENDER_API_KEY on step 6 forever.
        # /healthz answering is the credential-free proof the service exists.
        public_url=ok("tunnel") if track == "local" else ok("health"),
        webhook=ok("webhook"),
        keepalive=ok("health") if track == "local" else ok("uptime-pinger"),
    )
    return state, results


def render(track: str, step: Step | None, results: list[deploy.CheckResult]) -> str:
    """deploy.py's table, plus the one line doctor exists to print."""
    report = deploy.render_report(results)
    if step is None:
        return f"{report}\n\ntrack: {track} -- setup complete, every step satisfied."
    return (
        f"{report}\n\ntrack: {track} -- you are at step {step.number} of 8: "
        f"{step.title}\nnext: {step.command}"
    )


def as_json(track: str, step: Step | None, results: list[deploy.CheckResult]) -> str:
    return json.dumps(
        {
            "track": track,
            "step": None if step is None
            else {"number": step.number, "title": step.title, "command": step.command},
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail} for r in results
            ],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report where you are in setup and what to run next (read-only)"
    )
    parser.add_argument("--track", choices=TRACKS, default=None,
                        help="grade against this track (default: auto-detect)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable output")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        track = resolve_track(args.track)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    base = deploy.resolve_base_url()
    state, results = build_state(track, base)
    step = current_step(track, state)
    print(as_json(track, step, results) if args.as_json else render(track, step, results))
    # Exit 0 always: "you are mid-setup" is information, not failure. Only a
    # bad invocation is an error (exit 2 above).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`argparse`'s `choices=TRACKS` rejects an unknown track by raising `SystemExit(2)` before `resolve_track` ever runs, which is why the test above uses `pytest.raises`. The `try/except ValueError` around `resolve_track` stays as defence in depth for a programmatic caller that bypasses the parser.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_doctor.py -v`
Expected: all PASS. `test_doctor_never_calls_the_mutating_webhook_setter` is the important one — if it fails, doctor has stopped being read-only.

- [ ] **Step 5: Run doctor for real, on this checkout**

Run: `uv run python -m bot.scripts.doctor`
Expected: a table plus a "you are at step N of 8" line, exit 0. This repo is fully configured, so most rows should PASS; whatever it reports, it must not crash and must not print any value. Paste the output into your task report.

- [ ] **Step 6: Commit**

```bash
git add scripts/doctor.py tests/test_doctor.py
git commit -m "feat: add scripts/doctor.py -- a read-only, staged setup state report"
```

---

### Task 5: `scripts/create_github_app.py` — the App Manifest flow

Spec §4e. **HUMAN-RUN ONLY** — it writes real credentials to `.env`. An agent must never invoke it, exactly as `scripts/encode_credential.py` already forbids for itself.

`SETUP.md` §1 records that this project's own App was created this way, and gives the exact permission set to reproduce: `pull_requests: write`, `contents: read`, `issues: write`, `metadata: read`; events `pull_request`; and the App **must be private**.

**Files:**
- Create: `scripts/create_github_app.py`
- Test: `tests/test_create_github_app.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `MANIFEST_PERMISSIONS: dict[str, str]`, `MANIFEST_EVENTS: tuple[str, ...]`, `build_manifest(app_name, base_url, redirect_url) -> dict`, `AppCredentials` (`NamedTuple`: `app_id: int`, `private_key_pem: str`, `webhook_secret: str`), `exchange_code(code) -> AppCredentials`, `write_credentials(creds, path, overwrite=False) -> dict[str, int]`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_create_github_app.py
"""The manifest is the security boundary of this whole setup path, so its
shape is asserted rather than assumed. Every test writes to tmp_path only --
never the repo's real .env."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from scripts import create_github_app as cga

SENTINEL_SECRET = "SENTINEL-9c1e4b7a60df2358-WEBHOOK"
SENTINEL_PEM = "-----BEGIN RSA PRIVATE KEY-----\nSENTINEL-PEM-BODY\n-----END RSA PRIVATE KEY-----\n"


def test_manifest_requests_exactly_the_documented_permissions():
    manifest = cga.build_manifest("bot", "https://example.com", "http://localhost:8765/callback")
    assert manifest["default_permissions"] == {
        "pull_requests": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
    }
    assert manifest["default_events"] == ["pull_request"]


def test_manifest_keeps_the_app_private():
    """A PUBLIC App lets any third party self-install and have their events
    accepted while GITHUB_TARGET_REPO is unset (track-all mode) -- SETUP.md
    section 1 records this as the reason the App must stay private."""
    assert cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")["public"] is False


def test_manifest_points_the_hook_at_the_webhook_path():
    manifest = cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")
    assert manifest["hook_attributes"]["url"] == "https://e.com/webhook"
    assert manifest["redirect_url"] == "http://localhost:1/c"


def test_manifest_is_json_serialisable():
    """It is submitted as a form field, so it must round-trip through JSON."""
    json.loads(json.dumps(cga.build_manifest("bot", "https://e.com", "http://l/c")))


def test_exchange_code_parses_githubs_conversion_response():
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/CODE123/conversions").mock(
            return_value=httpx.Response(
                201,
                json={"id": 4242, "pem": SENTINEL_PEM, "webhook_secret": SENTINEL_SECRET},
            )
        )
        creds = cga.exchange_code("CODE123")
    assert creds.app_id == 4242
    assert creds.private_key_pem == SENTINEL_PEM
    assert creds.webhook_secret == SENTINEL_SECRET


def test_exchange_code_reports_a_failure_structurally(capsys):
    """A 4xx body can echo request content; report the status, not the body."""
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/BAD/conversions").mock(
            return_value=httpx.Response(422, json={"message": SENTINEL_SECRET})
        )
        with pytest.raises(SystemExit) as exc:
            cga.exchange_code("BAD")
    assert SENTINEL_SECRET not in str(exc.value)
    assert SENTINEL_SECRET not in capsys.readouterr().err


def test_write_credentials_writes_values_but_reports_only_lengths(tmp_path, capsys):
    env = tmp_path / ".env"
    creds = cga.AppCredentials(app_id=4242, private_key_pem=SENTINEL_PEM,
                               webhook_secret=SENTINEL_SECRET)
    reported = cga.write_credentials(creds, env)

    written = env.read_text(encoding="utf-8")
    assert "GITHUB_APP_ID=4242" in written
    assert SENTINEL_SECRET in written, "the file is the point -- it must carry the value"
    encoded = base64.b64encode(SENTINEL_PEM.encode()).decode()
    assert f"GITHUB_APP_PRIVATE_KEY={encoded}" in written, "PEM stored base64, not verbatim"

    assert set(reported) == {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"}
    assert all(isinstance(v, int) for v in reported.values())
    out = capsys.readouterr()
    for surface in (repr(reported), out.out, out.err):
        assert SENTINEL_SECRET not in surface
        assert "SENTINEL-PEM-BODY" not in surface


def test_write_credentials_refuses_to_clobber_an_existing_file(tmp_path):
    """A rerun must never silently destroy a working .env."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_APP_ID=1\n", encoding="utf-8")
    creds = cga.AppCredentials(1, SENTINEL_PEM, SENTINEL_SECRET)
    with pytest.raises(SystemExit):
        cga.write_credentials(creds, env)
    assert env.read_text(encoding="utf-8") == "GITHUB_APP_ID=1\n"
    cga.write_credentials(creds, env, overwrite=True)  # explicit opt-in works


def test_no_default_path_points_at_the_repo_root():
    """write_credentials must require an explicit path, so no test or misfire
    can land on the real .env."""
    import inspect

    signature = inspect.signature(cga.write_credentials)
    assert signature.parameters["path"].default is inspect.Parameter.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_create_github_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts.create_github_app'`.

- [ ] **Step 3: Write the script**

```python
"""Create this project's GitHub App in one browser round-trip.

    uv run python -m bot.scripts.create_github_app --base-url https://your-host

HUMAN-RUN ONLY. It writes real credentials to .env. An agent must never
invoke it -- same rule, and same reason, as scripts/encode_credential.py's
own docstring: doing so would put secret-derived bytes into a tool result.

GitHub's App Manifest flow: a browser form POSTs a manifest to
github.com/settings/apps/new, the operator approves, GitHub redirects back
with a one-time code, and POST /app-manifests/{code}/conversions returns the
App ID, PEM, and webhook secret together. That replaces creating the App by
hand, generating a private key by hand, and base64-encoding it by hand.
SETUP.md section 1 records that this project's own App was made this way.

The webhook URL is a placeholder at creation time -- the tunnel or Render URL
does not exist yet. scripts/deploy.py's github-app check corrects it later
("points here -- set only if wrong"), which is why an ephemeral tunnel URL is
fine (design spec 2026-08-18 section 4c).
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import NamedTuple

import httpx

_CONVERSIONS_API = "https://api.github.com/app-manifests/{code}/conversions"
_NEW_APP_URL = "https://github.com/settings/apps/new"
_HTTP_TIMEOUT = 15.0
_CALLBACK_PORT = 8765
# Long enough for a human to read GitHub's approval screen, short enough that a
# closed browser tab does not hang the terminal forever.
_CALLBACK_TIMEOUT = 300.0

# Exactly what the bot needs, and nothing more. pull_requests+issues write
# because a PR review comment is an issue comment on GitHub's API; contents
# read to fetch the diff; metadata read is mandatory for any App.
MANIFEST_PERMISSIONS = {
    "pull_requests": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
}
MANIFEST_EVENTS = ("pull_request",)


class AppCredentials(NamedTuple):
    app_id: int
    private_key_pem: str
    webhook_secret: str


def build_manifest(app_name: str, base_url: str, redirect_url: str) -> dict:
    """The manifest GitHub is asked to create an App from.

    public=False is a security boundary, not a preference: leaving
    GITHUB_TARGET_REPO unset makes the bot act on every repo its installation
    covers, which is only safe because a private App can only be installed by
    accounts the owner chooses. A public App would let any third party
    self-install and have their events accepted (SETUP.md section 1).
    """
    return {
        "name": app_name,
        "url": base_url,
        "public": False,
        "hook_attributes": {"url": f"{base_url.rstrip('/')}/webhook", "active": True},
        "redirect_url": redirect_url,
        "default_events": list(MANIFEST_EVENTS),
        "default_permissions": dict(MANIFEST_PERMISSIONS),
    }


def exchange_code(code: str) -> AppCredentials:
    """Trade the one-time redirect code for the App's credentials.

    A failure is reported by STATUS ONLY. GitHub's error bodies can echo parts
    of the submitted manifest, and this response carries the PEM and webhook
    secret -- so nothing from it is ever printed (CLAUDE.md).
    """
    response = httpx.post(
        _CONVERSIONS_API.format(code=code),
        headers={"Accept": "application/vnd.github+json"},
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"GitHub refused the manifest conversion (HTTP {response.status_code}). "
            "The code is single-use and expires quickly -- re-run to start over."
        )
    body = response.json()
    return AppCredentials(
        app_id=int(body["id"]),
        private_key_pem=body["pem"],
        webhook_secret=body["webhook_secret"],
    )


def write_credentials(
    creds: AppCredentials, path: Path, overwrite: bool = False
) -> dict[str, int]:
    """Write the three values to `path`; return name -> length.

    `path` is REQUIRED with no default: a default of Path(".env") would let a
    test or a mis-run clobber a real credential file. Refuses an existing file
    unless overwrite=True for the same reason.

    Returns lengths, never values -- the caller prints this, mirroring
    scripts/deploy.py::sync_env()'s `pushed {key} (len {n})` convention.
    """
    if path.exists() and not overwrite:
        raise SystemExit(
            f"{path} already exists; refusing to overwrite it. Move it aside "
            "first, or pass --overwrite if you are sure."
        )
    encoded_pem = base64.b64encode(creds.private_key_pem.encode()).decode()
    values = {
        "GITHUB_APP_ID": str(creds.app_id),
        "GITHUB_APP_PRIVATE_KEY": encoded_pem,
        "GITHUB_WEBHOOK_SECRET": creds.webhook_secret,
    }
    body = "".join(f"{name}={value}\n" for name, value in values.items())
    path.write_text(body, encoding="utf-8", newline="\n")
    return {name: len(value) for name, value in values.items()}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves the auto-submitting manifest form, then catches the redirect."""

    manifest_json = ""
    state = ""
    code: str | None = None
    received = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 -- stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/callback":
            if params.get("state", [""])[0] != type(self).state:
                self._reply(400, "<h1>State mismatch -- start over.</h1>")
                type(self).received.set()  # unblock main(); code stays None
                return
            type(self).code = params.get("code", [""])[0]
            self._reply(200, "<h1>Done. Return to your terminal.</h1>")
            type(self).received.set()
            return
        self._reply(200, self._form())

    def log_message(self, *args) -> None:
        """Silence the default request logging: the query string carries the
        one-time code, and stdout is not where that belongs."""

    def _form(self) -> str:
        return (
            "<!doctype html><body onload='document.forms[0].submit()'>"
            f"<form action='{_NEW_APP_URL}?state={type(self).state}' method='post'>"
            f"<input type='hidden' name='manifest' value='{type(self).manifest_json}'>"
            "<button type='submit'>Create the GitHub App</button></form></body>"
        )

    def _reply(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create this project's GitHub App")
    parser.add_argument("--name", default="pr-review-engine",
                        help="App name (must be globally unique on GitHub)")
    parser.add_argument("--base-url", default="https://example.invalid",
                        help="placeholder public URL; scripts/deploy.py corrects it later")
    parser.add_argument("--env-path", default=".env", help="where to write the credentials")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing env file")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    redirect = f"http://localhost:{_CALLBACK_PORT}/callback"
    manifest = build_manifest(args.name, args.base_url, redirect)
    _CallbackHandler.manifest_json = json.dumps(manifest).replace("'", "&#39;")
    _CallbackHandler.state = secrets.token_urlsafe(16)
    _CallbackHandler.code = None
    _CallbackHandler.received.clear()

    server = http.server.HTTPServer(("localhost", _CALLBACK_PORT), _CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = f"http://localhost:{_CALLBACK_PORT}/"
    print(f"opening {start} -- approve the App in your browser")
    webbrowser.open(start)
    try:
        # The handler sets this once GitHub's redirect arrives. A single Event
        # (created before the wait, not inside the loop) is what makes this a
        # real block rather than a spin.
        if not _CallbackHandler.received.wait(timeout=_CALLBACK_TIMEOUT):
            print(
                f"timed out after {_CALLBACK_TIMEOUT:.0f}s waiting for GitHub's redirect",
                file=sys.stderr,
            )
            return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 1
    finally:
        server.shutdown()

    if not _CallbackHandler.code:
        print("no code in GitHub's redirect", file=sys.stderr)
        return 1

    creds = exchange_code(_CallbackHandler.code)
    lengths = write_credentials(creds, Path(args.env_path), overwrite=args.overwrite)
    for name, length in lengths.items():
        print(f"wrote {name} (len {length})")
    print("next: uv run python -m bot.scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_create_github_app.py -v`
Expected: all PASS. **Do not run `main()`** — it would create a real GitHub App.

- [ ] **Step 5: Commit**

```bash
git add scripts/create_github_app.py tests/test_create_github_app.py
git commit -m "feat: create the GitHub App via manifest flow in one browser round-trip"
```

---

### Task 6: `scripts/init_env.py` — guided env scaffolding

Spec §4e. **HUMAN-RUN ONLY**, same rule as Task 5.

**Design point that keeps it safe:** to be idempotent it must know which values are *already set*, but it never needs to know what they are. So it reads **key names only** from an existing file, using the `^[A-Z_0-9]+=` idiom `CLAUDE.md` prescribes and `tests/test_config.py:19`'s `_key_names` already demonstrates. It therefore never holds an existing secret in memory at all.

**Files:**
- Create: `scripts/init_env.py`
- Test: `tests/test_init_env.py`

**Interfaces:**
- Consumes: `app.config.OPERATIONAL_KEYS`.
- Produces: `key_names(path) -> frozenset[str]`, `example_keys(path) -> tuple[str, ...]`, `split_keys(keys) -> tuple[tuple[str, ...], tuple[str, ...]]` (secret, operational), `render_env(values) -> str`, `write_env(text, path, overwrite=False) -> None`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_init_env.py
"""init_env never reads an existing VALUE -- only which keys are present. Every
test writes to tmp_path; none touches the repo's real .env."""
from __future__ import annotations

import pytest

from app.config import OPERATIONAL_KEYS
from scripts import init_env

SENTINEL = "SENTINEL-4e8b03d5f7a91c62-EXISTING"


def test_key_names_returns_names_only_never_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"GROQ_API_KEY={SENTINEL}\n"
        f"DATABASE_URL=postgresql://u:{SENTINEL}@h:5432/db?x=1\n"
        "# a comment\n"
        "\n"
        f"export GITHUB_WEBHOOK_SECRET={SENTINEL}\n",
        encoding="utf-8",
    )
    names = init_env.key_names(env)
    assert names == frozenset({"GROQ_API_KEY", "DATABASE_URL", "GITHUB_WEBHOOK_SECRET"})
    for name in names:
        assert SENTINEL not in name


def test_key_names_survives_crlf_and_a_value_containing_equals(tmp_path):
    """A Windows-authored .env and a DATABASE_URL both break a naive parser."""
    env = tmp_path / ".env"
    env.write_bytes(f"DATABASE_URL=postgres://a:b=c@h/db\r\nGROQ_API_KEY={SENTINEL}\r\n".encode())
    assert init_env.key_names(env) == frozenset({"DATABASE_URL", "GROQ_API_KEY"})


def test_key_names_on_a_missing_file_is_empty(tmp_path):
    assert init_env.key_names(tmp_path / "nope") == frozenset()


def test_example_keys_reads_the_committed_examples():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    secrets_keys = init_env.example_keys(root / ".env.example")
    config_keys = init_env.example_keys(root / ".env.config.example")
    assert "GITHUB_WEBHOOK_SECRET" in secrets_keys
    assert "LLM_PROVIDER" in config_keys


def test_split_keys_routes_by_operational_keys():
    secret, operational = init_env.split_keys(("GROQ_API_KEY", "LLM_PROVIDER"))
    assert secret == ("GROQ_API_KEY",)
    assert operational == ("LLM_PROVIDER",)
    assert "LLM_PROVIDER" in OPERATIONAL_KEYS


def test_render_env_emits_one_key_per_line_with_lf(tmp_path):
    text = init_env.render_env({"A": "1", "B": "2"})
    assert text == "A=1\nB=2\n"
    assert "\r" not in text


def test_write_env_refuses_to_clobber_without_an_explicit_opt_in(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEEP=1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        init_env.write_env("NEW=2\n", path)
    assert path.read_text(encoding="utf-8") == "KEEP=1\n"
    init_env.write_env("NEW=2\n", path, overwrite=True)
    assert path.read_text(encoding="utf-8") == "NEW=2\n"


def test_write_env_requires_an_explicit_path():
    import inspect

    assert inspect.signature(init_env.write_env).parameters["path"].default \
        is inspect.Parameter.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_init_env.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts.init_env'`.

- [ ] **Step 3: Write the script**

```python
"""Interactively scaffold .env and .env.config from the committed examples.

    uv run python -m bot.scripts.init_env

HUMAN-RUN ONLY -- it prompts for and writes real credentials. An agent must
never invoke it (same rule as scripts/encode_credential.py).

It is idempotent, and does that WITHOUT ever reading an existing value: to
decide whether a key is already set it reads key NAMES only, via the
'^[A-Z_0-9]+=' idiom CLAUDE.md prescribes (see tests/test_config.py's
_key_names for the same shape). So re-running is safe, and no existing secret
is ever held in memory by this script.

Which file a key belongs in comes from app/config.py's OPERATIONAL_KEYS:
listed = operational (.env.config), everything else = secret (.env). That is
the same split tests/test_config.py enforces, so this cannot drift from it.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

from app.config import OPERATIONAL_KEYS

# Captures the NAME and discards the value -- the whole point. A value may
# contain '=', spaces, quotes, or '#' and none of it can reach the result.
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z_0-9]+)=")

# Keys whose value is a file's base64 form rather than something typed.
_FILE_ENCODED_KEYS = frozenset({"GITHUB_APP_PRIVATE_KEY", "GCP_SERVICE_ACCOUNT_KEY"})


def key_names(path: Path) -> frozenset[str]:
    """The env-var NAMES defined in `path`; empty if it does not exist.

    Names only, never values. Handles CRLF, 'export ' prefixes, comments,
    blank lines, and values containing '=' -- because a regex that returns
    whole lines is exactly how a secret leaks (CLAUDE.md).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        match.group(1) for line in text.splitlines() if (match := _KEY_LINE.match(line))
    )


def example_keys(path: Path) -> tuple[str, ...]:
    """Every key an example file declares, in file order (commented-out
    optional settings included, so nothing silently goes unasked)."""
    text = path.read_text(encoding="utf-8")
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip().removeprefix("# ").removeprefix("#")
        match = _KEY_LINE.match(stripped)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return tuple(names)


def split_keys(keys: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(secret keys, operational keys). Secret-by-default: anything not on
    OPERATIONAL_KEYS is treated as a credential."""
    secret = tuple(k for k in keys if k not in OPERATIONAL_KEYS)
    operational = tuple(k for k in keys if k in OPERATIONAL_KEYS)
    return secret, operational


def render_env(values: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in values.items())


def write_env(text: str, path: Path, overwrite: bool = False) -> None:
    """`path` is REQUIRED with no default, and an existing file is refused
    unless overwrite=True -- so neither a test nor a mis-run can destroy a
    working credential file."""
    if path.exists() and not overwrite:
        raise SystemExit(f"{path} already exists; re-run with --overwrite to replace it.")
    path.write_text(text, encoding="utf-8", newline="\n")


def _ask(name: str, already_set: bool) -> str | None:
    """Prompt for one key. None means 'leave it out of the written file'."""
    if already_set:
        keep = input(f"{name} is already set -- keep it? [Y/n] ").strip().lower()
        if keep in ("", "y", "yes"):
            return None
    if name in _FILE_ENCODED_KEYS:
        location = input(f"{name}: path to the key file (blank to skip): ").strip()
        if not location:
            return None
        try:
            return base64.b64encode(Path(location).read_bytes()).decode()
        except OSError as exc:
            print(f"could not read that file ({type(exc).__name__}); skipping", file=sys.stderr)
            return None
    value = input(f"{name}: ").strip()
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold .env and .env.config")
    parser.add_argument("--overwrite", action="store_true", help="replace existing files")
    parser.add_argument("--root", default=".", help="where the example files live")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root)
    secret_path, config_path = root / ".env", root / ".env.config"
    declared = example_keys(root / ".env.example") + example_keys(root / ".env.config.example")
    secret_keys, operational_keys = split_keys(declared)
    existing = key_names(secret_path) | key_names(config_path)

    print("Values are written straight to .env / .env.config and never echoed back.\n")
    answers: dict[str, str] = {}
    for name in (*secret_keys, *operational_keys):
        value = _ask(name, name in existing)
        if value is not None:
            answers[name] = value

    for path, keys in ((secret_path, secret_keys), (config_path, operational_keys)):
        chosen = {k: v for k, v in answers.items() if k in keys}
        if not chosen:
            continue
        write_env(render_env(chosen), path, overwrite=args.overwrite)
        print(f"wrote {path} ({len(chosen)} keys)")

    print("next: uv run python -m bot.scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_init_env.py -v`
Expected: all PASS. **Do not run `main()` in this repo** — it would prompt for and write real credentials over the developer's own `.env`.

- [ ] **Step 5: Commit**

```bash
git add scripts/init_env.py tests/test_init_env.py
git commit -m "feat: add guided .env/.env.config scaffolding that never reads a value"
```

---

### Task 7: `.claude/commands/setup.md`

Spec §4f. Mirrors `.claude/commands/deploy.md`'s contract exactly: no logic of its own.

**Files:**
- Create: `.claude/commands/setup.md`
- Test: `tests/test_setup_command.py`

**Interfaces:**
- Consumes: `doctor --json`'s output shape (Task 4).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_setup_command.py
"""The command must carry the credential handoff rule. CLAUDE.md forbids an
agent from opening .env at all, so a slash command that walks a human through
setup has to hand off the moment a real secret is involved -- otherwise the
first thing it does is break the project's highest-priority rule."""
from __future__ import annotations

from pathlib import Path

_COMMAND = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "setup.md"


def test_the_command_exists_with_frontmatter():
    text = _COMMAND.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "description:" in text


def test_it_drives_the_doctor_cli_and_holds_no_logic_of_its_own():
    text = _COMMAND.read_text(encoding="utf-8")
    assert "bot.scripts.doctor" in text
    assert "--json" in text
    assert "works identically for people who do not use Claude Code" in text


def test_it_hands_off_every_credential_writing_tool():
    """Both writers must be handed to the human, never run by the agent."""
    text = _COMMAND.read_text(encoding="utf-8")
    for tool in ("bot.scripts.init_env", "bot.scripts.create_github_app"):
        assert tool in text, f"{tool} must be named"
    assert "! uv run" in text, "the `!` prefix is how the user runs it themselves"
    assert ".env" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_setup_command.py -v`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write the command**

```markdown
---
description: Walk through setting up this project from a fresh clone to a first review
---

Run the setup doctor and report where things stand:

```bash
uv run python -m bot.scripts.doctor --json
```

Read the JSON. It carries `track` (`local` or `hosted`), `step` (the current
`number`, `title`, and exact `command`), and one `checks` entry per row with a
`status` of `PASS`, `WARN`, `FAIL`, or `SKIPPED`.

Then, in a loop until `step` is `null`:

1. Show the user the current step number, its title, and the exact `command`.
2. Explain the first `FAIL` row in plain language, using its own `detail` —
   which already names what is wrong and what to do.
3. Treat `SKIPPED` as normal, not as a problem. A skipped row means its
   precondition does not exist yet (no Render service, no `RENDER_API_KEY`),
   which is the expected state early in setup.
4. Run the next command **only if it neither writes a credential nor opens a
   browser** — see the handoff rule below. Otherwise hand it to the user.
5. Re-run the doctor and repeat.

## Credential handoff — not optional

`CLAUDE.md` forbids you from opening `.env` for any reason, including a
single-line read. Two of this project's setup tools write real credentials to
it:

- `uv run python -m bot.scripts.init_env`
- `uv run python -m bot.scripts.create_github_app`

**Never run either of these yourself.** Ask the user to run it in this session
with the `!` prefix, so its output lands in the conversation without you
invoking it:

> Run this yourself: `! uv run python -m bot.scripts.create_github_app`

Both print names and lengths only, never values, so their output is safe to
read and reason about afterwards. The doctor is safe for you to run as often as
you like — it is read-only, and reports names, lengths, and booleans only.

If the user asks you to check or fix a value inside `.env`, decline and ask
them to do it themselves. That is the rule working, not an obstacle to route
around.

## For reference

Full explanations of each row live in the setup guide; `scripts/deploy.py`
covers the deployment-verification rows specifically. This command holds no
setup logic of its own — `scripts/doctor.py` is the tool, and it works
identically for people who do not use Claude Code.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_setup_command.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/setup.md tests/test_setup_command.py
git commit -m "feat: add /setup, a Claude Code wrapper around the setup doctor"
```

---

### Task 8: Full-suite verification and stage close-out

Not a code change — the gate before Stage 3.

- [ ] **Step 1: Run the whole suite clean**

Run: `uv run ruff check . && uv run pytest -q`
Expected: zero failures. Baseline entering the stage was **675 passing**; report the new count.

- [ ] **Step 2: Run the doctor on both tracks**

Run:
```bash
uv run python -m bot.scripts.doctor --track local
uv run python -m bot.scripts.doctor --track hosted
uv run python -m bot.scripts.doctor --json
```
Expected: three clean reports, exit 0 each. The `--json` output must parse. Confirm no output line contains anything resembling a credential value, and paste all three into your report.

- [ ] **Step 3: Confirm the read-only guarantee held**

Run: `git status --porcelain`
Expected: empty. Running the doctor three times must not have modified a single file. If anything changed, doctor is writing when it must not — stop and report.

- [ ] **Step 4: Report completion**

Summarise: tasks completed, test count before and after, the doctor's actual output on this checkout, and any deviation from this plan with its reason.

---

## Out of Scope for Stage 2

- **Stage 3** — the `guide/` MkDocs site, README's reduction, the `SETUP.md` split, the OS-idiom doc fixes (`base64 -w0` → `encode_credential.py`, `curl` → `--health-only`), `scripts/gen_docs.py`, and both CI jobs (spec §3, §5, §7).
- **No edits to `README.md` or `SETUP.md`.** They describe the manual path these tools replace, and get rewritten wholesale in Stage 3 rather than patched twice.
- **No tunnel process management** (spec §11). The doctor probes for `cloudflared` and detects a working tunnel; it never starts, supervises, or tears one down.
- **No named-tunnel / stable-hostname setup, and no polling fallback** for webhook delivery (spec §11).
- **The optional no-tunnel verification milestone** (spec §4a-i's
  `scripts/manual_verify_step3.py` step, which proves App auth, diff fetch, and
  comment upsert with no public URL) is *documentation*, not tooling. It belongs
  to Stage 3's `guide/setup/01`; doctor does not surface it, since it is an
  optional confidence check rather than a setup step that can be satisfied.
- **No changes to `app/`.** If a task appears to need one, stop and report rather than widening scope.
