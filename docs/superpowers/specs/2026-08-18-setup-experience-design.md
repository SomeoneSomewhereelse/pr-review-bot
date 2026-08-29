# Design — Setup experience: operator-facing guide, setup doctor, and pricing rework

**Date:** 2026-08-18
**Status:** Approved for planning
**Relates to:** `README.md`, `SETUP.md`, new `guide/` (MkDocs site), new
`scripts/doctor.py`, `scripts/init_env.py`, `scripts/create_github_app.py`,
`scripts/pricing_check.py`, `scripts/gen_docs.py`, `.claude/commands/setup.md`;
`app/config.py` (`Settings`, `OPERATIONAL_KEYS`), `app/main.py` (`lifespan`),
`app/orchestrator.py`, `app/formatting.py` (`format_comment`),
`app/providers/pricing.py`, `app/specialists/schemas.py` (`ReviewResult`),
`app/queue/store.py` (schema DDL), `app/queue/dispatcher.py`,
`app/queue/usage_cap_config.py`, `app/static/dashboard.html`,
`scripts/deploy.py` (`check_config`, `_unpriced_models`, `run_checks`,
`sync_env`, `sync_config_db`), `scripts/set_override.py`.

## 1. Problem

The project is deployable but not *re-*deployable by a stranger. Three
concrete failures:

**No linear zero-to-running path exists.** The real dependency chain is
clone → `uv sync` → create GitHub App → get an LLM key → Supabase project →
Render service → encode the PEM → set boot creds → install the App → pinger →
`deploy.py --sync-env` → `seed_demo_pr.py`. Reconstructing that order requires
reading two documents and mentally re-sequencing them. Neither presents it.

**`SETUP.md` (750 lines) is a build journal, not a setup guide.** Its own H1
reads "Step 0 prerequisites (completed)". Its sections run 1, 2, 2b, **2a**,
2c, 3 — appended in the order events happened, not the order a reader needs.
It interleaves genuine instructions with incident history (the Gemini Trust &
Safety block, the two Vertex bugs), a live-rehearsal results table, and
redo-from-scratch notes. That material is valuable provenance, but it sits in
the file a newcomer is told to read first.

**`README.md` (516 lines) is roughly 60% operator reference manual.** Lines
111-408 are the deploy CLI, the ten-check table, the override CLI, usage caps,
cooldown tuning, and image-registry deploys. A reader gets ~50 lines of "what
is this", then ~300 lines of ops manual, then "Known limitations" at line 461.

Two structural gaps compound it:

- **Tooling chicken-and-egg.** `scripts/deploy.py` verifies an *existing*
  deployment and requires `PUBLIC_BASE_URL`, which does not exist until Render
  is up. Nothing answers "am I ready to start?" or "where am I?".
- **Pricing is a hidden hard-block.** `_RATES` in `app/providers/pricing.py`
  has four entries. `app/orchestrator.py:108` calls `estimate_cost_usd`
  unguarded, so an unpriced model raises `KeyError` *after* three paid LLM
  calls. The `--sync-env` / `set_override --model` refusals exist solely to
  stop you reaching that line — which turns a four-entry rate table into a
  hard allowlist on which models may run at all.

## 2. Audience and scope decision

**Primary audience: a third-party operator** deploying their own instance on
their own accounts, with zero prior context. Every value they need must be
findable, and every step must appear in the order they will actually do it.

**In scope:** documentation restructure; a published MkDocs guide; a **local
and a hosted setup track** (§4a); a state-aware setup doctor; guided env
scaffolding; a GitHub App manifest flow; the pricing rework; removal of the
dollar usage cap; `LLM_PROVIDER` becoming required; DDL cleanup; removal of the
dashboard "How it works" section; generated reference docs with a CI drift
check.

**Non-goals** are listed in §11.

## 3. Documentation architecture

### 3a. `README.md` — 516 lines to ~150

**Bug to fix while here:** `README.md:21` still says *"enqueue/update a durable
**SQLite** ticket"*. SQLite was replaced by Postgres in the Supabase migration
(`app/queue/store.py` is psycopg3-only). That is precisely the line that misleads
a reader about local requirements, so it is in scope here rather than deferred.

