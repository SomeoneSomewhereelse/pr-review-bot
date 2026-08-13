# Design — Reinstating the Vertex AI provider

**Date:** 2026-08-13
**Status:** Approved for planning
**Relates to:** `app/providers/google_genai.py` (where `GeminiProvider` lives
and where `VertexProvider` joins it), `app/providers/factory.py` /
`registry.py` / `credentials.py` / `key_index.py` (the existing provider
seam this extends), `app/github_app.py::_read_private_key` (the precedent
for a b64-env-var / local-file credential split), `docs/superpowers/specs/
2026-08-12-api-key-index-override-design.md` (the DB-override pattern this
both reuses and extends), `CLAUDE.md`'s "Substitutions from the brief" and
"secrets only via env vars" rules (both change as a result of this design).

## 1. Problem

`SPEC.md` names Vertex AI as the brief's default LLM provider. It was
implemented once, then removed (see `CLAUDE.md`'s "Substitutions from the
brief" and `SETUP.md` §2): Vertex requires GCP billing, which this project's
no-card constraint ruled out, so the adapter could only ever be covered by
mocked tests and was deleted rather than carried as dead code.

That constraint no longer holds. GCP billing/ADC access is now available, so
Vertex can be reinstated as a real, live-runnable third provider alongside
`gemini` and `groq`.

Vertex differs from both existing providers in one fundamental way: its
credential is not a single API-key string. It's a GCP service-account
identity, ordinarily resolved via Application Default Credentials (ADC) —
`gcloud auth application-default login` locally, or a service-account key on
a server that can't run an interactive login. Reinstating Vertex means
extending the provider seam to carry a structurally different credential
shape, not just adding a fourth name to a list.

A second, independent requirement surfaced during design: the service
account must be swappable **on the fly, without a Render redeploy** — not
just at the next scheduled deploy. The existing numbered-env-var-slot
pattern (`GEMINI_API_KEY_1`, `_2`, ...) achieves "swap without redeploy" only
for slots *already provisioned*; a genuinely new credential still costs one
restart to add the Render env var. That's an acceptable cost for a 40-byte
API key. For Vertex, the goal is stronger: swap to *any* credential, known
or new, with zero restart, every time.

## 2. Decision

Four credential sources, checked in priority order, each answering a
different environment's need:

1. **DB override** (`runtime_config.vertex_service_account_b64`) — the
   production on-the-fly swap path. A pure Supabase write; the dispatcher
   picks it up on its next poll. No Render env var touched, no restart,
   ever — including the first time a given service account is used. This is
   the mechanism that actually satisfies the "no redeploy" requirement in
   full; the numbered-slot pattern below does not attempt to.
2. **`GCP_SERVICE_ACCOUNT_KEY_B64`** env var — a static Render/hosted
   fallback, present so the service has *something* to run on even before
   any DB override is ever set. Same b64-in-env-var shape as
   `GITHUB_APP_PRIVATE_KEY_B64` (~3KB vs. that key's ~2.2KB — same order of
   magnitude, not a new size class for this deployment).
3. **Local file** at `GCP_SERVICE_ACCOUNT_KEY_PATH` (default
   `./gcp-service-account-key.json`, gitignored), with numbered siblings
   `GCP_SERVICE_ACCOUNT_KEY_PATH_1`, `_2`, ... selected via the existing
   generic key-index mechanism (`vertex_key_index`). Local-dev-only, for
   testing against several different service accounts (e.g. a
   quota-exhausted one vs. a healthy one) without touching Supabase. This
   mirrors `GITHUB_APP_PRIVATE_KEY_PATH`'s local-file precedent, not the
   b64-env-var precedent — nobody hand-pastes a multi-KB blob into a local
   `.env` in this project; the file stays a file.
4. **Implicit ADC** — if none of the above resolve to a value, no explicit
   `credentials` object is passed to the client at all, and `google-auth`
   discovers `gcloud auth application-default login`'s local ADC file on its
   own. Zero-config local fallback.

Sources 1–2 answer "what does the deployed service run on, and how do I
change it without a restart." Source 3 answers "how do I test locally
against a specific, chosen service account." Source 4 is what makes local
dev usable with no setup beyond one `gcloud` command. They are independent
mechanisms that happen to share one resolution function, not one mechanism
wearing three names — worth keeping distinct in review, because a change to
one (e.g. adding a fifth numbered local slot) has no bearing on the others.

`GCP_PROJECT` (required, no default) and `GCP_LOCATION` (default
`us-central1`) are needed regardless of which credential source resolves —
`vertexai=True` cannot work without them, and both are locally detectable
with no network call, so `factory._build` fast-fails on a missing
`GCP_PROJECT` the same way it already fast-fails on a missing gemini/groq
key — except an *empty resolved credential* for vertex is not itself an
error (it means "fall through to implicit ADC"), unlike gemini/groq where an
empty string always means misconfigured.

## 3. Settings (`app/config.py`)

```python
gcp_project: str = ""
gcp_location: str = "us-central1"
gcp_service_account_key_b64: str = ""
gcp_service_account_key_path: str = "./gcp-service-account-key.json"
```

## 4. Credential resolution (`app/providers/vertex_credentials.py`, new)

One narrow module owns everything about resolving Vertex's credential —
distinct from `credentials.py`, which only knows the "one env var, one
string" shape gemini/groq use and can't represent a DB-override layer or a
JSON-parsing local file without losing that simplicity for the other two
providers.

```python
"""Resolves the Vertex service-account credential per the priority chain in
docs/superpowers/specs/2026-08-13-vertex-ai-provider-design.md section 2:
DB override -> GCP_SERVICE_ACCOUNT_KEY_B64 -> local file (index-aware) ->
None (implicit ADC). Never logged -- same discipline as
github_app.py::_read_private_key for the equivalent GitHub credential.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from app.config import settings
from app.providers import credentials

_override: str | None = None


def credential_override() -> str | None:
    return _override


def set_override_cache(value: str | None) -> None:
    global _override
    _override = value


def reset_override_cache() -> None:
    set_override_cache(None)


def _local_path(index: int) -> str:
    if index == 0:
        return settings.gcp_service_account_key_path
    return os.environ.get(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", "")


def resolve_service_account_info(index: int) -> dict | None:
    b64 = credential_override()
    if not b64:
        _, b64 = credentials.resolve("vertex", index)
    if b64:
        return json.loads(base64.b64decode(b64).decode())
    path = _local_path(index)
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text())
    return None
```

`credentials.resolve("vertex", index)` (unchanged, generic) covers source 2:
at index 0 it reads `settings.gcp_service_account_key_b64` (the
`base.lower()` convention already used for every provider); at index ≥ 1 it
would read `GCP_SERVICE_ACCOUNT_KEY_B64_{index}` from `os.environ` — a slot
this design doesn't provision anywhere, so it always resolves empty there
and falls through to the local file, harmlessly.

## 5. Provider adapter (`app/providers/google_genai.py`)

Added alongside `GeminiProvider`, per `SPEC.md`'s original description of
this file ("Vertex (vertexai=True) + Gemini (api_key) — one SDK, two
clients"). Both share `_complete()`.

```python
from google.oauth2 import service_account


class VertexProvider:
    """`vertex` -- gemini-flash-latest via Vertex AI (vertexai=True).
    Reinstated once GCP billing/ADC access became available -- see
    CLAUDE.md's "Substitutions from the brief"."""

    def __init__(
        self, project: str, location: str, service_account_info: dict | None
    ) -> None:
        creds = None
        if service_account_info is not None:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info
            )
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
```

`credentials=None` is `genai.Client`'s own default (confirmed against the
installed SDK signature) — passing it explicitly when `service_account_info`
is `None` is identical to omitting it, which is exactly what triggers
`google-auth`'s implicit ADC discovery.

## 6. Registry / base / factory wiring

```python
# app/providers/base.py
KNOWN_PROVIDERS = ("gemini", "groq", "vertex")

# app/providers/registry.py
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "LLM_MODEL"),
}
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "vertex": "vertex_key_index",
}
```

`factory._build` gains a vertex branch that does **not** go through the
existing "empty credential → raise" fast-fail (that check stays exactly as
written for gemini/groq, where empty always means misconfigured):

```python
def _build(provider: str, index: int) -> LLMProvider:
    if provider not in registry.PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r} (expected 'gemini', 'groq', or 'vertex')")
    if provider == "vertex":
        if not settings.gcp_project:
            raise ValueError("no credential configured for provider='vertex': GCP_PROJECT not set")
        info = vertex_credentials.resolve_service_account_info(index)
        return VertexProvider(
            project=settings.gcp_project, location=settings.gcp_location, service_account_info=info
        )
    env_name, api_key = credentials.resolve(provider, index)
    if not api_key:
        raise ValueError(
            f"no credential configured for provider={provider!r} index={index} ({env_name} not set)"
        )
    if provider == "gemini":
        return GeminiProvider(api_key=api_key)
    if provider == "groq":
        return GroqProvider(api_key=api_key)
    raise ValueError(f"registry lists {provider!r} but _build cannot construct it")
```

If credentials are genuinely missing in every environment (no DB override,
no env b64, no local file, no ADC), `genai.Client(...)`/`google-auth` raises
at construction. That flows into the existing generic
`asyncio.gather(..., return_exceptions=True)` → failed-specialist-row path —
no new error handling needed there.

## 7. DB-backed credential-blob override

### 7.1 Schema (`app/queue/store.py`)

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_service_account_b64 TEXT;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_key_index INTEGER;
```

`vertex_key_index` needs no new store functions — it's just a fourth column
under the already-generic `KEY_INDEX_COLUMNS` machinery
(`get_key_index_override`, `set_key_index_override`,
`get_all_key_index_overrides` all already iterate the dict, not a hardcoded
provider list).

### 7.2 Store functions

```python
def get_credential_override() -> str | None:
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT vertex_service_account_b64 FROM runtime_config WHERE id = 1"
        ).fetchone()
    return (row or {}).get("vertex_service_account_b64") or None


