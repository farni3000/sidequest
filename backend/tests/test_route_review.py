from app.ai.route_review import (
    describe_revision,
    sanitize_review,
    sanitize_revision,
)
from app.config import Settings
from app.map_image import RouteMap, mercator_to_latlon

BOUNDS = (600_000.0, 6_500_000.0, 640_000.0, 6_530_000.0)
CENTER = mercator_to_latlon(
    (BOUNDS[0] + BOUNDS[2]) / 2, (BOUNDS[1] + BOUNDS[3]) / 2
)


def _map() -> RouteMap:
    return RouteMap(image=b"", width=100, height=100, bounds=BOUNDS)


def test_sanitize_review_clamps_and_accepts_ok() -> None:
    review = sanitize_review(
        {
            "score": 120,
            "uncertainty": -3,
            "verdict": "ok",
            "summary": "Passt.",
            "issues": [
                {
                    "severity": "medium",
                    "category": "highlights",
                    "description": "Ein Highlight liegt etwas abseits.",
                }
            ],
        },
        "deepseek-flash",
    )
    assert review.score == 100
    assert review.uncertainty == 0.0
    assert review.accepted is True


def test_sanitize_review_blocks_on_high_issue() -> None:
    review = sanitize_review(
        {
            "score": 52,
            "uncertainty": 2,
            "verdict": "ok",
            "issues": [
                {
                    "severity": "high",
                    "category": "shape",
                    "description": "Die Route macht einen großen Umweg.",
                }
            ],
        },
        "deepseek-flash",
    )
    assert review.uncertainty == 1.0
    assert review.accepted is False


def test_sanitize_revision_filters_and_maps_free_waypoints() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=None,
        route_review_max_free_waypoints=2,
        route_review_max_free_waypoint_offset_km=1.0,
    )
    revision = sanitize_revision(
        {
            "can_fix": True,
            "reason": "Kürzer führen.",
            "remove_highlights": ["node/1", "node/unknown"],
            "add_highlights": [
                {"osm_id": "node/2", "rationale": "See!"},
                {"osm_id": "node/unknown", "rationale": "x"},
            ],
            "highlight_order": ["node/2", "node/unknown"],
            "loop_radius_factor": 5.0,
            "loop_rotation_deg": 400,
            "free_waypoints": [
                {"after": "start", "image_x": 0.5, "image_y": 0.5},
                {"after": "node/unknown", "image_x": 0.02, "image_y": 0.02},
                {"after": "end", "image_x": 0.5, "image_y": 0.48},
            ],
        },
        route_map=_map(),
        current_ids=["node/1"],
        candidate_ids={"node/1", "node/2"},
        current_path=[CENTER],
        settings=settings,
        model="deepseek-flash",
    )

    assert revision.remove_highlights == ["node/1"]
    assert [item.osm_id for item in revision.add_highlights] == ["node/2"]
    assert revision.highlight_order == ["node/2"]
    assert revision.loop_radius_factor == 1.8
    assert revision.loop_rotation_deg == 180.0
    # The corner waypoint is too far away; the other two are kept, max 2.
    assert len(revision.free_waypoints) == 2
    assert revision.free_waypoints[0].after == "start"
    assert revision.free_waypoints[1].after == "end"
    assert abs(revision.free_waypoints[0].lat - CENTER[0]) < 0.01
    assert abs(revision.free_waypoints[0].lon - CENTER[1]) < 0.01


def test_revision_without_changes_is_not_fixable() -> None:
    settings = Settings(_env_file=None, deepseek_api_key=None)
    revision = sanitize_revision(
        {"can_fix": True, "remove_highlights": ["unknown"]},
        route_map=_map(),
        current_ids=["node/1"],
        candidate_ids={"node/1"},
        current_path=[CENTER],
        settings=settings,
        model="deepseek-flash",
    )
    assert revision.can_fix is False
    assert revision.has_actions is False


def test_describe_revision_is_human_readable() -> None:
    from app.ai.route_review import RouteRevision

    revision = RouteRevision(
        can_fix=True,
        remove_highlights=["node/1"],
        free_waypoints=[],
    )
    text = describe_revision(revision, ["Aussichtspunkt"], [])
    assert "Aussichtspunkt entfernt" in text
