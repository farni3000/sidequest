"""Geodesic helpers used for corridors, detours and elevation handling."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points in kilometres."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def path_length_km(points: list[tuple[float, float]]) -> float:
    return sum(haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


def interpolate(a: tuple[float, float], b: tuple[float, float], n: int) -> list[tuple[float, float]]:
    """Return ``n`` points between ``a`` (exclusive) and ``b`` (inclusive)."""
    if n <= 0:
        return []
    return [
        (a[0] + (b[0] - a[0]) * (i + 1) / n, a[1] + (b[1] - a[1]) * (i + 1) / n)
        for i in range(n)
    ]


def distance_point_to_segment_km(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Approximate distance from ``p`` to segment ``a-b`` in km (local projection)."""
    lat0 = math.radians(p[0])
    kx = 111.320 * math.cos(lat0)
    ky = 110.574

    px, py = p[1] * kx, p[0] * ky
    ax, ay = a[1] * kx, a[0] * ky
    bx, by = b[1] * kx, b[0] * ky

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_point_to_path_km(
    p: tuple[float, float], path: list[tuple[float, float]]
) -> float:
    if not path:
        return float("inf")
    if len(path) == 1:
        return haversine_km(p, path[0])
    return min(
        distance_point_to_segment_km(p, path[i], path[i + 1]) for i in range(len(path) - 1)
    )


def elevation_gain_loss(elevations: list[float], threshold: float = 1.0) -> tuple[float, float]:
    """Sum positive/negative elevation deltas, ignoring noise below ``threshold``."""
    gain = loss = 0.0
    for prev, cur in zip(elevations, elevations[1:], strict=False):
        delta = cur - prev
        if delta > threshold:
            gain += delta
        elif delta < -threshold:
            loss += -delta
    return round(gain, 1), round(loss, 1)


def offset_point(
    point: tuple[float, float], bearing_deg: float, distance_km: float
) -> tuple[float, float]:
    """Point at ``distance_km`` and ``bearing_deg`` from ``point`` (spherical)."""
    lat1 = math.radians(point[0])
    lon1 = math.radians(point[1])
    bearing = math.radians(bearing_deg)
    angular = distance_km / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)
