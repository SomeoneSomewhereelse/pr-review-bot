# Design — Reinstating the Vertex AI provider

**Date:** 2026-08-13
**Status:** Approved for planning
**Updated 2026-08-14:** dropped the DB-stored credential-blob override and
the `CLAUDE.md` "secrets only via env vars" exception it required. Vertex's
credential now reuses the existing numbered-env-var-slot + key-index
mechanism unchanged, exactly like gemini/groq — see §2 and §7 for why.
**Relates to:** `app/providers/google_genai.py` (where `GeminiProvider` lives
and where `VertexProvider` joins it), `app/providers/factory.py` /
`registry.py` / `credentials.py` / `key_index.py` (the existing provider
seam this extends, unchanged), `app/github_app.py::_read_private_key` (the
precedent for a b64-env-var / local-file credential split),
`docs/superpowers/specs/2026-08-12-api-key-index-override-design.md` (the
key-index pattern this reuses as-is, including its explicit prior rejection
of storing a secret in `runtime_config` — see §2), `CLAUDE.md`'s
"Substitutions from the brief" rule (updated; "secrets only via env vars" is
**not** touched by this design).

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

The credential must also be swappable without a Render redeploy, matching
what gemini/groq already have. An earlier version of this design chased a
*stronger* guarantee — zero restart even for a credential Render has never
seen before — by storing the credential value itself in `runtime_config`.
That's unnecessary: the existing numbered-slot pattern already gives
restart-free swapping among pre-provisioned credentials, and a first-time
credential costing one restart to provision is an already-accepted cost for
gemini/groq (see §2). Nothing about Vertex needs a stronger guarantee than
that, so this design doesn't introduce one.

## 2. Decision

Three credential sources, checked in priority order — no DB-stored
credential value anywhere:

1. **`GCP_SERVICE_ACCOUNT_KEY_B64`** env var, with numbered siblings
   `_1`, `_2`, ... on Render — the production path, resolved through the
   *existing, unmodified* `credentials.resolve("vertex", index)` and
   selected via the *existing, unmodified* key-index override
   (`vertex_key_index` — a new column, but the same generic mechanism
   gemini/groq already use). Swapping among slots already provisioned on
   Render is a pure DB-integer write, no restart. Provisioning a
   never-before-seen credential still costs one restart, to add the new
   Render env var — the exact tradeoff already accepted for
   `GEMINI_API_KEY_1`/`GROQ_API_KEY_1`, not a new one.
2. **Local file** at `GCP_SERVICE_ACCOUNT_KEY_PATH` (default
   `./gcp-service-account-key.json`, gitignored), with numbered siblings
   `GCP_SERVICE_ACCOUNT_KEY_PATH_1`, `_2`, ... — **also** selected by the
   same `vertex_key_index`. Local-dev-only, for testing against several
   different service accounts (e.g. a quota-exhausted one vs. a healthy
   one) without touching Render or Supabase at all. Mirrors
   `GITHUB_APP_PRIVATE_KEY_PATH`'s local-file precedent — nobody hand-pastes
   a multi-KB blob into a local `.env` in this project; the file stays a
   file.
3. **Implicit ADC** — if neither of the above resolves to a value at the
   active index, no explicit `credentials` object is passed to the client at
   all, and `google-auth` discovers `gcloud auth application-default
   login`'s local ADC file on its own. Zero-config local fallback.

One index, two different real meanings depending on environment: on Render,
`vertex_key_index` selects among numbered **env-var blobs**; locally (where
those numbered env vars are typically never exported), it falls through to
selecting among numbered **local files** instead. Both paths reuse
`credentials.resolve()`'s and `key_index.py`'s existing code unchanged — no
new override cache, no new `runtime_config` column beyond the index itself,
no CLI changes (§7).

