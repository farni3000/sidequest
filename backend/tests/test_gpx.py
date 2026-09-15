import gpxpy

from app.gpx_export import build_gpx, gpx_filename


def test_gpx_contains_track_and_waypoints() -> None:
    geometry = [(50.49, 5.86), (50.45, 5.90), (50.39, 5.93)]
    elevations = [250.0, 275.0, 300.0]
    highlights = [{"name": "Aussichtspunkt", "lat": 50.45, "lon": 5.90, "description": "Toller Blick"}]

    xml = build_gpx("Testroute", geometry, elevations, highlights)
    parsed = gpxpy.parse(xml)

    assert parsed.name == "Testroute"
    assert len(parsed.tracks) == 1
    points = parsed.tracks[0].segments[0].points
    assert len(points) == 3
    assert points[0].elevation == 250.0

    assert len(parsed.waypoints) == 1
    assert parsed.waypoints[0].name == "Aussichtspunkt"


def test_gpx_without_highlights_is_valid() -> None:
    xml = build_gpx("Nur Route", [(50.0, 5.0), (50.1, 5.1)])
    parsed = gpxpy.parse(xml)
    assert parsed.waypoints == []


def test_gpx_filename_slug() -> None:
    assert gpx_filename("Spa -> Stavelot!").endswith(".gpx")
    assert " " not in gpx_filename("Spa Stavelot")
