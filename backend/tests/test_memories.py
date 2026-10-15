"""Persistent-memory API tests using isolated local SQLite files."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Memory


@pytest.fixture
def memory_client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker[Session], str]]:
    database_path = tmp_path / "memory-test.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    test_engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db() -> Iterator[Session]:
        with TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, TestSession, database_url
    finally:
        app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()


def make_memory(client: TestClient, content: str = "Prefers quiet morning work") -> dict:
    response = client.post(
        "/api/memories",
        json={"content": content, "category": "preference", "source": "user"},
    )
    assert response.status_code == 201
    return response.json()


def test_create_list_retrieve_and_persist_across_database_recreation(memory_client) -> None:
    client, _, database_url = memory_client
    created = make_memory(client)

    assert created["id"] > 0
    assert created["content"] == "Prefers quiet morning work"
    assert created["category"] == "preference"
    assert created["source"] == "user"
    assert created["created_at"].endswith("Z")
    assert created["updated_at"] >= created["created_at"]

    listed = client.get("/api/memories")
    retrieved = client.get(f"/api/memories/{created['id']}")
    assert listed.status_code == 200
    assert listed.json() == [created]
    assert retrieved.status_code == 200
    assert retrieved.json() == created

    # Close the original engine and reopen the same file to simulate a service restart.
    reopened_engine = create_engine(database_url, connect_args={"check_same_thread": False})
    try:
        with Session(reopened_engine) as new_session:
            persisted = new_session.scalar(select(Memory).where(Memory.id == created["id"]))
            assert persisted is not None
            assert persisted.content == "Prefers quiet morning work"
    finally:
        reopened_engine.dispose()


def test_update_changes_memory_and_timestamp(memory_client) -> None:
    client, _, _ = memory_client
    created = make_memory(client)

    response = client.put(
        f"/api/memories/{created['id']}",
        json={"content": "Prefers quiet evening work", "category": "routine", "source": "user"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["id"] == created["id"]
    assert updated["content"] == "Prefers quiet evening work"
    assert updated["category"] == "routine"
    assert updated["updated_at"] >= created["updated_at"]


def test_keyword_search_matches_content_category_and_is_local(memory_client) -> None:
    client, _, _ = memory_client
    first = make_memory(client, "Keep notes about mountain hikes")
    make_memory(client, "Use a quiet workspace")

    response = client.get("/api/memories/search", params={"q": "mountain hikes"})
    category_response = client.get("/api/memories/search", params={"q": "preference"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [first["id"]]
    assert len(category_response.json()) == 2


def test_delete_is_permanent_and_nonexistent_ids_return_404(memory_client) -> None:
    client, TestSession, _ = memory_client
    created = make_memory(client)

    response = client.delete(f"/api/memories/{created['id']}")

    assert response.status_code == 204
    assert client.get(f"/api/memories/{created['id']}").status_code == 404
    assert client.put(
        f"/api/memories/{created['id']}",
        json={"content": "A replacement", "category": "general", "source": "user"},
    ).status_code == 404
    assert client.delete(f"/api/memories/{created['id']}").status_code == 404
    assert client.get("/api/memories").json() == []
    with TestSession() as session:
        assert session.scalar(select(Memory).where(Memory.id == created["id"])) is None


@pytest.mark.parametrize(
    ("content", "status_code"),
    [
        ("   ", 422),
        ("x" * 5_001, 422),
        ("NEBIUS_API_KEY=test-secret-value", 422),
        ("My password is local-only-test-secret", 422),
    ],
)
def test_invalid_and_sensitive_input_is_rejected_without_echoing(
    memory_client, content: str, status_code: int
) -> None:
    client, _, _ = memory_client

    response = client.post(
        "/api/memories",
        json={"content": content, "category": "general", "source": "user"},
    )

    assert response.status_code == status_code
    assert "test-secret-value" not in response.text
    assert client.get("/api/memories").json() == []


def test_invalid_ids_and_search_inputs_are_rejected(memory_client) -> None:
    client, _, _ = memory_client

    assert client.get("/api/memories/0").status_code == 422
    assert client.get("/api/memories/not-an-id").status_code == 422
    assert client.get("/api/memories/search", params={"q": "   "}).status_code == 422
    assert client.get("/api/memories/search", params={"q": "word " * 11}).status_code == 422
