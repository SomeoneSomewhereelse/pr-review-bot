# Verbatim-Only Credential Convention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse `GITHUB_APP_PRIVATE_KEY_B64`/`_PATH` and `GCP_SERVICE_ACCOUNT_KEY_B64[_n]`/`_PATH[_n]` to one verbatim-only var per credential (`GITHUB_APP_PRIVATE_KEY`, `GCP_SERVICE_ACCOUNT_KEY[_n]`), deleting the local-file-path fallback, and close `check_config()`'s DB-model-override pricing blind spot.

**Architecture:** Four independent-ish changes landing in sequence: (1) a new human-run `scripts/encode_credential.py` helper, (2) the credential rename itself across `app/config.py`, `app/providers/registry.py`, `app/providers/vertex_credentials.py`, `app/github_app.py`, `scripts/deploy.py`, plus every test and doc that names the old vars, (3) a migration-checklist test mirroring the operational-config-split precedent, (4) teaching `scripts/deploy.py`'s `_unpriced_models()`/`check_config()` to resolve an active DB model override before checking pricing.

**Tech Stack:** Python 3.12, pydantic-settings, pytest, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-16-credential-convention-design.md`

## Global Constraints

- An agent must never open, read, or edit `.env` for any reason (CLAUDE.md's Secret handling section) — every step below that touches `.env` content is either a test reading only key NAMES (`_key_names()`'s `^[A-Z_0-9]+=` pattern, values discarded) or explicit user instruction, never a direct file open.
- No secret value may ever be printed, logged, or appear in a commit, test assertion string, or tool output — test fixtures use obviously-fake values (`"aGVsbG8="`, throwaway generated RSA keys), never real credential material.
- `scripts/encode_credential.py` is human-run only — its own docstring must say so explicitly; an agent must never invoke it against a real credential file.
- Every renamed/deleted Settings field, env var, and function must be updated everywhere it's referenced — a stray old-name reference left in code or a test is a leftover, not an acceptable partial migration.
- No `--force` bypass on any new refusal in this plan (per the design's §6 non-goal).

---

### Task 1: `scripts/encode_credential.py`

**Files:**
- Create: `scripts/encode_credential.py`
- Test: `tests/test_encode_credential.py`

**Interfaces:**
- Produces: `encode_credential.main(argv: list[str] | None = None) -> int` — reads a file path, prints its base64 form to stdout, returns an exit code (`0` ok, `2` usage/read error). No other task calls this; it's a standalone human-run CLI.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_encode_credential.py`:

```python
"""scripts/encode_credential.py -- prints a local file's base64 form.

Human-run only: never invoke this against a real credential file from an
agent session. Printing secret-derived bytes into a tool result is exactly
the failure mode CLAUDE.md's Secret handling section exists to prevent --
these tests use throwaway, obviously-fake bytes, never real material.
"""
from __future__ import annotations

import base64

from scripts import encode_credential


def test_prints_the_base64_form_of_the_file(tmp_path, capsys):
    payload = b"hello world, this is a fake credential\n"
    path = tmp_path / "fake-key.pem"
    path.write_bytes(payload)

    exit_code = encode_credential.main([str(path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.strip() == base64.b64encode(payload).decode()


def test_prints_nothing_else(tmp_path, capsys):
    """Exactly one line of output -- the base64 form, nothing else -- so a
    caller can pipe or paste it directly with no cleanup."""
    path = tmp_path / "fake-key.json"
    path.write_bytes(b'{"type": "service_account"}')

    encode_credential.main([str(path)])

    out = capsys.readouterr().out
    assert out.count("\n") == 1


def test_returns_two_and_names_the_path_on_a_missing_file(tmp_path, capsys):
    missing = tmp_path / "nope.pem"

    exit_code = encode_credential.main([str(missing)])

    assert exit_code == 2
    assert "nope.pem" in capsys.readouterr().err


def test_requires_exactly_one_argument(capsys):
    assert encode_credential.main([]) == 2
    assert encode_credential.main(["a", "b"]) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_encode_credential.py -v`
Expected: every test errors with `ModuleNotFoundError: No module named 'bot.scripts.encode_credential'` (or `ImportError`).

- [ ] **Step 3: Write the implementation**

Create `scripts/encode_credential.py`:

```python
"""Prints a local file's base64 form -- for pasting into GITHUB_APP_PRIVATE_KEY
or GCP_SERVICE_ACCOUNT_KEY[_n] in .env.

    uv run python -m bot.scripts.encode_credential path/to/file.pem

Human-run only. An agent must never invoke this against a real credential
file: doing so would print secret-derived bytes into its own tool output --
exactly the failure mode CLAUDE.md's Secret handling section exists to
prevent. This script's existence does not change who is allowed to run it
against real material; only the user, in their own terminal.

Works identically for a PEM or a JSON key -- base64 doesn't care about
content shape. Equivalent to `base64 -w0 < file`, wrapped for convenience.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: encode_credential.py <path>", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"could not read {path}: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(base64.b64encode(data).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_encode_credential.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/encode_credential.py tests/test_encode_credential.py
git commit -m "feat: add scripts/encode_credential.py, a human-run base64 helper for credential files"
```

---

### Task 2: Rename credential vars; delete the local-file-path fallback

**Files:**
- Modify: `app/config.py`, `app/providers/registry.py`, `app/providers/vertex_credentials.py`, `app/github_app.py`, `scripts/deploy.py`, `scripts/manual_verify_vertex.py`
- Modify (docs): `.env.example`, `README.md`, `SETUP.md`, `CLAUDE.md`
- Test: `tests/test_config.py`, `tests/test_provider_registry.py`, `tests/test_vertex_credentials.py`, `tests/test_github_app.py`, `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `Settings.github_app_private_key: str`, `Settings.gcp_service_account_key: str` (both replacing their `_b64`-suffixed predecessors; the `_path` fields are deleted, not renamed). `registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")`. Task 4 does not depend on any of these names (it only touches model-var resolution), but shares `scripts/deploy.py`'s `check_config()` function body, so it must land after this task to avoid diff conflicts.

This task is one atomic rename — a half-completed state (e.g. `Settings` renamed but `github_app.py` still reading the old field) leaves the whole test suite broken, so all production files are edited together, then all tests, then all docs, with a full-suite run as the gate between each group.

- [ ] **Step 1: Apply every production-code edit**

`app/config.py` — replace the credential block:

