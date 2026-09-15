"""OpenRouteService adapter (cloud, free tier; road/gravel primary)."""

from __future__ import annotations

import httpx

from ..geo import elevation_gain_loss, path_length_km
from .base import BikeType, RouteRequest, RouteResult, RoutingError

_PROFILE_MAP: dict[BikeType, str] = {
    BikeType.road: "cycling-road",
    BikeType.gravel: "cycling-regular",
    BikeType.trekking: "cycling-regular",
    BikeType.mtb: "cycling-mountain",
}


def select_profile(bike_type: BikeType) -> str:
    return _PROFILE_MAP.get(bike_type, "cycling-regular")


class OpenRouteServiceProvider:
    name = "openrouteservice"

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def route(self, req: RouteRequest) -> RouteResult:
        if len(req.waypoints) < 2:
            raise RoutingError("OpenRouteService needs at least two waypoints")

        profile = select_profile(req.bike_type)
        url = f"{self.base_url}/v2/directions/{profile}/geojson"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/geo+json",
        }
        body = {
            "coordinates": [[lon, lat] for lat, lon in req.waypoints],
            "elevation": True,
            "instructions": False,
            "units": "m",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise RoutingError(f"OpenRouteService request failed: {exc}") from exc

        if response.status_code != 200:
            raise RoutingError(
                f"OpenRouteService returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingError("OpenRouteService returned non-JSON response") from exc

        features = payload.get("features") or []
        if not features:
            raise RoutingError(f"OpenRouteService could not compute a route: {payload}")

        feature = features[0]
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2:
            raise RoutingError("OpenRouteService returned an empty geometry")

        geometry = [(float(lat), float(lon)) for lon, lat, *_ in coordinates]
        elevations = [float(c[2]) if len(c) > 2 and c[2] is not None else 0.0 for c in coordinates]
        props = feature.get("properties", {})
        summary = props.get("summary", {}) if isinstance(props, dict) else {}

        distance_m = summary.get("distance") or props.get("distance")
        distance_km = float(distance_m) / 1000.0 if distance_m else path_length_km(geometry)

        computed_gain, computed_loss = elevation_gain_loss(elevations)
        gain = props.get("ascent") or summary.get("ascent") or computed_gain
        loss = props.get("descent") or summary.get("descent") or computed_loss

        return RouteResult(
            geometry=geometry,
            distance_km=round(distance_km, 2),
            elevation_gain_m=round(float(gain), 1),
            elevation_loss_m=round(float(loss), 1),
            provider=self.name,
            profile=profile,
            elevations=elevations,
        )
