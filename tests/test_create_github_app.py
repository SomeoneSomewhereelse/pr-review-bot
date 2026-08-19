"""The manifest is the security boundary of this whole setup path, so its
shape is asserted rather than assumed. Every test writes to tmp_path only --
never the repo's real .env."""
from __future__ import annotations

import base64
import html
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
    accepted while GITHUB_TARGET_REPO is unset (track-all mode) --
    guide/setup/02-github-app.md records this as the reason the App must
    stay private."""
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


def test_main_reports_a_clear_message_when_the_callback_port_is_taken(monkeypatch, capsys):
    """A bare OSError (e.g. 'Address already in use') must not surface as an
    unhandled traceback -- main() should return 1 with an operator-friendly
    message, and never even reach the browser-opening step."""

    def _raise(*_args, **_kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr(cga.http.server, "HTTPServer", _raise)
    monkeypatch.setattr(
        cga.webbrowser, "open",
        lambda *_a, **_k: pytest.fail("must not open a browser after a bind failure"),
    )

    result = cga.main(["--base-url", "https://e.com"])

    assert result == 1
    err = capsys.readouterr().err
    assert str(cga._CALLBACK_PORT) in err
    assert "OSError" in err


def test_do_get_state_mismatch_sets_the_flag_and_leaves_code_none():
    """On a state mismatch, do_GET must record WHY (state_mismatch=True) and
    leave code None, so main() can report the real reason instead of the
    generic 'no code' message."""
    cga._CallbackHandler.state = "expected-state"
    cga._CallbackHandler.code = None
    cga._CallbackHandler.state_mismatch = False
    cga._CallbackHandler.received.clear()

    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    handler.path = "/callback?state=wrong&code=abc123"
    replies: list[tuple[int, str]] = []
    handler._reply = lambda status, body: replies.append((status, body))

    handler.do_GET()

    assert cga._CallbackHandler.state_mismatch is True
    assert cga._CallbackHandler.code is None
    assert cga._CallbackHandler.received.is_set()
    assert replies and replies[0][0] == 400


def test_do_get_matching_state_clears_the_mismatch_flag_and_sets_code():
    cga._CallbackHandler.state = "expected-state"
    cga._CallbackHandler.code = None
    cga._CallbackHandler.state_mismatch = False
    cga._CallbackHandler.received.clear()

    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    handler.path = "/callback?state=expected-state&code=abc123"
    handler._reply = lambda status, body: None

    handler.do_GET()

    assert cga._CallbackHandler.code == "abc123"
    assert cga._CallbackHandler.state_mismatch is False


def test_manifest_json_is_html_escaped_not_just_quote_escaped():
    """The old `.replace("'", "&#39;")` only handled single quotes; an `&` or
    `<` in an operator-supplied --name could still corrupt the HTML attribute
    or break the page."""
    manifest = cga.build_manifest("A & B <img onerror=alert(1)>", "https://e.com", "http://l/c")
    raw = json.dumps(manifest)
    escaped = html.escape(raw, quote=True)

    cga._CallbackHandler.manifest_json = escaped
    cga._CallbackHandler.state = "st"
    handler = cga._CallbackHandler.__new__(cga._CallbackHandler)
    form = handler._form()

    assert escaped in form
    assert "<img" not in form
    assert html.unescape(escaped) == raw
