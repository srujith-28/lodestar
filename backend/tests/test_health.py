"""Health endpoint integration test."""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app, create_app


client = TestClient(app)


def test_health_reports_api_and_database_as_healthy() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"]
    assert payload["database"] == "connected"


def test_built_frontend_assets_can_be_served_from_fastapi(tmp_path, monkeypatch) -> None:
    from app import main

    dist = tmp_path / "frontend" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>Lodestar demo</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('built');", encoding="utf-8")
    monkeypatch.setattr(main, "REPOSITORY_ROOT", tmp_path)

    with TestClient(create_app(Settings(_env_file=None))) as client:
        home = client.get("/")
        asset = client.get("/assets/app.js")

    assert home.status_code == 200
    assert "Lodestar demo" in home.text
    assert asset.status_code == 200
    assert "console.log" in asset.text
