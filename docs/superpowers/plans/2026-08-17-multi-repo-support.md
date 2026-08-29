# Multi-repo Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the bot act on more than one repo under a single GitHub App installation, with `GITHUB_TARGET_REPO` becoming an optional comma-separated allowlist (empty = track every repo the installation covers).

**Architecture:** `Settings.target_repos()` parses the existing `GITHUB_TARGET_REPO` string into a `frozenset[str]`; `app/webhook.py`'s filter and `scripts/deploy.py`'s checks consume it. Installation-id resolution moves from a repo-scoped GitHub API call to a new App-level one (`discover_installation_id_for_app`), since this project's scope is one GitHub App installation per account/org — no repo needs to seed the lookup anymore.

**Tech Stack:** Python, FastAPI, PyGithub, pytest (existing stack — no new dependencies).

**Spec:** `docs/superpowers/specs/2026-08-17-multi-repo-support-design.md`

## Global Constraints

- No live GitHub/LLM API calls in any test — everything is mocked (respx for `httpx`, the `fake_transport`/`github_seam` harnesses for PyGithub), per `SPEC.md` §8 and `CLAUDE.md`'s LLM-testing-hygiene rules.
- `GITHUB_TARGET_REPO` env var name is unchanged and stays in `app/config.py`'s `OPERATIONAL_KEYS` — no rename, no new key.
- Never touch `.env` (secrets file) directly; `.env.config` (operational, already open-able) may be referenced but this plan does not need to edit it.
- Every existing single-repo test/config (no comma in `GITHUB_TARGET_REPO`) must keep passing unchanged — this is an additive change, not a breaking one.

---

### Task 1: `Settings.target_repos()`