```python
    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key_path: str = "./github-app-private-key.pem"
    github_app_private_key_b64: str = ""
    github_webhook_secret: str = ""
```

with:

```python
    github_app_id: int = 0
    github_app_installation_id: int = 0
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
```

and replace:

```python
    # --- Vertex AI (LLM_PROVIDER=vertex). Unlike gemini/groq, the credential
    # is a GCP service-account identity rather than an API-key string:
    # GCP_SERVICE_ACCOUNT_KEY_B64 (hosted) -> a local key file -> implicit ADC.
    # See app/providers/vertex_credentials.py for the resolution order.
    # An OPTIONAL override: unset means "use the project_id embedded in the
    # resolved service-account key", so an operator handed nothing but a JSON
    # key needs no separate project lookup.
    gcp_project: str = ""
    # Which Vertex regional endpoint to call -- not an account property, so the
    # default needs no lookup either.
    gcp_location: str = "us-central1"
    gcp_service_account_key_b64: str = ""
    gcp_service_account_key_path: str = "./gcp-service-account-key.json"
```

with:

```python
    # --- Vertex AI (LLM_PROVIDER=vertex). Unlike gemini/groq, the credential
    # is a GCP service-account identity rather than an API-key string:
    # GCP_SERVICE_ACCOUNT_KEY (hosted, always base64) -> implicit ADC. See
    # app/providers/vertex_credentials.py for the resolution order.
    # An OPTIONAL override: unset means "use the project_id embedded in the
    # resolved service-account key", so an operator handed nothing but a JSON
    # key needs no separate project lookup.
    gcp_project: str = ""
    # Which Vertex regional endpoint to call -- not an account property, so the
    # default needs no lookup either.
    gcp_location: str = "us-central1"
    gcp_service_account_key: str = ""
```

`app/providers/registry.py` — replace:

```python
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # app/providers/vertex_credentials.py layers the local-file and
    # implicit-ADC fallbacks on top of what this entry resolves.
    #
    # VERTEX_MODEL, not LLM_MODEL: gemini and vertex are the same SDK but
    # different model catalogs -- gemini-flash-latest does not exist as a
    # Vertex publisher model (404). Sharing one var made a DB provider flip
    # between them guaranteed-broken. Completes the split whose reasoning
    # app/config.py already records for GROQ_MODEL.
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL"),
```

with:

```python
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # app/providers/vertex_credentials.py layers the implicit-ADC fallback on
    # top of what this entry resolves.
    #
    # VERTEX_MODEL, not LLM_MODEL: gemini and vertex are the same SDK but
    # different model catalogs -- gemini-flash-latest does not exist as a
    # Vertex publisher model (404). Sharing one var made a DB provider flip
    # between them guaranteed-broken. Completes the split whose reasoning
    # app/config.py already records for GROQ_MODEL.
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
```

`app/providers/vertex_credentials.py` — replace the entire file:

```python
"""Resolves the Vertex service-account credential: GCP_SERVICE_ACCOUNT_KEY
(env, index-aware) -> None, meaning "let google-auth discover implicit ADC".

Separate from credentials.py on purpose: that module knows exactly one
credential shape ("one env var, one string") and stays that way for the two
providers that need nothing more. The JSON parsing lives here instead of
complicating it for them.

One index, two real meanings depending on environment. On Render, where the
numbered GCP_SERVICE_ACCOUNT_KEY_{n} slots are actually provisioned, the
index selects among env-var blobs. Locally, exporting the same numbered
slots lets a developer test against several different service accounts (a
quota-exhausted one vs. a healthy one) without touching Render or Supabase
at all.

A malformed value raises rather than falling through to implicit ADC: a
corrupt env var must surface as a failure, not silently run the review
against a different account than the operator selected.
"""

from __future__ import annotations

import base64
import json

from app.providers import credentials


def resolve_service_account_info(index: int) -> dict | None:
    """The parsed service-account key for `index`, or None for implicit ADC.

    None is NOT an error here (unlike an empty gemini/groq credential): it is
    the signal to pass no explicit credentials to genai.Client, which is what
    makes google-auth discover `gcloud auth application-default login`'s local
    ADC file on its own.
    """
    _, b64 = credentials.resolve("vertex", index)
    if not b64:
        return None
    data = json.loads(base64.b64decode(b64, validate=True).decode())
    if not isinstance(data, dict):
        raise ValueError(
            f"GCP service-account credential at index {index} is not a JSON object"
        )
    return data
```

`app/github_app.py` — remove the now-unused `from pathlib import Path` import (line 13; nothing else in the file uses `Path`), and replace:

```python
def _read_private_key() -> str:
    """Prefer the base64 env var (host-portable); fall back to the PEM file for
    local dev. Never logged."""
    b64 = settings.github_app_private_key_b64
    if b64:
        import base64

        return base64.b64decode(b64).decode()
    key_path = Path(settings.github_app_private_key_path)
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    return key_path.read_text()
```

with:

```python
def _read_private_key() -> str:
    """Decode the base64-encoded App private key. Never logged."""
    return base64.b64decode(settings.github_app_private_key).decode()
```

and add `import base64` alongside the file's other top-level imports (after `from __future__ import annotations`):

```python
from __future__ import annotations

import base64

from github import Auth, Github, GithubException
```

`scripts/deploy.py`:

Remove the now-unused `import base64` (line 20) and `from pathlib import Path` (line 25) — both were used only inside `_private_key_b64()`, which this step deletes. Verify first with `grep -n "Path(\|base64\." scripts/deploy.py` that no other line in the file uses either name (as of this plan being written, none do).

Delete the `_private_key_b64()` function entirely:

```python
def _private_key_b64() -> tuple[str, str]:
    """The PEM in the base64 form Render needs, plus a problem string
    ("" when usable).

    Reads rather than stats: an existing-but-unreadable PEM must not report as
    available, because check_config would pass while _wanted_env raised on the
    same file. Returning the problem instead of raising keeps the CLI's exit
    contract intact -- a config problem is a FAIL row, not a traceback.
    """
    if settings.github_app_private_key_b64:
        return settings.github_app_private_key_b64, ""
    path = Path(settings.github_app_private_key_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return base64.b64encode(path.read_bytes()).decode(), ""
    except FileNotFoundError:
        return "", "GITHUB_APP_PRIVATE_KEY_B64 or _PATH"
    except OSError as exc:
        return "", f"unreadable PEM {path} ({type(exc).__name__})"
```

