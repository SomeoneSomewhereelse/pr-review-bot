# Provider-agnostic config and deploy hardening — design

**Date:** 2026-08-08
**Status:** Design approved; not yet planned or implemented.
**Supersedes parts of:** `2026-08-05-deploy-command-design.md` §6.1 and §8 (the
contradiction this document resolves), and Segment B of
`2026-08-03-demo-plan-design.md` (which still assumes a local `uvicorn` demo).
**Relates to:** `docs/2026-08-07-deploy-cli-checkpoint.md` (the completion record
that parked these items).

## 1. Problem

One failure class runs through every item here: **the deploy CLI reports green
for a service that cannot do its job.** Three independent instances:

1. `--sync-env` hardcodes an eight-variable push list that is inconsistent with
   how `LLM_PROVIDER` selects a provider. The default configuration
   (`llm_provider: str = "gemini"`, `app/config.py:16`) cannot sync at all, and
   a Groq-configured service can be silently switched to a provider whose key
   was never pushed.
2. `check_config` *stats* the private-key PEM while `_wanted_env` *reads* it, so
   an unreadable key passes the check and then raises outside any handler.
3. `check_render_service` reports that a deploy is live without reporting *what*
   is live, so an operator whose changes never reached the build sees six green
   rows.

A fourth item is the inverse — a green service reported as broken:
`_DEPLOY_FAILED_STATUSES` treats a superseded (`canceled`) deploy as a build
failure, and a 300s timeout is too short for a cold Docker build.

## 2. Decision 1 — one provider table, three consumers

`.env.example` already expresses the intended contract: all four providers
listed, each with its own credential and model variable, all empty, and
`LLM_PROVIDER` selecting one. `--sync-env` and `render.yaml` are the two places
that break it.

Today three planes disagree about what a provider is:

| plane | providers known | model vars |
| --- | --- | --- |
| `.env.example` | 4 — vertex, gemini, groq, github_models | all 4 |
| `_PROVIDER_KEYS` (`scripts/deploy.py:93`) | 3 — vertex absent | none |
| `render.yaml` | 2 credentials; `LLM_PROVIDER` hardcoded to `groq` | none |

Replace `_PROVIDER_KEYS` with a single table:

```python
# provider -> (credential env var, model env var)
_PROVIDERS = {
    "vertex":        ("GOOGLE_CLOUD_PROJECT", "LLM_MODEL"),
    "gemini":        ("GEMINI_API_KEY",       "LLM_MODEL"),
    "groq":          ("GROQ_API_KEY",         "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN",  "GITHUB_MODELS_MODEL"),
}
```

### 2.1 `check_config`

Requires the selected provider's credential, vertex included. Today
`_PROVIDER_KEYS.get("vertex")` returns `None`, so a vertex configuration
requires nothing and passes with nothing verified.

`check_config` validates the **environment-configured** provider even when a DB
override (§3) is active and working. This is intentional, not an oversight to be
"fixed" during implementation: a missing environment credential is a real latent
misconfiguration, because the environment value is what governs the moment the
override is cleared, the row is lost, or the database is unreachable. The
resulting pair — `config FAIL` alongside `provider PASS` — is accurate, and the
`provider` row's detail names the override so the combination reads correctly.

### 2.2 `--sync-env` push set

Pushed: the five always-required variables (`DATABASE_URL`, `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY_B64`, `GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`),
plus `LLM_PROVIDER`, plus the selected provider's **credential and model
variable**. Any other provider's credential is pushed when it has a local value
and skipped silently when it does not — never an error.

The model variable matters for the same reason as the credential: pin
`LLM_MODEL` to a dated alias locally for demo reproducibility (per `CLAUDE.md`)
and the service keeps running `config.py`'s default. The values agree today, so
the divergence is currently invisible.

The empty-value refusal scopes to that required set. A Groq-only `.env` must
never be asked for a Gemini key. The shipped stopgap guard in `sync_env()`
becomes unnecessary and is deleted.

**Sync is additive by design** — it never uses the destructive bulk endpoint —
so clearing a credential locally does not remove it from the service. Documented,
not fixed.

### 2.3 vertex and `--sync-env`

`--sync-env` refuses when the selected provider is `vertex`, with a message
stating why: vertex authenticates through ADC / service-account credentials that
cannot be pushed as a plain environment variable. Checked but not syncable —
stated rather than half-implemented.

### 2.4 `render.yaml`

Every provider credential and model variable is declared `sync: false`, and
`LLM_PROVIDER` changes from `value: groq` to `sync: false`.

`LLM_PROVIDER` is currently the only variable the blueprint owns a value for
while every other is `sync: false`. That makes two writers for one key, and the
blueprint is the one that wins on a re-sync. `sync: false` means "declared, the
operator supplies it" — exactly the opt-in shape wanted.

