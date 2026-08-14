"""Settings validation: catches a misconfigured .env at startup rather than
silently reverting to unbounded/disabled behavior at runtime.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_notice_sweep_batch_size_rejects_non_positive_values():
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            Settings(dispatcher_notice_sweep_batch_size=bad)


def test_notice_sweep_batch_size_accepts_positive_values():
    assert Settings(dispatcher_notice_sweep_batch_size=1).dispatcher_notice_sweep_batch_size == 1


def test_cooldown_factor_rejects_below_one():
    with pytest.raises(ValidationError):
        Settings(dispatcher_rereview_cooldown_factor=0.5)


def test_cooldown_factor_accepts_exactly_one():
    settings = Settings(dispatcher_rereview_cooldown_factor=1.0)
    assert settings.dispatcher_rereview_cooldown_factor == 1.0


def test_cooldown_factor_defaults_to_two():
    assert Settings().dispatcher_rereview_cooldown_factor == 2.0


def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in (
        "GCP_PROJECT",
        "GCP_LOCATION",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.gcp_project == ""
    assert settings.gcp_location == "us-central1"
    assert settings.gcp_service_account_key_b64 == ""
    assert settings.gcp_service_account_key_path == "./gcp-service-account-key.json"