Keeps: pitch, architecture diagram, tech stack, local quick start, Docker,
testing, known limitations, cost summary — everything answering *"what is this
and does it work?"* Adds one prominent **"Deploy your own →"** link near the
top. Loses lines 111-408 wholesale to the guide.

### 3b. The guide is a MkDocs Material site under `guide/`

`docs_dir: guide`, **not** `docs/`. `docs/` holds dated engineering handoffs
and `docs/superpowers/`; pointing MkDocs at it would either publish all of
that or need exclusion rules. A separate `guide/` moves nothing that exists.

MkDocs Material is chosen over plain Jekyll/just-the-docs for two reasons
specific to this content:

- **Admonitions** — the guide is dense with gotchas that must not read as body
  text (the Supabase pooler is port 5432, not 6543; a stray character in the
  pinger URL 404s on every check while looking healthy in the dashboard).
- **Content tabs** — `README.md:141` and `:209` already duplicate every command
  for bash and PowerShell. Tabs collapse that duplication instead of doubling
  every page.

```
guide/
  index.md              pitch, prerequisites, "what you'll need" (accounts, ~30 min)
  setup/01..08.md       the ordered path (see §4)
  operations/           deploy CLI · overrides · caps · cooldown · image deploys
  reference/            config values · pricing · check table · sync-env push set  (GENERATED, §7)
  background/           the journal: incidents, deviations, rehearsal history
```

### 3c. `SETUP.md` splits in two

Instructional content becomes `guide/setup/*`. Journal content (the Gemini
block, the two Vertex bugs, the rehearsal table, redo-from-scratch notes)
becomes `guide/background/`. It stays published — it is real evidence — just
out of the newcomer's path.

### 3d. Cross-references that break

`scripts/deploy.py:39` hardcodes
`_README_ANCHOR = "README.md#deploying-to-production-render--supabase"` and
prints it to users. `CLAUDE.md` cross-references `SETUP.md` §1/§2/§3 in
several places. Both must be updated in lockstep with the move, or the tooling
starts pointing at sections that no longer exist. `_README_ANCHOR` becomes a
guide URL, with a test asserting the target file exists under `guide/` (§7).

## 4. The setup spine

### 4a. Two tracks, shared prefix

**The app has zero Render coupling.** `public_base_url` is declared at
`app/config.py:62` and consumed **nowhere in `app/`** — only `scripts/deploy.py`
reads it. `render_api_key` / `render_service_name` are operator-tooling fields,
same story. The lifespan (`app/main.py:32-52`) requires exactly three things:
`GITHUB_WEBHOOK_SECRET`, a resolvable App installation, and a reachable Postgres
via `store.init_pool()`.

So the real dependency picture is much smaller than the current docs imply:

| Thing | Actually required? |
|---|---|
| **Postgres** | **Yes** — hard requirement; `store.py` is psycopg3-only |
| **A public HTTPS URL** | **Yes** — GitHub must reach the service; a tunnel satisfies it locally |
| Supabase | No — it is just *a* hosted Postgres; any Postgres works |
| Render | No — nothing in `app/` knows it exists |
| UptimeRobot | No — it exists solely because Render's free tier sleeps |
| Docker | No — one of three ways to get Postgres (Docker, native install, remote) |

This is evidenced, not theoretical: `SETUP.md:300` records that the pre-Render
setup was "local machine + Cloudflare Tunnel", and `SETUP.md:734` records
**PR #3 — the definitive end-to-end rehearsal — running through a quick tunnel**.

**Steps 1-4 are shared by both tracks:**

| # | Step | How |
|---|---|---|
| 1 | Clone and install | `git clone` · `uv sync` · `uv run pytest` to prove the checkout is sound |
| 2 | Create the GitHub App | `scripts/create_github_app.py` — one browser round-trip, writes App ID + PEM + webhook secret |
| 3 | Install it on your repo(s) | browser (irreducible: GitHub forbids an App installing itself) |
| 4 | Get an LLM key | browser; Groq recommended — free tier, no card |

**Track A — local** (5A-8A):