## 3. Decision 1b — DB-backed provider override

**Requirement:** the parked demo must swap providers mid-session on the hosted
service, without a redeploy.

The seam is already runtime-mutable: every read site reads
`settings.llm_provider` at call time (`providers/factory.py:17`,
`orchestrator.py:43/45/102`, `dispatcher.py:138`), and nothing caches a provider
instance at import. That is why `scripts/demo_provider_swap.py` works.

What is pinned at import is only the initial value — `settings = Settings()`
runs once (`config.py:56`). **No environment-variable route can avoid a
restart**, because the value only enters the process at boot. On Render that is
a measured ~60s (`SETUP.md:362`).

### 3.1 Schema

Appended to `store._SCHEMA`, provisioned on boot like `tickets`:

```sql
CREATE TABLE IF NOT EXISTS runtime_config (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider   TEXT,                    -- NULL = no override
    updated_at TEXT NOT NULL
);
```

Singleton row, so there is never ambiguity about which row wins. TEXT ISO-8601
timestamp, matching the existing convention.

### 3.2 Store functions

Synchronous, like every other store function:

- `get_provider_override() -> str | None`
- `set_provider_override(provider: str | None, now: str) -> None` — upsert;
  `None` clears.

### 3.3 Resolution

A single accessor, `active_provider()`, replaces all five reads of
`settings.llm_provider`. It returns a module-level cached override, falling back
to `settings.llm_provider`.

Partial adoption is not an option: if only the dispatcher consulted the
override, `factory.py` would still build the env-configured provider, gating on
one provider while calling another.

Cached rather than read-through, because a read-through puts a blocking DB call
inside `factory.py` on the event loop, against the project's
sync-store-plus-`asyncio.to_thread` convention. The dispatcher refreshes the
cache **at claim time** (`await asyncio.to_thread(store.get_provider_override)`)
— one extra SELECT per review, not one per idle tick.

`webhook.py:65` continues recording a possibly-stale provider on the ticket at
enqueue. This changes no meaning: `dispatcher.py:135` already states that the
recorded value is not authoritative and the gate uses the current provider.

**Failure behavior is fail-safe by construction.** The cache starts empty, so
before the first claim — and whenever a refresh fails or the database is
unreachable — `active_provider()` returns `settings.llm_provider`. The service
degrades to its configured provider rather than to no provider. A refresh that
raises must be caught and logged, never allowed to abort a review.

### 3.4 `scripts/set_provider.py`

A plain CLI: `uv run python scripts/set_provider.py groq`, and `--clear`. A
slash command may wrap it but holds no logic — the same split as `deploy.py` and
`.claude/commands/deploy.md`. A demo that hard-depends on Claude to show
provider-agnosticism cannot be run without Claude.

It validates only that the name is a key of `_PROVIDERS`. It runs locally and
cannot know whether that provider's credential exists **on the service** — the
`provider` check below is the safety net for that.

It writes to the same database the service reads, so it requires `DATABASE_URL`
and opens the store pool exactly as the service does. Against a local
`DATABASE_URL` it sets a local override and nothing reaches production — worth
stating in the docs, since the command's effect depends entirely on which
database `.env` points at.

### 3.5 Keeping the CLI honest

A runtime override reintroduces the failure class of §1 unless the CLI can see
it: if the live provider can differ from the configured one, validating the
configured provider's credential proves nothing.

**A seventh check, `provider`,** running after `database` (it needs the
connection; `check_config` stays environment-only so the cheapest-first ordering
holds). It reports the resolved provider and its source, and fails when an
override names a provider whose credential is not locally available:

```
provider   PASS  groq (env)
provider   FAIL  groq (DB override; env=gemini) — GROQ_API_KEY missing
```

**`sync_env()` refuses**, before any HTTP, when an active override would mask the
`LLM_PROVIDER` it is about to push — the same guard style as the existing
empty-value refusal. Otherwise `--sync-env` reports a provider change that
silently does nothing.

### 3.6 Demo impact

Segment B of `2026-08-03-demo-plan-design.md` becomes two instant DB writes
instead of two `uvicorn` restarts (already stale — they assume a local demo) or
two ~60s redeploys. That spec needs a follow-up edit; it is not rewritten here.

## 4. Decision 2 — read the PEM, do not stat it

The path is resolved in three places with the same four lines:
`_private_key_available()` (`deploy.py:100`), `_wanted_env()` (`deploy.py:332`),
and `app/github_app.py:104`. The first stats, the second reads. That divergence
is the bug: `is_file()` returns true, `read_bytes()` raises `PermissionError`,
and the check has already reported PASS.

