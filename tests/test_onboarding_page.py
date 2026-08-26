"""Tests for GET / — the onboarding wizard's static page shell: the frame
state machine, locking, and the "Change" re-edit path. Content-substring
checks against the served HTML/JS (this repo's existing convention for
single-file static pages — see tests/test_dashboard_page.py), not a JS
execution harness."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app

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
    """Mirrors scripts/create_github_app.py's MANIFEST_PERMISSIONS/
    MANIFEST_EVENTS — kept in sync by this test, not a shared module (there
    is no shared JS/Python boundary to put one in)."""
    client = await _client()
    body = (await client.get("/")).text
    assert '"pull_requests": "write"' in body or "pull_requests: \"write\"" in body
    assert '"contents": "read"' in body or "contents: \"read\"" in body
    assert '"issues": "write"' in body or "issues: \"write\"" in body
    assert '"metadata": "read"' in body or "metadata: \"read\"" in body
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
