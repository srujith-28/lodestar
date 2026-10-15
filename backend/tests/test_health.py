"""Health endpoint integration test."""

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_reports_api_and_database_as_healthy() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"]
    assert payload["database"] == "connected"