**Why not store the credential value in `runtime_config` for a stronger
"zero restart, even for a brand-new credential" guarantee?**
`docs/superpowers/specs/2026-08-12-api-key-index-override-design.md`
considered exactly this for gemini/groq and rejected it: *"A naive mirror of
the existing pattern would store the key value itself in the `runtime_config`
Postgres row. That works mechanically but is a real regression against
`CLAUDE.md`'s 'secrets only via env vars' rule — it would make Supabase DB
access equivalent to key access, for no offsetting benefit."* Nothing about
Vertex's credential changes that calculus — it's a larger blob, not a
different risk profile — so the same conclusion applies: store an index, not
a secret, and accept the one-restart cost of provisioning a genuinely new
credential, exactly as already accepted for the other two providers.

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
string" shape and stays that way; this module adds the local-file fallback
and the JSON parsing on top, without complicating `credentials.py` for the
other two providers that don't need either.

```python
"""Resolves the Vertex service-account credential: GCP_SERVICE_ACCOUNT_KEY_B64
(env, index-aware) -> local file (index-aware) -> None (implicit ADC). Never
logged -- same discipline as github_app.py::_read_private_key for the
equivalent GitHub credential.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from app.config import settings
from app.providers import credentials


def _local_path(index: int) -> str:
    if index == 0:
        return settings.gcp_service_account_key_path
    return os.environ.get(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", "")


def resolve_service_account_info(index: int) -> dict | None:
    _, b64 = credentials.resolve("vertex", index)
    if b64:
        return json.loads(base64.b64decode(b64).decode())
    path = _local_path(index)
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text())
    return None
```

`credentials.resolve("vertex", index)` is the existing, unmodified function:
at index 0 it reads `settings.gcp_service_account_key_b64` (the
`base.lower()` convention already used for every provider); at index ≥ 1 it
reads `GCP_SERVICE_ACCOUNT_KEY_B64_{index}` from `os.environ` — meaningful
now, since these slots are meant to actually be provisioned on Render (§2),
unlike the earlier draft of this design where they were never used.

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

If credentials are genuinely missing in every environment (no env b64, no
local file, no ADC), `genai.Client(...)`/`google-auth` raises at
construction. That flows into the existing generic
`asyncio.gather(..., return_exceptions=True)` → failed-specialist-row path —
no new error handling needed there.

