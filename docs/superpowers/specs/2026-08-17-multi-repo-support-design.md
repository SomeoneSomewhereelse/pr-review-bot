# Design — Multi-repo support (single GitHub App installation, optional allowlist)

**Date:** 2026-08-17
**Status:** Approved for planning
**Relates to:** `app/config.py` (`Settings`), `app/webhook.py` (`_enqueue_from_payload`),
`app/main.py` (`lifespan`), `app/github_app.py` (`discover_installation_id`,
`get_installation_auth`/`get_installation_client`), `scripts/deploy.py`
(`check_config`, `check_installation_and_webhook`, `_wanted_env`, `sync_env`, `main`),
`README.md`, `SETUP.md`, `SPEC.md`.

## 1. Problem

The bot is hard-restricted to exactly one repo via `GITHUB_TARGET_REPO`, a single string:

- `app/webhook.py`'s `_enqueue_from_payload` drops any webhook whose
  `repository.full_name` doesn't equal `settings.github_target_repo`.
- `app/main.py`'s boot logic, when `GITHUB_APP_INSTALLATION_ID` is unset, resolves the
  installation id **once**, seeded by that one repo
  (`github_app.discover_installation_id(settings.github_target_repo)`), and caches it into
  `settings.github_app_installation_id` for the process lifetime.
- `scripts/deploy.py` requires exactly one `GITHUB_TARGET_REPO` to run at all.

Everything downstream of the webhook filter is already repo-agnostic: `app/queue/store.py`'s
`tickets` table is keyed on `(repo_full_name, pr_number)` with `repo_full_name` a real column
on every row; `app/queue/dispatcher.py` and `app/github_app.py`'s diff-fetch/comment
functions already take `repo_full_name` as an explicit per-call parameter, not a global; the
dashboard has no reference to `GITHUB_TARGET_REPO` at all. So the actual restriction lives in
exactly two places: the webhook's allowlist check, and installation-id resolution being
baked-in-once around a single repo.

## 2. Scope decision

GitHub App installations are account/org-scoped, not repo-scoped — one installation can
already cover multiple repos in the same account/org (installed with "all repos" or several
explicitly-selected repos), and `GET /repos/{repo}/installation` returns the same
installation id for every repo under that one installation.

**Decided scope: multiple repos under the same GitHub account/org, one App installation.**
Repos across different accounts/orgs (each needing its own separate App installation) is
explicitly out of scope — that would need per-repo installation-id resolution at request
time instead of one value cached at boot, a materially bigger change. If that installation
lookup ever finds more than one installation for this App, it's treated as this
out-of-scope case and surfaced as a clear, actionable boot error (see §3), not silently
handled.

`GITHUB_TARGET_REPO` becomes **optional**: when set, it's an allowlist restricting which of
the installation's repos the bot actually acts on; when unset, the bot acts on every repo
the App installation is registered with.

## 3. Decision

### 3a. `app/config.py`

`github_target_repo: str = ""` stays as the raw field — comma-separated
(`"org/repo-a,org/repo-b"`), unchanged env var name, still listed in `OPERATIONAL_KEYS` as
`"GITHUB_TARGET_REPO"`. A single existing value (no comma) keeps working unchanged, so no
currently-deployed single-repo config needs to change.

`,` is safe as a delimiter because it can never occur inside a genuine `owner/repo` value:
GitHub restricts repo names to ASCII letters, digits, `.`, `-`, and `_`, and account/org
names to alphanumeric characters and `-` — a comma is illegal in either segment, so
splitting on it can never misinterpret a real repo's name.

New method:

```python
def target_repos(self) -> frozenset[str]:
    """Configured repo allowlist, or empty (= no restriction, act on every repo
    the App installation is registered with)."""
    return frozenset(r.strip() for r in self.github_target_repo.split(",") if r.strip())
```

### 3b. `app/webhook.py`

`_enqueue_from_payload` replaces the equality check:

