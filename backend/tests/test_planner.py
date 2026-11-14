import pytest
from sqlmodel import select

import app.map_image as map_image
import app.services.planner as planner
from app.ai.route_review import FreeWaypoint, RouteReview, RouteRevision
from app.map_image import RouteMap
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


def test_mtb_difficulty_only_applies_to_mtb() -> None:
    gravel = PlanForm(
        bike_type=BikeType.gravel,
        mtb_difficulty=MtbDifficulty.xc,
        start="Spa",
    )
    assert gravel.mtb_difficulty is None

    mtb = PlanForm(
        bike_type=BikeType.mtb,
        mtb_difficulty=MtbDifficulty.enduro,
        start="Spa",
    )
    assert mtb.mtb_difficulty is MtbDifficulty.enduro


# --- vision review loop ----------------------------------------------------


def _review_settings(settings):
    return settings.model_copy(
        update={
            "deepseek_api_key": "test-key",
            "route_review_enabled": True,
            "route_review_max_rounds": 3,
            "route_review_timeout_s": 5.0,
        }
    )


def _fake_route_map(monkeypatch) -> None:
    async def fake_render(*args, **kwargs):
        return RouteMap(image=b"jpeg", width=100, height=100, bounds=(0, 0, 1, 1))

    monkeypatch.setattr(map_image, "render_route_map", fake_render)


async def test_review_accepts_route_without_correction(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    _fake_route_map(monkeypatch)
    review_calls: list = []

    async def fake_review(route_map, context, settings):
        review_calls.append(context)
        return RouteReview(
            score=88,
            uncertainty=0.12,
            verdict="ok",
            summary="Passt.",
            model="deepseek-flash",
        )

    monkeypatch.setattr(planner, "review_route", fake_review)

    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot", distance_km=40)
    view = await create_plan(
        form,
        session,
        _review_settings(settings),
        providers=[MockProvider("mock")],
    )

    assert len(review_calls) == 1
    assert view.review is not None
    assert view.review.accepted is True
    assert view.review.score == 88
    assert view.review.rounds == 1
    assert len(view.highlights) == 1


async def test_review_revises_once_then_accepts(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    _fake_route_map(monkeypatch)
    reviews = [
        RouteReview(
            score=50,
            uncertainty=0.3,
            verdict="revise",
            summary="Zu weit.",
            model="deepseek-flash",
        ),
        RouteReview(
            score=90,
            uncertainty=0.1,
            verdict="ok",
            summary="Jetzt passt es.",
            model="deepseek-flash",
        ),
    ]
    review_calls = 0
    revision_calls = 0

    async def fake_review(route_map, context, settings):
        nonlocal review_calls
        review_calls += 1
        return reviews[review_calls - 1]

    async def fake_revision(*args, **kwargs):
        nonlocal revision_calls
        revision_calls += 1
        return RouteRevision(
            can_fix=True,
            reason="Highlight passt nicht.",
            remove_highlights=["node/1"],
            model="deepseek-flash",
        )

    monkeypatch.setattr(planner, "review_route", fake_review)
    monkeypatch.setattr(planner, "revise_route", fake_revision)

    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot", distance_km=40)
    view = await create_plan(
        form,
        session,
        _review_settings(settings),
        providers=[MockProvider("mock")],
    )

    assert review_calls == 2
    assert revision_calls == 1
    assert view.review is not None
    assert view.review.accepted is True
    assert view.review.rounds == 2
    assert view.highlights == []


async def test_review_stops_after_max_rounds(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    _fake_route_map(monkeypatch)
    review_calls = 0
    revision_calls = 0
    scores = [50, 60, 55]

    async def fake_review(route_map, context, settings):
        nonlocal review_calls
        review_calls += 1
        return RouteReview(
            score=scores[review_calls - 1],
            uncertainty=0.25,
            verdict="revise",
            summary="Noch nicht perfekt.",
            model="deepseek-flash",
        )

    async def fake_revision(*args, **kwargs):
        nonlocal revision_calls
        revision_calls += 1
        if revision_calls == 1:
            return RouteRevision(
                can_fix=True, remove_highlights=["node/1"], model="deepseek-flash"
            )
        return RouteRevision(
            can_fix=True,
            add_highlights=[{"osm_id": "node/1", "rationale": "Doch behalten."}],
            model="deepseek-flash",
        )

    monkeypatch.setattr(planner, "review_route", fake_review)
    monkeypatch.setattr(planner, "revise_route", fake_revision)

    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot", distance_km=40)
    view = await create_plan(
        form,
        session,
        _review_settings(settings),
        providers=[MockProvider("mock")],
    )

    assert review_calls == 3
    assert revision_calls == 2
    assert view.review is not None
    assert view.review.rounds == 3
    assert view.review.accepted is False
    assert any("nicht verbessert" in warning for warning in view.warnings)


async def test_review_failure_never_blocks_route(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    _fake_route_map(monkeypatch)

    async def failing_review(*args, **kwargs):
        raise RuntimeError("vision down")

    monkeypatch.setattr(planner, "review_route", failing_review)

    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot", distance_km=40)
    view = await create_plan(
        form,
        session,
        _review_settings(settings),
        providers=[MockProvider("mock")],
    )

    assert view.review is None
    assert any("Streckenprüfung" in warning for warning in view.warnings)
    assert len(view.highlights) == 1


async def test_free_waypoint_revision_is_routed(monkeypatch, session, settings) -> None:
    _patch(monkeypatch, [_poi()])
    _fake_route_map(monkeypatch)
    reviews = [
        RouteReview(score=40, verdict="revise", model="deepseek-flash"),
        RouteReview(score=95, verdict="ok", model="deepseek-flash"),
    ]
    review_calls = 0
    provider = MockProvider("mock")

    async def fake_review(route_map, context, settings):
        nonlocal review_calls
        review_calls += 1
        return reviews[review_calls - 1]

    async def fake_revision(*args, **kwargs):
        return RouteRevision(
            can_fix=True,
            free_waypoints=[
                FreeWaypoint(after="start", lat=50.52, lon=5.52, reason="Südlicher")
            ],
            model="deepseek-flash",
        )

    monkeypatch.setattr(planner, "review_route", fake_review)
    monkeypatch.setattr(planner, "revise_route", fake_revision)

    form = PlanForm(bike_type=BikeType.road, start="Spa", end="Stavelot", distance_km=40)
    await create_plan(
        form,
        session,
        _review_settings(settings),
        providers=[provider],
    )

    assert review_calls == 2
    assert any((50.52, 5.52) in call.waypoints for call in provider.calls)
