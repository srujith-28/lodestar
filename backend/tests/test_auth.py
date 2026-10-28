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
    keyed_identifier_hash,
    require_authenticated,
)
from app.database import Base, get_db
from app.main import app, create_app
from app.models import AuthSession
from app.services import auth_rate_limit
from app.services.auth_rate_limit import LoginRateLimiterUnavailable


TEST_PASSWORD = "test-only-password-long-enough"
TEST_SESSION_SECRET = "test-only-session-signing-secret-1234567890"
TEST_POSTGRES_URL = "postgresql://localhost/lodestar-test"


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
            "demo_enabled": False,
            "workspace_type": None,
        }
        assert client.get("/api/provider/status").status_code == 503


def test_production_requires_auth_settings_and_uses_secure_cookies(monkeypatch, tmp_path) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=TEST_POSTGRES_URL,
            lodestar_auth_password=SecretStr(TEST_PASSWORD),
            lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        )

    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=TEST_POSTGRES_URL,
        cors_origins="https://testserver",
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    from fastapi.testclient import TestClient as SecureTestClient

    from app.core.config import get_settings as settings_dependency
    test_engine = create_engine(
        f"sqlite:///{(tmp_path / 'production-auth.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=test_engine)
    test_sessions = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db() -> Iterator[Session]:
        with test_sessions() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, settings_dependency, lambda: settings)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_get_db)
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
    test_engine.dispose()


def test_guest_only_production_configuration_requires_session_secret_and_postgres() -> None:
    guest_only = Settings(
        _env_file=None,
        app_env="development",
        guest_demo_enabled=True,
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    assert guest_only.guest_demo_enabled is True
    assert guest_only.auth_configured is False

    with pytest.raises(ValidationError):
        Settings(_env_file=None, guest_demo_enabled=True)

    production_guest = Settings(
        _env_file=None,
        app_env="production",
        database_url=TEST_POSTGRES_URL,
        cors_origins="https://demo.example",
        guest_demo_enabled=True,
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    assert production_guest.secure_cookies is True


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


def test_login_rate_limit_and_proxy_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, login_rate_limit_max_attempts=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, login_rate_limit_window_seconds=30)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trusted_proxy_cidrs="not-a-cidr")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trusted_proxy_cidrs="0.0.0.0/0")


def test_expired_signed_session_is_rejected(auth_client: TestClient) -> None:
    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    current_time = int(time())
    session_id = "s" * 43
    expired_token = issue_session(
        "c" * 43,
        session_id,
        settings,
        issued_at=current_time - SESSION_MAX_AGE_SECONDS,
    )
    database_override = app.dependency_overrides[get_db]()
    db = next(database_override)
    try:
        db.add(
            AuthSession(
                session_id_hash=keyed_identifier_hash(session_id, settings, purpose="session"),
                expires_at_epoch=current_time + 60,
            )
        )
        db.commit()
    finally:
        database_override.close()
    auth_client.cookies.set(SESSION_COOKIE, expired_token)

    assert auth_client.get("/api/provider/status").status_code == 401


def test_logout_revokes_a_copied_session_token(auth_client: TestClient) -> None:
    login = sign_in(auth_client)
    copied_session = auth_client.cookies.get(SESSION_COOKIE)
    assert copied_session

    response = auth_client.post(
        "/api/auth/logout",
        headers={CSRF_HEADER: login["csrf_token"]},
    )
    assert response.status_code == 200
    assert auth_client.get("/api/provider/status").status_code == 401

    # Clearing the browser cookie is not the revocation mechanism: the server-side
    # active-session record was deleted, so replaying the copied signed cookie fails.
    auth_client.cookies.set(SESSION_COOKIE, copied_session)
    assert auth_client.get("/api/provider/status").status_code == 401
    assert auth_client.get("/api/auth/session").json() == {
        "configured": True,
        "authenticated": False,
        "demo_enabled": False,
        "workspace_type": None,
    }


