"""Mocked-SDK tests for bot/providers/catalog.py -- no live network calls,
ever, per root CLAUDE.md's LLM API testing hygiene section."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.providers import catalog


class _FakeApiError(Exception):
    """Duck-typed stand-in for an SDK error carrying an HTTP-status-shaped
    attribute -- avoids depending on the exact constructor signature of any
    real SDK's exception class."""

    def __init__(self, code: int) -> None:
        super().__init__(f"fake error {code}")
        self.code = code


def _model(name: str, actions=None) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.supported_actions = actions
    return m


class TestListGeminiModels:
    @patch("bot.providers.catalog.genai.Client")
    def test_success_strips_prefix_and_filters_non_generative(self, mock_client_cls):
        client = MagicMock()
        client.models.list.return_value = [
            _model("models/gemini-flash-latest", ["generateContent"]),
            _model("models/embedding-001", ["embedContent"]),
        ]
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.ok is True
        assert result.models == ["gemini-flash-latest"]
        assert result.error is None

    @patch("bot.providers.catalog.genai.Client")
    def test_unauthorized_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(401)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("bad-key")

        assert result.ok is False
        assert result.error == "unauthorized"
        assert result.models is None

    @patch("bot.providers.catalog.genai.Client")
    def test_rate_limited_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(429)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "rate_limited"

    @patch("bot.providers.catalog.genai.Client")
    def test_unclassified_error_is_provider_unreachable(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = RuntimeError("connection reset")
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "provider_unreachable"


class TestListGroqModels:
    @patch("bot.providers.catalog.Groq")
    def test_success_returns_model_ids(self, mock_groq_cls):
        client = MagicMock()
        response = MagicMock()
        response.data = [MagicMock(id="llama-3.3-70b-versatile"), MagicMock(id="llama3-8b-8192")]
        client.models.list.return_value = response
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is True
        assert result.models == ["llama-3.3-70b-versatile", "llama3-8b-8192"]

    @patch("bot.providers.catalog.Groq")
    def test_forbidden_maps_to_structural_error(self, mock_groq_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(403)
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is False
        assert result.error == "forbidden"


class TestListVertexModels:
    @patch("bot.providers.catalog.genai.Client")
    @patch("bot.providers.catalog.service_account.Credentials.from_service_account_info")
    def test_success_with_explicit_service_account(self, mock_from_info, mock_client_cls):
        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.return_value = [_model("publishers/google/models/gemini-2.5-flash")]
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models({"project_id": "proj-a", "token_uri": "x"})

        assert result.ok is True
        assert result.models == ["gemini-2.5-flash"]
        mock_client_cls.assert_called_once()
        assert mock_client_cls.call_args.kwargs["project"] == "proj-a"

    def test_no_project_derivable_is_invalid_service_account_json(self):
        result = catalog.list_vertex_models(None)

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("bot.providers.catalog.genai.Client")
    @patch("bot.providers.catalog.service_account.Credentials.from_service_account_info")
    def test_project_override_takes_precedence(self, mock_from_info, mock_client_cls):
        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.return_value = []
        mock_client_cls.return_value = client

        catalog.list_vertex_models(
            {"project_id": "embedded-proj"}, project_override="candidate-proj"
        )

        assert mock_client_cls.call_args.kwargs["project"] == "candidate-proj"

    @patch("bot.providers.catalog.service_account.Credentials.from_service_account_info")
    def test_bad_service_account_info_is_invalid_service_account_json(self, mock_from_info):
        mock_from_info.side_effect = ValueError("malformed")

        result = catalog.list_vertex_models({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "invalid_service_account_json"
