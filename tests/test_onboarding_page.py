"""Tests for GET / — the onboarding wizard's static page shell: the frame
state machine, locking, and the "Change" re-edit path. Content-substring
checks against the served HTML/JS (this repo's existing convention for
single-file static pages — see tests/test_dashboard_page.py), not a JS
execution harness."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app
from scripts.create_github_app import MANIFEST_EVENTS, MANIFEST_PERMISSIONS

FRAME_IDS = [
    "render-key", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
]


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_index_serves_all_six_frames_in_order():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    positions = [body.index(f'id="frame-{fid}"') for fid in FRAME_IDS]
    assert positions == sorted(positions)


async def test_only_the_render_key_frame_starts_unlocked():
    client = await _client()
    body = (await client.get("/")).text
    assert (
        'id="frame-render-key" class="frame" data-status="ready" '
        'data-locked="false" open'
    ) in body
    for fid in FRAME_IDS[1:]:
        assert (
            f'id="frame-{fid}" class="frame" data-status="locked" data-locked="true"'
        ) in body


async def test_locked_frames_cannot_be_toggled_open():
    client = await _client()
    body = (await client.get("/")).text
    assert "function guardLockedFrames" in body
    assert 'el.dataset.locked === "true"' in body


async def test_render_key_leaves_the_page_exactly_once():
    """The key must only ever transit the one relay call — anything else
    would be a second, unaudited exit path for a visitor's credential."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/render/validate-key"') == 1


async def test_manifest_code_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/github/exchange-manifest-code"') == 1


async def test_render_key_never_persists_to_local_storage():
    """localStorage persists across browser sessions/tabs; sessionStorage
    does not, and only the latter is acceptable for a visitor credential.
    This checks the credential's own storage key, not a blanket ban on
    localStorage — Task 5 legitimately uses localStorage for non-secret
    theme/language preferences elsewhere on this same page."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'sessionStorage.setItem(STORAGE_KEYS["render-key"], key)' in body
    assert 'localStorage.setItem(STORAGE_KEYS["render-key"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["render-key"]' not in body


async def test_completing_a_frame_unlocks_the_next_one():
    client = await _client()
    body = (await client.get("/")).text
    assert "function completeFrame" in body
    assert "if (next) unlockFrame(next);" in body


async def test_done_frames_show_an_explicit_change_control():
    client = await _client()
    body = (await client.get("/")).text
    assert 'class="frame-change"' in body
    assert "function beginChange" in body


async def test_changing_a_frame_relocks_every_later_frame():
    """A resubmission must invalidate whatever later frames already did,
    not just this frame's own value — design doc section 6."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function relockDownstreamOf" in body
    assert "relockDownstreamOf(id)" in body


async def test_submitted_key_is_cleared_from_the_input_after_success():
    client = await _client()
    body = (await client.get("/")).text
    assert 'input.value = "";' in body


async def test_page_has_a_mobile_breakpoint():
    client = await _client()
    body = (await client.get("/")).text
    assert "@media (max-width: 480px)" in body


async def test_frame2_has_a_name_input_and_create_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-name-input"' in body
    assert 'id="github-app-create-submit"' in body


async def test_frame2_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame2_instructions", "frame2_name_placeholder", "create_app_button",
        "err_github_name_empty", "err_github_callback_invalid",
        "err_github_exchange_failed",
    ):
        assert f'{key}:' in body
    assert body.count("create_app_button:") == 2  # STRINGS.en + STRINGS.he


async def test_manifest_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleGithubManifestCallback" in body
    assert "gh_step" in body


async def test_manifest_permissions_match_the_cli_script():
    """The page's JS MANIFEST_PERMISSIONS/MANIFEST_EVENTS must mirror
    scripts/create_github_app.py's, which is the single source of truth (a
    paired comment in each file points at the other; there is no shared
    JS/Python boundary a real shared constant could live in).

    This reads the actual Python constants rather than re-listing the same
    literals a second time: a copy of the literals here would keep passing
    after someone edited only the CLI script, which is exactly the drift this
    test exists to catch."""
    client = await _client()
    body = (await client.get("/")).text
    for name, level in MANIFEST_PERMISSIONS.items():
        assert f'{name}: "{level}"' in body, f"page is missing permission {name}: {level}"
    for event in MANIFEST_EVENTS:
        assert f'"{event}"' in body, f"page is missing default event {event}"
    # The page must not silently grant MORE than the CLI script does either.
    js_perms = body[body.index("const MANIFEST_PERMISSIONS = {"):]
    js_perms = js_perms[:js_perms.index("};")]
    assert js_perms.count(":") == len(MANIFEST_PERMISSIONS)
    js_events = body[body.index("const MANIFEST_EVENTS = ["):]
    js_events = js_events[:js_events.index("];")]
    assert js_events.count('"') == 2 * len(MANIFEST_EVENTS)
    assert "public: false" in body


async def test_installation_verify_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/github/verify-installation"') == 1


async def test_frame2_has_an_install_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="github-app-install-submit"' in body


async def test_install_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleGithubInstallCallback" in body
    assert '"install"' in body or "'install'" in body


