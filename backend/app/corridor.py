"""Corridor geometry derived from the heat slider."""

from __future__ import annotations

import math

from .config import Settings
from .geo import interpolate


def corridor_width_m(heat: int, settings: Settings) -> int:
    ratio = max(0.0, min(100.0, float(heat))) / 100.0
    width = settings.corridor_min_m + ratio * (settings.corridor_max_m - settings.corridor_min_m)
    return int(round(width))


def corridor_points(
    start: tuple[float, float], end: tuple[float, float], samples: int = 3
) -> list[tuple[float, float]]:
    """A sparse polyline used both for Overpass `around` and detour estimates."""
    if samples <= 1:
        return [start, end]
    return [start, *interpolate(start, end, samples - 1)]


def roundtrip_radius_km(distance_km: float | None, heat: int) -> float:
    """Radius of the loop; grows with requested distance and heat."""
    base = (distance_km or 20.0) / (2 * math.pi)
    heat_factor = 0.75 + 0.5 * (max(0, min(100, heat)) / 100.0)
    return max(3.0, base * heat_factor)


def loop_waypoints(
    center: tuple[float, float], radius_km: float, count: int = 3
) -> list[tuple[float, float]]:
    """Evenly spaced points around ``center`` to force a loop without highlights."""
    from .geo import offset_point

    count = max(2, count)
    return [
        offset_point(center, bearing, radius_km)
        for bearing in (360.0 * i / count for i in range(count))
    ]
