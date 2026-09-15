import pytest

from app.routing.base import BikeType, RouteRequest, RoutingError
from app.routing.selector import priority_for, route_with_fallback

from .conftest import MockProvider

_REQUEST = RouteRequest(waypoints=[(50.0, 5.0), (50.1, 5.1)], bike_type=BikeType.mtb)


def test_priority_mtb_prefers_brouter() -> None:
    assert priority_for(BikeType.mtb)[0] == "brouter"
    assert priority_for(BikeType.trekking)[0] == "brouter"


def test_priority_road_prefers_ors() -> None:
    assert priority_for(BikeType.road)[0] == "openrouteservice"
    assert priority_for(BikeType.gravel)[0] == "openrouteservice"


async def test_route_with_fallback_uses_second_provider() -> None:
    primary = MockProvider("primary", fail=True)
    secondary = MockProvider("secondary")
    result, warnings = await route_with_fallback([primary, secondary], _REQUEST)
    assert result.provider == "secondary"
    assert len(warnings) == 1
    assert "primary" in warnings[0]


async def test_route_with_fallback_all_fail() -> None:
    with pytest.raises(RoutingError):
        await route_with_fallback([MockProvider("a", fail=True), MockProvider("b", fail=True)], _REQUEST)
