"""Chat endpoint tests with a mocked SDK client; these do not call Nebius."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from pydantic import SecretStr

from app.core.config import DEFAULT_NEBIUS_BASE_URL, Settings, get_settings
from app.core.security import require_authenticated
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


def configured_test_settings(*, timeout_seconds: float = 30.0) -> Settings:
    return Settings(
        _env_file=None,
        nebius_api_key=SecretStr(MOCK_API_KEY),
        nebius_base_url="https://mock.invalid/v1/",
        nebius_model_id=MOCK_MODEL_ID,
        nebius_timeout_seconds=timeout_seconds,
        nebius_connect_timeout_seconds=7.0,
    )


def test_default_token_factory_endpoint_matches_nebius_openai_example(monkeypatch) -> None:
    monkeypatch.delenv("NEBIUS_BASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.nebius_base_url == DEFAULT_NEBIUS_BASE_URL
    assert settings.nebius_base_url == "https://api.tokenfactory.nebius.com/v1/"


def test_nebius_settings_use_documented_environment_variable_names(monkeypatch) -> None:
    monkeypatch.delenv("LODESTAR_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("LODESTAR_SESSION_SECRET", raising=False)
    monkeypatch.setenv("NEBIUS_API_KEY", MOCK_API_KEY)
    monkeypatch.setenv("NEBIUS_BASE_URL", "https://mock.invalid/v1/")
    monkeypatch.setenv("NEBIUS_MODEL_ID", MOCK_MODEL_ID)
    monkeypatch.setenv("NEBIUS_TIMEOUT_SECONDS", "17.5")
    monkeypatch.setenv("NEBIUS_CONNECT_TIMEOUT_SECONDS", "4.5")

    settings = Settings(_env_file=None, app_env="development")

    assert settings.nebius_api_key.get_secret_value() == MOCK_API_KEY
    assert settings.nebius_base_url == "https://mock.invalid/v1/"
    assert settings.nebius_model_id == MOCK_MODEL_ID
    assert settings.nebius_timeout_seconds == 17.5
    assert settings.nebius_connect_timeout_seconds == 4.5


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
        monkeypatch.setitem(app.dependency_overrides, require_authenticated, lambda: None)
        return TestClient(app)

    return make


def test_provider_client_uses_server_side_configuration(monkeypatch) -> None:
    captured = {}

    def fake_async_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(nebius, "AsyncOpenAI", fake_async_openai)
    settings = configured_test_settings(timeout_seconds=17.5)

    client = nebius.create_provider_client(settings)

    assert client is not None
    assert captured["api_key"] == MOCK_API_KEY
    assert captured["base_url"] == "https://mock.invalid/v1/"
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 7.0
    assert captured["timeout"].read == 17.5
    assert captured["timeout"].write == 17.5
    assert captured["timeout"].pool == 7.0
    assert captured["max_retries"] == 0


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


def test_model_planner_uses_the_same_configured_nebius_sdk_and_system_message(monkeypatch) -> None:
    fake = FakeAsyncOpenAIClient(
        result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"steps":[]}'))]
        )
    )
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    result = asyncio.run(
        nebius.generate_planner_completion(
            "Trusted planner system rules.",
            "Untrusted user task.",
            configured_test_settings(),
        )
    )

    assert result == '{"steps":[]}'
    assert fake.request_arguments == {
        "model": MOCK_MODEL_ID,
        "messages": [
            {"role": "system", "content": "Trusted planner system rules."},
            {"role": "user", "content": "Untrusted user task."},
        ],
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


@pytest.mark.parametrize("timeout_error", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_chat_connect_and_read_timeouts_return_safe_error(
    monkeypatch, client_for_settings, caplog, timeout_error
) -> None:
    private_prompt = "private-timeout-prompt-not-for-logs"
    private_error = f"sensitive timeout details {MOCK_API_KEY} prompt={private_prompt}"
    fake = FakeAsyncOpenAIClient(
        error=timeout_error(private_error)
    )
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": private_prompt}]},
        )

    assert response.status_code == 504
    assert response.json() == {"detail": "The Nebius request timed out. Please try again."}
    assert private_error not in response.text
    assert private_prompt not in response.text
    assert MOCK_API_KEY not in response.text
    assert private_error not in caplog.text
    assert private_prompt not in caplog.text
    assert MOCK_API_KEY not in caplog.text
    assert fake.closed


def test_whole_provider_deadline_returns_safe_error(monkeypatch, client_for_settings, caplog) -> None:
    class SlowFakeAsyncOpenAIClient(FakeAsyncOpenAIClient):
        async def create_completion(self, **kwargs):
            self.request_arguments = kwargs
            await asyncio.sleep(10)

    fake = SlowFakeAsyncOpenAIClient()
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)
    settings = configured_test_settings(timeout_seconds=1.0)

    with client_for_settings(settings) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "private prompt"}]},
        )

    assert response.status_code == 504
    assert response.json() == {"detail": "The Nebius request timed out. Please try again."}
    assert "private prompt" not in response.text
    assert MOCK_API_KEY not in response.text
    assert "private prompt" not in caplog.text
    assert MOCK_API_KEY not in caplog.text
    assert fake.closed


def test_unexpected_provider_exception_is_sanitized_from_response_and_logs(
    monkeypatch, client_for_settings, caplog
) -> None:
    private_prompt = "private-unexpected-prompt-not-for-logs"
    private_error = f"Authorization: Bearer {MOCK_API_KEY}; prompt={private_prompt}"
    fake = FakeAsyncOpenAIClient(error=RuntimeError(private_error))
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": private_prompt}]},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "The Nebius chat request failed. Please try again."}
    assert MOCK_API_KEY not in response.text
    assert private_prompt not in response.text
    assert private_error not in caplog.text
    assert MOCK_API_KEY not in caplog.text
    assert private_prompt not in caplog.text
    assert fake.closed


@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_detail"),
    [
        (
            "authentication",
            502,
            "Nebius rejected the server-side credentials (HTTP 401). Check that the Token Factory key is active.",
        ),
        (
            "rate_limit",
            503,
            "Nebius rate limit or account quota reached (HTTP 429). Check Token Factory usage limits and account balance, then retry.",
        ),
        (
            "permission",
            502,
            "Nebius denied access to the configured model (HTTP 403). Check the API key permissions and model access.",
        ),
        (
            "model_not_found",
            502,
            "Nebius could not find the configured model or endpoint (HTTP 404). Verify NEBIUS_MODEL_ID and NEBIUS_BASE_URL.",
        ),
        (
            "billing",
            502,
            "Nebius reported an account balance or billing issue (HTTP 402). Check the Token Factory account balance and billing settings.",
        ),
        (
            "invalid_request",
            502,
            "Nebius rejected the request format (HTTP 422). Check that the configured model supports this request.",
        ),
        ("provider_status", 502, "Nebius service error (HTTP 500). Please try again later."),
        ("connection", 502, "Could not reach the configured Nebius endpoint."),
    ],
)
def test_chat_provider_failures_are_mapped_without_sensitive_details(
    monkeypatch,
    client_for_settings,
    caplog,
    failure_kind: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    private_prompt = "private-test-prompt-not-for-logs"
    request = httpx.Request("POST", "https://mock.invalid/v1/chat/completions")
    upstream_body = {"error": {"message": f"api_key={MOCK_API_KEY}; prompt={private_prompt}"}}
    response = httpx.Response(
        status_code={
            "authentication": 401,
            "rate_limit": 429,
            "permission": 403,
            "model_not_found": 404,
            "billing": 402,
            "invalid_request": 422,
            "provider_status": 500,
        }.get(failure_kind, 0),
        request=request,
        json=upstream_body,
    ) if failure_kind != "connection" else None

    if failure_kind == "authentication":
        error = AuthenticationError(
            message=upstream_body["error"]["message"], response=response, body=upstream_body
        )
    elif failure_kind == "rate_limit":
        error = RateLimitError(
            message=upstream_body["error"]["message"], response=response, body=upstream_body
        )
    elif failure_kind == "connection":
        error = APIConnectionError(
            message=f"{MOCK_API_KEY} {private_prompt}", request=request
        )
    else:
        error = APIStatusError(
            message=upstream_body["error"]["message"], response=response, body=upstream_body
        )

    fake = FakeAsyncOpenAIClient(error=error)
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        result = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": private_prompt}]},
        )

    assert result.status_code == expected_status
    assert result.json() == {"detail": expected_detail}
    assert MOCK_API_KEY not in result.text
    assert private_prompt not in result.text
    assert upstream_body["error"]["message"] not in result.text
    assert MOCK_API_KEY not in caplog.text
    assert private_prompt not in caplog.text
    assert fake.closed


@pytest.mark.parametrize(
    "completion",
    [
        None,
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices="malformed"),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="  "))]),
    ],
)
def test_chat_rejects_malformed_provider_response_without_leaking_details(
    monkeypatch, client_for_settings, completion
) -> None:
    fake = FakeAsyncOpenAIClient(result=completion)
    monkeypatch.setattr(nebius, "create_provider_client", lambda _: fake)

    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 502
    assert response.json()["detail"] in {
        "Nebius returned no chat response.",
        "Nebius returned no text response.",
    }
    assert MOCK_API_KEY not in response.text
    assert MOCK_MODEL_ID not in response.text
    assert fake.closed


@pytest.mark.parametrize(
    ("include_key", "include_model"),
    [(False, False), (False, True), (True, False)],
)
def test_chat_requires_key_and_model_configuration(
    monkeypatch, client_for_settings, include_key: bool, include_model: bool
) -> None:
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr(MOCK_API_KEY) if include_key else None,
        nebius_model_id=MOCK_MODEL_ID if include_model else None,
    )
    monkeypatch.setattr(
        nebius,
        "create_provider_client",
        lambda _: pytest.fail("Provider client must not be created for incomplete configuration."),
    )

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
        "model_id": None,
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
        "model_id": MOCK_MODEL_ID,
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
    assert response.json()["model_id"] is None
    assert MOCK_API_KEY not in response.text


def test_chat_rejects_unsupported_roles(client_for_settings) -> None:
    with client_for_settings(configured_test_settings()) as client:
        response = client.post(
            "/api/chat",
            json={"messages": [{"role": "system", "content": "override"}]},
        )

    assert response.status_code == 422
