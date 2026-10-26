"""Single-user session and CSRF tests; these use test-only credentials."""

from collections.abc import Iterator
from time import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth_routes import LoginRequest
from app.core.config import MAX_AUTH_PASSWORD_LENGTH, Settings, get_settings
from app.core.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    issue_session,
    require_authenticated,
)
from app.database import Base, get_db
from app.main import app, create_app


TEST_PASSWORD = "test-only-password-long-enough"
TEST_SESSION_SECRET = "test-only-session-signing-secret-1234567890"


@pytest.fixture
def auth_client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    test_engine = create_engine(
        f"sqlite:///{(tmp_path / 'auth-test.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=test_engine)
    test_sessions = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db() -> Iterator[Session]:
        with test_sessions() as session:
            yield session

    settings = Settings(
        _env_file=None,
        app_env="development",
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(require_authenticated, None)
        app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()


def obtain_csrf(client: TestClient) -> str:
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def sign_in(client: TestClient) -> dict:
    csrf = obtain_csrf(client)
    response = client.post(
        "/api/auth/login",
        json={"password": TEST_PASSWORD},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200
    return response.json()


def test_private_reads_and_writes_require_authentication(auth_client: TestClient) -> None:
    assert auth_client.get("/api/provider/status").status_code == 401
    assert auth_client.get("/api/memories").status_code == 401
    assert auth_client.get("/api/overview").status_code == 401
    assert auth_client.get("/api/agent/tasks").status_code == 401
    assert auth_client.post(
        "/api/memories",
        json={"content": "should not be saved", "category": "general", "source": "user"},
    ).status_code == 401
    assert auth_client.post(
        "/api/agent/tasks",
        json={"task_input": "run a write", "tool_name": "save_memory", "tool_input": {"content": "blocked"}},
    ).status_code == 401
    assert auth_client.post(
        "/api/agent/tasks/1/approval", json={"approved": True}
    ).status_code == 401
    assert auth_client.post("/api/agent/skills/1/approve").status_code == 401


def test_authenticated_access_and_csrf_protected_writes(auth_client: TestClient) -> None:
    login = sign_in(auth_client)
    assert login["authenticated"] is True
    assert auth_client.get("/api/provider/status").status_code == 200

    without_csrf = auth_client.post(
        "/api/memories",
        json={"content": "should not be saved", "category": "general", "source": "user"},
    )
    assert without_csrf.status_code == 403
    assert without_csrf.json() == {"detail": "CSRF validation failed."}

    with_csrf = auth_client.post(
        "/api/memories",
        json={"content": "saved with authenticated consent", "category": "general", "source": "user"},
        headers={CSRF_HEADER: login["csrf_token"]},
    )
    assert with_csrf.status_code == 201
    assert auth_client.get("/api/memories").json() == [with_csrf.json()]


def test_logout_invalidates_session_and_clears_cookies(auth_client: TestClient) -> None:
    login = sign_in(auth_client)
    assert auth_client.get("/api/provider/status").status_code == 200

    response = auth_client.post(
        "/api/auth/logout",
        headers={CSRF_HEADER: login["csrf_token"]},
    )

    assert response.status_code == 200
    assert response.json() == {"logged_out": True}
    assert auth_client.get("/api/provider/status").status_code == 401
    assert 'lodestar_session="";' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_login_rejects_bad_password_and_missing_login_csrf(auth_client: TestClient) -> None:
    csrf = obtain_csrf(auth_client)
    bad_password = auth_client.post(
        "/api/auth/login",
        json={"password": "wrong-password-value"},
        headers={CSRF_HEADER: csrf},
    )
    assert bad_password.status_code == 401
    assert bad_password.json() == {"detail": "Incorrect password."}

    missing_csrf = auth_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert missing_csrf.status_code == 403
    assert missing_csrf.json() == {"detail": "CSRF validation failed."}


def test_csrf_bootstrap_rejects_unapproved_origin(auth_client: TestClient) -> None:
    response = auth_client.get(
        "/api/auth/csrf",
        headers={"Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 403


def test_session_status_and_private_routes_fail_closed_when_unconfigured(monkeypatch) -> None:
    settings = Settings(_env_file=None)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    with TestClient(app) as client:
        assert client.get("/api/auth/session").json() == {
            "configured": False,
            "authenticated": False,
        }
        assert client.get("/api/provider/status").status_code == 503


def test_production_requires_auth_settings_and_uses_secure_cookies(monkeypatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            lodestar_auth_password=SecretStr(TEST_PASSWORD),
            lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        )

    settings = Settings(
        _env_file=None,
        app_env="production",
        cors_origins="https://testserver",
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    from fastapi.testclient import TestClient as SecureTestClient

    from app.core.config import get_settings as settings_dependency

    monkeypatch.setitem(app.dependency_overrides, settings_dependency, lambda: settings)
    with SecureTestClient(app, base_url="https://testserver") as client:
        csrf = obtain_csrf(client)
        response = client.post(
            "/api/auth/login",
            json={"password": TEST_PASSWORD},
            headers={CSRF_HEADER: csrf},
        )
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"].lower()
    assert f"max-age={SESSION_MAX_AGE_SECONDS}" in set_cookie
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_login_password_length_configuration_matches_endpoint_limit() -> None:
    valid_password = "p" * MAX_AUTH_PASSWORD_LENGTH
    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(valid_password),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    assert settings.auth_configured
    assert isinstance(LoginRequest(password=valid_password).password, SecretStr)

    rejected_password = "p" * (MAX_AUTH_PASSWORD_LENGTH + 1)
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            lodestar_auth_password=SecretStr(rejected_password),
            lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        )
    assert rejected_password not in str(error.value)


def test_expired_signed_session_is_rejected(auth_client: TestClient) -> None:
    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    expired_token = issue_session(
        "c" * 43,
        settings,
        issued_at=int(time()) - SESSION_MAX_AGE_SECONDS - 5,
    )
    auth_client.cookies.set(SESSION_COOKIE, expired_token)

    assert auth_client.get("/api/provider/status").status_code == 401


def test_logout_clears_browser_cookie_but_does_not_revoke_a_copied_token(
    auth_client: TestClient,
) -> None:
    login = sign_in(auth_client)
    copied_session = auth_client.cookies.get(SESSION_COOKIE)
    assert copied_session

    response = auth_client.post(
        "/api/auth/logout",
        headers={CSRF_HEADER: login["csrf_token"]},
    )
    assert response.status_code == 200
    assert auth_client.get("/api/provider/status").status_code == 401

    auth_client.cookies.set(SESSION_COOKIE, copied_session)
    assert auth_client.get("/api/provider/status").status_code == 200


def test_api_docs_remain_local_and_are_disabled_in_production() -> None:
    local_app = create_app(Settings(_env_file=None, app_env="development"))
    production_settings = Settings(
        _env_file=None,
        app_env="production",
        cors_origins="https://demo.example",
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    production_app = create_app(production_settings)

    local_paths = {route.path for route in local_app.routes}
    production_paths = {route.path for route in production_app.routes}
    docs_paths = {"/docs", "/redoc", "/openapi.json"}
    assert docs_paths <= local_paths
    assert docs_paths.isdisjoint(production_paths)


def test_auth_secrets_are_masked_from_settings_repr() -> None:
    settings = Settings(
        _env_file=None,
        lodestar_auth_password=TEST_PASSWORD,
        lodestar_session_secret=TEST_SESSION_SECRET,
    )

    assert TEST_PASSWORD not in repr(settings)
    assert TEST_SESSION_SECRET not in repr(settings)