| # | Step | How |
|---|---|---|
| 5A | Get a Postgres | Docker, a native install, or any remote instance; set `DATABASE_URL` |
| 6A | Start a tunnel | `cloudflared tunnel --url http://localhost:8000` in a second terminal; set `PUBLIC_BASE_URL` to the printed URL |
| 7A | Register the webhook and verify | `uv run python -m bot.scripts.deploy` — Render/pinger checks `SKIP` cleanly with no `RENDER_API_KEY` |
| 8A | Run and review | `uv run uvicorn app.main:app` · then `seed_demo_pr.py` |

**Track B — hosted** (5B-8B):

| # | Step | How |
|---|---|---|
| 5B | Create the Supabase project | browser; copy the Session-mode pooler string (5432, not 6543) |
| 6B | Create the Render service | browser; Blueprint from `render.yaml`, then **four** env vars (§4b) |
| 7B | Sync and verify | `deploy.py --sync-env`, then `deploy.py` |
| 8B | Pinger, then first review | UptimeRobot monitor on `/healthz` · then `seed_demo_pr.py` |

Prerequisites on `guide/index.md`: **Python 3.12** (per `.python-version`;
README does not currently say), `uv`, **a Postgres you can reach** (Docker being
one of three options, not the requirement itself), and — for Track A only —
a tunnel binary.

### 4a-i. Why the tunnel is required, and what it is not

Without a tunnel GitHub never delivers a PR event, so the trigger — the actual
product — is never exercised. It is required for Track A.

There is nonetheless a real no-tunnel mode already exercised here:
`scripts/manual_verify_step3.py` has **no public-URL dependency** and proves App
auth, diff fetch, and comment upsert against a real PR; PR #2 in the rehearsal
table ran a direct `orchestrator.run_review()` call. That proves the *pipeline*
but not the *trigger*. It is documented as an **optional verification milestone
inside step 1** — worth doing before investing in the tunnel — not as a
supported deployment mode.

**Cloudflare is the documented default, not a hard dependency.**
`cloudflared tunnel --url` (TryCloudflare) is the only option needing no
account, no config, one binary, and one command — which is why this project used
it. ngrok now requires a free account and authtoken; Tailscale Funnel and VS Code
port forwarding both need accounts. The tunnel stays pluggable: all the app needs
is a public HTTPS URL in `PUBLIC_BASE_URL` plus a registered webhook.

Known friction, stated rather than solved: a quick tunnel's URL changes on every
restart, so step 7A is re-run each session. A named Cloudflare tunnel gives a
stable hostname but requires an account and DNS — out of scope here.

### 4b. Step 6B shrinks from nine env vars to four

`SETUP.md` §3.2 currently walks through entering nine variables by hand. Only
four are needed for the container to boot, and the codebase already knows
which: `check_boot_credentials_live` (`scripts/deploy.py:286`) names exactly
`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`,
`DATABASE_URL`. `--sync-env` pushes the other ~25 in step 7B.

### 4c. Two chicken-and-eggs, resolved by ordering

- *The App manifest needs a webhook URL that does not exist yet* — the tunnel
  URL (step 6A) or the Render URL (step 6B), neither known at step 2. The
  manifest ships a placeholder; step 7A/7B's `check_installation_and_webhook`
  (`deploy.py:314`) already does "webhook points here — set only if wrong", so
  it corrects the URL as a normal part of verification. Designed-in, not a
  workaround, and it is why Track A tolerates an ephemeral tunnel URL.
- *(Track B only) `--sync-env` needs a service that needs credentials to boot.*
  Hence step 6B's four vars, then step 7B for everything else.

### 4d. `scripts/doctor.py`

**Composition, not duplication.** `deploy.py`'s checks are already independent
functions each returning a `CheckResult` (`:211`-`:766`), with
`render_report()` and `_safe()` alongside. Doctor imports those for
post-deployment steps and adds only the *backwards* probes deploy.py has no
reason to own (does `.env` exist, does the PEM decode, is `LLM_PROVIDER` set).
Two check implementations that could drift is exactly what to avoid.

Contract:

- **Read-only and idempotent.** Never writes state; writing belongs to §4e.
- **Staged.** Local probes first; remote probes only for resources that now
  exist. Render not existing at step 1 is the *normal* state, not a failure.
- **One line beyond the table:** `You are at step N of 8. Next: <exact
  command>`, inferred from the first unsatisfied probe.
- **Degrades, never errors.** Missing `RENDER_API_KEY` / `UPTIMEROBOT_API_KEY`
  → `SKIP` with a hint, exactly as `deploy.py` does today.