In `check_config()`, replace:

```python
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    key_b64, key_problem = _private_key_b64()
    if key_problem and key_problem.startswith("unreadable"):
        problems.append(key_problem)
    elif key_problem:
        missing.append(key_problem)
    if not settings.github_webhook_secret:
```

with:

```python
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    if not settings.github_app_private_key:
        missing.append("GITHUB_APP_PRIVATE_KEY")
    if not settings.github_webhook_secret:
```

In `_ALWAYS_SYNCED`, replace `"GITHUB_APP_PRIVATE_KEY_B64"` with `"GITHUB_APP_PRIVATE_KEY"`.

In `_wanted_env()`, replace:

```python
    pem_b64, _ = _private_key_b64()
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY_B64": pem_b64,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
```

with:

```python
    wanted = {
        "DATABASE_URL": settings.database_url,
        "GITHUB_APP_ID": str(settings.github_app_id or ""),
        "GITHUB_APP_PRIVATE_KEY": settings.github_app_private_key,
        "GITHUB_TARGET_REPO": settings.github_target_repo,
```

`scripts/manual_verify_vertex.py` — replace the module docstring's credential-chain sentence:

```python
Not part of the pytest suite (CI never runs this) -- it depends on a real,
live call to Vertex AI using whatever credential
app/providers/vertex_credentials.py resolves: GCP_SERVICE_ACCOUNT_KEY_B64,
then a local key file, then implicit ADC (`gcloud auth application-default
login`).
```

with:

```python
Not part of the pytest suite (CI never runs this) -- it depends on a real,
live call to Vertex AI using whatever credential
app/providers/vertex_credentials.py resolves: GCP_SERVICE_ACCOUNT_KEY, then
implicit ADC (`gcloud auth application-default login`).
```

and replace:

```python
Resolves key-index slot 0 only: the DB key-index override is a dispatcher-
runtime concern (it is refreshed into a process-local cache per claimed
ticket), and a one-shot CLI has no such cache to read. To verify a different
service account locally, point GCP_SERVICE_ACCOUNT_KEY_PATH at it.
```

with:

```python
Resolves key-index slot 0 only: the DB key-index override is a dispatcher-
runtime concern (it is refreshed into a process-local cache per claimed
ticket), and a one-shot CLI has no such cache to read. To verify a different
service account locally, set GCP_SERVICE_ACCOUNT_KEY to its base64 form
(scripts/encode_credential.py).
```

and replace:

```python
        print(
            "\nno project to call with: set GCP_PROJECT, or provide a service-account "
            "key via GCP_SERVICE_ACCOUNT_KEY_B64 / GCP_SERVICE_ACCOUNT_KEY_PATH",
            file=sys.stderr,
        )
```

with:

```python
        print(
            "\nno project to call with: set GCP_PROJECT, or provide a service-account "
            "key via GCP_SERVICE_ACCOUNT_KEY",
            file=sys.stderr,
        )
```

- [ ] **Step 2: Run the full test suite to confirm it fails for the expected reason**

Run: `uv run pytest tests/ -x -q 2>&1 | head -60`
Expected: failures naming `github_app_private_key_b64`, `github_app_private_key_path`, `gcp_service_account_key_b64`, `gcp_service_account_key_path`, `GITHUB_APP_PRIVATE_KEY_B64`, or `GCP_SERVICE_ACCOUNT_KEY_B64` as unknown attributes/mismatched values — this is expected; the tests haven't been updated yet. If a failure names anything else (an import error unrelated to these names, a syntax error), stop and fix the production edit before proceeding.

- [ ] **Step 3: Update every test file**

`tests/test_config.py` — replace:

```python
def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in (
        "GCP_PROJECT",
        "GCP_LOCATION",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.gcp_project == ""
    assert settings.gcp_location == "us-central1"
    assert settings.gcp_service_account_key_b64 == ""
    assert settings.gcp_service_account_key_path == "./gcp-service-account-key.json"
```

with:

```python
def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in ("GCP_PROJECT", "GCP_LOCATION", "GCP_SERVICE_ACCOUNT_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.gcp_project == ""
    assert settings.gcp_location == "us-central1"
    assert settings.gcp_service_account_key == ""
```

`tests/test_provider_registry.py` — replace:

```python
def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "LLM_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL")
```

with:

```python
def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "LLM_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")
```

and replace:

```python
def test_slot_env_name_is_the_single_naming_seam():
    from app.providers import registry

    assert registry.slot_env_name("groq", 0) == "GROQ_API_KEY"
    assert registry.slot_env_name("groq", 2) == "GROQ_API_KEY_2"
    assert registry.slot_env_name("vertex", 1) == "GCP_SERVICE_ACCOUNT_KEY_B64_1"
```

with:

```python
def test_slot_env_name_is_the_single_naming_seam():
    from app.providers import registry

    assert registry.slot_env_name("groq", 0) == "GROQ_API_KEY"
    assert registry.slot_env_name("groq", 2) == "GROQ_API_KEY_2"
    assert registry.slot_env_name("vertex", 1) == "GCP_SERVICE_ACCOUNT_KEY_1"
```