```python
target_repos = settings.target_repos()
if target_repos and repo_full_name not in target_repos:
    logger.info("Ignoring webhook for non-target repo %s", repo_full_name)
    return
```

When `target_repos` is empty, every repo is accepted. This is not a new trust boundary: the
HMAC check earlier in `webhook()` already guarantees the payload genuinely came from this
App instance, and GitHub only ever delivers `pull_request` events for repos the App is
actually installed on. The allowlist is a narrowing an operator opts into, not the
mechanism that establishes legitimacy.

### 3c. `app/github_app.py` — app-level installation discovery

New function, used at boot in place of the repo-scoped one:

```python
def discover_installation_id_for_app() -> int:
    """Return the App's single installation id (GET /app/installations, App JWT).

    Raises AppNotInstalledError if there are zero installations. Raises a plain
    RuntimeError naming every installation's account login if there is more than
    one -- this project's scope (see design doc) is one account/org per App; a
    second installation means either a stray install to clean up or a genuine
    multi-org need, and either way an operator must pin GITHUB_APP_INSTALLATION_ID
    explicitly rather than have one silently picked for them.
    """
```

New function, used only by `scripts/deploy.py`'s verification check (§3d):

```python
def list_installation_repos() -> list[str]:
    """Full names of repos the installation token can access (GET
    /installation/repositories, first page only). Used for the deploy check's
    display/verification, not for security enforcement -- the webhook's
    legitimacy guarantee comes from HMAC verification, not from this list."""
```

`discover_installation_id(repo)` (existing, repo-scoped) is unchanged and keeps its
docstring/behavior; its only caller becomes `scripts/deploy.py`.

### 3d. `app/main.py`

`lifespan` drops the `settings.github_target_repo` argument entirely:

```python
if not settings.github_app_installation_id:
    settings.github_app_installation_id = await asyncio.to_thread(
        github_app.discover_installation_id_for_app
    )
```