- **`--json`** for machine consumption (§4f).
- **`--track {local,hosted}`**, defaulting to auto-detection: `RENDER_API_KEY`
  set, or `PUBLIC_BASE_URL` containing `onrender.com` → hosted; otherwise local.
  The explicit flag always wins. A documented rule, not inference magic — the
  tracks share steps 1-4 and only diverge afterwards.
- **Tunnel probe (Track A only), without owning the process.** Starting a
  long-lived foreground tunnel would break the read-only/idempotent contract
  above, so doctor checks `shutil.which("cloudflared")` and prints the per-OS
  install hint on a miss; the guide gives the command for a second terminal.
  Detection of a *working* tunnel needs no new code: `PUBLIC_BASE_URL` set plus
  `check_health_endpoint` (`deploy.py:406`, already credential-free) answering
  through it is the proof, and `check_installation_and_webhook` (`:314`)
  registers the URL.

**Prerequisite stage (step 1)** uses `shutil.which()` — which handles Windows
`PATHEXT`/`.exe` for free — plus `sys.version_info` for the interpreter. On a
miss it prints the install command for the detected platform. This is the one
legitimate use of `platform.system()` in the codebase: the code runs
everywhere, only the printed hint varies, and every hint carries the official
URL as a universally correct fallback.

Known bootstrap gap, stated rather than solved: doctor runs via `uv run`, so
it cannot tell you how to install `uv`. Python and `uv` install instructions
live in `guide/index.md` as tabbed prose; doctor owns everything after that.

### 4d-i. Secret safety: doctor never opens `.env`

`Settings` already loads both files (`env_file=(".env", ".env.config")`).
Doctor accesses **individual fields** and reduces each to a boolean or length
*at the point of access* — `bool(s.github_webhook_secret)`,
`len(s.github_app_private_key)` — and never holds a value in a variable that
can reach output. This is precisely `CLAUDE.md`'s stated contract: *"Read or
pass individual fields programmatically instead, and reduce any secret-bearing
value to a boolean/length/hash before it can reach a print statement."*

This deletes an entire class of parsing bug: no regex to defeat with `=`
inside a `DATABASE_URL`, an unencoded multi-line PEM, `export KEY=...`,
trailing comments, or CRLF line endings from a Windows-authored file. pydantic
already handles all of it, and that behavior is already tested.

**Then make leaks impossible by type, not by discipline.** The probe returns
`frozenset[str]` of key names and `dict[str, int]` of lengths — there is no
field a value *could* occupy. `tests/test_config.py:19`'s
`_key_names(path) -> set[str]` is the existing precedent for this shape.

### 4e. The two writing tools

**`scripts/create_github_app.py`** — POSTs a pre-filled manifest (permissions,
events, placeholder webhook URL) to `github.com/settings/apps/new`, serves a
one-shot localhost listener for the redirect, exchanges the returned `code` via
`POST /app-manifests/{code}/conversions`, and writes `GITHUB_APP_ID`, the
base64-encoded PEM, and `GITHUB_WEBHOOK_SECRET` into `.env`. Collapses the
longest and most error-prone section of the current guide, and removes the
manual base64 step entirely.

**`scripts/init_env.py`** — copies both `.example` files, prompts for each
value in guide order, base64-encodes any PEM inline, writes `.env` and
`.env.config`. Idempotent: re-running offers to keep existing values, so it is
also the resume path.

### 4f. `.claude/commands/setup.md`

Same contract as the existing `.claude/commands/deploy.md`, whose closing line
states the principle: *"This command holds no verification logic of its own —
`scripts/deploy.py` is the tool, and it works identically for people who do not
use Claude Code."*

The command loops: run `doctor --json`, explain the first failing line in plain
language, name the next command, repeat. With one hard rule written into the
command itself: **when a step requires entering a real credential, hand off** —
"run `! uv run python -m bot.scripts.init_env` yourself" — because `CLAUDE.md`
forbids the agent from opening `.env` at all. The agent only ever sees
doctor's names, lengths, and booleans. That handoff is not a limitation routed
around; it is what makes an agent-assisted setup safe.

## 5. OS-agnosticism

