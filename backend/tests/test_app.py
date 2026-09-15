from fastapi.testclient import TestClient

from app.main import app


def test_health_and_index() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        index = client.get("/")
        assert index.status_code == 200
        assert "Route planen" in index.text
        assert "Heat" in index.text


def test_gpx_unknown_plan_returns_404() -> None:
    with TestClient(app) as client:
        assert client.get("/plan/999999/gpx").status_code == 404