**Files:**
- Modify: `app/config.py:143` (end of `Settings` class, after the `render_service_name` field)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.target_repos() -> frozenset[str]` — splits `self.github_target_repo` on `,`, strips whitespace, drops empty entries; empty input → empty frozenset. Consumed by Task 2 (`app/webhook.py`) and Task 5 (`scripts/deploy.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` (near the other `Settings`-behavior tests, e.g. after `test_vertex_model_defaults_to_the_confirmed_working_vertex_model`):

```python
def test_target_repos_splits_comma_separated_list():
    settings = Settings(github_target_repo="org/repo-a,org/repo-b", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_strips_whitespace_around_entries():
    settings = Settings(github_target_repo=" org/repo-a , org/repo-b ", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo-a", "org/repo-b"})


def test_target_repos_empty_string_means_no_restriction():
    settings = Settings(github_target_repo="", _env_file=None)
    assert settings.target_repos() == frozenset()


def test_target_repos_single_value_has_no_comma():
    settings = Settings(github_target_repo="org/repo", _env_file=None)
    assert settings.target_repos() == frozenset({"org/repo"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k target_repos -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'target_repos'`

- [ ] **Step 3: Implement `target_repos()`**

In `app/config.py`, add this method as the last member of the `Settings` class, right after the `render_service_name: str = "pr-review-engine"` field and before the blank line preceding `settings = Settings()`:

```python

    def target_repos(self) -> frozenset[str]:
        """Configured repo allowlist, or empty (= no restriction -- act on
        every repo this App's installation is registered with).

        ',' is a safe delimiter: GitHub repo names may only contain ASCII
        letters, digits, '.', '-', and '_', and account/org names only
        alphanumeric characters and '-' -- a comma can never occur inside a
        genuine "owner/repo" value, so splitting on it can't misinterpret a
        real repo's name.
        """
        return frozenset(r.strip() for r in self.github_target_repo.split(",") if r.strip())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k target_repos -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full config test file to check for regressions**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add Settings.target_repos() for optional multi-repo allowlist"
```

---

### Task 2: Webhook filter accepts an allowlist or track-all

**Files:**
- Modify: `app/webhook.py:57-59`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `settings.target_repos() -> frozenset[str]` (Task 1).
- Produces: no new public interface — behavior change only (webhook accepts any repo when `target_repos()` is empty, else only repos in the set).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_webhook.py`, after the existing `test_webhook_ignores_non_target_repo`:

```python
async def test_webhook_accepts_repo_listed_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/repo-b"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-match"})
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]


async def test_webhook_rejects_repo_not_in_comma_separated_allowlist(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/repo-a,owner/repo-b")
    payload = {"action": "opened",
               "repository": {"full_name": "owner/OTHER-repo"},
               "pull_request": {"number": 9, "head": {"sha": "def"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-multi-nonmatch"})
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]


async def test_webhook_accepts_any_repo_when_target_repo_unset(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/any-repo"},
               "pull_request": {"number": 3, "head": {"sha": "xyz"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-trackall"})
    assert resp.status_code == 202
    assert db_query("SELECT count(*) FROM tickets") == [(1,)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_webhook.py -k "allowlist or trackall" -v`
Expected: the match/track-all tests FAIL (0 tickets enqueued instead of 1) because `_enqueue_from_payload` still does an exact-string equality check against the whole comma-joined value.

- [ ] **Step 3: Update the webhook filter**

In `app/webhook.py`, replace:

```python
    if repo_full_name != settings.github_target_repo:
        logger.info("Ignoring webhook for non-target repo %s", repo_full_name)
        return
```

with:

```python
    target_repos = settings.target_repos()
    if target_repos and repo_full_name not in target_repos:
        logger.info("Ignoring webhook for non-target repo %s", repo_full_name)
        return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_webhook.py -v`
Expected: all PASS, including the pre-existing `test_webhook_ignores_non_target_repo` (a single value with no comma still works as a one-element allowlist).

- [ ] **Step 5: Commit**

```bash
git add app/webhook.py tests/test_webhook.py
git commit -m "feat: accept a multi-repo allowlist (or track-all when unset) in the webhook filter"
```

---

### Task 3: App-level installation discovery + installation repo listing

**Files:**
- Modify: `app/github_app.py` (insert after `discover_installation_id`, before `set_webhook_url`)
- Test: `tests/test_github_app.py`

**Interfaces:**
- Produces:
  - `discover_installation_id_for_app() -> int` — resolves the App's single installation id via `GET /app/installations` (no repo argument). Raises `AppNotInstalledError` (existing type) if zero installations; raises `RuntimeError` naming every installation's account login if more than one.
  - `list_installation_repos() -> list[str]` — full names of repos the installation token can access, via `GET /installation/repositories` (first page only).
  - Consumed by Task 4 (`app/main.py`) and Task 5 (`scripts/deploy.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_app.py`, after `test_discover_installation_id_non_404_is_not_app_not_installed` (before `test_get_webhook_url_returns_the_configured_url`):

```python
def test_discover_installation_id_for_app_returns_id_with_one_installation(fake_transport):
    fake_transport.route(
        "GET", "/app/installations", [{"id": 555, "account": {"login": "someone"}}]
    )
    assert github_app.discover_installation_id_for_app() == 555


def test_discover_installation_id_for_app_raises_app_not_installed_when_empty(fake_transport):
    fake_transport.route("GET", "/app/installations", [])
    with pytest.raises(github_app.AppNotInstalledError):
        github_app.discover_installation_id_for_app()


def test_discover_installation_id_for_app_raises_when_ambiguous(fake_transport):
    fake_transport.route(
        "GET",
        "/app/installations",
        [
            {"id": 1, "account": {"login": "org-a"}},
            {"id": 2, "account": {"login": "org-b"}},
        ],
    )
    with pytest.raises(RuntimeError) as exc_info:
        github_app.discover_installation_id_for_app()
    message = str(exc_info.value)
    assert "org-a" in message
    assert "org-b" in message
    assert "GITHUB_APP_INSTALLATION_ID" in message


def test_list_installation_repos_returns_full_names(fake_transport):
    fake_transport.route(
        "GET",
        "/installation/repositories",
        {
            "total_count": 2,
            "repositories": [
                {"full_name": "someone/repo-a"},
                {"full_name": "someone/repo-b"},
            ],
        },
    )
    assert github_app.list_installation_repos() == ["someone/repo-a", "someone/repo-b"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_app.py -k "for_app or list_installation_repos" -v`
Expected: FAIL with `AttributeError: module 'app.github_app' has no attribute 'discover_installation_id_for_app'` (and similarly for `list_installation_repos`).

- [ ] **Step 3: Implement both functions**

In `app/github_app.py`, insert the following between the end of `discover_installation_id` (the line `return int(data["id"])`) and `def set_webhook_url`:

```python
def discover_installation_id_for_app() -> int:
    """Return the App's single installation id (GET /app/installations, App JWT).

    This project's scope (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md)
    is one GitHub account/org per App installation -- so, unlike
    discover_installation_id(repo), this needs no repo to seed the lookup and
    works whether or not GITHUB_TARGET_REPO is configured.

    Raises `AppNotInstalledError` if the App has no installations at all.
    Raises a plain `RuntimeError` naming every installation's account login if
    there is more than one -- that's the out-of-scope cross-org case; an
    operator must pin GITHUB_APP_INSTALLATION_ID explicitly rather than have
    one silently chosen for them.
    """
    gh = _app_jwt_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    if not data:
        raise AppNotInstalledError(
            "GitHub App has no installations: install it once via the GitHub UI "
            "(repo or org Settings -> GitHub Apps), then redeploy."
        )
    if len(data) > 1:
        accounts = ", ".join(installation["account"]["login"] for installation in data)
        raise RuntimeError(
            f"GitHub App has multiple installations ({accounts}) -- set "
            "GITHUB_APP_INSTALLATION_ID explicitly to pick one."
        )
    return int(data[0]["id"])


def list_installation_repos() -> list[str]:
    """Full names of repos the installation token can access (GET
    /installation/repositories, first page only).

    Used by scripts/deploy.py's github-app check for display/verification of a
    configured GITHUB_TARGET_REPO allowlist -- not a security boundary. The
    webhook's legitimacy guarantee comes from HMAC verification
    (app/webhook.py), not from this list.
    """
    gh = get_installation_client()
    _, data = gh.requester.requestJsonAndCheck("GET", "/installation/repositories")
    return [repo["full_name"] for repo in data.get("repositories", [])]


```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/github_app.py tests/test_github_app.py
git commit -m "feat: add app-level installation discovery and installation repo listing"
```

---

### Task 4: Boot resolves the installation id at the App level

**Files:**
- Modify: `app/main.py:21-30`
- Test: `tests/test_main_lifespan.py`

**Interfaces:**
- Consumes: `github_app.discover_installation_id_for_app() -> int` (Task 3).
- Produces: no new public interface — `lifespan`'s boot behavior no longer depends on `settings.github_target_repo` being set.

- [ ] **Step 1: Update the two affected tests (they currently assert the old repo-scoped call)**

In `tests/test_main_lifespan.py`, replace `test_lifespan_skips_discovery_when_installation_id_already_set`:

```python
async def test_lifespan_skips_discovery_when_installation_id_already_set(monkeypatch):
    """When GITHUB_APP_INSTALLATION_ID is already configured (e.g. local dev's
    .env), lifespan must not spend a GitHub App JWT call rediscovering it."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)

    def _boom() -> int:
        raise AssertionError(
            "discover_installation_id_for_app must not be called when already set"
        )

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _boom)

    async with main.lifespan(main.app):
        pass

    assert settings.github_app_installation_id == 123456
```

and replace `test_lifespan_discovers_installation_id_when_unset`:

```python
async def test_lifespan_discovers_installation_id_when_unset(monkeypatch):
    """When GITHUB_APP_INSTALLATION_ID is unset (0, e.g. on Render — see design
    spec §6, "becomes optional (auto-discovered)"), lifespan must resolve it
    via github_app.discover_installation_id_for_app before the dispatcher
    starts, and assign the resolved id onto settings -- app-level discovery,
    so this works regardless of whether GITHUB_TARGET_REPO is set (multi-repo
    support design doc §3d)."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 0)
    monkeypatch.setattr(settings, "github_target_repo", "")

    calls = []

    def _fake_discover() -> int:
        calls.append(1)
        return 999999

    monkeypatch.setattr(main.github_app, "discover_installation_id_for_app", _fake_discover)

    async with main.lifespan(main.app):
        pass

    assert calls == [1]
    assert settings.github_app_installation_id == 999999
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_main_lifespan.py -k discover -v`
Expected: FAIL — `main.lifespan` still calls `github_app.discover_installation_id` (repo-scoped, still exists and is unpatched), so the `_boom`/`_fake_discover` patches on `discover_installation_id_for_app` are never hit, and `test_lifespan_skips_discovery_when_installation_id_already_set` behaves fine (it's a no-discovery path) but `test_lifespan_discovers_installation_id_when_unset`'s `calls == [1]` assertion fails (empty list) since the real (unpatched) `discover_installation_id` runs instead and raises since there's no network mock, causing a lifespan `RuntimeError`.

- [ ] **Step 3: Update `app/main.py`**

Replace:

```python
    if not settings.github_app_installation_id:
        # Not set (e.g. on Render, per docs/superpowers/specs/.../design.md
        # §6: the installation id "becomes optional (auto-discovered)").
        # Resolve it once via the App JWT before anything tries to use it.
        # A genuine RuntimeError here (App not installed) is allowed to
        # propagate and fail startup loudly -- same pattern as init_pool()
        # failing loudly on an unreachable Postgres.
        settings.github_app_installation_id = await asyncio.to_thread(
            github_app.discover_installation_id, settings.github_target_repo
        )
```

with:

```python
    if not settings.github_app_installation_id:
        # Not set (e.g. on Render, per docs/superpowers/specs/.../design.md
        # §6: the installation id "becomes optional (auto-discovered)").
        # Resolve it once via the App JWT before anything tries to use it --
        # app-level (not repo-scoped), so this works whether or not
        # GITHUB_TARGET_REPO is configured (see docs/superpowers/specs/
        # 2026-08-17-multi-repo-support-design.md). A genuine RuntimeError
        # here (App not installed, or installed on more than one account) is
        # allowed to propagate and fail startup loudly -- same pattern as
        # init_pool() failing loudly on an unreachable Postgres.
        settings.github_app_installation_id = await asyncio.to_thread(
            github_app.discover_installation_id_for_app
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main_lifespan.py
git commit -m "feat: resolve GitHub App installation id at the app level, not per-repo"
```

---

### Task 5: `scripts/deploy.py` — optional repo, allowlist verification, track-all display

**Files:**
- Modify: `scripts/deploy.py` (`check_config`, new `_OPTIONAL_EMPTY_ENV_KEYS`, `check_installation_and_webhook`, `run_checks`, `main`, `sync_env`)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `settings.target_repos() -> frozenset[str]` (Task 1), `github_app.discover_installation_id_for_app() -> int` and `github_app.list_installation_repos() -> list[str]` (Task 3).
- Produces: `check_installation_and_webhook(repos: frozenset[str], base: str) -> CheckResult` (signature changed from `(repo: str, base: str)`); `run_checks(repos: frozenset[str], base: str) -> list[CheckResult]` (signature changed from `(repo: str, base: str)`).

- [ ] **Step 1: Update `check_config`'s tests**

In `tests/test_deploy_script.py`, replace `test_check_config_names_every_missing_key_at_once`:

```python
def test_check_config_names_every_missing_key_at_once(complete_config, monkeypatch):
    """One run should surface all of them, not the first alphabetically."""
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "github_app_private_key", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GITHUB_WEBHOOK_SECRET" in result.detail
    assert "GITHUB_APP_PRIVATE_KEY" in result.detail
```

and add a new test directly after it:

```python
def test_check_config_passes_with_an_empty_target_repo(complete_config, monkeypatch):
    """GITHUB_TARGET_REPO is optional (multi-repo support design doc §3e) --
    an empty value (track-all mode) must not be reported as missing."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    assert deploy.check_config().status == "PASS"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "check_config_names_every_missing or check_config_passes_with_an_empty_target_repo" -v`
Expected: `test_check_config_names_every_missing_key_at_once` FAILs (no `"GITHUB_APP_PRIVATE_KEY"` in detail, since `check_config` doesn't check that field's emptiness the way the test now expects alongside the webhook secret — actually verify: it still checks `github_app_private_key`; this test currently passes on that assertion already, the real failure is `test_check_config_passes_with_an_empty_target_repo`, which FAILs because `check_config()` still reports `GITHUB_TARGET_REPO` as missing.

- [ ] **Step 3: Update `check_config`**

In `scripts/deploy.py`, inside `check_config()`, remove:

```python
    if not settings.github_target_repo:
        missing.append("GITHUB_TARGET_REPO")
```

(This sits between the `GITHUB_WEBHOOK_SECRET` check and the `PUBLIC_BASE_URL` check — delete only these two lines, leaving the surrounding checks untouched.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -k "check_config" -v`
Expected: all PASS

- [ ] **Step 5: Update the `github_seam` fixture and its dependent tests**

In `tests/test_deploy_script.py`, replace the `github_seam` fixture:

```python
@pytest.fixture
def github_seam(monkeypatch):
    """Monkeypatch the github_app boundary and record webhook writes.

    The check's job is the decision logic (read -> compare -> conditionally
    write); github_app's own HTTP behavior is covered in tests/test_github_app.py
    with the requests-level fake_transport harness, which respx cannot replace.
    """
    from app import github_app

    state = {
        "installation_id": 424242,
        "current_url": "",
        "written": [],
        "repos": ["owner/repo"],
    }

    monkeypatch.setattr(
        github_app, "discover_installation_id_for_app", lambda: state["installation_id"]
    )
    monkeypatch.setattr(github_app, "list_installation_repos", lambda: state["repos"])
    monkeypatch.setattr(github_app, "get_webhook_url", lambda: state["current_url"])
    monkeypatch.setattr(github_app, "set_webhook_url", lambda url: state["written"].append(url))
    return state
```

Then update every existing call to `deploy.check_installation_and_webhook("owner/repo", "https://x.onrender.com")` in this file to `deploy.check_installation_and_webhook(frozenset({"owner/repo"}), "https://x.onrender.com")` — this applies to `test_webhook_already_correct_passes_without_writing`, `test_webhook_mismatch_is_updated`, `test_webhook_absent_is_set_on_first_deploy`, `test_failed_webhook_read_does_not_write`, and `test_failed_webhook_write_fails_with_the_status`.

Replace `test_app_not_installed_fails_with_an_actionable_detail`:

```python
def test_app_not_installed_fails_with_an_actionable_detail(github_seam, monkeypatch):
    from app import github_app

    def _raise():
        raise github_app.AppNotInstalledError("not installed")

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "install" in result.detail.lower()
    assert github_seam["written"] == []
```

Replace `test_installation_lookup_non_404_reports_the_underlying_status`:

```python
def test_installation_lookup_non_404_reports_the_underlying_status(github_seam, monkeypatch):
    """A 401 (bad key) and a 502 (GitHub degraded) must render differently --
    the generic RuntimeError message alone collapses both to the same string."""
    from github import GithubException

    from app import github_app

    def _raise():
        try:
            raise GithubException(401, {"message": "bad credentials"}, None)
        except GithubException as exc:
            raise RuntimeError("installation lookup failed") from exc

    monkeypatch.setattr(github_app, "discover_installation_id_for_app", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "401" in result.detail
    assert github_seam["written"] == []
```

Add three new tests directly after `test_installation_lookup_non_404_reports_the_underlying_status`:

```python
def test_installation_and_webhook_track_all_reports_installed_repo_count(github_seam):
    github_seam["repos"] = ["owner/a", "owner/b", "owner/c"]
    github_seam["current_url"] = "https://x.onrender.com/webhook"
    result = deploy.check_installation_and_webhook(frozenset(), "https://x.onrender.com")
    assert result.status == "PASS"
    assert "tracking all 3" in result.detail


def test_installation_and_webhook_flags_allowlist_entry_not_covered(github_seam):
    github_seam["repos"] = ["owner/repo"]
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo", "owner/missing-repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "owner/missing-repo" in result.detail


def test_installation_and_webhook_repo_list_failure_reports_status(github_seam, monkeypatch):
    from github import GithubException

    from app import github_app

    def _raise():
        raise GithubException(500, {"message": "boom"}, None)

    monkeypatch.setattr(github_app, "list_installation_repos", _raise)
    result = deploy.check_installation_and_webhook(
        frozenset({"owner/repo"}), "https://x.onrender.com"
    )
    assert result.status == "FAIL"
    assert "500" in result.detail
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "webhook or installation" -v`
Expected: every `github_seam`-based test FAILs with `AttributeError` or `TypeError` (`check_installation_and_webhook()` still takes `repo: str` and calls the old repo-scoped `discover_installation_id`, which the fixture no longer patches).

- [ ] **Step 7: Rewrite `check_installation_and_webhook`**

In `scripts/deploy.py`, replace the whole function:

```python
def check_installation_and_webhook(repo: str, base: str) -> CheckResult:
    """Installation discovery plus an idempotent webhook registration.

    Reads the current webhook URL before writing so a re-run reports "already
    correct" rather than silently re-PATCHing, and so a failed read never
    triggers a blind write that could clobber a good URL.
    """
    name = "github-app"
    try:
        installation_id = github_app.discover_installation_id(repo)
    except github_app.AppNotInstalledError:
        return CheckResult(name, "FAIL", f"App not installed on {repo}; install via GitHub UI")
    except RuntimeError as exc:
        status = getattr(exc.__cause__, "status", None)
        detail = "installation lookup failed; check App ID / private key"
        if status is not None:
            detail += f" ({status})"
        return CheckResult(name, "FAIL", detail)

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook read failed ({exc.status})"
        )
    if current == wanted:
        return CheckResult(name, "PASS", f"installation={installation_id}; webhook already correct")
    try:
        github_app.set_webhook_url(wanted)
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; webhook write failed ({exc.status})"
        )
    if current:
        return CheckResult(
            name, "PASS", f"installation={installation_id}; webhook updated from {current}"
        )
    return CheckResult(name, "PASS", f"installation={installation_id}; webhook set")
```

with:

```python
def check_installation_and_webhook(repos: frozenset[str], base: str) -> CheckResult:
    """Installation discovery, allowlist verification, plus an idempotent
    webhook registration.

    Resolves the installation id at the App level (github_app.
    discover_installation_id_for_app) -- this project's scope is one App
    installation per account/org, so no specific repo is needed to seed the
    lookup (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md).

    If `repos` (the GITHUB_TARGET_REPO allowlist) is non-empty, every entry is
    verified against the installation's actual repo list
    (github_app.list_installation_repos) -- an entry the installation does not
    cover is reported as a FAIL naming it: unlike a repo simply excluded from
    the allowlist (silently and correctly dropped by the webhook filter), a
    repo listed here but not installed never generates a webhook at all, so
    this check is the only place that misconfiguration is ever visible. If
    `repos` is empty (track-all mode), nothing is configured to verify, so the
    installation id and covered-repo count are reported as PASS.

    Reads the current webhook URL before writing so a re-run reports "already
    correct" rather than silently re-PATCHing, and so a failed read never
    triggers a blind write that could clobber a good URL.
    """
    name = "github-app"
    try:
        installation_id = github_app.discover_installation_id_for_app()
    except github_app.AppNotInstalledError:
        return CheckResult(name, "FAIL", "App not installed; install via GitHub UI")
    except RuntimeError as exc:
        status = getattr(exc.__cause__, "status", None)
        detail = "installation lookup failed; check App ID / private key"
        if status is not None:
            detail += f" ({status})"
        else:
            detail = str(exc)
        return CheckResult(name, "FAIL", detail)

    try:
        covered = github_app.list_installation_repos()
    except GithubException as exc:
        return CheckResult(
            name, "FAIL", f"installation={installation_id}; repo list failed ({exc.status})"
        )

    if repos:
        missing = sorted(r for r in repos if r not in covered)
        if missing:
            return CheckResult(
                name, "FAIL",
                f"installation={installation_id}; not covered by the installation: "
                + ", ".join(missing),
            )
        repo_detail = f"installation={installation_id}; allowlist covered ({len(repos)} repo(s))"
    else:
        repo_detail = f"installation={installation_id}; tracking all {len(covered)} repo(s)"

    wanted = f"{base}/webhook"
    try:
        current = github_app.get_webhook_url()
    except GithubException as exc:
        return CheckResult(name, "FAIL", f"{repo_detail}; webhook read failed ({exc.status})")
    if current == wanted:
        return CheckResult(name, "PASS", f"{repo_detail}; webhook already correct")
    try:
        github_app.set_webhook_url(wanted)
    except GithubException as exc:
        return CheckResult(name, "FAIL", f"{repo_detail}; webhook write failed ({exc.status})")
    if current:
        return CheckResult(name, "PASS", f"{repo_detail}; webhook updated from {current}")
    return CheckResult(name, "PASS", f"{repo_detail}; webhook set")
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -k "webhook or installation" -v`
Expected: all PASS

- [ ] **Step 9: Update `run_checks`, its tests, and `main`**

In `scripts/deploy.py`, change the `run_checks` signature and its call:

```python
def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All ten, foundational (and cheap, where possible) first, so a
    misconfiguration is reported before the checks that would fail as a
    consequence of it."""
    return [
        _safe("config", check_config),
        _safe("boot-creds-live", check_boot_credentials_live),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("provider", check_provider),
        _safe("provider-live", check_provider_live),
        _safe("api-key-live", check_api_key_live),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]
```

becomes:

```python
def run_checks(repos: frozenset[str], base: str) -> list[CheckResult]:
    """All ten, foundational (and cheap, where possible) first, so a
    misconfiguration is reported before the checks that would fail as a
    consequence of it."""
    return [
        _safe("config", check_config),
        _safe("boot-creds-live", check_boot_credentials_live),
        _safe("github-app", check_installation_and_webhook, repos, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("provider", check_provider),
        _safe("provider-live", check_provider_live),
        _safe("api-key-live", check_api_key_live),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]
```

In `tests/test_deploy_script.py`, update the two calls to `deploy.run_checks("owner/repo", BASE)` (in `test_run_checks_reports_all_ten_in_order` and `test_an_exploding_check_becomes_a_fail_and_does_not_abort_the_run`) to `deploy.run_checks(frozenset({"owner/repo"}), BASE)`.

Now update `main()`. Replace:

```python
    repo = settings.github_target_repo
    if not repo or not base:
        print(
            "GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
            "are required",
            file=sys.stderr,
        )
        return 2
    if args.sync_env:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(repo, base)
    print(render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0
```

with:

```python
    if not base:
        print(
            "a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) is required",
            file=sys.stderr,
        )
        return 2
    if args.sync_env:
        exit_code = sync_env()
        if exit_code != 0:
            return exit_code
    results = run_checks(settings.target_repos(), base)
    print(render_report(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0
```

- [ ] **Step 10: Update `main()`'s tests**

In `tests/test_deploy_script.py`, replace `test_main_returns_two_without_a_target_repo`:

```python
def test_main_proceeds_without_a_target_repo_track_all_mode(monkeypatch, capsys):
    """GITHUB_TARGET_REPO is optional (track-all mode) -- its absence alone
    must not block main() the way a missing base URL does."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(settings, "public_base_url", BASE)
    _stub_all_checks(monkeypatch, ["PASS"] * 6 + ["SKIPPED"] * 4)
    assert deploy.main([]) == 0
    assert "all checks passed" in capsys.readouterr().out
```

Leave `test_main_returns_two_without_a_base_url` as-is (still correct: a missing base URL alone must still return exit 2).

- [ ] **Step 11: Run the full deploy test file**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: all PASS

- [ ] **Step 12: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat: verify a multi-repo allowlist (or report track-all coverage) in deploy checks"
```

- [ ] **Step 13: Add the sync_env empty-value guard exception, with its test**

Add to `tests/test_deploy_script.py`, directly after `test_sync_env_refuses_to_push_an_empty_value`:

```python
def test_sync_env_pushes_an_empty_target_repo_without_tripping_the_empty_guard(
    sync_ready, monkeypatch, capsys
):
    """Track-all mode (design doc §3e): an empty GITHUB_TARGET_REPO is a
    deliberate, valid config value, not a missing one -- sync_env must not
    refuse to push it the way it refuses a genuinely missing required value."""
    monkeypatch.setattr(settings, "github_target_repo", "")
    monkeypatch.setattr(deploy._render, "find_service_id", lambda: None)
    code = deploy.sync_env()
    err = capsys.readouterr().err
    assert "GITHUB_TARGET_REPO" not in err
    assert code == 1          # got past the empty-value guard, failed on the missing service
```

- [ ] **Step 14: Run the test to verify it fails**

Run: `uv run pytest tests/test_deploy_script.py -k pushes_an_empty_target_repo -v`
Expected: FAIL — `sync_env()` currently returns 2 (refuses to push empty values) and prints `GITHUB_TARGET_REPO` to stderr.

- [ ] **Step 15: Add the exception constant and use it in `sync_env`'s guard**

In `scripts/deploy.py`, replace:

```python
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
)
```

with:

```python
_ALWAYS_SYNCED = (
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_TARGET_REPO",
    "GITHUB_WEBHOOK_SECRET",
)
# GITHUB_TARGET_REPO empty is a valid, deliberate "track all repos" config
# (docs/superpowers/specs/2026-08-17-multi-repo-support-design.md), not a
# missing required value -- exempt from sync_env()'s "refuse to push empty
# values" guard below.
_OPTIONAL_EMPTY_ENV_KEYS = frozenset({"GITHUB_TARGET_REPO"})
```

Then in `sync_env()`, replace:

```python
    wanted = _wanted_env()
    empty = sorted(key for key, value in wanted.items() if not value)
```

with:

```python
    wanted = _wanted_env()
    empty = sorted(
        key for key, value in wanted.items()
        if not value and key not in _OPTIONAL_EMPTY_ENV_KEYS
    )
```

- [ ] **Step 16: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: all PASS (including the pre-existing `test_sync_env_refuses_to_push_an_empty_value`, which exercises `GROQ_API_KEY`, unaffected by this exception).

- [ ] **Step 17: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "fix: don't refuse to sync a deliberately empty GITHUB_TARGET_REPO"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md:109`, `README.md:145`, `README.md:163`, `README.md:170`
- Modify: `SETUP.md:327`
- Modify: `SPEC.md:281`, `SPEC.md:357`

**Interfaces:** None — text-only changes, no code.

- [ ] **Step 1: Update `README.md`**

Line 109, replace:
```
1. **Install the GitHub App on the target repo** — repo Settings → GitHub Apps.
   A repo admin authorizes it once.
```
with:
```
1. **Install the GitHub App on your account/org** — repo Settings → GitHub Apps
   (or org Settings, if installing org-wide). Choose "All repositories" or
   select specific repos; an admin authorizes it once. `GITHUB_TARGET_REPO`
   (below) is a separate, optional allowlist that can further narrow which of
   the installed repos the bot actually acts on.
```

Line 145 (the `github-app` row of the checks table), replace:
```
| `github-app` | The App is installed, and its webhook points here (set only if wrong) | yes |
```
with:
```
| `github-app` | The App has exactly one installation; every repo in `GITHUB_TARGET_REPO` (if set) is actually covered by it; and its webhook points here (set only if wrong) | yes |
```

Line 163, replace:
```
For a narrower, credential-free check — "is the service up?" and nothing
else — `uv run python -m bot.scripts.deploy --health-only` runs only the
`health` check, needing just `PUBLIC_BASE_URL`/`RENDER_EXTERNAL_URL` and no
`GITHUB_TARGET_REPO` or any credential. Combining it with `--sync-env` is
refused (exit 2) — they're separate modes, not composable.
```
with:
```
For a narrower, credential-free check — "is the service up?" and nothing
else — `uv run python -m bot.scripts.deploy --health-only` runs only the
`health` check, needing just `PUBLIC_BASE_URL`/`RENDER_EXTERNAL_URL` and no
credential. Combining it with `--sync-env` is refused (exit 2) — they're
separate modes, not composable.
```

Line 170 (exit-code table), replace:
```
| 2 | the run could not proceed: `GITHUB_TARGET_REPO` or a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values, an unsupported `LLM_PROVIDER`, a model with no pricing-table entry, or an active DB override that would mask the push) |
```
with:
```
| 2 | the run could not proceed: a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values other than an intentionally-empty `GITHUB_TARGET_REPO`, an unsupported `LLM_PROVIDER`, a model with no pricing-table entry, or an active DB override that would mask the push) |
```

- [ ] **Step 2: Update `SETUP.md`**

Line 327, replace:
```
   - `GITHUB_TARGET_REPO`: e.g., `<your-user>/pr-review-bot-testbed`
```
with:
```
   - `GITHUB_TARGET_REPO`: optional. Comma-separated allowlist, e.g.
     `<your-user>/pr-review-bot-testbed,<your-user>/pr-review-bot-testbed-2`.
     Leave unset to have the bot act on every repo the App installation
     covers (see §1's install step) instead of a specific subset.
```

- [ ] **Step 3: Update `SPEC.md`**

Line 281, replace:
```
  `GITHUB_TARGET_REPO`, `LLM_PROVIDER`, plus provider creds (`GROQ_API_KEY`, etc.).
```
with:
```
  `GITHUB_TARGET_REPO` (optional, comma-separated allowlist — unset tracks every
  repo the App installation covers), `LLM_PROVIDER`, plus provider creds
  (`GROQ_API_KEY`, etc.).
```

Line 357, replace:
```
**Durable Postgres ticket, one per PR.** `app/queue/store.py` keeps one row per
`(repo_full_name, pr_number)` (from `GITHUB_TARGET_REPO` env var) with a `UNIQUE` constraint.
```
with:
```
**Durable Postgres ticket, one per PR.** `app/queue/store.py` keeps one row per
`(repo_full_name, pr_number)` (`repo_full_name` from the incoming webhook payload,
optionally narrowed by `GITHUB_TARGET_REPO`'s allowlist) with a `UNIQUE` constraint.
```

- [ ] **Step 4: Verify no other stale references remain**

Run: `grep -rn "GITHUB_TARGET_REPO" README.md SETUP.md SPEC.md`
Expected: every remaining mention reads correctly as "optional allowlist" language, not "the single target repo."

- [ ] **Step 5: Commit**

```bash
git add README.md SETUP.md SPEC.md
git commit -m "docs: describe GITHUB_TARGET_REPO as an optional multi-repo allowlist"
```

---

## Final verification

- [ ] Run the full test suite once, end to end: `uv run pytest -v`
- [ ] Expected: all tests pass, no skips introduced by this work.
