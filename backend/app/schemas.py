"""Request/response schemas for the HTTP layer."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from .routing.base import BikeType, MtbDifficulty


class PlanForm(BaseModel):
    bike_type: BikeType
    mtb_difficulty: MtbDifficulty | None = None
    start: str
    end: str | None = None
    round_trip: bool = False
    distance_km: float | None = None
    heat: int = 50
    freetext: str | None = None

    @field_validator("heat")
    @classmethod
    def _clamp_heat(cls, value: int) -> int:
        return max(0, min(100, value))

    @field_validator("mtb_difficulty", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("end", "freetext", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("distance_km")
    @classmethod
    def _positive_distance(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            return None
        return value
