"""SQLModel persistence tables.

Every user-facing table carries a nullable ``user_id`` from the start so that
multi-user support can be added later without a disruptive migration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RoutePlan(SQLModel, table=True):
    __tablename__ = "route_plan"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)

    bike_type: str
    mtb_difficulty: str | None = None

    start_label: str | None = None
    start_lat: float
    start_lon: float
    end_label: str | None = None
    end_lat: float
    end_lon: float

    round_trip: bool = False
    requested_distance_km: float | None = None
    heat: int = 50
    freetext: str | None = None

    provider_used: str | None = None
    distance_km: float = 0.0
    elevation_gain_m: float = 0.0
    elevation_loss_m: float = 0.0

    geometry_json: str = "[]"
    elevations_json: str = "[]"
    warnings_json: str = "[]"

    @property
    def geometry(self) -> list[list[float]]:
        return json.loads(self.geometry_json or "[]")

    @property
    def elevations(self) -> list[float]:
        return json.loads(self.elevations_json or "[]")

    @property
    def warnings(self) -> list[str]:
        return json.loads(self.warnings_json or "[]")

    def set_geometry(self, geometry: list[tuple[float, float]]) -> None:
        self.geometry_json = json.dumps(geometry)

    def set_elevations(self, elevations: list[float]) -> None:
        self.elevations_json = json.dumps(elevations)

    def set_warnings(self, warnings: list[str]) -> None:
        self.warnings_json = json.dumps(warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bike_type": self.bike_type,
            "mtb_difficulty": self.mtb_difficulty,
            "distance_km": self.distance_km,
            "provider_used": self.provider_used,
        }


class Highlight(SQLModel, table=True):
    __tablename__ = "highlight"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    plan_id: int = Field(foreign_key="route_plan.id", index=True)

    osm_id: str
    name: str
    category: str
    lat: float
    lon: float
    detour_km: float = 0.0
    rationale: str | None = None
    order_index: int = 0