**The Python is already clean.** No `sys.platform` / `os.name` branching in
`app/`, `scripts/`, or `tests/`; no `shell=True`; every subprocess call is
argv-list form; paths are `pathlib` throughout; `.gitattributes` pins
`eol=lf`. Nothing to fix.

**Every OS assumption lives in the docs**, and they share one cause: a raw
shell tool used where the project already has a portable Python CLI.

| Where | Assumption | Replacement |
|---|---|---|
| `SETUP.md:380,394` | `base64 -w0 < file` | `-w` is a GNU coreutils flag; macOS/BSD `base64` errors `invalid option -- w`. Use `scripts/encode_credential.py`, which already exists |
| `SETUP.md:363` | `curl .../healthz` | On Windows PowerShell `curl` aliases `Invoke-WebRequest`, which takes different arguments — it *looks* like it works and does not. Use `deploy.py --health-only`, which already exists and needs no credential |
| `SETUP.md:269` | `winget install Docker.DockerDesktop` | Windows-only; needs macOS/Linux siblings in a content tab |
| `README.md:141,209` | `VAR=value cmd` prefix (bash-only) | Already has a PowerShell twin — the right pattern; MkDocs tabs make it cheap to keep |

### 5a. Encoding and newlines in new code

The existing claim was verified rather than assumed, and it holds: `git` is the
**only** binary ever shelled out (always argv-list, never `shell=True`); no
`grep`, `sed`, `awk`, `cat`, `base64`, or `curl` appears in any Python. The
suspected `.read_text()` locale-encoding trap does **not** bite today —
`README.md`/`SETUP.md` decode under cp1252 without error, the doc tests at
`tests/test_deploy_script.py:1866,1912` match ASCII substrings only, and the one
file where it genuinely matters already declares it
(`app/dashboard.py:24`, `encoding="utf-8"`, for the Hebrew strings).

It **will** bite the new code. `gen_docs.py` emits Markdown containing em-dashes
and arrows, and §7's CI drift check compares regenerated output byte-for-byte. On
Windows, a missing `encoding=` writes cp1252 and a missing `newline=` writes CRLF;
either makes `git diff --exit-code` fail on the operator's machine, since
`.gitattributes` (`* text=auto eol=lf`) pins the working tree to LF.

**Rule:** every file read and write in new code passes `encoding="utf-8"`
explicitly, and every write passes `newline="\n"`. Enforced by test (§8k), not
by convention.

**Principle:** every documented action routes through
`uv run python -m scripts.*`, which is byte-identical on all three OSes. Two of
the four fixes are simply *using tooling that already exists*. The genuine
residue that cannot be routed through Python is `git`, `uv`, and installing
prerequisites — which is exactly step 1's job (§4d).

## 6. App changes

### 6a. Pricing becomes optional

`estimate_cost_usd` returns `float | None` instead of raising; `is_known()`
stays. `ReviewResult.est_cost_usd` (`app/specialists/schemas.py:62`) becomes
`float | None`. `app/orchestrator.py:108` needs no guard — it passes `None`
through.

Rendering **drops the segment** rather than printing a placeholder.
`app/formatting.py:117` and `:129` each carry one cost fragment; an unpriced
review reads:

```
_3 specialists · llama-3.1-8b-instant (groq) · 8.2s_
<sub>Runtime 8.2s · 4,120 tok in / 1,240 tok out · provider: groq</sub>
```

Dashboard aggregates need no change: `store.py:478,496` already use
`COALESCE(SUM(est_cost_usd), 0)`, and SQL `SUM` skips NULLs. Accepted semantic
wrinkle, documented rather than fixed: a total silently under-reports when
some reviews are unpriced. No "priced N of M" counter.

### 6b. The refusals become warnings

`deploy.py:170`'s `_unpriced_models` feeds `check_config` (`:211`), `sync_env`
(~`:1113`), and `set_override.py --model`. All three go from FAIL / exit-2 to a
WARN row that does not fail the run.

Docs consequence: `README.md:245-251` justifies the current hard refusal with
*"an unpriced model only fails at cost-estimation time, after all three
specialists have already made real, paid calls."* Once the estimate returns
`None`, that rationale does not weaken — it disappears. The passage is
deleted, not softened.

### 6c. `KEY_USAGE_COST_CAP_USD` removed

