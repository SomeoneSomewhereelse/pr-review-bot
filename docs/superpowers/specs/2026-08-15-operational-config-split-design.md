# Design — Split operational config out of `.env`; make every non-secret setting agent-changeable

**Date:** 2026-08-15
**Status:** Approved for planning
**Relates to:** `CLAUDE.md`'s "Secret handling" section (the rule that creates this gap and
the new "to change state, reach for the CLI" rule that this design makes true),
`ISSUES.md`'s two most recent entries (the exposures that produced that section),
`app/config.py` (`Settings`), `app/providers/registry.py` (`PROVIDERS`,
`KEY_INDEX_COLUMNS`), `app/providers/factory.py` (instance cache),
`app/providers/key_index.py` + `app/queue/cooldown_config.py` (the override-cache pattern
this mirrors), `scripts/deploy.py` (`_wanted_env`, `sync_env`), `scripts/_override.py`,
`scripts/set_override.py`, `scripts/set_cooldown.py`, `render.yaml`.

## 1. Problem

`CLAUDE.md`'s "Secret handling" section — added after three real secret exposures into
conversation transcripts, including a `grep` that printed a full GCP service-account key
and a harness "file changed externally" notification that dumped **every credential in the
project** with no command run at all — now forbids an agent from opening `.env` at all, for
any reason, including a single "safe" line.

`.env` mixes two unrelated categories in one file:

- **credentials and identity** — `GEMINI_API_KEY`, `GROQ_API_KEY*`, `DATABASE_URL`
  (password embedded in the connection string), `RENDER_API_KEY`,
  `UPTIMEROBOT_API_KEY`, `GCP_SERVICE_ACCOUNT_KEY_B64`, `GITHUB_WEBHOOK_SECRET`;
- **plain operational config** — `LLM_PROVIDER`, `LLM_MODEL`, `GROQ_MODEL`,
  `KEY_USAGE_TOKEN_CAP`, `KEY_USAGE_COST_CAP_USD`, `KEY_USAGE_RESET_TIME_UTC`.

So the rule, correctly, makes routine non-secret config changes impossible for an agent.
During the `test-usage-limit` session, switching the active provider (`groq` → `vertex`),
changing its model, and tuning usage caps for a live test each required a hand-edit of
`.env` followed by `scripts/deploy.py --sync-env` (which sources `_wanted_env()` from local
`Settings`, itself sourced from `.env`) and a full redeploy. Every one of those changes now
has to be delegated to the user.

Three findings during design sharpened the problem beyond "the file needs splitting":

1. **A latent correctness bug.** `registry.PROVIDERS` maps **both** `gemini` and `vertex`
   to the same model var `LLM_MODEL`, while `.env.example:73-77` records that the default
   `gemini-flash-latest` **does not exist** in Vertex's catalog (404; `gemini-2.5-flash` is
   the confirmed-working value). So `set_override.py vertex` — the redeploy-free provider
   flip that already exists — is *guaranteed broken* unless `LLM_MODEL` changes too, which
   today means editing `.env`, running `--sync-env`, and waiting out a redeploy. This is the
   mechanism that forced the `test-usage-limit` session into `.env` in the first place.
   `config.py:19-23` already records the reasoning for splitting `GROQ_MODEL` off — that a
   single shared `LLM_MODEL` "became ambiguous the moment a second provider family entered
   the picture." The gemini/vertex half of that split was simply never finished.

2. **The house pattern already exists and was never extended.** `scripts/set_override.py`
   (provider + key-slot index) and `scripts/set_cooldown.py` (cooldown base/cap/factor) are
   DB-backed overrides with env fallback, applied at runtime with **no redeploy and no
   `.env` edit**. They are agent-runnable today because they read credentials
   programmatically through `Settings` and print names, lengths, and equality results only.
   *Model* and *usage caps* are the two settings that never got that treatment — which is
   exactly the pair the live test needed.

