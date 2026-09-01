"""Tests for onboarding/github_client.py — validate_app() reads a visitor's
hand-created GitHub App's actual configuration back from GitHub and reports
per-item pass/fail against REQUIRED_PERMISSIONS/REQUIRED_EVENTS, installation,
and webhook URL. Never logs or echoes the private key. See
docs/superpowers/specs/2026-09-01-onboarding-github-app-manual-validation-design.md."""

from __future__ import annotations

import base64
import json
import time

import pytest
import requests as requests_lib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onboarding import github_client

WEBHOOK_URL = "https://my-service.onrender.com/webhook"


@pytest.fixture(scope="module")
def _throwaway_key_material() -> str:
    """A throwaway RSA key, base64-encoded like a real downloaded .pem's
    base64-encoded bytes would be. Only used for local JWT signing in these
    tests — every HTTP call is mocked below, so nothing is ever sent
    anywhere with it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture(autouse=True)
def _no_pygithub_rate_limit_sleep(monkeypatch):
    """PyGithub's Requester paces real requests with time.sleep(); every
    call here goes through fake_transport below, so the throttle protects
    nothing and only wastes wall-clock."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


class _FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses.
    PyGithub calls through `requests`, not `httpx` — this patches the
    requests.adapters.HTTPAdapter.send transport boundary, same pattern as
    tests/test_github_app.py's own FakeGithubTransport."""

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


REQUIRED_PERMISSIONS_RESPONSE = {
    "permissions": {
        "pull_requests": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
    },
    "events": ["pull_request"],
}


def _route_all_passing(fake_transport):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})


async def test_all_checks_pass_when_everything_matches(fake_transport, _throwaway_key_material):
    _route_all_passing(fake_transport)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    assert all(p.ok for p in result.permissions)
    assert all(e.ok for e in result.events)
    assert result.installation == github_client.InstallationFound(
        installation_id=456, account_login="octocat", repo_scope="all"
    )
    assert result.webhook == github_client.WebhookCheck(ok=True, actual_url=WEBHOOK_URL)


async def test_missing_permission_is_reported_not_ok(fake_transport, _throwaway_key_material):
    fake_transport.route(
        "GET",
        "/app",
        {
            "permissions": {"pull_requests": "write", "contents": "read", "metadata": "read"},
            "events": ["pull_request"],
        },
    )
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    issues = {p.name: p for p in result.permissions}
    assert issues["issues"] == github_client.PermissionCheck(
        name="issues", wanted="write", actual=None, ok=False
    )
    assert issues["pull_requests"].ok


async def test_broader_than_needed_permission_still_passes(fake_transport, _throwaway_key_material):
    """Rank-based: admin satisfies a write requirement -- least-privilege is
    an operator-side doctor.py WARN concern, not a blocker here."""
    response = dict(REQUIRED_PERMISSIONS_RESPONSE)
    response["permissions"] = dict(response["permissions"], pull_requests="admin")
    fake_transport.route("GET", "/app", response)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert next(p for p in result.permissions if p.name == "pull_requests").ok


async def test_missing_event_is_reported_not_ok(fake_transport, _throwaway_key_material):
    response = dict(REQUIRED_PERMISSIONS_RESPONSE)
    response["events"] = []
    fake_transport.route("GET", "/app", response)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.events == [github_client.EventCheck(name="pull_request", ok=False)]


async def test_no_installation_is_reported(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route("GET", "/app/installations", [])
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.installation == github_client.InstallationNotFound()


async def test_multiple_installations_is_reported_distinctly(
    fake_transport, _throwaway_key_material
):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [
            {"id": 1, "account": {"login": "octocat"}, "repository_selection": "all"},
            {"id": 2, "account": {"login": "monalisa"}, "repository_selection": "all"},
        ],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.installation == github_client.MultipleInstallationsFound(
        account_logins=["octocat", "monalisa"]
    )


async def test_mismatched_webhook_url_is_not_ok(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {"url": "https://wrong.example/webhook"})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.webhook == github_client.WebhookCheck(
        ok=False, actual_url="https://wrong.example/webhook"
    )


async def test_unset_webhook_is_empty_string_not_the_synthetic_path(
    fake_transport, _throwaway_key_material
):
    """PyGithub's Requester injects a synthetic `url` key (the literal
    request path) into a GET response dict that lacks one -- an
    unconfigured webhook must not be mistaken for one pointed at that path."""
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route(
        "GET",
        "/app/installations",
        [{"id": 456, "account": {"login": "octocat"}, "repository_selection": "all"}],
    )
    fake_transport.route("GET", "/app/hook/config", {})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result.webhook == github_client.WebhookCheck(ok=False, actual_url="")


async def test_bad_credentials_short_circuits_with_no_checklist_data(
    fake_transport, _throwaway_key_material
):
    fake_transport.route("GET", "/app", {"message": "Bad credentials"}, 401)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="unauthorized")


async def test_forbidden_is_unauthorized(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", {"message": "Forbidden"}, 403)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="unauthorized")


async def test_server_error_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("GET", "/app", {}, 500)
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="github_unreachable")


async def test_malformed_base64_private_key_is_invalid_key():
    result = await github_client.validate_app(999, "not-valid-base64!!", WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="invalid_key")


async def test_valid_base64_but_not_a_real_pem_is_invalid_key():
    garbage_pem_b64 = base64.b64encode(b"not a real PEM").decode()
    result = await github_client.validate_app(999, garbage_pem_b64, WEBHOOK_URL)
    assert result == github_client.AppCredentialsInvalid(reason="invalid_key")


async def test_installation_lookup_failure_does_not_hide_permission_results(
    fake_transport, _throwaway_key_material
):
    """No check may abort the run: a transient failure fetching installations
    must not suppress the permissions/events results already fetched from
    GET /app."""
    fake_transport.route("GET", "/app", REQUIRED_PERMISSIONS_RESPONSE)
    fake_transport.route("GET", "/app/installations", {}, 500)
    fake_transport.route("GET", "/app/hook/config", {"url": WEBHOOK_URL})
    result = await github_client.validate_app(999, _throwaway_key_material, WEBHOOK_URL)
    assert isinstance(result, github_client.AppValidated)
    assert all(p.ok for p in result.permissions)
    assert result.installation == github_client.InstallationNotFound()
    assert result.webhook.ok


def test_required_permissions_and_events_match_the_cli_script():
    from bot.scripts.create_github_app import MANIFEST_EVENTS, MANIFEST_PERMISSIONS

    assert github_client.REQUIRED_PERMISSIONS == MANIFEST_PERMISSIONS
    assert github_client.REQUIRED_EVENTS == MANIFEST_EVENTS


def test_no_manifest_exchange_or_single_installation_verify_leftover():
    """These functions belonged to the removed manifest-flow/typed-
    installation-id design -- their reappearance means half of that flow
    came back without the other half."""
    assert not hasattr(github_client, "exchange_manifest_code")
    assert not hasattr(github_client, "verify_installation")
    assert not hasattr(github_client, "GithubAppCreated")
    assert not hasattr(github_client, "InstallationVerified")