`tests/test_vertex_credentials.py` — replace the entire file (the local-file-fallback tests no longer have a code path to exercise, so they're deleted rather than adapted):

```python
"""app/providers/vertex_credentials.py -- Vertex's two-layer credential
chain: GCP_SERVICE_ACCOUNT_KEY (index-aware) -> None, meaning "let
google-auth discover implicit ADC".

Hermetic by construction: the autouse fixture points the credential at
nothing, because a developer's real .env legitimately sets
GCP_SERVICE_ACCOUNT_KEY to a real value -- without it, the "nothing
resolves" tests would pass in CI and fail locally.
"""
from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.providers import vertex_credentials

KEY = {
    "type": "service_account",
    "project_id": "proj-from-key",
    "client_email": "svc@proj-from-key.iam.gserviceaccount.com",
}
OTHER_KEY = {**KEY, "project_id": "proj-from-slot-1"}


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture(autouse=True)
def _no_real_gcp_credentials(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key", "")
    for index in (1, 2):
        monkeypatch.delenv(f"GCP_SERVICE_ACCOUNT_KEY_{index}", raising=False)


def test_index_zero_decodes_the_base_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_decodes_its_own_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key", _b64(KEY))
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY_1", _b64(OTHER_KEY))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_returns_none_when_nothing_resolves():
    """NOT an error for vertex -- None means "pass no explicit credentials to
    the client", which is exactly what triggers google-auth's implicit ADC
    discovery. Contrast gemini/groq, where an empty credential always means
    misconfigured."""
    assert vertex_credentials.resolve_service_account_info(0) is None


def test_a_numbered_index_does_not_fall_back_to_index_zero(monkeypatch):
    """An unprovisioned slot must resolve to "nothing here", not silently to
    the base slot -- a swap to an empty index must be visible, not a no-op."""
    monkeypatch.setattr(settings, "gcp_service_account_key", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(2) is None


def test_malformed_base64_raises_rather_than_falling_through(monkeypatch):
    """A corrupt env var must surface, not quietly degrade to implicit ADC --
    that would run against a different account (or none) than the operator
    intended."""
    monkeypatch.setattr(settings, "gcp_service_account_key", "!!!not-base64!!!")
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_valid_base64_that_is_not_json_raises(monkeypatch):
    monkeypatch.setattr(
        settings, "gcp_service_account_key", base64.b64encode(b"nope").decode()
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_decoding_to_a_json_list_not_an_object_raises(monkeypatch):
    """The documented contract is `dict | None` -- a syntactically valid JSON
    value that isn't an object (e.g. a list) must surface as an error, not be
    handed to VertexProvider as if it were a service-account key."""
    monkeypatch.setattr(
        settings,
        "gcp_service_account_key",
        base64.b64encode(json.dumps([1, 2, 3]).encode()).decode(),
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)
```

`tests/test_github_app.py` — replace the `_throwaway_app_credentials` fixture:

```python
@pytest.fixture(autouse=True)
def _throwaway_app_credentials(tmp_path, monkeypatch):
    """Point settings at a freshly generated, throwaway RSA key.

    Keeps these tests independent of the real (gitignored) App credentials —
    only JWT *signing* happens locally with this key; every HTTP call is
    mocked below, so nothing is ever sent anywhere with it.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = tmp_path / "throwaway-key.pem"
    pem_path.write_bytes(pem)

    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem_path))
```

with:

```python
@pytest.fixture(autouse=True)
def _throwaway_app_credentials(monkeypatch):
    """Point settings at a freshly generated, throwaway RSA key.

    Keeps these tests independent of the real (gitignored) App credentials —
    only JWT *signing* happens locally with this key; every HTTP call is
    mocked below, so nothing is ever sent anywhere with it.
    """
    import base64

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)
    monkeypatch.setattr(
        settings, "github_app_private_key", base64.b64encode(pem).decode()
    )
```

and replace:

```python
def test_read_private_key_prefers_base64_env(monkeypatch):
    import base64

    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    monkeypatch.setattr(
        settings,
        "github_app_private_key_b64",
        base64.b64encode(pem.encode()).decode(),
    )
    assert github_app._read_private_key() == pem
```

with:

```python
def test_read_private_key_decodes_the_base64_env(monkeypatch):
    import base64

    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    monkeypatch.setattr(
        settings,
        "github_app_private_key",
        base64.b64encode(pem.encode()).decode(),
    )
    assert github_app._read_private_key() == pem
```

`tests/test_deploy_script.py` — replace the `_no_real_provider_credentials` fixture body:

```python
    for name in ("gemini_api_key", "groq_api_key", "gcp_service_account_key_b64"):
        monkeypatch.setattr(settings, name, "")
```

with:

```python
    for name in ("gemini_api_key", "groq_api_key", "gcp_service_account_key"):
        monkeypatch.setattr(settings, name, "")
```

Replace the `complete_config` fixture:

```python
@pytest.fixture
def complete_config(monkeypatch, tmp_path):
    """Every value check_config requires, present and valid."""
    pem = tmp_path / "key.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    return pem
```

with:

```python
@pytest.fixture
def complete_config(monkeypatch):
    """Every value check_config requires, present and valid."""
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key", "aGVsbG8=")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "public_base_url", "https://x.onrender.com")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
```

Delete these two tests entirely (their premise — choosing between a b64 var and a file path — no longer exists):

```python
def test_check_config_accepts_base64_key_without_a_pem_file(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    monkeypatch.setattr(settings, "github_app_private_key_b64", "aGVsbG8=")
    assert deploy.check_config().status == "PASS"


def test_check_config_fails_when_the_pem_path_does_not_exist(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nonexistent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail
```

Replace `test_check_config_requires_the_gcp_key_when_vertex_selected`:

```python
def test_check_config_requires_the_gcp_key_when_vertex_selected(complete_config, monkeypatch):
    """deploy.py answers "can this be DEPLOYED", and Render has neither a local
    key file nor a `gcloud` ADC login -- so the base64 form is genuinely
    required there even though a local run could resolve either fallback."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GCP_SERVICE_ACCOUNT_KEY_B64" in result.detail
```

with:

```python
def test_check_config_requires_the_gcp_key_when_vertex_selected(complete_config, monkeypatch):
    """deploy.py answers "can this be DEPLOYED", and Render has no `gcloud`
    ADC login -- so the credential is genuinely required there even though a
    local run could fall back to implicit ADC."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_service_account_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GCP_SERVICE_ACCOUNT_KEY" in result.detail
```

Delete the `unreadable_pem` fixture and both tests that depend on it (their premise — a PEM file that exists but can't be read — no longer exists):

```python
@pytest.fixture
def unreadable_pem(complete_config):
    """chmod 000 -- root reads anything, so this cannot be tested as root."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; cannot test an unreadable PEM")
    complete_config.chmod(0o000)
    yield complete_config
    complete_config.chmod(0o600)


def test_check_config_fails_on_an_unreadable_pem(unreadable_pem):
    """is_file() said yes while read_bytes() raised, so config passed and the
    failure surfaced later as a traceback."""
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "unreadable" in result.detail


def test_check_config_distinguishes_unreadable_from_missing(unreadable_pem):
    """Different problems need different actions: fix permissions vs create a
    key. Reporting both as 'missing' sends the operator to the wrong fix."""
    detail = deploy.check_config().detail
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" not in detail
```

Replace `test_check_config_reports_a_missing_pem_as_missing`:

```python
def test_check_config_reports_a_missing_pem_as_missing(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY_B64 or _PATH" in result.detail
```

with:

```python
def test_check_config_reports_a_missing_private_key_as_missing(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "github_app_private_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail
```

Delete `test_check_config_uses_b64_without_touching_the_filesystem` entirely (redundant with `test_check_config_passes_when_everything_is_present` now that there is only one source):

```python
def test_check_config_uses_b64_without_touching_the_filesystem(
    complete_config, monkeypatch
):
    monkeypatch.setattr(settings, "github_app_private_key_b64", "Zm9v")
    monkeypatch.setattr(settings, "github_app_private_key_path", "/nope/absent.pem")
    assert deploy.check_config().status == "PASS"
```

Replace the `sync_ready` fixture:

```python
@pytest.fixture
def sync_ready(monkeypatch, tmp_path):
    """Every value _wanted_env() reads, non-empty, plus a Render key."""
    pem = tmp_path / "key.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/postgres")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key_b64", "")
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem))
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    # database_url is a dummy, unreachable host -- stub psycopg.connect so the
    # masking guard's "no override" outcome is deterministic rather than an
    # accident of DNS failure (tests must never open a real DB connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
```

with:

```python
@pytest.fixture
def sync_ready(monkeypatch):
    """Every value _wanted_env() reads, non-empty, plus a Render key."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h:5432/postgres")
    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_private_key", "aGVsbG8=")
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    # database_url is a dummy, unreachable host -- stub psycopg.connect so the
    # masking guard's "no override" outcome is deterministic rather than an
    # accident of DNS failure (tests must never open a real DB connection).
    monkeypatch.setattr(deploy.psycopg, "connect", lambda *a, **k: _FakeConn(None))
```

Delete `test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback` entirely (depends on the deleted `unreadable_pem` fixture; its premise no longer exists):

```python
def test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback(
    unreadable_pem, monkeypatch, capsys
):
    """The parked residual: _wanted_env's OSError sat outside sync_env's try."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_x")
    called = []
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: called.append(1))
    code = deploy.sync_env()
    assert code == 2
    assert "GITHUB_APP_PRIVATE_KEY_B64" in capsys.readouterr().err
    assert called == []
```

In `test_wanted_env_pushes_every_providers_model_var`, replace:

```python
    monkeypatch.setattr(deploy, "_private_key_b64", lambda: ("pem-b64", ""))
```

with:

```python
    monkeypatch.setattr(settings, "github_app_private_key", "pem-b64")
```

- [ ] **Step 4: Run the full test suite to confirm it passes**

Run: `uv run pytest tests/ -q`
Expected: all tests PASS (the doc-mentioning tests, `test_env_var_names_match_the_docs` and `test_exit_codes_are_documented`, will still pass at this point since they only check substring presence — Step 5 below still updates the docs for correctness/consistency, not because these tests demand it).

- [ ] **Step 5: Update docs**

`.env.example` — replace:

```
GITHUB_APP_INSTALLATION_ID=
# Local development: path to the downloaded private-key PEM (kept out of git; see .gitignore).
GITHUB_APP_PRIVATE_KEY_PATH=./github-app-private-key.pem
# Render/hosted: base64-encoded private key (set in dashboard, never committed).
GITHUB_APP_PRIVATE_KEY_B64=
GITHUB_WEBHOOK_SECRET=
```

with:

```
GITHUB_APP_INSTALLATION_ID=
# Base64-encoded private key -- verbatim only, never a file path (kept out of
# git). Encode a local PEM with: uv run python -m bot.scripts.encode_credential
# path/to/github-app-private-key.pem
GITHUB_APP_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=
```

and replace:

```
# vertex (Vertex AI via google-genai's vertexai=True -- needs GCP billing)
# The credential is a GCP service-account JSON key, not an API-key string.
# Render/hosted: base64 of the whole key file (set in the dashboard, never committed).
GCP_SERVICE_ACCOUNT_KEY_B64=
# GCP_SERVICE_ACCOUNT_KEY_B64_1=
# GCP_SERVICE_ACCOUNT_KEY_B64_2=
# Local development: path to the downloaded key JSON (kept out of git; see .gitignore).
# Selected by the SAME key index as the B64 slots above; B64 wins when both
# resolve. With neither set, google-auth falls back to your own
# `gcloud auth application-default login` credentials.
GCP_SERVICE_ACCOUNT_KEY_PATH=./gcp-service-account-key.json
# GCP_SERVICE_ACCOUNT_KEY_PATH_1=
# GCP_SERVICE_ACCOUNT_KEY_PATH_2=
```

with:

```
# vertex (Vertex AI via google-genai's vertexai=True -- needs GCP billing)
# The credential is a GCP service-account JSON key -- base64-encoded,
# verbatim only, never a file path. Encode a local key file with:
# uv run python -m bot.scripts.encode_credential path/to/service-account-key.json
# With none of the slots below set, google-auth falls back to your own
# `gcloud auth application-default login` credentials.
GCP_SERVICE_ACCOUNT_KEY=
# GCP_SERVICE_ACCOUNT_KEY_1=
# GCP_SERVICE_ACCOUNT_KEY_2=
```

`README.md` — replace:

```
The push set is **provider-derived**, not a fixed list: it always pushes
`DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, and **every
provider's model var** (`LLM_MODEL`, `GROQ_MODEL`, `VERTEX_MODEL`) — a DB
override (see below) can activate any provider with no redeploy, so every
provider's model var has to already be on the service, not just the
currently-selected one's. The **selected provider's** credential is always
pushed too — e.g. `LLM_PROVIDER=groq` pushes `GROQ_API_KEY`, not
`GEMINI_API_KEY`. Any *other* provider's credential is pushed too, but only
if you happen to have it set locally — an unselected provider's key is never
demanded (this includes vertex's `GCP_SERVICE_ACCOUNT_KEY_B64`, which now has
its own `VERTEX_MODEL` rather than sharing gemini's `LLM_MODEL`). It refuses
to start (exit 2)
```

with:

```
The push set is **provider-derived**, not a fixed list: it always pushes
`DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, and **every
provider's model var** (`LLM_MODEL`, `GROQ_MODEL`, `VERTEX_MODEL`) — a DB
override (see below) can activate any provider with no redeploy, so every
provider's model var has to already be on the service, not just the
currently-selected one's. The **selected provider's** credential is always
pushed too — e.g. `LLM_PROVIDER=groq` pushes `GROQ_API_KEY`, not
`GEMINI_API_KEY`. Any *other* provider's credential is pushed too, but only
if you happen to have it set locally — an unselected provider's key is never
demanded (this includes vertex's `GCP_SERVICE_ACCOUNT_KEY`, which now has
its own `VERTEX_MODEL` rather than sharing gemini's `LLM_MODEL`). It refuses
to start (exit 2)
```

and replace:

```
Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). `vertex` rides the identical
mechanism with a differently-shaped credential: `GCP_SERVICE_ACCOUNT_KEY_B64`,
`_1`, `_2`, ... on Render, and locally the same index instead selects among
`GCP_SERVICE_ACCOUNT_KEY_PATH`, `_1`, `_2`, ... key files — so
`uv run python -m bot.scripts.set_override vertex --index 1` swaps service
accounts with no redeploy and no CLI change. Each provider tracks its own
key-index independently, so switching providers never disturbs the slot
chosen for the other two, and no secret value is ever written to, read
```

with:

```
Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). `vertex` rides the identical
mechanism with a differently-shaped (but still verbatim, base64-encoded, no
file path) credential: `GCP_SERVICE_ACCOUNT_KEY`, `_1`, `_2`, ... — so
`uv run python -m bot.scripts.set_override vertex --index 1` swaps service
accounts with no redeploy and no CLI change. Each provider tracks its own
key-index independently, so switching providers never disturbs the slot
chosen for the other two, and no secret value is ever written to, read
```

and replace:

```
- **Vertex AI**: live and fully verified (`LLM_PROVIDER=vertex`), reinstated
  2026-08-14 — it had been removed while this project's no-card constraint
  made it unrunnable, and came back once GCP billing/ADC access became
  available. Unlike the other two providers its credential is a GCP
  service-account identity: `GCP_SERVICE_ACCOUNT_KEY_B64` (hosted) → a local
  key file → implicit ADC. Two bugs were found and fixed via real live calls
```

with:

```
- **Vertex AI**: live and fully verified (`LLM_PROVIDER=vertex`), reinstated
  2026-08-14 — it had been removed while this project's no-card constraint
  made it unrunnable, and came back once GCP billing/ADC access became
  available. Unlike the other two providers its credential is a GCP
  service-account identity: `GCP_SERVICE_ACCOUNT_KEY` (hosted, base64) →
  implicit ADC. Two bugs were found and fixed via real live calls
```

`SETUP.md` — replace:

```
- Private key: downloaded as part of the manifest exchange, saved to
  `github-app-private-key.pem` at the repo root (gitignored). Referenced by
  path via `GITHUB_APP_PRIVATE_KEY_PATH` in `.env` (chosen over base64-encoding
  the key inline).
```

with:

```
- Private key: downloaded as part of the manifest exchange, saved to
  `github-app-private-key.pem` at the repo root (gitignored). Set
  `GITHUB_APP_PRIVATE_KEY` in `.env` to its base64 form (verbatim only, never
  a file path) — encode it with
  `uv run python -m bot.scripts.encode_credential github-app-private-key.pem`.
```

and replace:

```
   - `GITHUB_APP_PRIVATE_KEY_B64`: base64-encoded PEM (see "Secrets encoding" below)
```
with:
```
   - `GITHUB_APP_PRIVATE_KEY`: base64-encoded PEM (see "Secrets encoding" below)
```

and replace:
```
   - (Other provider creds as needed: `GEMINI_API_KEY`, `GCP_SERVICE_ACCOUNT_KEY_B64`, etc.)
```
with:
```
   - (Other provider creds as needed: `GEMINI_API_KEY`, `GCP_SERVICE_ACCOUNT_KEY`, etc.)
```

and replace:

```
### 3.3 Secrets encoding

The PEM file must be base64-encoded for the `GITHUB_APP_PRIVATE_KEY_B64` env var:

```bash
base64 -w0 < github-app-private-key.pem
```

Copy the output and paste it into the Render dashboard's `GITHUB_APP_PRIVATE_KEY_B64`
field (the app code will decode it at startup).
```

with:

```
### 3.3 Secrets encoding

The PEM file must be base64-encoded for the `GITHUB_APP_PRIVATE_KEY` env var:

```bash
uv run python -m bot.scripts.encode_credential github-app-private-key.pem
```

(equivalently, `base64 -w0 < github-app-private-key.pem` — both produce the
same output). Copy the output and paste it into the Render dashboard's
`GITHUB_APP_PRIVATE_KEY` field (the app code will decode it at startup).
```

and replace:

```
  `app/providers/vertex_credentials.py`:
  1. `GCP_SERVICE_ACCOUNT_KEY_B64` (+ numbered `_1`/`_2` siblings) — the
     hosted/Render path, selected by the same `vertex_key_index` override
     gemini/groq use.
  2. `GCP_SERVICE_ACCOUNT_KEY_PATH` (default `./gcp-service-account-key.json`,
     gitignored; + numbered siblings) — local-dev only, for testing several
     service accounts without touching Render or Supabase.
  3. Implicit ADC — with neither of the above, `google-auth` discovers
     `gcloud auth application-default login`'s local credentials on its own.
```

with:

```
  `app/providers/vertex_credentials.py`:
  1. `GCP_SERVICE_ACCOUNT_KEY` (+ numbered `_1`/`_2` siblings, base64-encoded,
     verbatim only — encode a local key file with
     `uv run python -m bot.scripts.encode_credential path/to/key.json`) —
     selected by the same `vertex_key_index` override gemini/groq use.
  2. Implicit ADC — with the above unset, `google-auth` discovers
     `gcloud auth application-default login`'s local credentials on its own.
```

and replace:

```
  **Deploying vertex to Render requires the base64 form.** Render has neither
  a local key file nor a `gcloud` login, so `scripts/deploy.py`'s `config` and
  `provider` checks FAIL for `LLM_PROVIDER=vertex` unless
  `GCP_SERVICE_ACCOUNT_KEY_B64` is set locally — that is the value `--sync-env`
  pushes. A file-only local setup is fine for running the app locally, but is
  deliberately not considered deployable. `--sync-env` does not push
```

with:

```
  **Deploying vertex to Render requires the credential to be set.** Render
  has no `gcloud` login, so `scripts/deploy.py`'s `config` and `provider`
  checks FAIL for `LLM_PROVIDER=vertex` unless `GCP_SERVICE_ACCOUNT_KEY` is
  set locally — that is the value `--sync-env` pushes. `--sync-env` does not push
```

and replace:

```
   The set of vars pushed is **provider-derived**, not a fixed list: it
   always pushes `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
```

with:

```
   The set of vars pushed is **provider-derived**, not a fixed list: it
   always pushes `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
```

`CLAUDE.md` — replace:

```
  that shared a line with `GCP_SERVICE_ACCOUNT_KEY_B64`). The only safe way
```

with:

```
  that shared a line with `GCP_SERVICE_ACCOUNT_KEY`). The only safe way
```

and replace:

```
  service-account identity rather than an API-key string:
  `GCP_SERVICE_ACCOUNT_KEY_B64` (hosted, numbered slots) → a local key file →
  implicit ADC, resolved in `app/providers/vertex_credentials.py`. No secret
  reaches Postgres — only the slot index, exactly as for gemini/groq.
```

with:

```
  service-account identity rather than an API-key string:
  `GCP_SERVICE_ACCOUNT_KEY` (hosted, numbered slots, base64, verbatim only —
  see the 2026-08-16 credential-convention design) → implicit ADC, resolved
  in `app/providers/vertex_credentials.py`. No secret reaches Postgres — only
  the slot index, exactly as for gemini/groq.
```

- [ ] **Step 6: Run the full test suite one more time**

Run: `uv run pytest tests/ -q`
Expected: all tests PASS, including `test_env_var_names_match_the_docs` and `test_exit_codes_are_documented`.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/providers/registry.py app/providers/vertex_credentials.py \
  app/github_app.py scripts/deploy.py scripts/manual_verify_vertex.py \
  .env.example README.md SETUP.md CLAUDE.md \
  tests/test_config.py tests/test_provider_registry.py tests/test_vertex_credentials.py \
  tests/test_github_app.py tests/test_deploy_script.py
git commit -m "feat: verbatim-only credential convention (GITHUB_APP_PRIVATE_KEY, GCP_SERVICE_ACCOUNT_KEY[_n]), no local-file-path fallback"
```

---

### Task 3: Migration-checklist test for the retired credential vars

**Files:**
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `_key_names()`, `_REPO_ROOT`, `re` (all already defined/imported in `tests/test_config.py`).
- Produces: nothing consumed by a later task — this is a standalone checklist test, mirroring `test_no_operational_key_lives_in_the_secrets_file`'s role for the operational-config-split migration.

This test reads only `.env`'s key NAMES (never values) via the module's existing `_key_names()` helper — it is the one agent-safe way to check `.env`'s content per CLAUDE.md's Secret handling section. Unlike Tasks 1, 2, and 4, this task has no "make it pass" step within the agent's control: whether it passes depends on whether the *user* has migrated their own `.env` (removing the four retired names), which is explicit user action outside this plan's scope — exactly like `test_no_operational_key_lives_in_the_secrets_file` was expected to be red until the user completed that migration. Do not attempt to make this test pass by touching `.env` yourself.

- [ ] **Step 1: Write the test**

Add to `tests/test_config.py` (after `test_no_unlisted_key_lives_in_the_config_file`):

```python
_RETIRED_CREDENTIAL_KEYS = frozenset(
    {
        "GITHUB_APP_PRIVATE_KEY_B64",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
    }
)
_RETIRED_NUMBERED_RE = re.compile(r"^(GCP_SERVICE_ACCOUNT_KEY_B64|GCP_SERVICE_ACCOUNT_KEY_PATH)_\d+$")


def test_no_legacy_credential_var_lives_in_the_secrets_file():
    """Migration checklist for the verbatim-only credential convention
    (docs/superpowers/specs/2026-08-16-credential-convention-design.md):
    these four names, and vertex's numbered _B64_n/_PATH_n siblings, are
    retired and no Settings field reads them anymore. Reports NAMES only --
    see CLAUDE.md's "Secret handling" section."""
    names = _key_names(_REPO_ROOT / ".env")
    legacy = {
        name
        for name in names
        if name in _RETIRED_CREDENTIAL_KEYS or _RETIRED_NUMBERED_RE.match(name)
    }
    assert not legacy, (
        f"retired credential var(s) still in .env, no longer read: {sorted(legacy)} -- "
        "rename GITHUB_APP_PRIVATE_KEY_B64 to GITHUB_APP_PRIVATE_KEY, "
        "GCP_SERVICE_ACCOUNT_KEY_B64[_n] to GCP_SERVICE_ACCOUNT_KEY[_n] (base64-encode any "
        "local key file first with scripts/encode_credential.py), and remove the _PATH "
        "variants entirely"
    )
```

- [ ] **Step 2: Run the test and record its current status**

Run: `uv run pytest tests/test_config.py::test_no_legacy_credential_var_lives_in_the_secrets_file -v`

This may PASS (if `.env` was already migrated, e.g. during Task 2's own testing) or FAIL (naming the retired vars still present in the real `.env`) — either is a correct, expected outcome at this point; this test's job is to report reality, not to be forced green. If it fails, that is the migration checklist doing its job — do not edit `.env` yourself to silence it.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: every test PASSES except possibly the one added in Step 1, per Step 2's outcome.

- [ ] **Step 4: Commit**

```bash
git add tests/test_config.py
git commit -m "test: add migration checklist for retired credential vars (verbatim-only convention)"
```

---

### Task 4: `check_config()` resolves an active DB model override before its pricing check

**Files:**
- Modify: `scripts/deploy.py`
- Modify (docs): `README.md`, `SETUP.md`
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `deploy._resolved_model_overrides() -> dict[str, str | None]` (already exists, used today by `sync_env()`).
- Produces: `_unpriced_models(overrides: dict[str, str | None] | None = None) -> list[tuple[str, str, str, str]]` — the `overrides` parameter is new and optional; `sync_env()`'s existing no-argument call site is unaffected.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deploy_script.py` (after `test_check_config_reports_an_unpriced_model_alongside_other_missing_keys`):

```python
def test_check_config_uses_the_local_value_without_a_database_url(
    complete_config, monkeypatch
):
    """No DATABASE_URL -> no override to resolve -> local-only check, exactly
    the pre-existing behavior."""
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy.check_config().status == "PASS"


def test_check_config_fails_on_an_unpriced_db_model_override(complete_config, monkeypatch):
    """The residual gap this fixes: set_override.py --model --force can put an
    unpriced model into live rotation, and check_config must not report PASS
    for it just because .env.config's own value is fine."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(
        deploy, "_resolved_model_overrides",
        lambda: {"gemini": None, "groq": None, "vertex": "totally-made-up-model"},
    )
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "totally-made-up-model" in result.detail
    assert "vertex" in result.detail
    assert "override" in result.detail.lower()


def test_check_config_passes_a_priced_db_model_override(complete_config, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(
        deploy, "_resolved_model_overrides",
        lambda: {"gemini": None, "groq": None, "vertex": "gemini-2.5-flash"},
    )
    assert deploy.check_config().status == "PASS"


def test_check_config_degrades_to_local_only_when_the_db_read_fails(
    complete_config, monkeypatch
):
    """A DB-read failure must degrade to the local-only check, never crash the
    whole config row for an unrelated reason -- mirrors check_provider()'s own
    degrade-on-exception behavior."""
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(deploy, "_resolved_model_overrides", _boom)
    assert deploy.check_config().status == "PASS"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "db_model_override or database_url or db_read_fails" -v`
Expected: `test_check_config_fails_on_an_unpriced_db_model_override` FAILS (reports `PASS`, since `check_config` doesn't resolve overrides yet); the other three PASS already (they describe today's actual behavior) — confirm this before proceeding, since it tells you the fix is additive, not a behavior change to the no-override path.

- [ ] **Step 3: Implement**

In `scripts/deploy.py`, replace `_unpriced_models()`:

```python
def _unpriced_models() -> list[tuple[str, str, str, str]]:
    """Every provider whose locally-configured model has no rate-table entry,
    as (provider, model_var, model, known-models string).

    Checked for EVERY provider, not just the active one -- exactly as
    _wanted_env() pushes every provider's model var: a DB provider override
    can activate any of them with no redeploy, so an unpriced value sitting in
    a currently-inactive provider's var is a live landmine, not a harmless one.

    An empty model var is skipped deliberately: that is a distinct,
    pre-existing failure mode, and piling a second, confusing message onto it
    adds noise rather than clarity. In practice it never fires -- every
    Settings model field carries a non-empty, priced default.

    Shared by check_config() (which reports all of them) and sync_env()
    (which refuses on the first), so the two can never disagree about what
    counts as unpriced.
    """
    unpriced: list[tuple[str, str, str, str]] = []
    for provider, (_credential, model_var) in sorted(_PROVIDERS.items()):
        model = getattr(settings, model_var.lower(), "")
        if model and not pricing.is_known(provider, model):
            known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
            unpriced.append((provider, model_var, model, known))
    return unpriced
```

with:

```python
def _unpriced_models(
    overrides: dict[str, str | None] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Every provider whose EFFECTIVE model has no rate-table entry, as
    (provider, model_var, model, known-models string).

    Checked for EVERY provider, not just the active one -- exactly as
    _wanted_env() pushes every provider's model var: a DB provider override
    can activate any of them with no redeploy, so an unpriced value sitting in
    a currently-inactive provider's var is a live landmine, not a harmless one.

    `overrides`, when given, is a {provider: DB model override or None} map
    (as returned by _resolved_model_overrides()) -- the EFFECTIVE model (an
    active override, else the local value) is what gets checked, so a
    --force'd unpriced override is caught too. check_config() passes this (it
    must report what will actually run); sync_env() omits it (it is refusing
    a PUSH of the local value -- an active override's own pricing is that
    override's own already-checked concern, from set_override.py --model's
    own refusal).

    An empty model is skipped deliberately: that is a distinct, pre-existing
    failure mode, and piling a second, confusing message onto it adds noise
    rather than clarity. In practice it never fires -- every Settings model
    field carries a non-empty, priced default.

    Shared by check_config() (which reports all of them) and sync_env()
    (which refuses on the first), so the two can never disagree about what
    counts as unpriced.
    """
    overrides = overrides or {}
    unpriced: list[tuple[str, str, str, str]] = []
    for provider, (_credential, model_var) in sorted(_PROVIDERS.items()):
        local_model = getattr(settings, model_var.lower(), "")
        model = overrides.get(provider) or local_model
        if model and not pricing.is_known(provider, model):
            known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
            unpriced.append((provider, model_var, model, known))
    return unpriced
```

In `check_config()`, replace:

```python
    # A problem, not a missing key: the var HAS a value, it is simply not one
    # the pricing table recognizes. Existing detail_lines assembly (missing,
    # then problems) needs no change.
    for provider, model_var, model, known in _unpriced_models():
        problems.append(
            f"{model_var}={model!r} has no pricing-table entry for {provider} "
            f"(known: {known})"
        )
```

with:

```python
    # A problem, not a missing key: the var HAS a value, it is simply not one
    # the pricing table recognizes. Existing detail_lines assembly (missing,
    # then problems) needs no change.
    overrides: dict[str, str | None] = {}
    if settings.database_url:
        try:
            overrides = _resolved_model_overrides()
        # deliberate: DB trouble degrades to a local-only pricing check, never
        # a crash -- mirrors check_provider()'s own degrade-on-exception
        except Exception:  # noqa: BLE001
            overrides = {}
    for provider, model_var, model, known in _unpriced_models(overrides):
        if overrides.get(provider):
            problems.append(
                f"{provider} model override {model!r} has no pricing-table entry "
                f"(known {provider} models: {known}); clear it or add a pricing.py "
                f"entry: uv run python -m bot.scripts.set_override {provider} "
                "--clear-model --no-activate"
            )
        else:
            problems.append(
                f"{model_var}={model!r} has no pricing-table entry for {provider} "
                f"(known: {known})"
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: all tests PASS, including the four added in Step 1 and every pre-existing `check_config`/`sync_env` test (the local-value message text is unchanged, so `test_check_config_fails_on_an_unpriced_model`, `test_check_config_ignores_default_models`, and `test_check_config_reports_an_unpriced_model_alongside_other_missing_keys` all still pass with no edits).

- [ ] **Step 5: Update docs**

`README.md` and `SETUP.md` — in both files, replace:

```
| `config` | Every setting the service needs is resolvable locally, and every provider's model var has a pricing-table entry | yes |
```

with:

```
| `config` | Every setting the service needs is resolvable locally, and every provider's model var (including an active DB override) has a pricing-table entry | yes |
```

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py README.md SETUP.md
git commit -m "fix: check_config resolves an active DB model override before its pricing check"
```