async def test_github_app_credential_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.setItem(STORAGE_KEYS["github-app"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["github-app"]' not in body


async def test_restore_from_session_handles_partial_github_app_state():
    client = await _client()
    body = (await client.get("/")).text
    assert "showGithubAppReadyToInstall()" in body
    assert "function restoreFromSession" in body


async def test_frame2_has_a_reset_path_wired_into_lock_and_change():
    """showGithubAppReadyToInstall() hides the create section for good unless
    something reverses it. Without that reversal, both re-entry paths strand
    frame 2: its own "Change" reopens a frame that can only offer "Install
    App" (with stale credentials still in sessionStorage, so a refresh
    silently re-completes the frame), and relocking it from frame 1 clears
    the storage but leaves "Install App" on screen, where clicking it hits
    installGithubApp()'s !stored branch and shows a misleading error."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function resetGithubAppCreateSection" in body

    lock_body = body[body.index("function lockFrame"):body.index("function unlockFrame")]
    assert "resetGithubAppCreateSection()" in lock_body
    assert 'id === "github-app"' in lock_body

    change_body = body[
        body.index("function beginChange"):body.index("async function validateRenderKey")
    ]
    assert "resetGithubAppCreateSection()" in change_body
    # beginChange must clear this frame's own stored credentials too — only
    # lockFrame/relockDownstreamOf did that before, and neither runs on a
    # frame's own Change. (Scoped to frame 2 deliberately: frame 1's
    # equivalent gap is a separate parked issue, see root ISSUES.md.)
    assert 'sessionStorage.removeItem(STORAGE_KEYS["github-app"])' in change_body


async def test_created_but_not_installed_state_is_visible_to_the_visitor():
    """Spec section 3 step 7: after approving the App on GitHub the visitor
    must see an explicit intermediate state. Toggling two `display`
    properties inside a collapsed <details> whose badge still reads "Not
    started" is invisible — the frame has to open and the badge has to say
    what happened."""
    client = await _client()
    body = (await client.get("/")).text
    show_body = body[
        body.index("function showGithubAppReadyToInstall"):body.index("function githubAppError")
    ]
    assert 'frameEl("github-app").open = true' in show_body
    assert 'setFrameStatus("github-app", "app_created")' in show_body
    assert body.count("badge_app_created:") == 2  # STRINGS.en + STRINGS.he


async def test_stored_github_app_credentials_are_parsed_defensively():
    """A corrupted sessionStorage value (extension, devtools, an older
    version of this page) must not throw out of the DOMContentLoaded handler
    and take every later listener with it — same failure mode, and same
    defensive shape, as readStoredLang/readStoredTheme."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function readStoredGithubApp" in body
    # Exactly one parse of this key on the whole page — the guarded one
    # inside the helper. Any other call site must go through the helper.
    assert body.count('JSON.parse(sessionStorage.getItem(STORAGE_KEYS["github-app"])') == 1
    helper_body = body[body.index("function readStoredGithubApp"):]
    helper_body = helper_body[:helper_body.index("\n  }")]
    assert "try {" in helper_body
    assert "catch" in helper_body


async def test_oauth_code_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/supabase/exchange-oauth-code"') == 1


async def test_list_organizations_leaves_the_page_exactly_once():
    """This endpoint (and create-project, project-status, connection-info)
    goes through the shared callSupabaseRelay helper rather than a direct
    fetch() call, so the audit target is "the endpoint string appears
    exactly once as a callSupabaseRelay(...) argument" — the same
    one-exit-path property the fetch()-based version of this test checks
    for exchange-oauth-code and refresh-access-token, adapted for the
    indirection this shared helper introduces."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/list-organizations"') == 1


async def test_create_project_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/create-project"') == 1


async def test_refresh_access_token_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/supabase/refresh-access-token"') == 1


async def test_frame3_has_a_name_input_and_connect_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-project-name-input"' in body
    assert 'id="supabase-connect-submit"' in body


async def test_frame3_has_an_org_picker():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-org-select"' in body
    assert 'id="supabase-org-submit"' in body


async def test_frame3_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame3_instructions", "frame3_name_placeholder", "connect_supabase_button",
        "frame3_org_instructions", "create_project_button",
        "err_supabase_name_empty", "err_supabase_callback_invalid",
    ):
        assert f"{key}:" in body
    assert body.count("connect_supabase_button:") == 2  # STRINGS.en + STRINGS.he


async def test_oauth_callback_handler_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function handleSupabaseOauthCallback" in body
    assert "supabase_step" in body


async def test_pkce_challenge_uses_sha256():
    client = await _client()
    body = (await client.get("/")).text
    assert "crypto.subtle.digest(\"SHA-256\"" in body
    assert "code_challenge_method" in body


async def test_generated_db_password_is_alphanumeric_only():
    """The generated password must never need percent-encoding, sidestepping
    the manual guide's existing footgun entirely."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function generateDbPassword" in body
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" in body


async def test_reactive_refresh_helper_present():
    client = await _client()
    body = (await client.get("/")).text
    assert "async function callSupabaseRelay" in body
    assert '"unauthorized"' in body


async def test_connect_supabase_guards_crypto_failures():
    """generatePkcePair() calls crypto.subtle.digest, the page's only Web
    Crypto call outside random-byte generation — insecure context or an
    unsupported API would throw there. Unlike every fetch() elsewhere in
    this file (each wrapped in try/catch, routed through supabaseError),
    an uncaught throw here would leave "Connect Supabase" silently doing
    nothing with no visible error state. This asserts the guard exists,
    rather than simulating a crypto.subtle failure — this file's tests are
    content-substring checks against served HTML/JS, not a JS execution
    harness (see module docstring)."""
    client = await _client()
    body = (await client.get("/")).text
    fn_body = body[
        body.index("async function connectSupabase"):body.index("async function handleSupabaseOauthCallback")
    ]
    assert "try {" in fn_body
    assert "catch (err) {" in fn_body
    assert "generatePkcePair()" in fn_body
    # The catch must actually surface a visible error, not swallow it.
    catch_body = fn_body[fn_body.index("catch (err) {"):]
    assert "supabaseError(" in catch_body
