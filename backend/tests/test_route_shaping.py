from app.ai.route_shaping import (
    build_shaping_candidates,
    fallback_shaping_points,
    sanitize_draft,
    sanitize_shaping_selection,
)
from app.config import Settings
from app.map_image import RouteMap, _map_bounds, lonlat_to_mercator


def _geometry() -> list[tuple[float, float]]:
    # ~42 km, one point roughly every 0.7 km.
    return [(50.0, 5.0 + index * 0.01) for index in range(61)]


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key=None,
        gpx_min_point_spacing_km=2.0,
        gpx_max_gap_km=6.0,
        gpx_point_snap_tolerance_km=0.35,
    )


def _route_map(geometry: list[tuple[float, float]]) -> RouteMap:
    bounds = _map_bounds(geometry, 100, 100)
    return RouteMap(image=b"", width=100, height=100, bounds=bounds)


def _image_xy(route_map: RouteMap, lat: float, lon: float) -> tuple[float, float]:
    minx, miny, maxx, maxy = route_map.bounds
    mx, my = lonlat_to_mercator(lat, lon)
    return (mx - minx) / (maxx - minx), (maxy - my) / (maxy - miny)


def test_candidates_cover_route_with_spacing() -> None:
    geometry = _geometry()
    elevations = [100.0 + index for index in range(len(geometry))]
    candidates = build_shaping_candidates(
        geometry,
        elevations,
        [{"osm_id": "node/1", "name": "See", "lat": 50.0, "lon": 5.3}],
        anchors=[{"lat": 50.0, "lon": 5.15, "reason": "Am Fluss"}],
        spacing_km=4.0,
        max_points=30,
        min_spacing_km=2.0,
    )

    assert 8 <= len(candidates) <= 30
    assert candidates[0].kind == "start"
    assert candidates[-1].kind == "end"
    assert any(candidate.kind == "highlight" for candidate in candidates)
    assert any(candidate.kind == "draft" for candidate in candidates)
    assert all(
        candidate.km < following.km
        for candidate, following in zip(candidates, candidates[1:], strict=False)
    )


def test_fallback_points_are_ordered() -> None:
    candidates = build_shaping_candidates(_geometry(), None, [])
    points = fallback_shaping_points(candidates)

    assert points
    assert [point.order for point in points] == list(range(1, len(points) + 1))


def test_sanitize_shaping_keeps_forced_points_and_snaps_additions() -> None:
    geometry = _geometry()
    route_map = _route_map(geometry)
    candidates = build_shaping_candidates(geometry, None, [], spacing_km=4.0)
    keep_order = min(4, len(candidates))
    wanted = geometry[30]
    image_x, image_y = _image_xy(route_map, *wanted)

    selection = sanitize_shaping_selection(
        {
            "keep": [str(keep_order)],
            "add": [
                {"image_x": image_x, "image_y": image_y, "reason": "Schöne Kurve"}
            ],
            "summary": "Passt.",
        },
        route_map=route_map,
        geometry=geometry,
        elevations=None,
        candidates=candidates,
        settings=_settings(),
        model="deepseek-flash",
    )

    kinds = {point.kind for point in selection.points}
    assert {"start", "end"} <= kinds
    assert any(point.kind == "scenic" for point in selection.points)
    assert selection.summary == "Passt."
    assert all(
        point.km <= following.km
        for point, following in zip(
            selection.points, selection.points[1:], strict=False
        )
    )


def test_sanitize_draft_orders_highlights_and_shaping_points() -> None:
    geometry = _geometry()
    route_map = _route_map(geometry)
    start = geometry[0]
    end = geometry[-1]
    candidates_by_id = {
        "node/1": {"osm_id": "node/1", "name": "Altstadt"},
        "node/2": {"osm_id": "node/2", "name": "See"},
    }
    image_x, image_y = _image_xy(route_map, *geometry[20])

    draft = sanitize_draft(
        {
            "waypoints": [
                {"kind": "highlight", "osm_id": "node/2", "reason": "See!"},
                {
                    "kind": "shaping",
                    "image_x": image_x,
                    "image_y": image_y,
                    "reason": "Am Fluss",
                },
                {"kind": "highlight", "osm_id": "node/1", "reason": "Altstadt"},
            ],
            "summary": "Schöne Runde.",
        },
        route_map=route_map,
        existing=[("node/1", "Bisher")],
        candidates_by_id=candidates_by_id,
        start=start,
        end=end,
        round_trip=False,
        radius_km=None,
        corridor_width_m=1500,
        max_highlights=6,
        max_shaping=12,
        model="deepseek-flash",
    )

    assert [item.osm_id for item in draft.highlights] == ["node/2", "node/1"]
    assert len(draft.shaping) == 1
    assert draft.shaping[0].after == "node/2"
    assert draft.shaping[0].reason == "Am Fluss"


def test_draft_drops_points_outside_corridor() -> None:
    route_map = RouteMap(
        image=b"",
        width=100,
        height=100,
        bounds=_map_bounds([(50.0, 5.0), (50.0, 5.6), (51.5, 5.3)], 100, 100),
    )
    # Far north of the start-end line.
    image_x, image_y = _image_xy(route_map, 51.5, 5.3)

    draft = sanitize_draft(
        {
            "waypoints": [
                {
                    "kind": "shaping",
                    "image_x": image_x,
                    "image_y": image_y,
                    "reason": "Zu weit weg",
                }
            ]
        },
        route_map=route_map,
        existing=[],
        candidates_by_id={},
        start=(50.0, 5.0),
        end=(50.0, 5.6),
        round_trip=False,
        radius_km=None,
        corridor_width_m=800,
        max_highlights=6,
        max_shaping=12,
        model="deepseek-flash",
    )

    assert draft.shaping == []
