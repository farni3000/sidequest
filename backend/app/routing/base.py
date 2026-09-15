"""Routing provider interface shared by all adapters."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class BikeType(StrEnum):
    road = "road"
    gravel = "gravel"
    mtb = "mtb"
    trekking = "trekking"


class MtbDifficulty(StrEnum):
    xc = "xc"
    all_mountain = "all_mountain"
    enduro = "enduro"


BIKE_TYPE_LABELS: dict[BikeType, str] = {
    BikeType.road: "Rennrad",
    BikeType.gravel: "Gravel",
    BikeType.mtb: "MTB",
    BikeType.trekking: "Trekking / Wandern",
}

MTB_DIFFICULTY_LABELS: dict[MtbDifficulty, str] = {
    MtbDifficulty.xc: "Cross Country (XC)",
    MtbDifficulty.all_mountain: "All Mountain",
    MtbDifficulty.enduro: "Enduro",
}


class RouteRequest(BaseModel):
    """A request to route through an ordered list of (lat, lon) waypoints."""

    waypoints: list[tuple[float, float]]
    bike_type: BikeType
    mtb_difficulty: MtbDifficulty | None = None


class RouteResult(BaseModel):
    geometry: list[tuple[float, float]]
    distance_km: float
    elevation_gain_m: float
    provider: str
    elevation_loss_m: float = 0.0
    profile: str | None = None
    elevations: list[float] = []


class RoutingError(RuntimeError):
    """Raised when a provider cannot compute a route."""


@runtime_checkable
class RoutingProvider(Protocol):
    name: str

    async def route(self, req: RouteRequest) -> RouteResult:  # pragma: no cover - protocol
        ...
