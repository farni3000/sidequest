from app.config import Settings
from app.corridor import corridor_points, corridor_width_m, loop_waypoints, roundtrip_radius_km


def test_corridor_width_scales_with_heat(settings: Settings) -> None:
    assert corridor_width_m(0, settings) == settings.corridor_min_m
    assert corridor_width_m(100, settings) == settings.corridor_max_m
    middle = corridor_width_m(50, settings)
    assert settings.corridor_min_m < middle < settings.corridor_max_m


def test_corridor_width_clamps_out_of_range(settings: Settings) -> None:
    assert corridor_width_m(-20, settings) == settings.corridor_min_m
    assert corridor_width_m(500, settings) == settings.corridor_max_m


def test_corridor_points_include_endpoints() -> None:
    points = corridor_points((50.0, 5.0), (50.5, 5.5), samples=4)
    assert points[0] == (50.0, 5.0)
    assert points[-1] == (50.5, 5.5)
    assert len(points) == 4


def test_loop_waypoints_are_around_center() -> None:
    points = loop_waypoints((50.0, 5.0), radius_km=10.0, count=4)
    assert len(points) == 4
    for point in points:
        assert point != (50.0, 5.0)


def test_roundtrip_radius_grows_with_heat() -> None:
    assert roundtrip_radius_km(40, 0) < roundtrip_radius_km(40, 100)
    assert roundtrip_radius_km(None, 50) >= 3.0
