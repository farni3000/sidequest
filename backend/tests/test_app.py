import json

from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.services.planner import PlanView, ReviewView


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


def test_plan_stream_emits_status_and_result(monkeypatch) -> None:
    async def fake_create_plan(form, session, settings, on_event=None):
        if on_event:
            await on_event({"type": "status", "message": "Teststatus"})
        return PlanView(
            provider_used="mock",
            bike_type="road",
            bike_type_label="Rennrad",
            start_label="Spa",
            end_label="Stavelot",
            distance_km=42.0,
            elevation_gain_m=500.0,
            elevation_loss_m=480.0,
            heat=50,
            geometry=[[50.5, 5.5], [50.6, 5.6]],
            elevations=[100.0, 200.0],
            review=ReviewView(
                checked=True,
                accepted=True,
                score=90,
                uncertainty=0.1,
                rounds=1,
                summary="Passt.",
                model="deepseek-flash",
            ),
            gpx_url="/plan/1/gpx",
        )

    monkeypatch.setattr(main, "create_plan", fake_create_plan)

    with TestClient(app) as client:
        response = client.post(
            "/plan",
            data={
                "bike_type": "road",
                "start": "Spa",
                "end": "Stavelot",
                "heat": "50",
            },
            headers={"Accept": "application/x-ndjson"},
        )

    assert response.status_code == 200
    assert "application/x-ndjson" in response.headers["content-type"]
    payloads = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert payloads[0] == {"type": "status", "message": "Teststatus"}
    assert payloads[-1]["type"] == "result"
    assert "KI-geprüft" in payloads[-1]["html"]
    assert "deepseek-flash" in payloads[-1]["html"]


def test_plan_html_fallback_still_works(monkeypatch) -> None:
    async def fake_create_plan(form, session, settings, on_event=None):
        return PlanView(
            provider_used="mock",
            bike_type="road",
            bike_type_label="Rennrad",
            start_label="Spa",
            end_label="Stavelot",
            distance_km=42.0,
            elevation_gain_m=500.0,
            elevation_loss_m=480.0,
            heat=50,
            geometry=[[50.5, 5.5], [50.6, 5.6]],
            elevations=[100.0, 200.0],
            gpx_url="/plan/1/gpx",
        )

    monkeypatch.setattr(main, "create_plan", fake_create_plan)

    with TestClient(app) as client:
        response = client.post(
            "/plan",
            data={
                "bike_type": "road",
                "start": "Spa",
                "end": "Stavelot",
                "heat": "50",
            },
            headers={"Accept": "text/html"},
        )

    assert response.status_code == 200
    assert "Deine Route" in response.text
    assert "route-map" in response.text
