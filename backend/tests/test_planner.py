import pytest
from sqlmodel import select

import app.services.planner as planner
from app.models import Highlight, RoutePlan
from app.poi.overpass import POI, OverpassError
from app.poi.ranking import RankedItem, RankingResult
from app.routing.base import BikeType, MtbDifficulty
from app.schemas import PlanForm
from app.services.planner import PlanError, create_plan

from .conftest import MockProvider


def _poi() -> POI:
    return POI(
        osm_id="node/1",
        name="Aussichtspunkt",
        category="viewpoint",
        lat=50.51,
        lon=5.51,
        detour_km=2.0,
    )


def _patch(monkeypatch, pois: list[POI]) -> None:
    async def fake_geocode(text, settings):
        return (50.5, 5.5, text)

    async def fake_fetch(points, width, bike_type, settings, reference_path=None):
        return pois

    async def fake_rank(pois, context, settings):
        return RankingResult(
            items=[RankedItem(osm_id=poi.osm_id, order=i + 1, rationale="toll") for i, poi in enumerate(pois)],
            source="deepseek",
        )

    monkeypatch.setattr(planner, "geocode", fake_geocode)
    monkeypatch.setattr(planner, "fetch_pois", fake_fetch)
    monkeypatch.setattr(planner, "rank_pois", fake_rank)


async def test_create_plan_persists_and_returns_highlights(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    provider = MockProvider("mock")
    form = PlanForm(
        bike_type=BikeType.road,
        start="Spa",
        end="Stavelot",
        heat=60,
        distance_km=40,
    )
    view = await create_plan(form, session, settings, providers=[provider])

    assert view.provider_used == "mock"
    assert view.distance_km == 42.0
    assert len(view.highlights) == 1
    assert view.highlights[0].name == "Aussichtspunkt"
    assert view.gpx_url == f"/plan/{view.id}/gpx"

    plans = session.exec(select(RoutePlan)).all()
    assert len(plans) == 1
    highlights = session.exec(select(Highlight)).all()
    assert len(highlights) == 1


async def test_roundtrip_without_highlights_creates_loop(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [])
    provider = MockProvider("mock")
    form = PlanForm(bike_type=BikeType.mtb, mtb_difficulty=MtbDifficulty.xc, start="Spa", round_trip=True, distance_km=30, heat=50)
    await create_plan(form, session, settings, providers=[provider])
    assert len(provider.calls[0].waypoints) >= 3


async def test_overpass_failure_still_routes(monkeypatch, session, settings) -> None:
    async def fake_geocode(text, settings):
        return (50.5, 5.5, text)

    async def failing_fetch(points, width, bike_type, settings, reference_path=None):
        raise OverpassError("timeout")

    async def fake_rank(pois, context, settings):
        return RankingResult(items=[])

    monkeypatch.setattr(planner, "geocode", fake_geocode)
    monkeypatch.setattr(planner, "fetch_pois", failing_fetch)
    monkeypatch.setattr(planner, "rank_pois", fake_rank)

    form = PlanForm(bike_type=BikeType.gravel, start="Spa", end="Stavelot")
    view = await create_plan(form, session, settings, providers=[MockProvider("mock")])
    assert view.highlights == []
    assert any("Highlight" in warning for warning in view.warnings)


async def test_routing_failure_raises_plan_error(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [])
    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot")
    with pytest.raises(PlanError):
        await create_plan(form, session, settings, providers=[MockProvider("down", fail=True)])