def set_credential_override(value: str | None, now: str) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config (id, vertex_service_account_b64, updated_at) "
            "VALUES (1, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "vertex_service_account_b64 = EXCLUDED.vertex_service_account_b64, "
            "updated_at = EXCLUDED.updated_at",
            (value, now),
        )
```

Mirrors `get_provider_override`/`set_provider_override` exactly — singleton
row, same `ON CONFLICT` shape.

### 7.3 Dispatcher refresh (`app/queue/dispatcher.py`)

A fourth refresh block, same cadence and degrade-on-exception shape as the
existing three:

```python
try:
    cred_override = await asyncio.to_thread(store.get_credential_override)
    vertex_credentials.set_override_cache(cred_override)
except Exception:  # noqa: BLE001
    logger.exception("failed to refresh the vertex credential override; using env/ADC")
    vertex_credentials.reset_override_cache()
```

(The `vertex_key_index` column needs no dispatcher change — it's already
covered by the existing `get_all_key_index_overrides` refresh block, which
iterates `KEY_INDEX_COLUMNS` generically.)

## 8. CLAUDE.md rule change

"Secrets only via env vars; no secret is ever logged" gets an explicit,
named exception:

> Secrets only via env vars, **except the Vertex service-account credential,
> which may also live in `runtime_config.vertex_service_account_b64`
> (Supabase) — a deliberate exception made to support swapping it on the
> fly without a Render redeploy. No secret is ever logged, regardless of
> which store holds it.**

The "Substitutions from the brief" section's Vertex bullet is rewritten from
"removed" to "reinstated once GCP billing/ADC access became available,"
keeping the original removal reasoning as history rather than deleting it.

## 9. CLI (`scripts/set_override.py`)

Two new flags, vertex-only:

```
uv run python -m scripts.set_override vertex --credential-file ./gcp-service-account-key.json
uv run python -m scripts.set_override vertex --clear-credential
```

- `--credential-file PATH`: reads the file, validates it parses as JSON and
  has the expected service-account shape (`type == "service_account"`,
  non-empty `project_id`, `private_key`, `client_email`) — a local,
  no-network check, refusing malformed input before it ever reaches
  Supabase. Base64-encodes the raw bytes and writes via
  `store.set_credential_override()`. Activates vertex as the active
  provider unless `--no-activate` (mirroring `--index`'s existing
  `--no-activate` behavior).
- `--clear-credential`: `store.set_credential_override(None, now)`, falling
  back to env b64 / local file / ADC.
- Both flags error if `provider != "vertex"` — no other provider has this
  concept — and are mutually exclusive with each other and with
  `--index`/`--clear-index` in the same invocation, keeping each call
  single-purpose.
- `--force` does not apply to `--credential-file`: a malformed
  service-account file is never a legitimate write, unlike the Render/local
  verification checks elsewhere in this script, where `--force` overrides a
  live-state mismatch that might itself be stale or wrong. There's no
  "unverifiable, proceed with a warning" case here either — the JSON-shape
  check is always locally resolvable, so it's a hard error or nothing.

**One necessary deviation for vertex's existing `--index`/`--clear-index`
path:** today, setting an index runs `_override.verify_render_slot`, which
checks Render's live env vars for `{base}_{index}`. For vertex, `--index`
selects a *local file* (`GCP_SERVICE_ACCOUNT_KEY_PATH_{index}`), which never
exists on Render at all — running the existing Render check would either be
meaningless or actively misleading ("missing on Render" for a file that was
never supposed to be there). So `provider == "vertex"` skips
`verify_render_slot` entirely and instead checks local file existence
directly: `Path(_local_path(index)).is_file()`. Unlike the Render check
(which degrades to a warning when it can't verify, because network/API
availability is genuinely outside the operator's control), a missing local
file is always definitively checkable — so this refuses unless `--force`,
the same "verified and it's wrong → refuse" branch the Render check uses for
a confirmed-missing slot.

## 10. Docs

- **`CLAUDE.md`** — §8 (as above); "Substitutions from the brief" Vertex
  bullet rewritten (reinstated, not removed).
- **`SETUP.md`** §2 — add a dated update recording the constraint lift and
  the ADC/ DB-override setup, keeping the existing removal history intact
  above it (matching this file's existing pattern of dated updates rather
  than rewriting history).
- **`README.md`** — "Known limitations" Vertex bullet updated to describe it
  as live; new "Swapping the Vertex credential without a redeploy" section
  mirroring the existing provider/cooldown/key-index override sections.
- **`SPEC.md`** — already describes vertex as the default provider;
  needs no correction, only confirmation it still matches (the `runtime_config`
  override section gains the two new columns in its listing).
- **`cost.md`** — replace the hypothetical "$300 GCP trial credit" costing
  with a real per-token rate entry once one is looked up (see §12).
- **`.env.example`** — `LLM_PROVIDER` comment gains `vertex`; new
  `GCP_PROJECT`, `GCP_LOCATION`, `GCP_SERVICE_ACCOUNT_KEY_B64`,
  `GCP_SERVICE_ACCOUNT_KEY_PATH` entries, following the existing
  path-for-local/b64-for-hosted comment style used for the GitHub App key.
- **`.gitignore`** — add `gcp-service-account-key.json` (the default local
  filename), alongside the existing `*.pem` entry.

## 11. Surface

- `app/config.py` — 4 new settings.
- `app/providers/base.py` — `KNOWN_PROVIDERS` gains `"vertex"`.
- `app/providers/registry.py` — `PROVIDERS` and `KEY_INDEX_COLUMNS` gain a
  vertex entry each.
- `app/providers/google_genai.py` — `VertexProvider`, sharing `_complete()`.
- `app/providers/vertex_credentials.py` — new; resolution chain + override
  cache.
- `app/providers/factory.py` — vertex branch in `_build`, bypassing the
  generic empty-credential fast-fail.
- `app/queue/store.py` — 2-column migration; `get_credential_override`,
  `set_credential_override`.
- `app/queue/dispatcher.py` — fourth override refresh block.
- `scripts/set_override.py` — `--credential-file`, `--clear-credential`;
  vertex-specific local-file check replacing `verify_render_slot` for
  vertex's `--index` path.
- `app/providers/pricing.py` — new `("vertex", "gemini-flash-latest")` rate
  entry.
- `CLAUDE.md`, `SETUP.md`, `README.md`, `SPEC.md`, `cost.md`,
  `.env.example`, `.gitignore` — per §10.
- `tests/test_providers.py`, `tests/test_deploy_script.py`,
  `tests/test_set_override_script.py` — see §12, rewriting the four existing
  "vertex is rejected" assertions.

## 12. Testing (deterministic-first, per `CLAUDE.md`'s LLM testing-hygiene rule)

**Rewritten (vertex was rejected → vertex is accepted):**
- `test_providers.py::test_factory_rejects_retired_vertex_provider` →
  replaced with a construction test for the new vertex branch (mocked
  credentials, asserts a `VertexProvider` is returned, not a `ValueError`).
- `test_deploy_script.py::test_providers_table_covers_every_supported_provider`
  → asserts `{"gemini", "groq", "vertex"}`.
- `test_deploy_script.py::test_check_config_fails_on_an_unrecognized_provider`
  and `test_check_config_reports_a_bad_provider_alongside_other_missing_keys`
  → swap the example unsupported value from `"vertex"` to a genuinely
  unsupported placeholder (e.g. `"unknown"`), since vertex stops being an
  example of an unsupported provider.
- `test_deploy_script.py::test_api_key_live_skips_for_an_unsupported_provider`
  → same swap.
- `test_set_override_script.py::test_rejects_an_unsupported_provider` → same
  swap.

**New:**
- `vertex_credentials.py::resolve_service_account_info` — one test per
  priority-chain layer: DB override present → used regardless of env/file;
  DB override absent + env b64 present → decoded; both absent + local file
  present → read and parsed; nothing present → `None`; malformed b64/JSON at
  any layer surfaces as a clear error, not a silent fallthrough.
- `vertex_credentials.py` override cache — mirrors `active.py`'s existing
  cache tests (starts `None`, set/reset).
- `factory.py` — vertex branch returns a `VertexProvider`; missing
  `GCP_PROJECT` raises before any credential resolution or network call;
  empty credential (no DB/env/file/ADC signal locally mocked) does **not**
  raise from `_build` itself (the raise, if any, comes from the mocked SDK
  client construction, not from `_build`'s own check) — this is the one
  behavioral difference from gemini/groq worth a dedicated test.
- `google_genai.py::VertexProvider` — mocked `genai.Client`; confirms
  `credentials=None` when `service_account_info` is `None`, and a real
  `service_account.Credentials` object when it isn't.
- `store.py` — round-trip `get/set_credential_override`; schema migration
  adds both new columns to a pre-existing table.
- `dispatcher.py` — fourth refresh block follows the same
  once-per-claimed-ticket, degrade-on-exception shape as the existing three.
- `scripts/set_override.py` — `--credential-file` with valid/malformed
  JSON; `--clear-credential`; both flags rejected for a non-vertex provider;
  `--index`/`--clear-index` for vertex checks local file existence (not
  Render) and refuses on a missing file unless `--force`.
- **One live-verification script**, `scripts/manual_verify_vertex.py`,
  mirroring `scripts/manual_verify_step4.py` — a single deliberate call
  confirming real structured output against actual Vertex, run once per
  CLAUDE.md's "one deliberate call per real verification need" rule, not
  repeated or looped.

## 13. Non-goals

- No numbered `GCP_SERVICE_ACCOUNT_KEY_B64_{n}` Render env-var slots — the
  DB override already gives unconditional no-redeploy swapping, so
  pre-provisioning multiple hosted blob slots would be redundant complexity
  solving an already-solved problem.
- No Render-side verification for vertex's local-file key-index — verified
  locally instead (§9), since the files in question never exist on Render.
- No automatic credential rotation, expiry tracking, or scheduling — a human
  runs `set_override.py --credential-file` when they decide to swap, same
  manual-trigger model as every other override in this codebase.
- No change to how gemini/groq resolve credentials — this design only adds
  a new branch alongside the existing ones.
