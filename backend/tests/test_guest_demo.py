"""Guest demo isolation, sample seed, reset, and rate-limit integration tests."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.security import CSRF_HEADER, require_authenticated
from app.database import Base, get_db
from app.main import app


TEST_PASSWORD = "guest-test-private-password"
TEST_SECRET = "guest-test-session-secret-at-least-32-characters"


@pytest.fixture
def guest_demo_client(tmp_path) -> Iterator[TestClient]:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'guest-demo.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SECRET),
        guest_demo_enabled=True,
    )
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(require_authenticated, None)
        engine.dispose()


def csrf(client: TestClient) -> str:
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def start_guest(client: TestClient) -> dict:
    token = csrf(client)
    response = client.post("/api/auth/demo", headers={CSRF_HEADER: token})
    assert response.status_code == 200, response.text
    return response.json()


def test_guest_seed_is_labeled_real_local_work_and_start_is_idempotent(
    guest_demo_client: TestClient,
) -> None:
    client = guest_demo_client
    started = start_guest(client)
    assert started["workspace_type"] == "guest"
    assert started["authenticated"] is True

    memories = client.get("/api/memories").json()
    assert len(memories) == 3
    assert all(memory["is_sample"] is True for memory in memories)
    assert sum("fictional" in memory["content"].lower() for memory in memories) >= 2

    tasks = client.get("/api/agent/tasks").json()
    assert len(tasks) == 1
    sample_task = tasks[0]
    assert sample_task["is_sample"] is True
    assert sample_task["status"] == "completed"
    assert sample_task["plan_steps"][0]["tool_name"] == "memory_search"
    trace = client.get(f"/api/agent/tasks/{sample_task['id']}/trace").json()
    assert [event["event_type"] for event in trace["events"]][-1] == "task_completed"
    assert trace["events"][-2]["result_data"]["count"] >= 1

    repeated = client.post(
        "/api/auth/demo",
        headers={CSRF_HEADER: started["csrf_token"]},
    )
    assert repeated.status_code == 200
    assert repeated.json()["workspace_type"] == "guest"
    assert len(client.get("/api/memories").json()) == 3


def test_guest_access_works_without_a_private_password(guest_demo_client: TestClient) -> None:
    from app.core.config import get_settings

    guest_only = Settings(
        _env_file=None,
        guest_demo_enabled=True,
        lodestar_session_secret=SecretStr(TEST_SECRET),
    )
    app.dependency_overrides[get_settings] = lambda: guest_only
    status_response = guest_demo_client.get("/api/auth/session")
    assert status_response.json() == {
        "configured": False,
        "authenticated": False,
        "demo_enabled": True,
        "workspace_type": None,
    }
    started = start_guest(guest_demo_client)
    assert started["workspace_type"] == "guest"
    assert guest_demo_client.get("/api/memories").status_code == 200


def test_guest_workspaces_are_isolated_and_reset_only_deletes_the_callers_records(
    guest_demo_client: TestClient,
) -> None:
    guest_one = guest_demo_client
    guest_one_login = start_guest(guest_one)
    guest_one_csrf = guest_one_login["csrf_token"]
    one_memory = guest_one.post(
        "/api/memories",
        json={"content": "Guest one private note", "category": "test", "source": "test"},
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    one_task = guest_one.post(
        "/api/agent/tasks",
        json={"task_input": "Read the time", "tool_name": "current_datetime"},
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    memory_count_before_write = len(guest_one.get("/api/memories").json())
    write_task = guest_one.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save an explicitly approved demo note",
            "tool_name": "save_memory",
            "tool_input": {
                "content": "Guest one approved note",
                "category": "demo",
                "source": "guest action",
            },
        },
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    assert write_task["status"] == "awaiting_approval"
    assert len(guest_one.get("/api/memories").json()) == memory_count_before_write
    denied = guest_one.post(
        f"/api/agent/tasks/{write_task['id']}/approval",
        json={"approved": False},
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    assert denied["status"] == "failed"
    assert len(guest_one.get("/api/memories").json()) == memory_count_before_write

    approved_write = guest_one.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save a second explicitly approved demo note",
            "tool_name": "save_memory",
            "tool_input": {
                "content": "Guest one approved note two",
                "category": "demo",
                "source": "guest action",
            },
        },
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    saved = guest_one.post(
        f"/api/agent/tasks/{approved_write['id']}/approval",
        json={"approved": True},
        headers={CSRF_HEADER: guest_one_csrf},
    ).json()
    assert saved["status"] == "completed"
    assert len(guest_one.get("/api/memories").json()) == memory_count_before_write + 1

    with TestClient(app) as guest_two:
        guest_two_login = start_guest(guest_two)
        assert len(guest_two.get("/api/memories").json()) == 3
        assert len(guest_two.get("/api/agent/tasks").json()) == 1
        assert guest_two.get(f"/api/memories/{one_memory['id']}").status_code == 404
        assert guest_two.get(f"/api/agent/tasks/{one_task['id']}").status_code == 404

        reset = guest_one.post(
            "/api/demo/reset",
            headers={CSRF_HEADER: guest_one_csrf},
        )
        assert reset.status_code == 200
        assert reset.json() == {"reset": True, "sample_data_restored": True}
        reset_memories = guest_one.get("/api/memories").json()
        assert len(reset_memories) == 3
        assert all(item["is_sample"] for item in reset_memories)
        reset_tasks = guest_one.get("/api/agent/tasks").json()
        assert len(reset_tasks) == 1 and reset_tasks[0]["is_sample"] is True

        # The second guest keeps its own records after guest one's reset.
        assert len(guest_two.get("/api/memories").json()) == 3
        assert len(guest_two.get("/api/agent/tasks").json()) == 1
        assert guest_two_login["workspace_type"] == "guest"


def test_private_workspace_records_are_never_visible_or_reset_by_guests(
    guest_demo_client: TestClient,
) -> None:
    private_client = guest_demo_client
    private_csrf = csrf(private_client)
    login = private_client.post(
        "/api/auth/login",
        json={"password": TEST_PASSWORD},
        headers={CSRF_HEADER: private_csrf},
    )
    assert login.status_code == 200
    private_memory = private_client.post(
        "/api/memories",
        json={"content": "Private record stays private", "category": "note", "source": "user"},
        headers={CSRF_HEADER: login.json()["csrf_token"]},
    ).json()
    logout = private_client.post(
        "/api/auth/logout",
        headers={CSRF_HEADER: login.json()["csrf_token"]},
    )
    assert logout.status_code == 200

    guest_client = TestClient(app)
    guest_login = start_guest(guest_client)
    assert len(guest_client.get("/api/memories").json()) == 3
    assert guest_client.get(f"/api/memories/{private_memory['id']}").status_code == 404
    denied_reset = guest_client.post(
        "/api/demo/reset",
        headers={CSRF_HEADER: guest_login["csrf_token"]},
    )
    assert denied_reset.status_code == 200

    with TestClient(app) as private_again:
        private_csrf = csrf(private_again)
        login_again = private_again.post(
            "/api/auth/login",
            json={"password": TEST_PASSWORD},
            headers={CSRF_HEADER: private_csrf},
        )
        assert login_again.status_code == 200
        records = private_again.get("/api/memories").json()
        assert [record["id"] for record in records] == [private_memory["id"]]
        assert records[0]["is_sample"] is False
    guest_client.close()


def test_guest_request_cap_is_per_workspace_and_returns_retry_after(
    guest_demo_client: TestClient,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SECRET),
        guest_demo_enabled=True,
        guest_api_rate_limit_max_requests=10,
    )
    from app.core.config import get_settings

    app.dependency_overrides[get_settings] = lambda: settings
    first = guest_demo_client
    start_guest(first)
    for _ in range(10):
        assert first.get("/api/provider/status").status_code == 200
    blocked = first.get("/api/provider/status")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    with TestClient(app) as second:
        start_guest(second)
        assert second.get("/api/provider/status").status_code == 200


def test_guest_bootstrap_limit_ignores_spoofed_forwarding_headers(
    guest_demo_client: TestClient,
) -> None:
    from app.core.config import get_settings

    settings = Settings(
        _env_file=None,
        lodestar_auth_password=SecretStr(TEST_PASSWORD),
        lodestar_session_secret=SecretStr(TEST_SECRET),
        guest_demo_enabled=True,
        guest_session_rate_limit_max_attempts=1,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    client = guest_demo_client
    started = start_guest(client)
    assert client.post(
        "/api/auth/logout",
        headers={CSRF_HEADER: started["csrf_token"]},
    ).status_code == 200
    token = csrf(client)
    blocked = client.post(
        "/api/auth/demo",
        headers={CSRF_HEADER: token, "X-Forwarded-For": "203.0.113.77"},
    )
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 900
