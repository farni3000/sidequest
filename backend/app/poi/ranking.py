"""deepseek-based highlight selection and ranking (with deterministic fallback)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ..ai.deepseek import complete_json
from ..config import Settings
from ..routing.base import BikeType, MtbDifficulty
from .overpass import POI

_OUTPUT_SCHEMA = {
    "highlights": [
        {
            "osm_id": "the POI id exactly as provided",
            "order": 1,
            "rationale": "one short German sentence why this stop is worth it",
        }
    ],
    "summary": "optional one-sentence German summary of the route character",
}


class RankingContext(BaseModel):
    bike_type: BikeType
    mtb_difficulty: MtbDifficulty | None = None
    freetext: str | None = None
    heat: int = 50
    requested_distance_km: float | None = None
    max_highlights: int = 6
    budget_km: float = 15.0


class RankedItem(BaseModel):
    osm_id: str
    order: int
    rationale: str


class RankingResult(BaseModel):
    items: list[RankedItem]
    summary: str | None = None
    source: str = "fallback"


def _fallback(pois: list[POI], context: RankingContext) -> RankingResult:
    selected: list[RankedItem] = []
    spent = 0.0
    for poi in sorted(pois, key=lambda p: p.detour_km):
        if len(selected) >= context.max_highlights:
            break
        if selected and spent + poi.detour_km > context.budget_km:
            continue
        spent += poi.detour_km
        selected.append(
            RankedItem(
                osm_id=poi.osm_id,
                order=len(selected) + 1,
                rationale=(
                    f"Nächstgelegener {poi.category}-POI, ca. {poi.detour_km:.1f} km Umweg."
                ),
            )
        )
    return RankingResult(items=selected, summary=None, source="fallback")


def _build_prompt(pois: list[POI], context: RankingContext) -> str:
    payload = [
        {
            "osm_id": poi.osm_id,
            "name": poi.name,
            "category": poi.category,
            "detour_km": poi.detour_km,
            "distance_to_route_km": poi.distance_to_route_km,
        }
        for poi in pois
    ]
    prefs = {
        "bike_type": context.bike_type.value,
        "mtb_difficulty": context.mtb_difficulty.value if context.mtb_difficulty else None,
        "freitext_wuensche": context.freetext,
        "heat": context.heat,
        "gewuenschte_distanz_km": context.requested_distance_km,
        "max_highlights": context.max_highlights,
        "umweg_budget_km": round(context.budget_km, 1),
    }
    return (
        "Wähle für die folgende Radroute die lohnendsten Highlights aus und bringe sie "
        "in eine sinnvolle Besuchsreihenfolge. Beachte das Umweg-Budget und den Freitext. "
        "Wähle nur POIs aus der Liste und erfinde keine IDs.\n\n"
        "Antworte ausschließlich mit JSON in genau dieser Form:\n"
        f"{json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        f"Rahmenbedingungen:\n{json.dumps(prefs, ensure_ascii=False, indent=2)}\n\n"
        f"Gefundene POIs:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


async def rank_pois(
    pois: list[POI], context: RankingContext, settings: Settings
) -> RankingResult:
    """Ask deepseek to pick highlights; fall back to nearest-by-detour on any error."""
    if not pois:
        return RankingResult(items=[], summary=None, source="fallback")

    if not settings.has_deepseek:
        return _fallback(pois, context)

    try:
        completion = await complete_json(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "Du bist ein erfahrener Radtouren-Planer. Wähle nur Highlights, "
                        "die den Wünschen entsprechen und das Umweg-Budget einhalten. "
                        "Antworte ausschließlich mit JSON."
                    ),
                },
                {"role": "user", "content": _build_prompt(pois, context)},
            ],
            require_image=False,
            temperature=0.2,
            timeout_s=settings.deepseek_timeout_s,
        )
        return _parse_arguments(completion.data, pois, context)
    except Exception:  # noqa: BLE001 - AI must never break planning
        return _fallback(pois, context)


def _parse_arguments(
    arguments: dict[str, Any], pois: list[POI], context: RankingContext
) -> RankingResult:
    valid_ids = {poi.osm_id for poi in pois}
    items: list[RankedItem] = []
    for raw in arguments.get("highlights", []):
        osm_id = str(raw.get("osm_id", ""))
        if osm_id not in valid_ids:
            continue
        try:
            order = int(raw.get("order", len(items) + 1))
        except (TypeError, ValueError):
            order = len(items) + 1
        items.append(
            RankedItem(
                osm_id=osm_id,
                order=order,
                rationale=str(raw.get("rationale", "")).strip() or "Empfohlenes Highlight.",
            )
        )

    items.sort(key=lambda item: item.order)
    items = items[: context.max_highlights]
    for index, item in enumerate(items, start=1):
        item.order = index

    if not items:
        return _fallback(pois, context)

    summary = arguments.get("summary")
    return RankingResult(
        items=items,
        summary=str(summary).strip() if summary else None,
        source="deepseek",
    )
