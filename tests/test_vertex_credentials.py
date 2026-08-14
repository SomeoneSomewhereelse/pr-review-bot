"""app/providers/vertex_credentials.py -- Vertex's three-layer credential
chain: GCP_SERVICE_ACCOUNT_KEY_B64 (index-aware) -> a local key file
(index-aware) -> None, meaning "let google-auth discover implicit ADC".

Hermetic by construction: the autouse fixture points every layer at nothing,
because a developer's real .env legitimately sets GCP_SERVICE_ACCOUNT_KEY_PATH
to a key file that exists on their machine -- without it, the "nothing
resolves" tests would pass in CI and fail locally.
"""
from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.providers import vertex_credentials

KEY = {
    "type": "service_account",
    "project_id": "proj-from-key",
    "client_email": "svc@proj-from-key.iam.gserviceaccount.com",
}
OTHER_KEY = {**KEY, "project_id": "proj-from-slot-1"}


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture(autouse=True)
def _no_real_gcp_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "")
    monkeypatch.setattr(
        settings, "gcp_service_account_key_path", str(tmp_path / "absent.json")
    )
    for index in (1, 2):
        monkeypatch.delenv(f"GCP_SERVICE_ACCOUNT_KEY_B64_{index}", raising=False)
        monkeypatch.delenv(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", raising=False)


def test_index_zero_decodes_the_base_b64_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_decodes_its_own_b64_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY_B64_1", _b64(OTHER_KEY))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_falls_back_to_the_local_key_file_when_no_b64_is_set(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_falls_back_to_its_own_numbered_local_file(monkeypatch, tmp_path):
    """The local-dev case the design exists for: two real service accounts
    (e.g. a quota-exhausted one and a healthy one) selected by the SAME index
    that selects env-var slots on Render."""
    slot_1 = tmp_path / "key-1.json"
    slot_1.write_text(json.dumps(OTHER_KEY))
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY_PATH_1", str(slot_1))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_b64_wins_over_a_local_file_at_the_same_index(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(OTHER_KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_returns_none_when_nothing_resolves():
    """NOT an error for vertex -- None means "pass no explicit credentials to
    the client", which is exactly what triggers google-auth's implicit ADC
    discovery. Contrast gemini/groq, where an empty credential always means
    misconfigured."""
    assert vertex_credentials.resolve_service_account_info(0) is None


def test_a_numbered_index_does_not_fall_back_to_index_zero(monkeypatch):
    """An unprovisioned slot must resolve to "nothing here", not silently to
    the base slot -- a swap to an empty index must be visible, not a no-op."""
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(2) is None


def test_malformed_base64_raises_rather_than_falling_through(monkeypatch, tmp_path):
    """A corrupt env var must surface, not quietly degrade to the next layer --
    that would run against a different account than the operator selected."""
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "!!!not-base64!!!")
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_valid_base64_that_is_not_json_raises(monkeypatch):
    monkeypatch.setattr(
        settings, "gcp_service_account_key_b64", base64.b64encode(b"nope").decode()
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_b64_decoding_to_a_json_list_not_an_object_raises(monkeypatch):
    """The documented contract is `dict | None` -- a syntactically valid JSON
    value that isn't an object (e.g. a list) must surface as an error, not be
    handed to VertexProvider as if it were a service-account key."""
    monkeypatch.setattr(
        settings,
        "gcp_service_account_key_b64",
        base64.b64encode(json.dumps([1, 2, 3]).encode()).decode(),
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_local_file_containing_a_json_list_not_an_object_raises(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_malformed_json_in_the_local_file_raises(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text("{ not json")
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_a_configured_path_that_does_not_exist_is_not_an_error(monkeypatch, tmp_path):
    """.env.example ships GCP_SERVICE_ACCOUNT_KEY_PATH pre-filled with the
    default filename, so "configured but absent" is the ordinary state for
    anyone not using vertex -- it must mean "no key here", never a crash."""
    monkeypatch.setattr(
        settings, "gcp_service_account_key_path", str(tmp_path / "never-created.json")
    )
    assert vertex_credentials.resolve_service_account_info(0) is None