A spend cap whose correctness depends entirely on rates the code itself calls
"representative... verify before relying on it for real spend" is a safety
control that can silently fail open — worse than no cap, because the operator
believes they are covered. `KEY_USAGE_TOKEN_CAP` needs no rate table at all
(token counts come from the provider's usage response, exactly), and is
already documented as winning outright when both are set. A dollar-shaped
budget remains expressible: divide by the rate once, at config time, and set a
token cap.

Removals: `config.py:25,135` · `usage_cap_config.py:37,46` ·
`dispatcher.py:245-258` (the ternary at `:258` collapses to plain
`tokens >= token_cap`) · `deploy.py:99,964,1012` · `.env.config.example:54` ·
`store.py` runtime_config read/write (`:785,792,812,817`) · four test modules.

The documentation win exceeds the code win: README's cap section loses half
its length, and the **"`KEY_USAGE_TOKEN_CAP` wins outright when both are set"**
rule — currently stated in `config.py`, `README.md`, and
`.env.config.example` — vanishes, because there is no longer a second cap.

### 6d. Schema DDL: final shape, no migration code

`app/queue/store.py` currently carries 15 historical
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` lines (`:48-49`, `:55-66`, `:82`).
**These fold into their `CREATE TABLE IF NOT EXISTS` definitions and are
deleted.** A fresh clone should provision the final shape on first boot and
carry no migration machinery.

Consequences: `reviews.est_cost_usd` is declared nullable from the start
(replacing `NOT NULL` at `:78`), and `runtime_config` simply never has a
`key_usage_cost_cap_usd` column. No vestigial column, no `DROP COLUMN`, no
`ALTER`.

The corresponding action on the existing live database is an operational
one-off, deliberately **out of this spec** — see §9.

### 6e. `LLM_PROVIDER` required, but not as a pydantic required field

The decision is "no implicit default; fail loudly at startup". The *mechanism*
matters, because **`app/config.py:158` instantiates `settings = Settings()` at
module scope.** A pydantic required field would therefore raise at **import**
time, breaking `uv run pytest`, `encode_credential.py`, and — fatally —
`doctor.py` itself. Doctor exists to *tell you* `LLM_PROVIDER` is unset; it
must not crash on import before it can say so. It would also touch all 22
`Settings(...)` call sites in `tests/test_config.py`.

So: the field stays `str = ""` (the current `"gemini"` default at
`app/config.py:64` is deleted — no implicit provider), and validation happens
where it can produce a real message:

- **`app/main.py`'s lifespan** — fails startup loudly, matching the pattern
  already documented at `main.py:46` (*"allowed to propagate and fail startup
  loudly"*), with a message naming the three valid values.
- **`doctor` and `deploy.py::check_config`** — a FAIL row naming the fix,
  before a deploy ever happens.

Edge case resolved by *not* special-casing it: a DB provider override can
technically supply a provider at runtime, but `LLM_PROVIDER` stays required
regardless — the override is a switch layered on a base, not a substitute for
one.

### 6f. Rate provenance

`_RATES` values become a `NamedTuple`:
`Rate(rate_in, rate_out, source_url, verified)`. The arithmetic in
`estimate_cost_usd` is unchanged; the free-text comment block at the top of
`pricing.py` becomes queryable data.

**`scripts/pricing_check.py`** fetches Groq's `/openai/v1/models` — which
returns `pricing.prompt` / `pricing.completion` inline, and is where the
current Groq entry came from — compares against `_RATES`, and prints
paste-ready lines for anything missing or drifted. Google publishes no
equivalent endpoint, so those entries stay manual with a recorded URL.
Metadata-only, therefore clear of `CLAUDE.md`'s one-deliberate-live-call rule.
This feeds the generated `guide/reference/pricing.md`.

### 6g. Dashboard: "How it works" removed

Presentation-only scaffolding; a more polished version will live on the Pages
landing page. (It was added by
`docs/superpowers/specs/2026-08-11-how-it-works-section-design.md`.)

`app/static/dashboard.html` (664 lines) loses ~120:

- `:80` the standalone `#hiwJumpBtn` margin rule
- `:186-254` the `.how-it-works` / `.hiw-*` CSS block (it is the last run in
  the stylesheet — `:255` is `</style>`)
- `:259` the `hiwJumpBtn` button
- `:290-337` the `<section id="howItWorks">`
- `:369-380` (en) and `:408-419` (he) `hiw_*` string keys
- `:636-638` the scroll handler

**Keep `sp_name_security` / `_performance` / `_quality`** (`:365-366`,
`:404-405`) — they are also used by the reviews table via the mapping at
`:424-426`, not only by the flow diagram.

The content is not discarded: its five-step flow already mirrors README's
architecture diagram and seeds the Pages landing-page section.

## 7. Generated reference docs and CI

**`scripts/gen_docs.py`** emits four files into `guide/reference/`, each
stamped `<!-- generated by scripts/gen_docs.py — do not edit -->`:

| File | Source of truth |
|---|---|
| `config.md` | `Settings.model_fields` + `OPERATIONAL_KEYS` — name, type, default, secret-or-operational |
| `pricing.md` | `_RATES`, including §6f's source URL and verified date |
| `checks.md` | the deploy check table |
| `sync-env.md` | `--sync-env`'s push set |

`checks.md` needs one small refactor to have a source at all: today the check
table exists **only** as README prose. Each `check_*` gains a registry entry
(name, what it verifies, required-or-optional), which `run_checks`
(`deploy.py:1242`) then consumes too — removing a second hand-maintained list
rather than adding one.

Why generation rather than prose: the same value is currently written in up to
four places. `300` (the re-review cooldown base) appears in `app/config.py:114`,
as a commented line in `.env.config.example`, in `README.md:340` prose, and in
`SETUP.md` §3.7. Same for `04:00 UTC`, the `45s` timeout, the ten-check table,
and the `--sync-env` push set. Generation makes it structurally impossible for
a doc to disagree with the code.

**CI gains two jobs:**

1. Regenerate and fail on `git diff --exit-code guide/reference/`.
2. Build and deploy the MkDocs site to GitHub Pages on push to `main`.

No interaction with Render: `render.yaml`'s
`buildFilter.ignoredPaths: ["**/*.md"]` already means an all-Markdown `guide/`
push never triggers a redeploy.

## 8. Testing

**8a. Secret-leak sentinel suite** (the security-critical one):

- Temp `.env` where every value is a unique high-entropy sentinel. Capture
  stdout, stderr, the `CheckResult` list, the `--json` payload, **and** any
  exception's `repr` plus formatted traceback. Assert no sentinel appears in
  any of them.
- **Negative control in the same test:** assert key *names* and a plausible
  length *do* appear — otherwise a probe that silently returns nothing passes
  trivially.
- **Error path:** `CLAUDE.md` notes that pydantic's `ValidationError` echoes
  `input_value`. Force a secret-bearing field to fail validation; assert the
  surfaced message carries the field name and a structural reason, never the
  sentinel.
- **Round-trip through `--json`**, since `json.dumps` on a carelessly built
  payload is a distinct leak path from `print`.

**8b. Doctor state machine** — table-driven over every step of both tracks: given a synthetic
state, assert the reported step number and next command. Remote probes mocked.

**8c. Prerequisites** — monkeypatch `shutil.which`; parameterize
`platform.system()` over Linux/Darwin/Windows so per-OS hints are verified *on
any OS*.

**8d. Pricing-optional** — unknown model → `estimate_cost_usd` returns `None`;
`format_comment` omits both fragments and stays well-formed; NULL round-trips
through the store.

**8e. Import-time regression guard** — importing `app.config` with
`LLM_PROVIDER` unset must **not** raise; the lifespan **must**. This pins the
§6e trap.

**8f. Cost-cap removal** — delete `test_cost_cap_applies_when_no_token_cap_is_set`
(`tests/test_dispatcher.py:1020`) and `test_token_cap_wins_outright_when_both_caps_are_set`
(`:1040`, no second cap left to win against); simplify
`test_no_cap_configured_never_queries_usage` (`:1058`) to a single cap. Assert
`KEY_USAGE_COST_CAP_USD` is absent from `OPERATIONAL_KEYS` and read nowhere.

**8g. Manifest flow** — mocked conversions endpoint; assert the three keys land
in `.env` and that nothing is printed; sentinel-checked as in 8a.

**8h. `gen_docs.py` determinism** — run twice, byte-identical.

**8i. Dashboard** — no `hiw_` key remains; `sp_name_*` still resolve.

**8j. Track selection** — auto-detection resolves to `hosted` with
`RENDER_API_KEY` set or an `onrender.com` base URL, `local` otherwise; the
explicit `--track` flag overrides both. Track A grades the tunnel probe;
Track B does not.

**8k. Encoding rule** — a repo-wide test asserting every `open`/`read_text`/
`write_text` call in new modules passes `encoding=`, and every write passes
`newline=`. Plus a round-trip: `gen_docs.py` output is byte-identical when the
process locale encoding is forced to cp1252.

**8l. Anchor integrity** — `deploy.py`'s guide URL resolves to a file that
exists under `guide/`.

Live verification is unchanged: one deliberate call per genuine need, per
`CLAUDE.md`'s LLM API testing hygiene rules.

## 9. Operational one-off (deliberately out of this spec)

The existing live database must be recreated rather than migrated. Procedure:
drop `tickets`, `reviews`, and `runtime_config`; restart the Render service;
boot DDL recreates them in the §6d shape.

Three consequences to handle deliberately rather than discover:

- `reviews` is gone → the dashboard starts empty **and usage-cap accounting
  resets to zero**, since it is summed from that table rather than counted in
  memory.
- `tickets` is gone → any ticket deferred on a cooldown or rate-limit wait is
  never reviewed. **Do this with a drained queue.**
- `runtime_config` is gone → provider/model/key-index overrides and synced
  cooldown/cap values reset. Follow with `deploy.py --sync-config-db` and
  re-apply any `set_override`.

`scripts/reset_queue.py` cannot serve here — it truncates, and the schema
itself is changing. **No `--recreate` flag is added to it**, per the
no-migration-code-in-a-fresh-clone principle.

## 10. Build order

This is large for a single plan. It decomposes along a natural dependency
seam, and the order matters: **app changes first**, because they change what
the documentation must say. Writing the guide before §6 lands would mean
documenting behavior that is about to be deleted.

1. **App changes (§6)** — pricing optional, refusals to warnings, cost cap
   removed, DDL folded to final shape, `LLM_PROVIDER` required, rate
   provenance, dashboard section removed. Self-contained; ships and is
   verifiable on its own. §9's one-off happens at the end of this stage.
2. **Setup tooling (§4)** — `doctor.py` (including the prerequisite stage and
   the §4d-i secret-safe probing), `init_env.py`, `create_github_app.py`, and
   `.claude/commands/setup.md`. Depends on stage 1 only for
   `check_config`'s `LLM_PROVIDER` FAIL row.
3. **Docs, site, generation, CI (§3, §5, §7)** — `gen_docs.py` and the check
   registry, the `guide/` tree, README's reduction, the OS-idiom fixes, the
   MkDocs build, and both CI jobs. Depends on 1 and 2 being final, since it
   documents them and generates from them.

Each stage is independently mergeable and leaves the repo in a working state.

## 11. Non-goals

- No migration framework, and no migration code of any kind in the repo (§6d,
  §9).
- No removal of the estimated-cost figure from the PR comment. `cost.md`
  remains the graded artifact, and the figure is already hedged (`~$`, `est.`)
  at `formatting.py:117,129`; what it lacked was provenance, addressed in §6f.
- No "priced N of M reviews" counter on the dashboard (§6a).
- No `DROP COLUMN` on `runtime_config` (§6d).
- No wizard that owns setup flow control. Four steps are irreducibly
  browser-bound; a wizard would spend most of its runtime as a "press Enter
  when done" prompt around work it cannot do, and would be fragile the moment
  an operator deviates or resumes the next day. The doctor reports state; the
  human sequences.
- No GitHub Wiki. The guide stays versioned with the code it documents and
  reviewable in PRs.
- No tunnel process management. Doctor probes for `cloudflared` and detects a
  working tunnel, but never starts, supervises, or tears one down (§4d).
- No named-tunnel / stable-hostname setup, and no polling fallback for webhook
  delivery. Track A accepts an ephemeral URL and a re-run of step 7A (§4a-i).
- No changes to the review pipeline, specialists, queue semantics, or provider
  adapters beyond those named in §6.