One helper replaces both CLI call sites:

```python
def _private_key_b64() -> tuple[str, str]:
    """The PEM in the base64 form Render needs, plus a problem string
    ("" when usable).

    Reads rather than stats: an existing-but-unreadable PEM must not report as
    available, because check_config would pass while _wanted_env raises on the
    same file.
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

Absent and unreadable get different messages because they need different
actions — create a key versus fix permissions. The unreadable case uses
`CheckResult`'s existing newline continuation:

```
config   FAIL  missing: GITHUB_APP_ID
               unreadable PEM /path/github-app-private-key.pem (PermissionError)
```

`--sync-env` then becomes correct without moving anything into a `try`:
`_wanted_env()` returns `""`, the existing empty-value guard fires, and the
result is `refusing to push empty values; fix .env first:
GITHUB_APP_PRIVATE_KEY_B64` at exit 2 — the documented contract, no traceback.

This closes three recorded items at once: the parked `OSError` residual, the
duplicated path resolution, and the silent-PASS blind spot that made the
residual reachable through a green report.

**Out of scope:** `app/github_app.py:104`'s third copy. Different contract — the
app *should* raise on an unreadable key at boot; the CLI's job is to report it
first. Merging them would force one behavior on both.

## 5. Decision 3 — report what is live; do not legislate the topology

### 5.1 `render.yaml` is not changed

`autoDeploy` is not ours to choose. `render.yaml` governs only services created
from it as a blueprint; for a service built from a pre-built image it is inert,
so `autoDeploy: false` would not even reach the operator it was meant to serve.

Setting it false would also invert a deliberate property. `config.py:48-50`
states the rule for operator tooling: *"Absence degrades a check to SKIPPED,
never to an error."* `RENDER_API_KEY` is optional by design; making it mandatory
to ship any code contradicts that, and a contributor without the key could no
longer deploy.

The two triggers are not redundant. `git push` ships **code**; `--sync-env`
ships **config**. Render env-var changes genuinely do not auto-deploy — that is
why `_trigger_and_wait` exists (`deploy.py:350`).

### 5.2 Three deploy-polling fixes

1. **`"canceled"` leaves `_DEPLOY_FAILED_STATUSES`** (`deploy.py:64`).
   Cancellation is what happens when a second deploy supersedes the first — the
   expected outcome of a collision, not a build failure. It is reported
   distinctly: the environment variables *were* pushed and a newer deploy is
   running. Exit 1, with a message that says which of those it is.
2. **`_DEPLOY_TIMEOUT_SECONDS` rises to 900.** The measured 65.5s/56.7s
   (`SETUP.md:362`) were redeploys with warm layers. A cold Docker build with a
   full dependency install can exceed five minutes — so the first deploy, the
   one that most needs to be right, is the most likely to report a false
   failure. Status transitions print as they change, so a long wait is visibly
   progressing.
3. **`--sync-env` waits for an in-flight deploy to settle before triggering its
   own.** Not *adopting* it: a deploy that started before the env-var push may
   have resolved its environment already, so adopting it could report "deploy
   live" for a container running the old configuration — the same green-but-wrong
   class. Waiting costs one build cycle and guarantees the pushed values are in
   the live container.

All three are inert on a service with no auto-deploy: no second trigger means
the wait never fires and the cancellation path never occurs.

### 5.3 `check_render_service` reports the live artifact

Today it reads only `status` (`deploy.py:266`) — "latest deploy live" says
nothing about *what* is live. `_trigger_and_wait` POSTs an empty body, so an
image-backed service re-pulls the same image and a repo-backed one rebuilds the
connected branch's latest commit. Neither reads the local working tree.

The check detects the topology and reports accordingly:

| topology | behavior |
| --- | --- |
| repo-backed | report live commit and branch; compare against `git rev-parse HEAD`; FAIL on mismatch, naming both; FAIL on a dirty tree (uncommitted changes can be in no build) |
| image-backed | report the live image ref; state plainly that no local comparison was possible |
| no git directory | SKIPPED, with the reason |

```
render-service  PASS  live: dep-abc @ 4e39cda (main)
render-service  FAIL  live: dep-abc @ 4e39cda, but local HEAD is 1b10b18
                      — 3 unpushed commits; push or redeploy
render-service  FAIL  live: dep-abc @ 4e39cda (local HEAD matches, tree dirty
                      — uncommitted changes cannot be in any build)
render-service  PASS  live: dep-abc @ ghcr.io/you/pr-review:v3
                      (image-backed; no local comparison possible)