3. **Slot discovery loads secrets to answer a names question.**
   `_override.local_numbered_slots()` calls `dotenv_values(".env")` and returns
   `{name: value}` — every local credential live in memory — purely to answer "which slots
   exist?". `deploy.py:606` genuinely needs the values; `local_value()` and every
   verification path do not. And because there is no inventory anywhere, an agent cannot
   answer "is `--index 2` valid?" without opening `.env`, leaving `set_override.py --index`
   half-blind for precisely the actor the CLI exists to serve.

## 2. Decision

Four pieces, landing together:

1. **Split the file.** `.env` holds credentials and identity; a new `.env.config` holds
   operational settings; `Settings` reads both. An explicit allowlist in code defines the
   boundary, guarded by tests.
2. **Extend the DB-override layer** to per-provider model and to the usage-cap trio, so the
   settings that change during a live demo change without a redeploy — and so a provider
   flip carries its model with it.
3. **Finish the model-var split** (`VERTEX_MODEL`), so each provider owns its model at
   *both* the env layer and the override layer.
4. **Make credential-slot discovery value-free**, and give the slot-naming scheme a single
   seam.

Rejected during design, recorded so they are not re-litigated:

- **A declared slot manifest** (`GROQ_API_KEY_SLOTS=0,1,2` in `.env.config`). Unnecessary:
  slot enumeration is already answerable from **names alone** on both sides — `os.environ`
  keys on Render, `.env` keys locally. A manifest would add a second source of truth that
  can drift from reality. Loading values to enumerate is an implementation artifact, not an
  inherent requirement.
- **A `scripts/set_config.py` that writes `.env.config`.** Its only purpose was to avoid
  opening a dangerous file; a provably-safe file removes that purpose. Agents edit
  `.env.config` with `Edit` like any other plain file.
- **A single unified `scripts/config.py`** fronting both file and DB. It would hide a real
  semantic difference — a file change needs a redeploy, a DB override does not — behind one
  surface, and it departs from the per-concern script layout used everywhere else.
- **Named slots** (labels instead of integer indices). A label still resolves to a
  credential var name, so it decouples nothing the numbering doesn't, while churning the DB
  schema, the CLI, and every doc that says "index".
- **A single active-scoped model override** (one column applied to whichever provider is
  active). Every provider flip would need the model re-set in the same breath — the exact
  coupling that caused this incident, relocated from `.env` into the DB.
- **Sync-everything or secrets-only `--sync-env`.** See §5.

## 3. File split and config sourcing

| File | Holds | Git | Agent may open |
|---|---|---|---|
| `.env` | credentials + identity | ignored | **never** |
| `.env.config` | operational settings | ignored | yes, freely |
| `.env.config.example` | committed template, carrying the commentary currently in `.env.example`'s operational sections | tracked | yes |

`app/config.py:8` becomes:

```python
model_config = SettingsConfigDict(env_file=(".env", ".env.config"), extra="ignore")
```

Verified behavior (pydantic-settings 2.14.2, pydantic 2.13.4): both files are merged, **the
last file wins** on a key present in both, and a real process env var beats both. That last
property is why Render is unaffected — neither file exists in the container; env vars are
injected directly.

Nothing else in the app changes: every module still reads `settings.<field>`, and
`_wanted_env()` keeps working untouched because it sources from `Settings`, not from a file.
`app/providers/credentials.py`'s direct `.env` read is unaffected — it reads *credentials*,
which stay in `.env`.

### 3.1 The allowlist

A module-level `OPERATIONAL_KEYS` frozenset in `app/config.py`, whose docstring states the
rule: **listed = operational; everything else is secret by default.**

Initial contents:

- `LLM_PROVIDER`, `LLM_MODEL`, `GROQ_MODEL`, `VERTEX_MODEL`
- `KEY_USAGE_TOKEN_CAP`, `KEY_USAGE_COST_CAP_USD`, `KEY_USAGE_RESET_TIME_UTC`
- `GCP_PROJECT`, `GCP_LOCATION`
- `LLM_REQUEST_TIMEOUT_SECONDS`, each `DISPATCHER_*` setting, `RENDER_SERVICE_NAME`
- `GITHUB_TARGET_REPO`
- `PUBLIC_BASE_URL`

