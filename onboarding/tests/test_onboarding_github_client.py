"""Tests for onboarding/github_client.py — GitHub App manifest-code
exchange never logs or returns anything but the documented fields, and
distinguishes a rejected/expired code from GitHub being unreachable. See
docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md
sections 3-4."""

from __future__ import annotations

import base64
import inspect
import json
import time

import httpx
import pytest
import requests as requests_lib
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onboarding import github_client

CODE = "SENTINEL_MANIFEST_CODE"
CONVERSIONS_URL = f"https://api.github.com/app-manifests/{CODE}/conversions"


async def test_valid_code_returns_app_credentials():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": 42,
                    "slug": "my-pr-review-bot",
                    "pem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "webhook_secret": "whsec_sentinel",
                },
            )
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert isinstance(result, github_client.GithubAppCreated)
    assert result.app_id == 42
    assert result.slug == "my-pr-review-bot"
    assert result.webhook_secret == "whsec_sentinel"
    assert base64.b64decode(result.private_key_b64).decode() == (
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
    )


async def test_expired_or_bad_code_is_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="exchange_failed")


async def test_github_5xx_is_unreachable_not_exchange_failed():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(500))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_malformed_200_body_is_unreachable_not_a_crash():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, text="not json"))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_response_missing_expected_fields_is_unreachable():
    with respx.mock:
        respx.post(CONVERSIONS_URL).mock(return_value=httpx.Response(201, json={"id": 42}))
        result = await github_client.exchange_manifest_code(CODE)
    assert result == github_client.GithubAppExchangeFailed(reason="github_unreachable")


async def test_exchange_sends_no_authorization_header():
    """The manifest code IS the credential — no auth header is needed or
    sent, matching scripts/create_github_app.py::exchange_code()."""
    with respx.mock:
        route = respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={"id": 1, "slug": "x", "pem": "pem", "webhook_secret": "whsec"},
            )
        )
        await github_client.exchange_manifest_code(CODE)
    assert "authorization" not in {h.lower() for h in route.calls.last.request.headers}


async def test_exchange_sends_a_descriptive_user_agent():
    """GitHub's API guidelines require a User-Agent naming the application.
    httpx's default is a bare "python-httpx/<version>" -- the generic-library
    shape those guidelines call out and anti-scraping heuristics score worst."""
    with respx.mock:
        route = respx.post(CONVERSIONS_URL).mock(
            return_value=httpx.Response(
                201,
                json={"id": 1, "slug": "x", "pem": "pem", "webhook_secret": "whsec"},
            )
        )
        await github_client.exchange_manifest_code(CODE)
    sent = route.calls.last.request.headers["user-agent"]
    assert sent == github_client.USER_AGENT
    assert "httpx" not in sent


@pytest.fixture(scope="module")
def _throwaway_key_material() -> str:
    """A throwaway RSA key, base64-encoded like the real
    exchange_manifest_code output would be. Only used for local JWT
    signing in these tests — every HTTP call is mocked below, so nothing
    is ever sent anywhere with it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture(autouse=True)
def _no_pygithub_rate_limit_sleep(monkeypatch):
    """Mirrors tests/test_github_app.py's own fixture of the same name:
    PyGithub's Requester paces real requests with time.sleep(); every call
    here goes through fake_transport below, so the throttle protects
    nothing and only wastes wall-clock."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class _FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses.
    PyGithub calls through `requests`, not `httpx` — respx cannot intercept
    it — so this patches the same requests.adapters.HTTPAdapter.send
    transport boundary tests/test_github_app.py's own FakeGithubTransport
    uses, scoped down to what onboarding/github_client.py actually needs
    (one App-JWT endpoint, no installation-token exchange)."""

    def __init__(self):
        self.routes: list[tuple[str, str, dict, int]] = []

    def route(self, method: str, url_substring: str, json_body, status_code: int = 200):
        self.routes.append((method.upper(), url_substring, json_body, status_code))

    def send(self, request: requests_lib.PreparedRequest, **kwargs) -> requests_lib.Response:
        for method, url_substring, json_body, status_code in sorted(
            self.routes, key=lambda r: -len(r[1])
        ):
            if request.method == method and url_substring in request.url:
                resp = requests_lib.Response()
                resp.status_code = status_code
                resp.headers["Content-Type"] = "application/json"
                resp._content = json.dumps(json_body).encode("utf-8")
                resp.encoding = "utf-8"
                resp.url = request.url
                resp.reason = "OK"
                resp.request = request
                return resp
        raise AssertionError(f"Unmocked request: {request.method} {request.url}")


@pytest.fixture
def fake_transport(monkeypatch):
    transport = _FakeGithubTransport()
    monkeypatch.setattr(requests_lib.adapters.HTTPAdapter, "send", transport.send)
    return transport


async def test_found_installation_returns_id_account_and_scope(
    fake_transport, _throwaway_key_material
):
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "selected"}],
    )
    result = await github_client.find_installation(
        app_id=999, private_key_b64=_throwaway_key_material
    )
    assert result == github_client.InstallationFound(
        installation_id=456, account_login="octocat", repo_scope="selected"
    )


async def test_empty_installation_list_is_not_installed(fake_transport, _throwaway_key_material):
    """The visitor created the App but has not installed it yet -- a normal,
    expected state now that the wizard no longer drives that navigation and
    cannot know when they finish."""
    fake_transport.route("GET", "/app/installations", [])
    result = await github_client.find_installation(
        app_id=999, private_key_b64=_throwaway_key_material
    )
    assert result == github_client.InstallationInvalid(reason="not_installed")


def test_installation_id_is_never_taken_from_the_caller():
    """GitHub's setup-URL docs warn the redirect's installation_id can be
    spoofed. It is discovered through the App's own JWT instead, so the
    function must not accept one at all."""
    params = inspect.signature(github_client.find_installation).parameters
    assert "installation_id" not in params


async def test_unauthorized_is_invalid_credentials(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations", {"message": "Bad credentials"}, 401)
    result = await github_client.find_installation(
        app_id=999, private_key_b64=_throwaway_key_material
    )
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_server_error_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app/installations", {}, 500)
    result = await github_client.find_installation(
        app_id=999, private_key_b64=_throwaway_key_material
    )
    assert result == github_client.InstallationInvalid(reason="github_unreachable")


async def test_malformed_base64_private_key_is_invalid_credentials():
    result = await github_client.find_installation(app_id=999, private_key_b64="not-valid-base64!!")
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_valid_base64_but_not_a_real_pem_is_invalid_credentials():
    garbage_pem_b64 = base64.b64encode(b"not a real PEM").decode()
    result = await github_client.find_installation(app_id=999, private_key_b64=garbage_pem_b64)
    assert result == github_client.InstallationInvalid(reason="invalid_credentials")


async def test_installation_response_missing_expected_fields_is_unreachable(
    fake_transport, _throwaway_key_material
):
    fake_transport.route("GET", "/app/installations", [{"id": 456}])
    result = await github_client.find_installation(
        app_id=999, private_key_b64=_throwaway_key_material
    )
    assert result == github_client.InstallationInvalid(reason="github_unreachable")


def test_no_webhook_patch_helper_exists():
    """The wizard bakes the App's real webhook URL into the manifest at
    creation time, so there is nothing to correct afterwards. A
    PATCH /app/hook/config helper reappearing here means the placeholder-
    then-patch flow came back -- bot/github_app.py keeps its own
    operator-side set_webhook_url for the CLI/deploy path, which is
    unrelated and stays."""
    assert not hasattr(github_client, "set_webhook_url")
    assert "/app/hook/config" not in inspect.getsource(github_client)