```

The last line is the honest one: it names the deployed artifact and states that
the CLI did not verify it matches local edits, rather than implying it did.

Render always builds on Render; it never uploads a local working tree. "Never
push" therefore resolves to *push an image instead of source* — build locally,
push to a registry, point the service at the tag. That workflow is **documented
in README/SETUP, not automated**: the CLI cannot build or push an image, and
automating only the final step of a pipeline whose earlier steps stay manual
would misrepresent what it does.

## 6. Recorded branch-review items absorbed here

From `docs/2026-08-07-deploy-cli-checkpoint.md` §3, folded in because this work
already touches the same code:

- **`--help` and unknown-flag handling** (argparse). Now load-bearing rather
  than cosmetic: `--sync-env` is matched with `"--sync-env" in args`
  (`deploy.py:472`), so a typo like `--sync-en` silently runs checks only and
  reports success for a sync that never happened. More flags are arriving.
- **`E501` selected in ruff** at the existing `line-length = 100`. `CLAUDE.md`
  states the convention but `pyproject.toml:29-30` sets only the width, leaving
  it unenforced; six test lines already exceed it and new tests are coming.
- **`test_env_var_names_match_the_docs`** must change anyway (the synced set is
  now provider-dependent); its CWD-relative doc paths are fixed at the same time.
- **Exit-code causes pinned in the docs** — spec §7.2 lists three causes for
  exit 2 that §10 never required the docs to carry. The docs-parity test is the
  natural place.
- **Stale `discover_installation_id` docstring.**
- **§11's detail-length budget**, unimplementable as literally written since
  `check_config`'s enumeration legitimately exceeds it. The newline-continuation
  cases in §4 and §5.3 make this worse; the budget is restated to apply
  per-line, not per-detail.

**Deliberately not absorbed:** the health URL built independently in two checks,
unbounded monitor-URL enumeration in `check_uptime_pinger`, and missing
pagination on the Render list endpoints (fail-safe today; a >20-service account
breaks `_find_render_service_id`, but loudly).

## 7. Testing

Against real Postgres via testcontainers, matching the existing DB layer
(`TESTCONTAINERS_RYUK_DISABLED=1` on WSL2 + Docker Desktop).

**Store:** default `None`; set→get; set twice replaces rather than inserting a
second row; clear→`None`; `CHECK (id = 1)` genuinely rejects a second row.

**Resolution:** `active_provider()` returns the env value with no override, the
override when set, and observes a change made behind the cache after a refresh.

**Dispatcher (the behavioral test that matters):** a claimed ticket runs against
the override, not `settings.llm_provider`.

**Provider table:** `check_config` requires the selected provider's credential
for each of the four providers, including vertex; ignores other providers'
credentials; `--sync-env` pushes the selected provider's credential *and* model
variable; a Groq-only `.env` is never asked for a Gemini key; `--sync-env`
refuses under `vertex`.

**Private key:** absent → FAIL naming the env var; present but `chmod 000` →
FAIL naming *unreadable* specifically, not "missing"; b64 set → PASS without
touching the filesystem; `--sync-env` against an unreadable PEM → exit 2 with
the empty-value message and no traceback. The `chmod 000` cases skip under root,
which reads anything — stated so a CI container does not silently pass them.

**Guards:** `provider` check across all three states (env, override satisfied,
override unsatisfied); `sync_env()` refuses under an active masking override.

**`set_provider.py`:** rejects a name absent from `_PROVIDERS`; `--clear` clears.

**Deploy polling and topology:** `canceled` reported distinctly from
`build_failed`; the in-flight wait triggers only after the prior deploy settles;
repo-backed mismatch, dirty tree, and image-backed reporting each produce their
documented row.

**Mutation checks** on the dispatcher override assertion, the `sync_env` masking
guard, and the HEAD-mismatch assertion: deliberately break each, confirm the
test fails, revert. This discipline caught a dead assertion on this branch
already.

## 8. Assumptions to verify during implementation

Not verified here, and not to be trusted on the strength of this document:

1. Whether psycopg3's `execute()` accepts two statements in one call, which
   determines whether `_SCHEMA` stays a single string.
2. Whether Render actually reconciles a blueprint `value:` back over an
   API-pushed one on re-sync. This does not change §2.4 — one writer is correct
   either way — only how urgent it is. The first push after the `render.yaml`
   change is what tests it.
3. Whether Render cancels or queues a superseded deploy. Either way, folding
   cancellation in with build failures is wrong.
4. The Render API fields relied on in §5.3: the deploy object's commit id, and
   the service field distinguishing repo-backed from image-backed.

## 9. Out of scope

- Rewriting `2026-08-03-demo-plan-design.md` (§3.6 notes the needed edit).
- Merging `app/github_app.py`'s PEM loading with the CLI's (§4).
- Building or publishing container images (§5.3).
- The three non-absorbed review items (§6).