Every entry is a **literal key name**, enumerated one by one — never a prefix or glob
pattern. A pattern would silently classify future keys that happen to match, which is
exactly the secret-by-default guarantee this list exists to provide.

`PUBLIC_BASE_URL` earns its place on a stronger argument than "non-secret": a `/healthz`
check needs **only the URL and no credential at all**, so with it in `.env.config` an agent
can verify a deploy is up without touching `.env` even once. `RENDER_API_KEY` stays in
`.env` and stays optional — its absence degrades a check to SKIPPED, never to an error, as
`config.py:88-90` documents.

Deliberately **excluded**, as explicit calls rather than oversights: `GITHUB_APP_ID`
(non-secret but identity-shaped and near-never changed — secret-by-default keeps it put),
and `GCP_SERVICE_ACCOUNT_KEY_PATH` / `GITHUB_APP_PRIVATE_KEY_PATH` (paths that *point at*
credentials; both are slated for deletion by the §9 credential-convention follow-up, which
makes their classification moot rather than wrong). Each is a one-line addition later if it
chafes.

**The allowlist is a classification, not a move list.** It states where a key belongs; it
forces migration only for keys actually present in `.env` today.

### 3.2 Guards

Two tests in `tests/test_config.py`:

1. **Placement.** Read both files' **key names only** (`^[A-Z_0-9]+=`, values discarded —
   the shape `CLAUDE.md` mandates) and fail if an operational key appears in `.env`, or a
   non-allowlisted key appears in `.env.config`. Both files are gitignored, so the check
   skips cleanly when a file is absent (CI, fresh clone). Failure output names keys only.
2. **Allowlist integrity.** Every `OPERATIONAL_KEYS` entry lowercases to a real `Settings`
   field, so a typo cannot classify a key that does not exist.

Enforcement is test-time, not runtime: a drifted local file must never brick the deployed
service.

### 3.3 Finishing the model-var split

`registry.PROVIDERS["vertex"]` becomes `("GCP_SERVICE_ACCOUNT_KEY_B64", "VERTEX_MODEL")`
— the credential name is left exactly as it is today on purpose; §9's follow-up renames it
to `GCP_SERVICE_ACCOUNT_KEY`, and doing that here would drag a credential migration into a
plan that is otherwise operational-config-only —
and `Settings` gains a `vertex_model` field (default in §5). This completes the split whose
reasoning `config.py:19-23` already records for `GROQ_MODEL`, and it is what makes each
provider own its model at the **env** layer, not only at the override layer — without it,
`--sync-env` under `provider=vertex` pushes a Vertex model into the shared `LLM_MODEL`, and
a later DB flip to `gemini` reads that Vertex model.

`orchestrator._active_model()`'s hardcoded `if provider == "groq"` branch
(`orchestrator.py:49-52`) disappears entirely: with each provider's model var in the
registry, resolution is a registry lookup for all three, delegated to §4.2's
`active_model()`.

## 4. The DB-override layer

### 4.1 Schema

