# Design — Verbatim-only credential convention; close check_config's DB-override pricing gap

**Date:** 2026-08-16
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-08-15-operational-config-split-design.md` §9
(the "Verbatim-only credential convention — decided, deferred to its own spec" item this
design closes, and the "one-secret-per-file" item this design keeps cheap but does not
build), `docs/superpowers/specs/2026-08-16-model-pricing-validation-design.md`'s Non-goals
(the "validating an active DB model override against the pricing table" residual this
design also closes), `app/providers/registry.py`, `app/providers/vertex_credentials.py`,
`app/providers/credentials.py`, `app/github_app.py`, `app/config.py` (`Settings`),
`scripts/deploy.py` (`check_config`, `_private_key_b64`, `_resolved_model_overrides`),
`scripts/_override.py`, `CLAUDE.md`'s Secret handling section.

## 1. Problem

Two unrelated gaps, bundled into one spec because they're both small, both already fully
scoped by prior design work, and doing them together means one implementation/review pass
instead of two.

### 1a. One logical credential, multiple shapes

A credential var today holds either the credential material itself (`GEMINI_API_KEY`,
`GROQ_API_KEY`) or a **path to a file containing it** (`GITHUB_APP_PRIVATE_KEY_PATH`,
`GCP_SERVICE_ACCOUNT_KEY_PATH`), with a base64 sibling for the hosted case
(`GITHUB_APP_PRIVATE_KEY_B64`, `GCP_SERVICE_ACCOUNT_KEY_B64[_n]`). One logical vertex
credential thus spans up to six env vars. This is three different shapes for "a
credential," which `vertex_credentials.py`'s own docstring already cites as its reason to
exist as a module separate from `credentials.py`. Whether a value is base64-encoded is a
fixed property of the credential *type* (a PEM or a JSON key always needs it — `.env`
cannot hold multiline values; an API key never does), not a per-deployment choice, so
encoding it into every var name (`_B64`) is redundant labeling, not information.

### 1b. check_config reports PASS on a runtime-active unpriced model

`scripts/deploy.py`'s `check_config()` validates `.env.config` model values against
`app/providers/pricing.py` (landed in the 2026-08-16 model-pricing-validation design), but
never resolves an active DB model override first — unlike its sibling `check_provider()`,
which does resolve the DB provider override before reporting. `set_override.py --model X
--force` can put a genuinely unpriced model into live rotation; `check_config` still
reports the `config` row `PASS` in that state, because it only ever looked at
`Settings`/`.env.config`, never at what `active_model()` would actually resolve to at
runtime. `sync_env()`'s existing model-override-disagreement guard still prevents *pushing*
a conflicting value while such an override is active, so this is a local reporting blind
spot, not a live-safety hole — but a misleadingly green `config` row is exactly the kind of
gap this project's checklist exists to not have.

## 2. Decision

### 2a. Naming

| Old | New |
|---|---|
| `GITHUB_APP_PRIVATE_KEY_B64` | `GITHUB_APP_PRIVATE_KEY` |
| `GITHUB_APP_PRIVATE_KEY_PATH` | *(deleted)* |
| `GCP_SERVICE_ACCOUNT_KEY_B64[_n]` | `GCP_SERVICE_ACCOUNT_KEY[_n]` |
| `GCP_SERVICE_ACCOUNT_KEY_PATH[_n]` | *(deleted)* |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | unchanged (already verbatim, already the target shape) |

`Settings` fields follow the same rename: `github_app_private_key_b64` →
`github_app_private_key`; `gcp_service_account_key_b64` → `gcp_service_account_key`.
`Settings.github_app_private_key_path` and `Settings.gcp_service_account_key_path` are
deleted, not deprecated.

No registry-level "is this base64" flag is added. Nothing today reads such a flag
generically — `github_app.py` and `vertex_credentials.py` each already hardcode their own
correct base64-decode step, and gemini/groq need no decoding at all. Adding one would
relocate a boolean without eliminating any hardcoding. Skipped per YAGNI; revisit only if a
third b64-shaped credential arrives and genuinely needs shared decode logic.

### 2b. Code deletions

- **`app/providers/vertex_credentials.py`**: `_local_path()` and its call in
  `resolve_service_account_info()` are deleted. The function becomes: resolve via
  `credentials.resolve("vertex", index)` (now reading `GCP_SERVICE_ACCOUNT_KEY[_n]`),
  base64-decode, parse JSON, else `None` for implicit ADC. The three-layer resolution
  (env → local file → ADC) becomes two layers (env → ADC) — ADC itself is unaffected,
  since an empty var already meant implicit ADC and still does.
- **`scripts/deploy.py`**: `_private_key_b64()` collapses to
  `settings.github_app_private_key, ""` inline at its one call site in
  `check_config()`/`_wanted_env()` (or stays as a one-line wrapper if that reads more
  clearly at implementation time — a plan-level call, not a design-level one). The
  "unreadable PEM" `problems` branch in `check_config()` is deleted along with it: with no
  file path to fail to read, that failure mode no longer exists.
- **`app/github_app.py::_read_private_key()`**: loses its file-path branch entirely —
  becomes `base64.b64decode(settings.github_app_private_key).decode()`, one line, no
  `Path`/`.is_absolute()`/`.read_text()` machinery left in this function. An empty value
  surfaces downstream as whatever `Auth.AppAuth` does with an empty key string — the same
  "let it fail structurally, don't add a validation the rest of the codebase doesn't have
  either" posture `check_config()` already takes for other missing-credential cases (it
  reports `GITHUB_APP_PRIVATE_KEY` as `missing`, same as today, just under the new name).

### 2c. `registry.PROVIDERS`

One-line change: `"vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL")`. Every caller that
derives numbered-slot names goes through `registry.slot_env_name()`, so this one edit
propagates correctly to `credentials.resolve()`, `scripts/_override.py`, and
`scripts/set_override.py --list` with no further changes.

### 2d. `scripts/encode_credential.py` (new)

```
uv run python -m bot.scripts.encode_credential path/to/file.pem
```

Reads the file, prints its base64 form to stdout, nothing else — no flags, no file
writing, no network calls. Works identically for a PEM or a JSON key (base64 doesn't care
about content shape), so it replaces both the PEM-specific `base64 -w0` one-liner SETUP.md
§3.3 already documents and the equivalent manual step for a GCP key file.

**This is a human-run tool, not an agent-run one.** Its docstring says so explicitly: an
agent must never invoke it against a real credential file, because doing so would print
secret-derived bytes into its own tool output — precisely the failure mode CLAUDE.md's
Secret handling section exists to prevent, and precisely the class of action that section
already reserves for the user ("if you ever need to know or verify a secret's actual value,
ask the user to check it themselves"). The script's existence doesn't change who's allowed
to run it against real material.

### 2e. `check_config()`'s effective-model resolution

New helper (or direct reuse — a plan-level call) of `deploy.py`'s existing
`_resolved_model_overrides()`, which already reads all three model-override columns in one
connection. `check_config()` gains, alongside its existing provider/credential check:

```python
overrides = {}
if settings.database_url:
    try:
        overrides = _resolved_model_overrides()
    # deliberate: DB trouble degrades to a local-only check, never a crash --
    # mirrors every other DB-touching check in this file
    except Exception:  # noqa: BLE001
        overrides = {}