This is identical whether `target_repos()` is empty or populated — boot no longer depends
on there being any configured repo at all, which is what makes track-all mode possible in
the first place (there'd otherwise be no repo to seed the old repo-scoped lookup with).

### 3e. `scripts/deploy.py`

- **`check_config()`**: drops the `GITHUB_TARGET_REPO` "missing" check — it's optional now.
- **`check_installation_and_webhook`**: signature changes from `(repo: str, base: str)` to
  `(repos: frozenset[str], base: str)`. Resolves the installation id via
  `discover_installation_id_for_app()`. If `repos` is non-empty, calls
  `list_installation_repos()` once and reports any configured repo **not** present in that
  list as a FAIL, naming it — this is the only place such a misconfiguration is ever
  visible (see §4). If `repos` is empty, reports the installation id and repo count/list as
  PASS with no per-repo check (nothing is configured to verify).
- **`main()`**: no longer requires `settings.github_target_repo` to be truthy — only `base`
  (public URL) is required. Passes `settings.target_repos()` into `run_checks`.
- **`_wanted_env()` / `sync_env()`**: `GITHUB_TARGET_REPO` must be push-able as an
  **empty** value — that's a valid, meaningful "track all" config, not a missing one.
  `sync_env()`'s existing `empty = sorted(key for key, value in wanted.items() if not
  value)` guard gets a small allow-empty exception for this one key (e.g. a
  `_OPTIONAL_EMPTY_KEYS = {"GITHUB_TARGET_REPO"}` consulted by that guard), so the
  guard's "refuse to push empty values" behavior doesn't misfire on a deliberately-empty
  track-all config while still catching genuinely-missing required values.
- **`--health-only`** help text is unchanged (already correctly says no
  `GITHUB_TARGET_REPO` is needed for that path).

### 3f. Docs

- **README.md / SETUP.md**: `GITHUB_TARGET_REPO` documented as optional and
  comma-separated; unset means "track every repo this App installation covers." Clarify
  that the App itself can be installed on "all repos" or a specific subset via GitHub's
  own UI, independent of this allowlist — the allowlist narrows further, it doesn't
  substitute for installation scope.
- **SPEC.md**: update the single-repo restriction language to reflect the new scope.

## 4. Behavior clarification: two distinct "wrong repo" cases

These are easy to conflate but are not the same thing, and only one of them is visible to
the webhook at all:

- **Repo not in the allowlist** (handled by `_enqueue_from_payload`): the App *is* installed
  on that repo, so GitHub *does* deliver events for it — the filter deliberately drops them
  because the operator didn't include that repo in `GITHUB_TARGET_REPO`. Intentional,
  already logged.
- **Misconfigured allowlist entry** (caught only by `deploy.py`'s check): the operator
  listed a repo in `GITHUB_TARGET_REPO`, intending it to be covered, but the App was never
  installed there. **No webhook ever arrives for that repo at all** — there's nothing for
  the webhook filter to reject or log. This looks, from the operator's side, identical to
  "the bot just isn't running" on that repo, with zero visibility into why. `deploy.py`'s
  `check_installation_and_webhook` is the only place this can be caught, which is why it's
  a FAIL there rather than being redundant with the webhook's own silent-drop logging.

## 5. Testing

- **`tests/test_config.py`** (or a new small test): `Settings.target_repos()` — splits and
  strips a comma-separated value, empty string → empty frozenset, whitespace-only entries
  ignored.
- **`tests/test_webhook.py`**: extend/replace `test_webhook_ignores_non_target_repo` with:
  a comma-separated multi-repo allowlist accepting a listed repo and rejecting an unlisted
  one; empty `github_target_repo` accepting any repo.
- **`tests/test_main_lifespan.py`**: update the boot test to call
  `discover_installation_id_for_app` instead of the repo-scoped function; add a case
  covering "installation id unset, `github_target_repo` unset" (the track-all boot path)
  succeeding.
- **`app/github_app.py`** unit tests: `discover_installation_id_for_app` — zero
  installations raises `AppNotInstalledError`; exactly one returns its id; more than one
  raises `RuntimeError` naming every account login; `list_installation_repos` returns the
  full-name list from a mocked response.
- **`tests/test_deploy_script.py`**: `check_installation_and_webhook` — empty `repos` PASS
  path (installation id + count, no per-repo check); non-empty `repos` with every entry
  covered (PASS); non-empty `repos` with one entry not covered by the installation (FAIL
  naming it). `check_config` no longer flags a missing `GITHUB_TARGET_REPO`. `sync_env`
  pushes an empty `GITHUB_TARGET_REPO` without tripping the empty-value guard, while an
  actually-missing required var still trips it.

No live calls: every behavior here is deterministic and mockable (GitHub API responses
faked), consistent with `SPEC.md` §8 and `CLAUDE.md`'s LLM-testing-hygiene rules — nothing
in this design makes an LLM call, and nothing here touches live GitHub API credentials
beyond what existing tests already mock.

## 6. Non-goals

- **Cross-account/cross-org multi-repo** (separate App installations per repo,
  per-ticket installation-id resolution, persisted per-repo installation ids). Explicitly
  deferred — see §2. If this becomes a real need later, the natural extension point is
  storing installation ids the same way provider overrides live in `runtime_config`,
  resolved per ticket rather than baked into `Settings` at startup; this design doesn't
  foreclose that but doesn't build any of it.
- **Per-repo configuration** (different providers, models, or cooldown settings per repo).
  Out of scope — every repo the bot acts on shares the same global provider/model/cap
  configuration, unchanged from today.
- **Paginating `list_installation_repos()` beyond the first page.** Used only for a
  deploy-time display/verification check, not a security boundary; a demo/small-scale
  deployment won't exceed one page. Revisit only if an installation genuinely spans enough
  repos to need it.