Six nullable columns on `runtime_config`, added with the same idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` DDL already used at `store.py:55-60` — no
migration framework:

`gemini_model`, `groq_model`, `vertex_model` (TEXT); `key_usage_token_cap` (INTEGER);
`key_usage_cost_cap_usd` (DOUBLE PRECISION); `key_usage_reset_time_utc` (TEXT, `HH:MM`
or `HH:MM:SS`).

Per-provider model columns are reached through a hardcoded whitelist dict in `registry`,
mirroring `KEY_INDEX_COLUMNS`' documented role as "the injection guard for those callers" —
every statement looks the column name up through the dict rather than building it from a
caller's `provider` string.

### 4.2 `app/providers/active_model.py`

`active_model(provider) -> str`, mirroring `key_index.py` exactly: import-light, no
DB imports, cache pushed in via `set_override_cache`, and fail-safe by construction — an
empty cache degrades to the env value per `registry.PROVIDERS`, never to a crash or an empty
model.

It becomes the **single** model resolver. This part is load-bearing for correctness, not
tidiness:

- `google_genai.py:66,106` and `groq.py:63` currently set `self._model = settings.llm_model`
  in `__init__`, and `factory._instances` caches instances by `(provider, index)` **for the
  process lifetime**. A DB model override would therefore silently no-op on a warm process.
- Worse, `orchestrator._active_model()` (`orchestrator.py:110`, which feeds the PR comment)
  would report the *new* model while the call used the *old* one — a silent divergence
  between reported and executed behavior.

So: `factory._build()` resolves the model and passes it into the adapter constructor;
`_instances` is keyed by **`(provider, index, model)`**, making a model-override change a
cache miss and a fresh instance — the same mechanism `factory.py`'s docstring already relies
on for key swaps; and `orchestrator._active_model()` delegates to `active_model()`, so
reported and executed model cannot diverge.

### 4.3 `app/queue/usage_cap_config.py`

`effective_caps() -> tuple[int | None, float | None, time]`, mirroring `cooldown_config.py`
including its **all-or-nothing** rule: an override that reads back invalid is discarded as a
whole triple, never partially applied, so a bad field can never pair with a stale one.
`dispatcher.py:216-230` reads through it instead of `settings.key_usage_*` directly.

### 4.4 Dispatcher refresh

Two more blocks in the per-ticket refresh loop at `dispatcher.py:157-184`, with the
identical fail-safe shape already used there — `logger.exception` plus
`reset_override_cache()` on failure, degrading to env defaults rather than to no cap.

### 4.5 CLI

- **`scripts/set_override.py --model <name>` / `--clear-model`**, folded into the existing
  write-ordering discipline: index → model → provider activation, so a partial failure never
  leaves a provider active against a stale model. Verification and `--force` semantics
  unchanged.
- **`scripts/set_override.py --list`** — per provider: which slots are populated locally,
  which are present on Render, the active index, the active model. **Names and booleans
  only**, per `_render.py::env_vars()`'s contract. This is what lets an agent validate
  `--index 2` without opening `.env`.
- **`scripts/set_usage_cap.py`** (new) — `--tokens / --cost / --reset / --clear`, modeled on
  `set_cooldown.py`: `allow_abbrev=False`, a Render-reachability notice that never refuses
  the write, and merged-group validation against env defaults **before** writing. That
  validation matters more here than for cooldown: `config.py:78-83` records that a bad cap
  defers every ticket **stickily** — a ticket's `not_before` is already a real future
  timestamp by then, so fixing the env var and redeploying does not release already-deferred
  tickets.

## 5. Deploy path

**Chosen rule:** `--sync-env` keeps pushing provider and model from local; `KEY_USAGE_*` are
declared in `render.yaml` as `sync: false` — a dashboard-set baseline, never pushed, with
the DB override as the live-change path. This follows the precedent already set by
`DISPATCHER_REREVIEW_COOLDOWN_*`, which are in `render.yaml` and deliberately absent from
`_ALWAYS_SYNCED`. Rejected: pushing everything from local (breaks that precedent, and makes
every cap tweak cost a redeploy), and a secrets-only sync (costs a manual dashboard step on
a fresh deploy for `LLM_PROVIDER`/`LLM_MODEL`, which bootstrap automatically today).

**`_wanted_env()` — one behavior change.** Today it pushes only the *selected* provider's
model var (`deploy.py:596-600`). With per-provider model vars, a redeploy-free DB flip to a
provider whose model var was never pushed would read a missing or wrong value on Render. So
it pushes **every** provider's model var that has a local value. They are non-secret and
cheap, and this is what makes the DB flip safe.

**`Settings.vertex_model` defaults to `gemini-2.5-flash`** — the value `ISSUES.md` and
`.env.example:76` record as confirmed-working on Vertex — so it can never trip the
empty-value guard at `deploy.py:733-743`.

**`sync_env()` — one new guard**, symmetric with the existing one at `deploy.py:718-731`
(which refuses to sync when a DB *provider* override disagrees with what is being pushed):
the same refusal, with the same "clear it first" message shape, when a DB *model* override
disagrees. Keeps "what you pushed is what runs" true rather than nearly true.

**`render.yaml`** gains `VERTEX_MODEL`, `KEY_USAGE_TOKEN_CAP`, `KEY_USAGE_COST_CAP_USD`,
`KEY_USAGE_RESET_TIME_UTC` as `sync: false`. It also gains the GCP vars
(`GCP_SERVICE_ACCOUNT_KEY_B64`, `GCP_PROJECT`, `GCP_LOCATION`), which it declares nowhere
today despite `vertex` being a live provider — an adjacent gap that leaves the manifest
under-describing the service.

## 6. Credential/index decoupling

The index is already decoupled at the storage layer (a non-secret integer in a DB column).
Three things re-couple it, and all three are fixed here:

1. **`local_numbered_slots()` splits in two.** `local_slot_indices(base) -> tuple[int, ...]`
   discards values **inside the function** and is what every caller gets by default; a
   narrow value-bearing variant is used only by `_wanted_env()`, carrying
   `_render.py::env_vars()`'s contract verbatim ("reduce to a boolean or an equality result
   immediately — never store, print, or pass it on"). The leak-prone shape becomes opt-in
   rather than the default.
2. **`f"{base}_{index}"` moves into `registry.slot_env_name(provider, index)`**, replacing
   the duplicate constructions at `credentials.py:42` and `_override.py:42,54`.
3. **`set_override.py --list`** (§4.5) makes the slot inventory answerable without opening
   `.env`.

The `slot_env_name()` seam is also deliberately the thing that keeps **one-secret-per-file**
(see §9) a one-module change later rather than a sweep.

## 7. Migration

Steps only the user can perform — an agent must not touch `.env`:

1. Create `.env.config` from the committed `.env.config.example`, filling in the operational
   values currently in use.
2. Remove those same keys from `.env`.

**The order is load-bearing.** `.env.config` wins by precedence, so step 1 before step 2
means there is never a window where a setting is unset.

The agent supplies the exact key names and the example file, and never sees a value. The
placement guard (§3.2) reports completion: **the failing test is the migration checklist**,
and it names keys without reading values. Expect that test to be red between landing this
work and completing the migration; that is intended, not a defect.

## 8. Testing (deterministic-first)

Beyond unit coverage of each new module:

- **Config sourcing** — multi-file precedence behaves as verified: `.env.config` wins over
  `.env`; a real env var beats both.
- **Allowlist integrity** — every entry maps to a real `Settings` field.
- **Placement guard** — detects an operational key in `.env` and a non-allowlisted key in
  `.env.config`; skips cleanly when either file is absent; output contains key names only.
- **Report-equals-executed** — `orchestrator._active_model()` and the adapter's actual model
  agree after an override change. This is the regression §4.2 exists to prevent.
- **Warm-process override** — a model change produces a `factory._instances` cache miss.
- **`usage_cap_config`** — an invalid override is discarded as a whole triple; a refresh
  failure degrades to env defaults.
- **`set_usage_cap.py`** — refuses a merged trio that would read back inert.
- **`_wanted_env`** — includes every provider's model var that has a local value.
- **`sync_env`** — refuses when a DB model override disagrees with the model being pushed.
- **No value printed** — seed a sentinel credential value and assert it appears nowhere in
  the new CLIs' captured stdout/stderr.

Live calls: none required. Every behavior above is deterministic and mockable, per
`SPEC.md` §8 and `CLAUDE.md`'s LLM-testing-hygiene rules.

## 9. Non-goals / Open items

- **Verbatim-only credential convention — decided, deferred to its own spec.** A credential
  var always holds the credential material itself, **never a file path**. Today one logical
  vertex credential can span six env vars (`GCP_SERVICE_ACCOUNT_KEY_B64`, `_B64_1`,
  `_B64_2`, `GCP_SERVICE_ACCOUNT_KEY_PATH`, `_PATH_1`, `_PATH_2`), GitHub's PEM has its own
  two-var pair, and gemini/groq keys are literal-only — three different shapes for "a
  credential", which `vertex_credentials.py`'s docstring cites as its own reason to exist.
  The convention collapses them to one shape. It carries a naming consequence: the `_B64`
  suffix goes too, because whether a value is base64-encoded is a fixed property of the
  credential *type* (a PEM and a JSON key always need it; `.env` cannot hold multiline
  values; an API key never does) and therefore belongs declared once in `registry`, not
  repeated in every var name. Names become `GITHUB_APP_PRIVATE_KEY`,
  `GCP_SERVICE_ACCOUNT_KEY[_n]`, `GEMINI_API_KEY`.

  What it deletes: `Settings.github_app_private_key_path` and
  `.gcp_service_account_key_path`; `vertex_credentials._local_path()` and the numbered-file
  fallback (leaving decode → parse → dict, else `None` for ADC); `deploy.py::_private_key_b64()`
  and `check_config()`'s "unreadable PEM" branch; the file-vs-b64 branch at
  `github_app.py:96-104`. ADC is unaffected — an empty var still means implicit ADC.

  Two costs recorded deliberately: it moves `.env` *away* from bounding blast radius (the
  full PEM and SA JSON definitively live there), which in this project changes uniformity
  rather than real exposure, since the `_B64` forms were already present and are what the
  harness diff dumped; and swapping a local service-account key becomes re-encoding rather
  than editing a path, absorbed by the numbered slots, which are the supported mechanism
  anyway. Sequenced **after** this work so there is one `.env` migration per landing rather
  than two.

- **One-secret-per-file** (`secrets/GROQ_API_KEY_1`, …). It is the strongest available
  answer to `ISSUES.md`'s worst incident — one harness diff exposing *every* credential
  becomes impossible once no single file holds them all — but it is a separate project from
  this config gap, and it multiplies the hand-migration burden that only the user can carry.
  Explicitly deferred; §6's `slot_env_name()` seam keeps it cheap to adopt later. Note it
  interacts with the verbatim convention above: verbatim makes this *more* valuable, not
  less, and adopting the two in separate passes would mean a third hand-migration of `.env`
  — so whichever is designed second should account for the other rather than being specced
  in isolation.

- **A deploy-status / `/healthz` CLI subcommand.** Raised during design, not built here.
  Moving `PUBLIC_BASE_URL` into `.env.config` (§3.1) is the enabling condition: the check
  needs only the URL, so such a subcommand would require no credential at all and could not
  leak one.
- **Secret rotation.** The credentials exposed in the incidents behind `CLAUDE.md`'s Secret
  handling section remain unrotated at the user's deferral. Out of scope here, still
  outstanding.
- **Docs updated as part of this work, not after:** `README.md` (both CLI surfaces),
  `SETUP.md`, `.env.example` (operational sections move out to `.env.config.example`),
  `app/CLAUDE.md` if the provider-module contracts shift, and `CLAUDE.md`'s parenthetical at
  the `.env`-opening rule — which currently reads "this project's current CLI/scripts have
  no way to change some local config without an agent touching `.env` directly; that gap is
  tracked as a separate follow-up." That sentence becomes false when this lands, and
  removing it is part of the work.

- **A model set directly in `.env.config` still has no pricing-table guard.** `LLM_MODEL`,
  `GROQ_MODEL`, and `VERTEX_MODEL` are all `OPERATIONAL_KEYS`, freely hand-editable in
  `.env.config` — and unlike `scripts/set_override.py --model` (validated against
  `app/providers/pricing.py` as of this branch's final-review fix wave, which refuses an
  unpriced value unless `--force` is given), a value set this way is never checked against
  the pricing table at all. An unpriced or dated-alias model reaches an uncaught `KeyError`
  at `app/orchestrator.py`'s `estimate_cost_usd()` call — only after all three specialists
  have already made real, paid LLM calls, since cost estimation happens after the fan-out
  completes, not before it. `set_override.py --model`'s validation closes this for the
  DB-override path but not the file-edit path; a full fix would validate at
  `scripts/deploy.py`'s `check_config()` (a local pre-flight check) or `sync_env()` (before
  pushing to Render) time too, so a bad model in `.env.config` is caught before it ever
  reaches a live dispatcher run.
