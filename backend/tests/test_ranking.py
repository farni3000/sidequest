from types import SimpleNamespace

from app.config import Settings
from app.poi.overpass import POI
from app.poi.ranking import (
    RankingContext,
    _fallback,
    _parse_arguments,
    rank_pois,
)
from app.routing.base import BikeType


def _pois() -> list[POI]:
    return [
        POI(osm_id="node/1", name="Nah", category="viewpoint", lat=50.5, lon=5.5, detour_km=1.0),
        POI(osm_id="node/2", name="Mittel", category="peak", lat=50.6, lon=5.6, detour_km=5.0),
        POI(osm_id="node/3", name="Weit", category="lake", lat=50.7, lon=5.7, detour_km=30.0),
    ]


def test_fallback_respects_budget_and_count() -> None:
    context = RankingContext(
        bike_type=BikeType.road, max_highlights=2, budget_km=10.0
    )
    result = _fallback(_pois(), context)
    assert result.source == "fallback"
    assert [item.osm_id for item in result.items] == ["node/1", "node/2"]


def test_parse_arguments_drops_unknown_ids_and_reorders() -> None:
    context = RankingContext(bike_type=BikeType.road, max_highlights=5)
    arguments = {
        "highlights": [
            {"osm_id": "node/2", "order": 2, "rationale": "b"},
            {"osm_id": "node/1", "order": 1, "rationale": "a"},
            {"osm_id": "node/999", "order": 3, "rationale": "x"},
        ],
        "summary": "schön",
    }
    result = _parse_arguments(arguments, _pois(), context)
    assert result.source == "deepseek"
    assert [item.osm_id for item in result.items] == ["node/1", "node/2"]
    assert result.items[0].order == 1
    assert result.summary == "schön"


async def test_rank_pois_without_key_uses_fallback() -> None:
    settings = Settings(_env_file=None, deepseek_api_key=None)
    context = RankingContext(bike_type=BikeType.road, max_highlights=3, budget_km=100)
    result = await rank_pois(_pois(), context, settings)
    assert result.source == "fallback"


async def test_rank_pois_calls_deepseek(monkeypatch) -> None:
    settings = Settings(
        _env_file=None, deepseek_api_key="test-key", deepseek_base_url="https://ds.test"
    )

    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            arguments='{"highlights":[{"osm_id":"node/2","order":1,"rationale":"Gipfel!"}]}'
        )
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeCompletions:
        async def create(self, **kwargs):
            return response

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)

    context = RankingContext(bike_type=BikeType.road, max_highlights=3, budget_km=100)
    result = await rank_pois(_pois(), context, settings)
    assert result.source == "deepseek"
    assert result.items[0].osm_id == "node/2"
