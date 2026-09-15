from app.geo import (
    distance_point_to_segment_km,
    elevation_gain_loss,
    haversine_km,
    offset_point,
    path_length_km,
)


def test_haversine_known_distance() -> None:
    # Brussels -> Antwerp is roughly 41 km
    distance = haversine_km((50.8503, 4.3517), (51.2194, 4.4025))
    assert 38 < distance < 44


def test_path_length_sums_segments() -> None:
    points = [(50.0, 5.0), (50.1, 5.0), (50.2, 5.0)]
    assert path_length_km(points) > 0


def test_point_to_segment_zero_on_line() -> None:
    distance = distance_point_to_segment_km((50.05, 5.0), (50.0, 5.0), (50.1, 5.0))
    assert distance < 0.1


def test_elevation_gain_loss_ignores_noise() -> None:
    gain, loss = elevation_gain_loss([100, 105, 104, 120, 90], threshold=0.5)
    assert gain > 0
    assert loss > 0


def test_offset_point_changes_location() -> None:
    assert offset_point((50.0, 5.0), 90, 5.0) != (50.0, 5.0)
