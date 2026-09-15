import httpx
import respx

from app.routing.base import BikeType, MtbDifficulty, RouteRequest
from app.routing.brouter import BRouterProvider
from app.routing.brouter import select_profile as brouter_profile
from app.routing.graphhopper import GraphHopperProvider
from app.routing.openrouteservice import OpenRouteServiceProvider
from app.routing.openrouteservice import select_profile as ors_profile

_REQUEST = RouteRequest(
    waypoints=[(50.4925, 5.8663), (50.3951, 5.9313)], bike_type=BikeType.trekking
)


def test_brouter_profile_selection() -> None:
    assert brouter_profile(BikeType.road, None) == "fastbike"
    assert brouter_profile(BikeType.gravel, None) == "gravel"
    assert brouter_profile(BikeType.mtb, MtbDifficulty.enduro) == "mtb"


def test_ors_profile_selection() -> None:
    assert ors_profile(BikeType.road) == "cycling-road"
    assert ors_profile(BikeType.mtb) == "cycling-mountain"
    assert ors_profile(BikeType.trekking) == "cycling-regular"


@respx.mock
async def test_brouter_parses_geojson() -> None:
    payload = {
        "features": [
            {
                "geometry": {
                    "coordinates": [[5.86, 50.49, 250.0], [5.93, 50.39, 300.0]]
                },
                "properties": {
                    "track-length": "21546",
                    "filtered ascend": "312",
                    "total-time": "5105",
                },
            }
        ]
    }
    respx.get("http://brouter.test/brouter").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await BRouterProvider("http://brouter.test").route(_REQUEST)
    assert result.provider == "brouter"
    assert result.distance_km == 21.55
    assert result.elevation_gain_m == 312.0
    assert len(result.geometry) == 2
    assert result.geometry[0] == (50.49, 5.86)


@respx.mock
async def test_brouter_raises_on_error_feature() -> None:
    respx.get("http://brouter.test/brouter").mock(
        return_value=httpx.Response(200, json={"message": "no route"})
    )
    try:
        await BRouterProvider("http://brouter.test").route(_REQUEST)
    except Exception as exc:  # noqa: BLE001
        assert "BRouter" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RoutingError")


@respx.mock
async def test_ors_parses_geojson() -> None:
    payload = {
        "features": [
            {
                "geometry": {
                    "coordinates": [[5.86, 50.49, 250.0], [5.93, 50.39, 300.0]]
                },
                "properties": {"summary": {"distance": 12345, "ascent": 100, "descent": 90}},
            }
        ]
    }
    respx.post("https://ors.test/v2/directions/cycling-road/geojson").mock(
        return_value=httpx.Response(200, json=payload)
    )
    request = RouteRequest(waypoints=_REQUEST.waypoints, bike_type=BikeType.road)
    result = await OpenRouteServiceProvider("key", "https://ors.test").route(request)
    assert result.distance_km == 12.35
    assert result.elevation_gain_m == 100.0


@respx.mock
async def test_graphhopper_parses_geojson() -> None:
    payload = {
        "paths": [
            {
                "distance": 8000,
                "ascend": 120,
                "descend": 110,
                "points": {"coordinates": [[5.86, 50.49, 250.0], [5.93, 50.39, 300.0]]},
            }
        ]
    }
    respx.post("https://gh.test/route").mock(return_value=httpx.Response(200, json=payload))
    result = await GraphHopperProvider("key", "https://gh.test").route(_REQUEST)
    assert result.distance_km == 8.0
    assert result.elevation_gain_m == 120.0
