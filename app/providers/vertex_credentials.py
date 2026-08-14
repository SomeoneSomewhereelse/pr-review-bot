"""Resolves the Vertex service-account credential: GCP_SERVICE_ACCOUNT_KEY_B64
(env, index-aware) -> a local key file (index-aware) -> None (implicit ADC).
Never logged -- the same discipline app/github_app.py::_read_private_key
applies to the equivalent GitHub credential.

Separate from credentials.py on purpose: that module knows exactly one
credential shape ("one env var, one string") and stays that way for the two
providers that need nothing more. The local-file fallback and the JSON
parsing live here instead of complicating it for them.

One index, two real meanings depending on environment. On Render, where the
numbered GCP_SERVICE_ACCOUNT_KEY_B64_{n} slots are actually provisioned, the
index selects among env-var blobs. Locally, where those are typically never
exported, the same index falls through to selecting among numbered local
FILES -- which is how a developer tests against several different service
accounts (a quota-exhausted one vs. a healthy one) without touching Render
or Supabase at all.

A malformed value raises rather than falling through to the next layer: a
corrupt env var must surface as a failure, not silently run the review
against a different account than the operator selected.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from app.config import settings
from app.providers import credentials


def _local_path(index: int) -> str:
    """The key-file path for `index` -- Settings at 0, os.environ above it.

    Mirrors credentials.resolve()'s split for exactly the same reason:
    Settings cannot declare an unbounded family of numbered fields.
    """
    if index == 0:
        return settings.gcp_service_account_key_path
    return os.environ.get(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", "")


def resolve_service_account_info(index: int) -> dict | None:
    """The parsed service-account key for `index`, or None for implicit ADC.

    None is NOT an error here (unlike an empty gemini/groq credential): it is
    the signal to pass no explicit credentials to genai.Client, which is what
    makes google-auth discover `gcloud auth application-default login`'s local
    ADC file on its own.
    """
    _, b64 = credentials.resolve("vertex", index)
    if b64:
        data = json.loads(base64.b64decode(b64, validate=True).decode())
        if not isinstance(data, dict):
            raise ValueError(
                f"GCP service-account credential at index {index} is not a JSON object"
            )
        return data
    path = _local_path(index)
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError(
                f"GCP service-account credential file at index {index} is not a JSON object"
            )
        return data
    return None
