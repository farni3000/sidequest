"""BRouter adapter (self-hosted route server, dedicated MTB/trekking profiles)."""

from __future__ import annotations

import httpx

from .base import (
    BikeType,
    MtbDifficulty,
    RouteRequest,
    RouteResult,
    RoutingError,
)

_PROFILE_MAP: dict[BikeType, str] = {
    BikeType.road: "fastbike",
    BikeType.gravel: "gravel",
    BikeType.trekking: "trekking",
    BikeType.mtb: "mtb",
}

# BRouter has a single `mtb` profile; difficulty is expressed through routing
# preferences rather than separate profiles, so we keep the mapping explicit
# here to make future profile tuning easy.
_MTB_PROFILES: dict[MtbDifficulty, str] = {
    MtbDifficulty.xc: "mtb",
    MtbDifficulty.all_mountain: "mtb",
    MtbDifficulty.enduro: "mtb",
}


def select_profile(bike_type: BikeType, difficulty: MtbDifficulty | None) -> str:
    if bike_type is BikeType.mtb:
        return _MTB_PROFILES.get(difficulty or MtbDifficulty.xc, "mtb")
    return _PROFILE_MAP.get(bike_type, "trekking")


class BRouterProvider:
    name = "brouter"

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def route(self, req: RouteRequest) -> RouteResult:
        if len(req.waypoints) < 2:
            raise RoutingError("BRouter needs at least two waypoints")

        profile = select_profile(req.bike_type, req.mtb_difficulty)
        lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in req.waypoints)
        params = {
            "lonlats": lonlats,
            "profile": profile,
            "format": "geojson",
            "geometry": "true",
            "altitude": "true",
            "track": "false",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/brouter", params=params)
        except httpx.HTTPError as exc:
            raise RoutingError(f"BRouter request failed: {exc}") from exc

        if response.status_code != 200:
            raise RoutingError(
                f"BRouter returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingError("BRouter returned non-JSON response") from exc

        if "features" not in payload or not payload["features"]:
            message = payload.get("message") or payload.get("error") or payload
            raise RoutingError(f"BRouter could not compute a route: {message}")

        feature = payload["features"][0]
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2:
            raise RoutingError("BRouter returned an empty geometry")

        geometry = [(float(lat), float(lon)) for lon, lat, *_ in coordinates]
        elevations = [float(c[2]) if len(c) > 2 and c[2] is not None else 0.0 for c in coordinates]
        props = feature.get("properties", {})

        distance_km = props.get("track-length")
        distance_km = float(distance_km) / 1000.0 if distance_km else None

        gain = (
            props.get("filtered ascend")
            or props.get("filtered-ascend")
            or props.get("plain-ascend")
        )
        loss = (
            props.get("filtered descend")
            or props.get("filtered-descend")
            or props.get("plain-descend")
        )

        from ..geo import elevation_gain_loss, path_length_km

        computed_gain, computed_loss = elevation_gain_loss(elevations)
        if distance_km is None:
            distance_km = path_length_km(geometry)
        if gain is None:
            gain = computed_gain
        if loss is None:
            loss = computed_loss

        return RouteResult(
            geometry=geometry,
            distance_km=round(distance_km, 2),
            elevation_gain_m=round(float(gain), 1),
            elevation_loss_m=round(float(loss), 1),
            provider=self.name,
            profile=profile,
            elevations=elevations,
        )
