"""GraphHopper adapter (optional third provider, Phase 2 / low priority)."""

from __future__ import annotations

import httpx

from ..geo import elevation_gain_loss, path_length_km
from .base import BikeType, RouteRequest, RouteResult, RoutingError

_PROFILE_MAP: dict[BikeType, str] = {
    BikeType.road: "racingbike",
    BikeType.gravel: "bike",
    BikeType.trekking: "bike",
    BikeType.mtb: "mtb",
}


def select_profile(bike_type: BikeType) -> str:
    return _PROFILE_MAP.get(bike_type, "bike")


class GraphHopperProvider:
    name = "graphhopper"

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def route(self, req: RouteRequest) -> RouteResult:
        if len(req.waypoints) < 2:
            raise RoutingError("GraphHopper needs at least two waypoints")

        profile = select_profile(req.bike_type)
        url = f"{self.base_url}/route"
        params = {
            "key": self.api_key,
            "profile": profile,
            "points_encoded": "false",
            "elevation": "true",
        }
        body = {"points": [[lon, lat] for lat, lon in req.waypoints]}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, params=params, json=body)
        except httpx.HTTPError as exc:
            raise RoutingError(f"GraphHopper request failed: {exc}") from exc

        if response.status_code != 200:
            raise RoutingError(
                f"GraphHopper returned HTTP {response.status_code}: {response.text[:200]}"
            )

        payload = response.json()
        paths = payload.get("paths") or []
        if not paths:
            raise RoutingError(f"GraphHopper could not compute a route: {payload}")

        path = paths[0]
        coordinates = path.get("points", {}).get("coordinates", [])
        if len(coordinates) < 2:
            raise RoutingError("GraphHopper returned an empty geometry")

        geometry = [(float(lat), float(lon)) for lon, lat, *_ in coordinates]
        elevations = [float(c[2]) if len(c) > 2 and c[2] is not None else 0.0 for c in coordinates]

        distance_km = float(path.get("distance", 0.0)) / 1000.0 or path_length_km(geometry)
        computed_gain, computed_loss = elevation_gain_loss(elevations)
        gain = path.get("ascend") or computed_gain
        loss = path.get("descend") or computed_loss

        return RouteResult(
            geometry=geometry,
            distance_km=round(distance_km, 2),
            elevation_gain_m=round(float(gain), 1),
            elevation_loss_m=round(float(loss), 1),
            provider=self.name,
            profile=profile,
            elevations=elevations,
        )
