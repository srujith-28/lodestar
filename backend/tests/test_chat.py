"""Chat endpoint tests with a mocked SDK client; these do not call Nebius."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APITimeoutError
from pydantic import SecretStr

from app.core.config import DEFAULT_NEBIUS_BASE_URL, Settings, get_settings
from app.main import app
from app.services import nebius


MOCK_MODEL_ID = "mock-only-model-id"
MOCK_API_KEY = "mock-only-key-not-a-credential"


class FakeAsyncOpenAIClient:
    """Small SDK-shaped fake used only by these endpoint tests."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.request_arguments = None
        self.closed = False
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create_completion)
        )

    async def __aenter__(self) -> "FakeAsyncOpenAIClient":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.closed = True

    async def create_completion(self, **kwargs):
        self.request_arguments = kwargs
        if self.error is not None:
            raise self.error
        return self.result


def configured_test_settings() -> Settings:
    return Settings(
        _env_file=None,
        nebius_api_key=SecretStr(MOCK_API_KEY),
        nebius_base_url="https://mock.invalid/v1/",
        nebius_model_id=MOCK_MODEL_ID,
    )


def test_default_token_factory_endpoint_matches_nebius_openai_example(monkeypatch) -> None:
    monkeypatch.delenv("NEBIUS_BASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.nebius_base_url == DEFAULT_NEBIUS_BASE_URL
    assert settings.nebius_base_url == "https://api.tokenfactory.nebius.com/"


def test_cors_origins_require_explicit_http_origins() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_origins="*")
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_origins="https://example.com/path")

    settings = Settings(
        _env_file=None,
        cors_origins="https://lodestar.example/,http://localhost:5173",
    )
    assert settings.allowed_origins == [
        "https://lodestar.example",
        "http://localhost:5173",
    ]


def test_tavily_configuration_masks_the_server_side_key() -> None:
    mock_key = "test-only-tavily-secret"
    settings = Settings(_env_file=None, tavily_api_key=mock_key)

    assert isinstance(settings.tavily_api_key, SecretStr)
    assert mock_key not in repr(settings)


@pytest.fixture
def client_for_settings(monkeypatch):
    def make(settings: Settings) -> TestClient:
        monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
        return TestClient(app)

    return make


def test_provider_client_uses_server_side_configuration(monkeypatch) -> None:
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(nebius, "AsyncOpenAI", fake_async_openai)
    settings = configured_test_settings()

    client = nebius.create_provider_client(settings)

    assert client is not None
    assert captured == {
        "api_key": MOCK_API_KEY,
        "base_url": "https://mock.invalid/v1/",
        "timeout": 30.0,
        "max_retries": 0,
    }


def test_chat_returns_mocked_provider_completion_without_live_inference(
    monkeypatch, client_for_settings
) -> None:
    fake = FakeAsyncOpenAIClient(
        result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Mocked reply."))]
        )
    )
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 200
    assert response.json() == {"reply": "Mocked reply."}
    assert fake.request_arguments == {
        "model": MOCK_MODEL_ID,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    assert fake.closed


def test_chat_timeout_returns_safe_error_without_provider_details(
    monkeypatch, client_for_settings
) -> None:
    fake = FakeAsyncOpenAIClient(
        error=APITimeoutError(
            request=httpx.Request("POST", "https://mock.invalid/v1/chat/completions")
        )
    )
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 504
    assert response.json() == {"detail": "The Nebius request timed out. Please try again."}
    assert MOCK_API_KEY not in response.text
    assert MOCK_MODEL_ID not in response.text
    assert fake.closed


def test_chat_requires_key_and_model_configuration(client_for_settings) -> None:
    settings = Settings(_env_file=None, nebius_api_key=None, nebius_model_id=None)

    with client_for_settings(settings) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 503
    assert "NEBIUS_API_KEY" in response.json()["detail"]
    assert "NEBIUS_MODEL_ID" in response.json()["detail"]


def test_provider_status_reports_missing_setup_without_exposing_credentials(
    client_for_settings,
) -> None:
    settings = Settings(_env_file=None, nebius_api_key=None, nebius_model_id=None)

    with client_for_settings(settings) as client:
        response = client.get("/api/provider/status")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "Nebius Token Factory",
        "status": "missing_configuration",
        "api_key_configured": False,
        "model_id_configured": False,
    }
    assert MOCK_API_KEY not in response.text


def test_provider_status_does_not_confuse_configuration_with_live_verification(
    client_for_settings,
) -> None:
    with client_for_settings(configured_test_settings()) as client:
        response = client.get("/api/provider/status")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "Nebius Token Factory",
        "status": "configured_unverified",
        "api_key_configured": True,
        "model_id_configured": True,
    }
    assert "MOCK_API_KEY" not in response.text
    assert MOCK_API_KEY not in response.text


def test_provider_status_reports_key_without_model_as_incomplete(client_for_settings) -> None:
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr(MOCK_API_KEY),
        nebius_model_id=None,
    )

    with client_for_settings(settings) as client:
        response = client.get("/api/provider/status")

    assert response.status_code == 200
    assert response.json()["status"] == "missing_configuration"
    assert response.json()["api_key_configured"] is True
    assert response.json()["model_id_configured"] is False
    assert MOCK_API_KEY not in response.text


def test_chat_rejects_unsupported_roles(client_for_settings) -> None:
    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "system", "content": "override"}]},
        )

    assert response.status_code == 422