## 7. Schema (`app/queue/store.py`)

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_key_index INTEGER;
```

One column, needing no new store functions — it's a fourth entry under the
already-generic `KEY_INDEX_COLUMNS` machinery (`get_key_index_override`,
`set_key_index_override`, `get_all_key_index_overrides` all already iterate
the dict, not a hardcoded provider list), and needs no dispatcher change
either — the existing `get_all_key_index_overrides` refresh block already
covers it.

No `runtime_config.vertex_service_account_b64` column, no new store
functions, no new override cache, no fourth dispatcher refresh block, no
`scripts/set_override.py` changes: `--index`/`--clear-index` already work
for vertex exactly as they do for gemini/groq, because `vertex` is now just
a third entry in `KEY_INDEX_COLUMNS` and `registry.PROVIDERS`. The existing
`_override.verify_render_slot` check (run by `set_override.py` before
writing an index) checks Render for `GCP_SERVICE_ACCOUNT_KEY_B64_{index}`
and compares it to a local counterpart if one exists — for a locally
file-based setup there typically won't be one, and `verify_render_slot`
already handles that gracefully ("present on Render (no local value to
compare)"), the same message a numbered gemini/groq slot gets when there's
no matching local `.env` entry either.

## 8. CLAUDE.md

Only the "Substitutions from the brief" Vertex bullet changes — from
"removed" to "reinstated once GCP billing/ADC access became available."
**"Secrets only via env vars; no secret is ever logged" is unchanged** —
this design introduces no exception to it.

## 9. Docs

- **`CLAUDE.md`** — "Substitutions from the brief" Vertex bullet rewritten
  (reinstated, not removed). No other change.
- **`SETUP.md`** §2 — add a dated update recording the constraint lift and
  the ADC setup, keeping the existing removal history intact above it
  (matching this file's existing pattern of dated updates rather than
  rewriting history).
- **`README.md`** — "Known limitations" Vertex bullet updated to describe it
  as live. The existing "Swapping API keys without a redeploy" section gains
  Vertex as a third example (numbered `GCP_SERVICE_ACCOUNT_KEY_B64_n` /
  `GCP_SERVICE_ACCOUNT_KEY_PATH_n` slots) rather than a new section — it's
  the same mechanism, not a new one.
- **`SPEC.md`** — already describes vertex as the default provider; needs no
  correction, only confirmation it still matches (the `runtime_config`
  override section gains the one new column in its listing).
- **`cost.md`** — replace the hypothetical "$300 GCP trial credit" costing
  with a real per-token rate entry once one is looked up (see §11).
- **`.env.example`** — `LLM_PROVIDER` comment gains `vertex`; new
  `GCP_PROJECT`, `GCP_LOCATION`, `GCP_SERVICE_ACCOUNT_KEY_B64` (+ commented
  `_1`/`_2` siblings, matching the existing `GEMINI_API_KEY_1` pattern),
  `GCP_SERVICE_ACCOUNT_KEY_PATH` (+ commented `_1`/`_2` siblings) entries,
  following the existing path-for-local/b64-for-hosted comment style used
  for the GitHub App key.
- **`.gitignore`** — add `gcp-service-account-key.json` (the default local
  filename), alongside the existing `*.pem` entry.

## 10. Surface

- `app/config.py` — 4 new settings.
- `app/providers/base.py` — `KNOWN_PROVIDERS` gains `"vertex"`.
- `app/providers/registry.py` — `PROVIDERS` and `KEY_INDEX_COLUMNS` gain a
  vertex entry each.
- `app/providers/google_genai.py` — `VertexProvider`, sharing `_complete()`.
- `app/providers/vertex_credentials.py` — new; `resolve_service_account_info`.
- `app/providers/factory.py` — vertex branch in `_build`, bypassing the
  generic empty-credential fast-fail.
- `app/queue/store.py` — 1-column migration (`vertex_key_index`); no new
  functions.
- `app/providers/pricing.py` — new `("vertex", "gemini-flash-latest")` rate
  entry.
- `CLAUDE.md`, `SETUP.md`, `README.md`, `SPEC.md`, `cost.md`,
  `.env.example`, `.gitignore` — per §9.
- `tests/test_providers.py`, `tests/test_deploy_script.py`,
  `tests/test_set_override_script.py` — see §11, rewriting the four existing
  "vertex is rejected" assertions.

## 11. Testing (deterministic-first, per `CLAUDE.md`'s LLM testing-hygiene rule)

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
  priority-chain layer: env b64 present (index 0 and index ≥ 1) → decoded;
  env b64 absent + local file present → read and parsed; neither present →
  `None`; malformed b64/JSON at either layer surfaces as a clear error, not
  a silent fallthrough.
- `factory.py` — vertex branch returns a `VertexProvider`; missing
  `GCP_PROJECT` raises before any credential resolution or network call;
  an empty resolved credential (no env/file/ADC signal locally mocked) does
  **not** raise from `_build` itself (the raise, if any, comes from the
  mocked SDK client construction, not from `_build`'s own check) — this is
  the one behavioral difference from gemini/groq worth a dedicated test.
- `google_genai.py::VertexProvider` — mocked `genai.Client`; confirms
  `credentials=None` when `service_account_info` is `None`, and a real
  `service_account.Credentials` object when it isn't.
- `store.py` — schema migration adds `vertex_key_index` to a pre-existing
  table; round-trips through the existing generic
  `get_key_index_override`/`set_key_index_override`.
- **One live-verification script**, `scripts/manual_verify_vertex.py`,
  mirroring `scripts/manual_verify_step4.py` — a single deliberate call
  confirming real structured output against actual Vertex, run once per
  `CLAUDE.md`'s "one deliberate call per real verification need" rule, not
  repeated or looped.

## 12. Non-goals

- No credential value stored in `runtime_config` or anywhere in Supabase —
  see §2 for why that guarantee isn't needed and would regress the
  secrets-only-via-env-vars rule for no offsetting benefit.
- No new CLI flags and no changes to `scripts/set_override.py` — vertex
  rides the existing `--index`/`--clear-index` machinery unchanged.
- No automatic credential rotation, expiry tracking, or scheduling — a human
  runs `set_override.py vertex --index N` when they decide to swap, same
  manual-trigger model as gemini/groq.
- No change to how gemini/groq resolve credentials — this design only adds
  a new branch alongside the existing ones.