def test_tampered_signed_session_is_rejected(auth_client: TestClient) -> None:
    sign_in(auth_client)
    valid_session = auth_client.cookies.get(SESSION_COOKIE)
    assert valid_session
    payload, signature = valid_session.split(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    auth_client.cookies.set(SESSION_COOKIE, f"{payload}.{replacement}{signature[1:]}")

    assert auth_client.get("/api/provider/status").status_code == 401


def test_login_attempts_are_rate_limited_and_forwarded_header_is_not_trusted(
    auth_client: TestClient,
) -> None:
    csrf = obtain_csrf(auth_client)
    headers = {CSRF_HEADER: csrf}

    for attempt in range(5):
        response = auth_client.post(
            "/api/auth/login",
            json={"password": "wrong-password-value"},
            headers={**headers, "X-Forwarded-For": f"198.51.100.{attempt + 1}"},
        )
        assert response.status_code == 401

    blocked = auth_client.post(
        "/api/auth/login",
        json={"password": "wrong-password-value"},
        headers={**headers, "X-Forwarded-For": "203.0.113.200"},
    )

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many sign-in attempts. Try again later."}
    assert 1 <= int(blocked.headers["Retry-After"]) <= 900


def test_login_fails_closed_when_atomic_rate_limiter_is_unavailable(
    auth_client: TestClient,
    monkeypatch,
) -> None:
    import app.api.auth_routes as auth_routes

    def unavailable(*args, **kwargs):
        raise LoginRateLimiterUnavailable("internal database detail")

    monkeypatch.setattr(auth_routes, "reserve_login_attempt", unavailable)
    csrf = obtain_csrf(auth_client)
    response = auth_client.post(
        "/api/auth/login",
        json={"password": TEST_PASSWORD},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Login rate limiting is unavailable for the configured database."
    }
    assert "internal database detail" not in response.text


def test_login_rate_limit_window_resets(auth_client: TestClient, monkeypatch) -> None:
    now = [1_800_000_000]
    monkeypatch.setattr(auth_rate_limit.time, "time", lambda: now[0])
    csrf = obtain_csrf(auth_client)
    headers = {CSRF_HEADER: csrf}

    for _ in range(5):
        response = auth_client.post(
            "/api/auth/login",
            json={"password": "wrong-password-value"},
            headers=headers,
        )
        assert response.status_code == 401
    assert auth_client.post(
        "/api/auth/login",
        json={"password": "wrong-password-value"},
        headers=headers,
    ).status_code == 429

    now[0] += 901
    after_window = auth_client.post(
        "/api/auth/login",
        json={"password": "wrong-password-value"},
        headers=headers,
    )
    assert after_window.status_code == 401


def test_successful_login_clears_its_failed_attempt_bucket(auth_client: TestClient) -> None:
    csrf = obtain_csrf(auth_client)
    headers = {CSRF_HEADER: csrf}
    for _ in range(4):
        response = auth_client.post(
            "/api/auth/login",
            json={"password": "wrong-password-value"},
            headers=headers,
        )
        assert response.status_code == 401

    successful = auth_client.post(
        "/api/auth/login",
        json={"password": TEST_PASSWORD},
        headers=headers,
    )
    assert successful.status_code == 200

    csrf = successful.json()["csrf_token"]
    for _ in range(5):
        response = auth_client.post(
            "/api/auth/login",
            json={"password": "wrong-password-value"},
            headers={CSRF_HEADER: csrf},
        )
        assert response.status_code == 401
    assert auth_client.post(
        "/api/auth/login",
        json={"password": "wrong-password-value"},
        headers={CSRF_HEADER: csrf},
    ).status_code == 429


def test_trusted_proxy_forwarded_chain_is_used_only_for_configured_proxy(auth_client: TestClient, monkeypatch) -> None:
    from starlette.requests import Request

    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        trusted_proxy_cidrs="192.0.2.0/24",
    )
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "headers": [(b"x-forwarded-for", b"198.51.100.7, 192.0.2.11")],
        "server": ("testserver", 80),
        "client": ("192.0.2.10", 12345),
    }

    key = auth_rate_limit.login_client_key(Request(scope), settings)

    direct_settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    direct_scope = {**scope, "headers": [], "client": ("198.51.100.7", 12345)}
    direct_key = auth_rate_limit.login_client_key(Request(direct_scope), direct_settings)
    assert key == direct_key


def test_direct_client_addresses_have_independent_rate_limit_identities() -> None:
    from starlette.requests import Request

    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )

    def request_from(address: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/auth/login",
                "raw_path": b"/api/auth/login",
                "query_string": b"",
                "headers": [],
                "server": ("testserver", 80),
                "client": (address, 12345),
            }
        )

    first = auth_rate_limit.login_client_key(request_from("198.51.100.7"), settings)
    second = auth_rate_limit.login_client_key(request_from("198.51.100.8"), settings)
    assert first != second


def test_forwarding_headers_are_ignored_for_untrusted_or_missing_proxy() -> None:
    from starlette.requests import Request

    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        trusted_proxy_cidrs="192.0.2.0/24",
    )

    def request_from(peer: str | None, forwarded: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/auth/login",
                "raw_path": b"/api/auth/login",
                "query_string": b"",
                "headers": [(b"x-forwarded-for", forwarded.encode("ascii"))],
                "server": ("testserver", 80),
                "client": (peer, 12345) if peer else None,
            }
        )

    untrusted_one = auth_rate_limit.login_client_key(
        request_from("203.0.113.9", "198.51.100.1"), settings
    )
    untrusted_two = auth_rate_limit.login_client_key(
        request_from("203.0.113.9", "198.51.100.2"), settings
    )
    direct = auth_rate_limit.login_client_key(request_from("203.0.113.9", ""), settings)
    assert untrusted_one == untrusted_two == direct

    missing_one = auth_rate_limit.login_client_key(request_from(None, "198.51.100.1"), settings)
    missing_two = auth_rate_limit.login_client_key(request_from(None, "198.51.100.2"), settings)
    assert missing_one == missing_two


def test_api_docs_remain_local_and_are_disabled_in_production() -> None:
    local_app = create_app(Settings(_env_file=None, app_env="development"))
    production_settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=TEST_POSTGRES_URL,
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


def test_database_url_is_masked_and_production_requires_postgresql() -> None:
    local_settings = Settings(_env_file=None)
    assert "sqlite:///./lodestar.db" not in repr(local_settings)
    assert local_settings.database_url.get_secret_value() == "sqlite:///./lodestar.db"

    with pytest.raises(ValidationError, match="Production requires PostgreSQL"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="sqlite:///./lodestar.db",
            cors_origins="https://demo.example",
            lodestar_auth_password=SecretStr(TEST_PASSWORD),
            lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
        )

    production_settings = Settings(
        _env_file=None,
        app_env="production",
        database_url=TEST_POSTGRES_URL,
        cors_origins="https://demo.example",
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SESSION_SECRET),
    )
    assert production_settings.database_url.get_secret_value() == TEST_POSTGRES_URL
    assert TEST_POSTGRES_URL not in repr(production_settings)


def test_database_url_rejects_unsupported_dialects() -> None:
    with pytest.raises(ValidationError, match="SQLite or PostgreSQL"):
        Settings(_env_file=None, database_url="mysql://localhost/lodestar")
