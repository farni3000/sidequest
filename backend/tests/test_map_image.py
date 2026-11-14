import io

from PIL import Image

import app.map_image as map_image
from app.config import Settings
from app.map_image import MapMarker, mercator_to_latlon, render_route_map


def _geometry() -> list[tuple[float, float]]:
    return [
        (50.4925, 5.8665),
        (50.55, 5.95),
        (50.62, 5.85),
        (50.52, 5.82),
        (50.4925, 5.8665),
    ]


async def test_render_route_map_draws_route_and_markers(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key=None,
        route_review_image_width=800,
        route_review_image_height=600,
    )

    async def fake_map(bounds, width, height, *, timeout_s=45.0):
        image = Image.new("RGB", (width, height), "#dce8d8")
        out = io.BytesIO()
        image.save(out, format="JPEG")
        return out.getvalue()

    monkeypatch.setattr(map_image, "_fetch_base_map", fake_map)

    route_map = await render_route_map(
        _geometry(),
        [
            MapMarker(
                name="Aussichtspunkt", category="viewpoint", lat=50.55, lon=5.95, order=1
            )
        ],
        settings,
        distance_km=42.3,
        requested_distance_km=40,
        elevation_gain_m=640,
        bike_label="Rennrad",
        round_trip=True,
        start_label="Spa",
        end_label="Rundtour ab Spa",
    )

    assert route_map.width == 800
    assert route_map.height == 600
    image = Image.open(io.BytesIO(route_map.image))
    assert image.size == (800, 600)
    assert route_map.data_url().startswith("data:image/jpeg;base64,")


def test_normalized_coordinates_roundtrip() -> None:
    bounds = (0.0, 0.0, 1000.0, 1000.0)
    from app.map_image import RouteMap

    route_map = RouteMap(image=b"", width=100, height=100, bounds=bounds)
    lat, lon = route_map.normalized_to_latlon(0.5, 0.5)
    expected = mercator_to_latlon(500.0, 500.0)
    assert abs(lat - expected[0]) < 1e-9
    assert abs(lon - expected[1]) < 1e-9


def test_map_bounds_keeps_requested_aspect_ratio() -> None:
    bounds = map_image._map_bounds(_geometry(), 1200, 900)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    assert abs(width / height - 1200 / 900) < 0.01
