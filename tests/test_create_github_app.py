"""The manifest is the security boundary of this whole setup path, so its
shape is asserted rather than assumed. Every test writes to tmp_path only --
never the repo's real .env."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from scripts import create_github_app as cga

SENTINEL_SECRET = "SENTINEL-9c1e4b7a60df2358-WEBHOOK"
SENTINEL_PEM = "-----BEGIN RSA PRIVATE KEY-----\nSENTINEL-PEM-BODY\n-----END RSA PRIVATE KEY-----\n"


def test_manifest_requests_exactly_the_documented_permissions():
    manifest = cga.build_manifest("bot", "https://example.com", "http://localhost:8765/callback")
    assert manifest["default_permissions"] == {
        "pull_requests": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
    }
    assert manifest["default_events"] == ["pull_request"]


def test_manifest_keeps_the_app_private():
    """A PUBLIC App lets any third party self-install and have their events
    accepted while GITHUB_TARGET_REPO is unset (track-all mode) -- SETUP.md
    section 1 records this as the reason the App must stay private."""
    assert cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")["public"] is False


def test_manifest_points_the_hook_at_the_webhook_path():
    manifest = cga.build_manifest("bot", "https://e.com", "http://localhost:1/c")
    assert manifest["hook_attributes"]["url"] == "https://e.com/webhook"
    assert manifest["redirect_url"] == "http://localhost:1/c"


def test_manifest_is_json_serialisable():
    """It is submitted as a form field, so it must round-trip through JSON."""
    json.loads(json.dumps(cga.build_manifest("bot", "https://e.com", "http://l/c")))


def test_exchange_code_parses_githubs_conversion_response():
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/CODE123/conversions").mock(
            return_value=httpx.Response(
                201,
                json={"id": 4242, "pem": SENTINEL_PEM, "webhook_secret": SENTINEL_SECRET},
            )
        )
        creds = cga.exchange_code("CODE123")
    assert creds.app_id == 4242
    assert creds.private_key_pem == SENTINEL_PEM
    assert creds.webhook_secret == SENTINEL_SECRET


def test_exchange_code_reports_a_failure_structurally(capsys):
    """A 4xx body can echo request content; report the status, not the body."""
    with respx.mock:
        respx.post("https://api.github.com/app-manifests/BAD/conversions").mock(
            return_value=httpx.Response(422, json={"message": SENTINEL_SECRET})
        )
        with pytest.raises(SystemExit) as exc:
            cga.exchange_code("BAD")
    assert SENTINEL_SECRET not in str(exc.value)
    assert SENTINEL_SECRET not in capsys.readouterr().err


def test_write_credentials_writes_values_but_reports_only_lengths(tmp_path, capsys):
    env = tmp_path / ".env"
    creds = cga.AppCredentials(app_id=4242, private_key_pem=SENTINEL_PEM,
                               webhook_secret=SENTINEL_SECRET)
    reported = cga.write_credentials(creds, env)

    written = env.read_text(encoding="utf-8")
    assert "GITHUB_APP_ID=4242" in written
    assert SENTINEL_SECRET in written, "the file is the point -- it must carry the value"
    encoded = base64.b64encode(SENTINEL_PEM.encode()).decode()
    assert f"GITHUB_APP_PRIVATE_KEY={encoded}" in written, "PEM stored base64, not verbatim"

    assert set(reported) == {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"}
    assert all(isinstance(v, int) for v in reported.values())
    out = capsys.readouterr()
    for surface in (repr(reported), out.out, out.err):
        assert SENTINEL_SECRET not in surface
        assert "SENTINEL-PEM-BODY" not in surface


def test_write_credentials_refuses_to_clobber_an_existing_file(tmp_path):
    """A rerun must never silently destroy a working .env."""
    env = tmp_path / ".env"
    env.write_text("GITHUB_APP_ID=1\n", encoding="utf-8")
    creds = cga.AppCredentials(1, SENTINEL_PEM, SENTINEL_SECRET)
    with pytest.raises(SystemExit):
        cga.write_credentials(creds, env)
    assert env.read_text(encoding="utf-8") == "GITHUB_APP_ID=1\n"
    cga.write_credentials(creds, env, overwrite=True)  # explicit opt-in works


def test_no_default_path_points_at_the_repo_root():
    """write_credentials must require an explicit path, so no test or misfire
    can land on the real .env."""
    import inspect

    signature = inspect.signature(cga.write_credentials)
    assert signature.parameters["path"].default is inspect.Parameter.empty
