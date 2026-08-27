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


async def test_project_status_leaves_the_page_exactly_once():
    """Like Task 6's list-organizations/create-project tests: this endpoint
    goes through the shared callSupabaseRelay helper, not a direct fetch()
    call, so the audit target is the endpoint string appearing exactly once
    as a callSupabaseRelay(...) argument."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/project-status"') == 1


async def test_connection_info_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('callSupabaseRelay("/api/supabase/connection-info"') == 1


async def test_frame3_has_a_check_again_button():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="supabase-check-status-submit"' in body


async def test_polling_uses_a_five_second_interval_and_five_minute_timeout():
    client = await _client()
    body = (await client.get("/")).text
    assert "SUPABASE_POLL_INTERVAL_MS = 5000" in body
    assert "SUPABASE_POLL_TIMEOUT_MS = 300000" in body


async def test_target_status_is_active_healthy_and_init_failed_is_terminal():
    client = await _client()
    body = (await client.get("/")).text
    assert '"ACTIVE_HEALTHY"' in body
    assert '"INIT_FAILED"' in body


async def test_connection_string_assembled_client_side_from_non_secret_shape():
    """The backend never returns Supabase's own connection_string field
    (spec section 3 step 9) — the browser builds it from db_user/db_host/
    db_port/db_name (returned) plus db_pass (already held)."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function fetchSupabaseConnectionInfo" in body
    assert "postgresql://${body.db_user}:${stored.db_pass}@${body.db_host}:${body.db_port}/${body.db_name}" in body