for provider, (_credential, model_var) in _PROVIDERS.items():
    local_model = getattr(settings, model_var.lower(), "")
    effective_model = overrides.get(provider) or local_model
    if effective_model and not pricing.is_known(provider, effective_model):
        known = ", ".join(pricing.models_for(provider)) or "(none known for this provider)"
        source = "DB override" if overrides.get(provider) else model_var
        problems.append(
            f"{provider} model {effective_model!r} ({source}) has no pricing-table "
            f"entry (known: {known})"
        )
```

No `DATABASE_URL` set → `overrides` stays `{}`, behavior is identical to the
2026-08-16 model-pricing-validation design's original local-only check. DB unreachable →
same degrade, not a failure of the `config` row for an unrelated reason (consistent with
`check_provider()`'s own `except Exception` degrade one function up). This closes the named
residual precisely: a `--force`'d unpriced DB override now makes `config` report `FAIL`,
naming the override as the source.

`sync_env()` needs no change here — its existing model-override-disagreement guard already
governs what gets *pushed*; a DB override's own pricing is orthogonal to that push and was
never sync_env's gap to begin with (see the parent spec's Non-goals entry: "the push path is
not fully exposed" — it already wasn't the exposed part).

## 3. Migration

Still user-only — an agent must never open `.env`. Steps:

1. Rename `GITHUB_APP_PRIVATE_KEY_B64` → `GITHUB_APP_PRIVATE_KEY` and
   `GCP_SERVICE_ACCOUNT_KEY_B64[_n]` → `GCP_SERVICE_ACCOUNT_KEY[_n]` in `.env`.
2. For any local setup currently relying on `GITHUB_APP_PRIVATE_KEY_PATH` or
   `GCP_SERVICE_ACCOUNT_KEY_PATH[_n]` (a raw file, not yet base64'd): run
   `scripts/encode_credential.py` (or the existing `base64 -w0` one-liner) against that
   file yourself, and paste the result into the renamed var.
3. Remove `GITHUB_APP_PRIVATE_KEY_PATH` and `GCP_SERVICE_ACCOUNT_KEY_PATH[_n]` from `.env`
   entirely — they're not read anymore, and instructing on their removal is what surfaces
   a stale reliance on them rather than letting it silently stop working.

**A new placement-guard test**, `test_no_legacy_credential_var_lives_in_the_secrets_file`
(mirroring `test_no_operational_key_lives_in_the_secrets_file`'s role as a migration
checklist): reads `.env`'s key names only (`^[A-Z_0-9]+=`, values discarded) and fails if
any of the four retired names (`GITHUB_APP_PRIVATE_KEY_B64`, `GITHUB_APP_PRIVATE_KEY_PATH`,
`GCP_SERVICE_ACCOUNT_KEY_B64`, `GCP_SERVICE_ACCOUNT_KEY_PATH`) — or their numbered
siblings, matched by the same `_(\d+)$` pattern `_override.py` already uses — are still
present. Expected red between landing this work and completing the migration, same as the
operational-config-split precedent; that's the intended signal, not a defect.

## 4. Testing

- **Renames**: every existing test referencing `github_app_private_key_b64`,
  `github_app_private_key_path`, `gcp_service_account_key_b64`, or
  `gcp_service_account_key_path` (across `test_config.py`, `test_github_app.py`,
  `test_provider_registry.py`, `test_vertex_credentials.py`, `test_deploy_script.py`)
  updated to the new field/var names.
- **Deletions**: tests exercising the now-deleted file-path fallback (local-file
  resolution in `vertex_credentials.py`, the unreadable-PEM branch in `check_config`, the
  file-path branch in `_read_private_key`) are removed, not left as dead/xfail.
- **`registry.PROVIDERS["vertex"]`**: existing `slot_env_name` tests continue to pass
  unchanged (the function itself doesn't change) but with the new base name.
- **`encode_credential.py`**: given a fixture file with known bytes, its base64 output
  matches `base64.b64encode` of the same bytes; prints nothing else to stdout.
- **Migration guard**: detects each of the four retired names (including a numbered
  sibling) present in a fake `.env`; passes when none are present; skips cleanly when
  `.env` doesn't exist.
- **`check_config`'s effective-model check**: no `DATABASE_URL` → local-only check
  (existing behavior, regression-guarded); DB override present and priced → `PASS`; DB
  override present and unpriced → `FAIL` naming the override as the source (the core
  regression test for item 1b); DB read raises → degrades to local-only check rather than
  failing the row for an unrelated reason.

No live calls: every behavior here is deterministic and mockable, consistent with
`SPEC.md` §8 and `CLAUDE.md`'s LLM-testing-hygiene rules — nothing in this design makes an
LLM call.

## 5. Docs

- `.env.example`: credential section rewritten to the new names, `_PATH` lines and their
  commentary removed, `encode_credential.py` mentioned as the base64 step.
- `README.md`: the four locations naming `GITHUB_APP_PRIVATE_KEY_B64` / the
  `GCP_SERVICE_ACCOUNT_KEY_B64`/`_PATH` trio updated to the new names and two-layer
  (env → ADC) resolution.
- `SETUP.md`: §2b's three-layer vertex credential description becomes two layers; §3.3
  "Secrets encoding" points at `encode_credential.py` (keeping the raw `base64 -w0`
  one-liner as an equally-valid alternative, not removing it); the always-synced-vars
  mention updated to the new name.
- `CLAUDE.md`: the Secret handling section's `grep` example (currently
  `GCP_SERVICE_ACCOUNT_KEY_B64`) updated to `GCP_SERVICE_ACCOUNT_KEY`; the Substitutions
  section's vertex entry drops its "→ a local key file →" clause (no longer a resolution
  layer) and reflects the completed rename.
- `docs/superpowers/specs/2026-08-15-operational-config-split-design.md` §9's
  "Verbatim-only credential convention" bullet gets a one-line addendum pointing at this
  doc as its resolution, same convention as the model-pricing-validation spec's own
  addendum to that file.

## 6. Non-goals

- **One-secret-per-file.** Still deferred — this design keeps it cheap to adopt later (the
  rename doesn't touch `slot_env_name()`'s seam, which is what makes that future change a
  one-module edit rather than a sweep) but does not build it. The parent spec's own
  interaction note stands: whichever of the two is designed second should account for the
  other; this design is the first of the pair, so nothing here forecloses it.
- **A registry-level encoding flag.** Decided against in §2a; revisit only if a third
  b64-shaped credential needs shared decode logic that two hardcoded call sites don't
  already provide adequately.
- **Changing how ADC (implicit ADC via `gcloud auth application-default login`) works.**
  Untouched — an empty `GCP_SERVICE_ACCOUNT_KEY` still means "fall through to ADC," exactly
  as an empty `GCP_SERVICE_ACCOUNT_KEY_B64` did before.
- **`sync_env()` changes for item 1b.** Explicitly not needed — see §2e's last paragraph.
