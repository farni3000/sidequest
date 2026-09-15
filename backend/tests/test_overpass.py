import httpx
import respx

from app.poi.overpass import build_query, fetch_pois, parse_elements
from app.routing.base import BikeType


def test_build_query_targets_bike_specific_tags() -> None:
    query = build_query([(50.5, 5.5), (50.4, 5.6)], 2000, BikeType.mtb)
    assert "node(" in query
    assert "way(" in query
    assert '"mtb:scale"' in query
    assert "tourism=viewpoint" in query
    assert "out center" in query
    assert "[out:json]" in query


def test_parse_elements_infers_category_and_detour() -> None:
    elements = [
        {
            "type": "node",
            "id": 1,
            "lat": 50.5005,
            "lon": 5.5,
            "tags": {"name": "Blick", "tourism": "viewpoint"},
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 50.51, "lon": 5.5},
            "tags": {"name": "Trail", "mtb:scale": "2"},
        },
        {"type": "node", "id": 3, "lat": 50.5, "lon": 5.5, "tags": {}},
    ]
    pois = parse_elements(elements, [(50.5, 5.5), (50.4, 5.6)])
    assert len(pois) == 2
    categories = {poi.category for poi in pois}
    assert categories == {"viewpoint", "mtb_trail"}
    assert all(poi.detour_km >= 0 for poi in pois)


@respx.mock
async def test_fetch_pois_parses_response(settings) -> None:
    payload = {
        "elements": [
            {
                "type": "node",
                "id": 10,
                "lat": 50.5,
                "lon": 5.5,
                "tags": {"name": "Aussicht", "tourism": "viewpoint"},
            }
        ]
    }
    respx.post(settings.overpass_url).mock(return_value=httpx.Response(200, json=payload))
    pois = await fetch_pois([(50.5, 5.5)], 1000, BikeType.road, settings)
    assert len(pois) == 1
    assert pois[0].name == "Aussicht"