async def test_supabase_credential_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.setItem(STORAGE_KEYS["supabase"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["supabase"]' not in body


async def test_restore_from_session_resumes_polling_for_a_ref_without_a_connection_string():
    client = await _client()
    body = (await client.get("/")).text
    assert "showSupabaseProvisioning()" in body
    assert "pollUntilReady(Date.now())" in body
    assert "function restoreFromSession" in body


async def test_stored_supabase_credentials_are_parsed_defensively():
    """Same guard as readStoredGithubApp() -- a corrupted sessionStorage
    value must not throw out of DOMContentLoaded and take every later
    listener with it."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function readStoredSupabase" in body
    helper_body = body[body.index("function readStoredSupabase"):]
    helper_body = helper_body[:helper_body.index("\n  }")]
    assert "try {" in helper_body
    assert "catch" in helper_body
    # Every other call site must go through the helper -- the only direct
    # JSON.parse of this key left on the page is inside the helper itself.
    assert body.count('JSON.parse(sessionStorage.getItem(STORAGE_KEYS["supabase"])') == 1


async def test_terminal_supabase_errors_reset_the_connect_section():
    """INIT_FAILED (from handleProjectStatusResult), an exhausted-refresh
    unauthorized (from callSupabaseRelay's callers, via
    supabaseErrorForReason), and project_creation_rejected are dead ends --
    resetSupabaseConnectSection() must run before the error is shown so
    "Connect Supabase" is back on screen to restart the flow, not just
    fold into the existing error-clearing convention."""
    client = await _client()
    body = (await client.get("/")).text

    init_failed_start = body.index('body.status === "INIT_FAILED"')
    init_failed_body = body[init_failed_start:body.index("return \"pending\";")]
    reset_pos = init_failed_body.index("resetSupabaseConnectSection()")
    error_pos = init_failed_body.index('supabaseError("err_supabase_provisioning_failed")')
    assert reset_pos < error_pos

    reason_fn_start = body.index("function supabaseErrorForReason")
    reason_fn_body = body[reason_fn_start:body.index("async function callSupabaseRelay")]

    rejected_branch = reason_fn_body[:reason_fn_body.index('if (reason === "unauthorized")')]
    assert "resetSupabaseConnectSection()" in rejected_branch
    assert rejected_branch.index("resetSupabaseConnectSection()") < rejected_branch.index(
        'document.getElementById("supabase-error").textContent = message;'
    )

    unauthorized_branch = reason_fn_body[reason_fn_body.index('if (reason === "unauthorized")'):]
    assert "resetSupabaseConnectSection();" in unauthorized_branch.split("const key = {")[0]


async def test_org_picker_opens_the_frame_and_updates_its_badge():
    """A visitor with 2+ orgs returns from Supabase's consent screen to a
    frame restoreFromSession() already unlocked to "ready" (badge "Not
    started") and left closed -- without opening the frame and re-badging
    it here, there's no visible sign their authorization worked."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function showSupabaseOrgPicker" in body
    show_body = body[
        body.index("function showSupabaseOrgPicker"):body.index("async function confirmSupabaseOrg")
    ]
    assert 'frameEl("supabase").open = true' in show_body
    assert 'setFrameStatus("supabase", "choosing_org")' in show_body
    assert body.count("badge_choosing_org:") == 2  # STRINGS.en + STRINGS.he

    fetch_orgs_body = body[
        body.index("async function fetchSupabaseOrganizations"):body.index("function showSupabaseOrgPicker")
    ]
    assert "showSupabaseOrgPicker();" in fetch_orgs_body


async def test_relay_callers_re_read_storage_after_the_await_before_writing_back():
    """A stale pre-await snapshot must not clobber tokens callSupabaseRelay
    refreshed mid-call: the read that feeds the final sessionStorage.setItem
    must happen after the callSupabaseRelay await, not before."""
    client = await _client()
    body = (await client.get("/")).text

    kickoff_body = body[
        body.index("async function kickOffProjectCreation"):body.index("function showSupabaseProvisioning")
    ]
    await_pos = kickoff_body.index("await callSupabaseRelay(")
    reread_pos = kickoff_body.index("stored = readStoredSupabase() || stored;")
    write_pos = kickoff_body.index('sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));')
    assert await_pos < reread_pos < write_pos

    conn_info_body = body[
        body.index("async function fetchSupabaseConnectionInfo"):body.index("function restoreFromSession")
    ]
    await_pos = conn_info_body.index("await callSupabaseRelay(")
    reread_pos = conn_info_body.index("stored = readStoredSupabase() || stored;")
    write_pos = conn_info_body.index('sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));')
    assert await_pos < reread_pos < write_pos


async def test_connection_info_missing_local_state_shows_an_error_not_a_silent_stall():
    """Polling already reported "ready" and stopped by this point -- a bare
    return here used to leave the frame stuck at "Provisioning..." forever
    with no error and no button."""
    client = await _client()
    body = (await client.get("/")).text
    fn_start = body.index("async function fetchSupabaseConnectionInfo")
    fn_body = body[fn_start:body.index("function restoreFromSession")]
    guard_body = fn_body[:fn_body.index("const body = await callSupabaseRelay(")]
    assert "return;" in guard_body
    assert 'supabaseError("err_supabase_callback_invalid");' in guard_body


async def test_refresh_does_not_overwrite_a_valid_refresh_token_with_a_missing_one():
    """Supabase's OAuth schema does not guarantee refresh_token on every
    refresh response (SupabaseTokens.refresh_token: str | None) -- an
    unconditional overwrite would clobber a still-valid refresh token with
    null/undefined, permanently disabling future refreshes."""
    client = await _client()
    body = (await client.get("/")).text
    relay_start = body.index("async function callSupabaseRelay")
    relay_body = body[relay_start:body.index("async function connectSupabase")]
    assert "stored.access_token = refreshBody.access_token;" in relay_body
    assert "if (refreshBody.refresh_token) {" in relay_body
    assert "stored.refresh_token = refreshBody.refresh_token;" in relay_body
    # The unconditional overwrite this replaces must be gone.
    assert relay_body.count("stored.refresh_token = refreshBody.refresh_token;") == 1
    guard_pos = relay_body.index("if (refreshBody.refresh_token) {")
    assign_pos = relay_body.index("stored.refresh_token = refreshBody.refresh_token;")
    close_brace_pos = relay_body.index("}", assign_pos)
    assert guard_pos < assign_pos < close_brace_pos


async def test_supabase_oauth_callback_storage_write_is_guarded():
    """A sessionStorage.setItem failure (quota/blocked) must route through
    supabaseError(...) like every other step in this function, not become
    an unhandled rejection with the freshly-exchanged tokens lost."""
    client = await _client()
    body = (await client.get("/")).text
    fn_start = body.index("async function handleSupabaseOauthCallback")
    fn_body = body[fn_start:body.index("async function fetchSupabaseOrganizations")]
    setitem_pos = fn_body.index('sessionStorage.setItem(STORAGE_KEYS["supabase"]')
    try_pos = fn_body.rindex("try {", 0, setitem_pos)
    catch_pos = fn_body.index("} catch (err) {", setitem_pos)
    assert try_pos < setitem_pos < catch_pos
    catch_body = fn_body[catch_pos:fn_body.index("await fetchSupabaseOrganizations();")]
    assert "supabaseError(" in catch_body
    assert body.count("err_supabase_storage_failed:") == 2  # STRINGS.en + STRINGS.he
