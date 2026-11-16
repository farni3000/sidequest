"""Vision-assisted route shaping: draft the route and pick sparse GPX points.

The review itself lives in ``route_review.py`` and is intentionally unchanged.
This module adds two model calls around it:

* ``draft_route`` runs before routing and lets the vision model place shaping
  points on scenic roads based on an overview map.
* ``select_gpx_points`` runs after routing and reduces the dense track to a
  handful of important points. Komoot can then snap the route to known ways.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..geo import distance_point_to_segment_km, haversine_km
from ..map_image import RouteMap
from .deepseek import complete_json

_FORCED_KINDS = ("start", "end", "highlight", "draft")
_PRIORITY = {
    "start": 5,
    "end": 5,
    "highlight": 4,
    "draft": 4,
    "turn": 3,
    "point": 2,
    "scenic": 1,
}
_VALID_KINDS = ("start", "end", "highlight", "draft", "scenic", "turn", "point")


class DraftContext(BaseModel):
    bike_label: str
    mtb_label: str | None = None
    start_label: str
    end_label: str
    round_trip: bool = False
    requested_distance_km: float | None = None
    heat: int = 50
    freetext: str | None = None
    summary: str | None = None


class DraftHighlight(BaseModel):
    osm_id: str
    reason: str = ""


class DraftShaping(BaseModel):
    after: str = "start"
    lat: float
    lon: float
    reason: str = ""


class RouteDraft(BaseModel):
    highlights: list[DraftHighlight] = Field(default_factory=list)
    shaping: list[DraftShaping] = Field(default_factory=list)
    summary: str = ""
    model: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(self.highlights or self.shaping)


@dataclass(frozen=True)
class ShapingCandidate:
    order: int
    index: int
    lat: float
    lon: float
    elevation: float
    km: float
    kind: str = "point"
    label: str = ""
    osm_id: str = ""
    reason: str = ""

    @property
    def forced(self) -> bool:
        return self.kind in _FORCED_KINDS


class SelectedShapingPoint(BaseModel):
    order: int = 0
    index: int
    lat: float
    lon: float
    elevation: float = 0.0
    km: float = 0.0
    kind: str = "point"
    label: str = ""
    reason: str = ""
    osm_id: str = ""

    @property
    def forced(self) -> bool:
        return self.kind in _FORCED_KINDS


class ShapingSelection(BaseModel):
    points: list[SelectedShapingPoint] = Field(default_factory=list)
    summary: str = ""
    model: str = ""


_DRAFT_SYSTEM = """Du bist ein erfahrener Radtouren-Planer und entwirfst eine Route
anhand einer topografischen Karte. Du bekommst Start, Ziel bzw. Rundtour-Radius,
den gewünschten Korridor, die bereits gesetzten Highlights und weitere POIs.
Die Karte ist nordorientiert, enthält Ortsnamen, Straßen und Höhenlinien.

Deine Aufgabe: Lege eine geordnete Folge von Wegpunkten fest, die den Charakter
der Tour bestimmt. Nutze dafür vorhandene Highlights (osm_id) und setze freie
Zwischenpunkte auf schöne, fahrbare Abschnitte (Fluss, Wald, Kamm, Aussicht,
ruhige Nebenstraßen). Überlasse das Feinrouting zwischen den Punkten dem
Routenplaner.

Antworte ausschließlich mit JSON in genau dieser Form:
{
  "waypoints": [
    {"kind": "highlight", "osm_id": "osm_id", "reason": "Kurze Begründung."},
    {"kind": "shaping", "image_x": 0.42, "image_y": 0.61, "reason": "Schöne Straße am Fluss."}
  ],
  "summary": "Ein kurzer Satz zum Routencharakter."
}

Regeln:
- waypoints in Fahrtrichtung sortieren.
- Die gesetzten Highlights dürfen umsortiert, aber nicht weggelassen werden.
- Höchstens {max_highlights} Highlights insgesamt; ergänze nur POIs aus der
  Kandidatenliste und erfinde keine IDs.
- Höchstens {max_shaping} freie Zwischenpunkte; keine Mikro-Zwischenpunkte.
- image_x und image_y liegen zwischen 0 und 1, Ursprung oben links.
- Freie Punkte müssen im Korridor bzw. innerhalb der Rundtour liegen und
  dürfen keinen großen Umweg erzeugen.
- Keine Markdown-Codezäune, nur das JSON-Objekt."""

_SHAPING_SYSTEM = """Du wählst die Wegpunkte für den GPX-Export einer fertigen
Radroute. Beim Import in Komoot wird die Route an bekannte Wege angeglichen;
Komoot plant die Straßen zwischen den gelieferten Punkten. Die App zeigt
weiterhin die vollständige Route.

Wähle deshalb nur die wichtigen Punkte, die den Charakter der Tour tragen:
Start/Ziel, Highlights, markante Kurven und schöne Abschnitte (Fluss, Wald,
Kamm, Aussicht). Auf geraden, uninteressanten Abschnitten dürfen größere Lücken
entstehen; an schönen oder komplizierten Stellen dürfen die Punkte dichter
liegen. Ziel sind Abstände von etwa 3–5 km.

Antworte ausschließlich mit JSON in genau dieser Form:
{
  "keep": ["3", "7", "12"],
  "add": [
    {"image_x": 0.44, "image_y": 0.58, "reason": "Kurvige Straße am Waldrand."}
  ],
  "summary": "Ein kurzer Satz zu den gewählten Punkten."
}

Regeln:
- "keep" enthält nur Nummern aus der Kandidatenliste.
- Start, Ziel und Highlights werden automatisch behalten; du musst sie nicht
  extra nennen.
- "add": höchstens {max_add} zusätzliche Punkte direkt auf der Route.
- image_x und image_y liegen zwischen 0 und 1, Ursprung oben links.
- Keine Punkte abseits der eingezeichneten Route erfinden.
- Keine Markdown-Codezäune, nur das JSON-Objekt."""


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cumulative_km(geometry: list[tuple[float, float]]) -> list[float]:
    cumulative = [0.0]
    for start, end in zip(geometry, geometry[1:], strict=False):
        cumulative.append(cumulative[-1] + haversine_km(start, end))
    return cumulative


def _nearest_index(
    geometry: list[tuple[float, float]], lat: float, lon: float
) -> int:
    best_index = 0
    best_distance = math.inf
    for index, point in enumerate(geometry):
        distance = haversine_km((lat, lon), point)
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


def _turn_angle(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    first = _bearing(a, b)
    second = _bearing(b, c)
    diff = abs(second - first) % 360.0
    return min(diff, 360.0 - diff)


def build_shaping_candidates(
    geometry: list[tuple[float, float]],
    elevations: list[float] | None,
    highlights: list[dict[str, Any]] | None,
    *,
    anchors: list[dict[str, Any]] | None = None,
    spacing_km: float = 4.0,
    max_points: int = 30,
    min_spacing_km: float = 2.0,
) -> list[ShapingCandidate]:
    """Create deterministic candidate points along a routed geometry."""
    if len(geometry) < 2:
        return []

    elevation_values = list(elevations or [])
    cumulative = _cumulative_km(geometry)
    total_km = cumulative[-1]

    def elevation_at(index: int) -> float:
        if 0 <= index < len(elevation_values):
            return float(elevation_values[index] or 0.0)
        return 0.0

    chosen: dict[int, dict[str, Any]] = {}

    def add(
        index: int,
        kind: str,
        label: str = "",
        osm_id: str = "",
        reason: str = "",
    ) -> None:
        index = max(0, min(len(geometry) - 1, int(index)))
        existing = chosen.get(index)
        if existing is not None and _PRIORITY.get(existing["kind"], 0) >= _PRIORITY.get(
            kind, 0
        ):
            return
        lat, lon = geometry[index]
        chosen[index] = {
            "index": index,
            "lat": lat,
            "lon": lon,
            "elevation": elevation_at(index),
            "km": cumulative[index],
            "kind": kind if kind in _VALID_KINDS else "point",
            "label": label,
            "osm_id": osm_id,
            "reason": reason,
        }

    add(0, "start", "Start")
    add(len(geometry) - 1, "end", "Ziel")

    for highlight in highlights or []:
        try:
            lat = float(highlight["lat"])
            lon = float(highlight["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        index = _nearest_index(geometry, lat, lon)
        add(
            index,
            "highlight",
            str(highlight.get("name") or "Highlight"),
            str(highlight.get("osm_id") or ""),
            str(highlight.get("rationale") or ""),
        )

    # Points the vision model used to shape the route must stay in the GPX.
    for anchor in anchors or []:
        try:
            lat = float(anchor["lat"])
            lon = float(anchor["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        index = _nearest_index(geometry, lat, lon)
        add(
            index,
            "draft",
            "Schöner Abschnitt",
            "",
            str(anchor.get("reason") or "Vom Bildmodell gewählter Abschnitt."),
        )

    step = max(0.5, spacing_km)
    km = step
    while km < total_km - 0.25:
        index = bisect.bisect_left(cumulative, km)
        if index < len(geometry):
            add(index, "point", f"km {km:.0f}")
        km += step

    # A few pronounced turns make sure komoot keeps the intended road choice.
    n = len(geometry)
    window = max(1, n // 120)
    stride = max(1, n // 260)
    turns: list[tuple[float, int]] = []
    for index in range(window, n - window, stride):
        angle = _turn_angle(
            geometry[index - window], geometry[index], geometry[index + window]
        )
        if angle >= 55.0:
            turns.append((angle, index))
    turns.sort(reverse=True)

    added_turns: list[int] = []
    for _angle, index in turns:
        if len(added_turns) >= 8:
            break
        if any(
            abs(cumulative[index] - cumulative[other]) < min_spacing_km
            for other in chosen
        ):
            continue
        add(index, "turn", "Kurve")
        added_turns.append(index)

    # Keep the total bounded without dropping start/end/highlights.
    forced = [item for item in chosen.values() if item["kind"] in _FORCED_KINDS]
    others = sorted(
        (item for item in chosen.values() if item["kind"] not in _FORCED_KINDS),
        key=lambda item: item["km"],
    )
    allowed = max(0, max_points - len(forced))
    if len(others) > allowed:
        if allowed == 0:
            others = []
        else:
            stride_float = len(others) / allowed
            sampled: list[dict[str, Any]] = []
            seen: set[int] = set()
            for slot in range(allowed):
                item = others[min(len(others) - 1, int(slot * stride_float))]
                if item["index"] not in seen:
                    sampled.append(item)
                    seen.add(item["index"])
            others = sampled

    final = sorted([*forced, *others], key=lambda item: item["km"])
    return [
        ShapingCandidate(order=index + 1, **item)
        for index, item in enumerate(final)
    ]


def _candidate_point(candidate: ShapingCandidate) -> SelectedShapingPoint:
    return SelectedShapingPoint(
        index=candidate.index,
        lat=candidate.lat,
        lon=candidate.lon,
        elevation=candidate.elevation,
        km=candidate.km,
        kind=candidate.kind,
        label=candidate.label,
        reason=candidate.reason,
        osm_id=candidate.osm_id,
    )


def fallback_shaping_points(
    candidates: list[ShapingCandidate],
) -> list[SelectedShapingPoint]:
    """Deterministic selection used when no vision model is available."""
    points = [_candidate_point(candidate) for candidate in candidates]
    for index, point in enumerate(points, start=1):
        point.order = index
    return points


def _assemble_points(
    points: list[SelectedShapingPoint],
    candidates: list[ShapingCandidate],
    geometry: list[tuple[float, float]],
    elevations: list[float] | None,
    *,
    min_spacing_km: float,
    max_gap_km: float,
    max_points: int,
) -> list[SelectedShapingPoint]:
    if not geometry:
        return []
    cumulative = _cumulative_km(geometry)
    elevation_values = list(elevations or [])

    def elevation_at(index: int) -> float:
        if 0 <= index < len(elevation_values):
            return float(elevation_values[index] or 0.0)
        return 0.0

    by_index: dict[int, SelectedShapingPoint] = {}
    for point in points:
        index = max(0, min(len(geometry) - 1, int(point.index)))
        point.index = index
        point.lat, point.lon = geometry[index]
        point.km = cumulative[index]
        point.elevation = elevation_at(index)
        existing = by_index.get(index)
        if existing is not None and _PRIORITY.get(existing.kind, 0) >= _PRIORITY.get(
            point.kind, 0
        ):
            continue
        by_index[index] = point

    ordered = sorted(by_index.values(), key=lambda item: item.km)

    # Enforce a minimum spacing, but never drop start/end/highlights.
    spaced: list[SelectedShapingPoint] = []
    for point in ordered:
        if not spaced:
            spaced.append(point)
            continue
        previous = spaced[-1]
        if point.km - previous.km >= min_spacing_km:
            spaced.append(point)
        elif point.forced and not previous.forced:
            spaced[-1] = point
        elif point.forced and previous.forced:
            spaced.append(point)

    # Fill gaps that are too large for komoot to infer the intended road.
    used = {point.index for point in spaced}
    filled: list[SelectedShapingPoint] = []
    for position, point in enumerate(spaced):
        filled.append(point)
        if position + 1 >= len(spaced):
            continue
        following = spaced[position + 1]
        current = point
        while (
            following.km - current.km > max_gap_km
            and len(filled) < max_points
        ):
            middle_km = current.km + (following.km - current.km) / 2
            choice: ShapingCandidate | None = None
            best_delta = math.inf
            for candidate in candidates:
                if candidate.index in used:
                    continue
                if not (current.km + 0.2 < candidate.km < following.km - 0.2):
                    continue
                delta = abs(candidate.km - middle_km)
                if delta < best_delta:
                    best_delta = delta
                    choice = candidate

            if choice is not None:
                new_point = _candidate_point(choice)
                used.add(choice.index)
            else:
                index = min(len(geometry) - 1, bisect.bisect_left(cumulative, middle_km))
                if index in used:
                    break
                used.add(index)
                lat, lon = geometry[index]
                new_point = SelectedShapingPoint(
                    index=index,
                    lat=lat,
                    lon=lon,
                    elevation=elevation_at(index),
                    km=cumulative[index],
                    kind="point",
                    label=f"km {cumulative[index]:.0f}",
                )
            filled.append(new_point)
            current = new_point

    for order, point in enumerate(filled, start=1):
        point.order = order
    return filled


def sanitize_shaping_selection(
    raw: dict[str, Any],
    *,
    route_map: RouteMap,
    geometry: list[tuple[float, float]],
    elevations: list[float] | None,
    candidates: list[ShapingCandidate],
    settings: Settings,
    model: str = "",
) -> ShapingSelection:
    by_order = {candidate.order: candidate for candidate in candidates}
    forced = [candidate for candidate in candidates if candidate.forced]

    keep: list[SelectedShapingPoint] = []
    raw_keep = raw.get("keep")
    if isinstance(raw_keep, list):
        for item in raw_keep:
            try:
                order = int(item)
            except (TypeError, ValueError):
                continue
            candidate = by_order.get(order)
            if candidate is not None:
                keep.append(_candidate_point(candidate))

    points = [_candidate_point(candidate) for candidate in forced] + keep

    raw_add = raw.get("add")
    if isinstance(raw_add, list):
        for item in raw_add[:3]:
            if not isinstance(item, dict):
                continue
            image_x = _as_float(item.get("image_x"), math.nan)
            image_y = _as_float(item.get("image_y"), math.nan)
            if not math.isfinite(image_x) or not math.isfinite(image_y):
                continue
            if image_x > 1.0 or image_y > 1.0:
                if 0.0 <= image_x <= 100.0 and 0.0 <= image_y <= 100.0:
                    image_x /= 100.0
                    image_y /= 100.0
            if not (0.01 <= image_x <= 0.99 and 0.01 <= image_y <= 0.99):
                continue
            lat, lon = route_map.normalized_to_latlon(image_x, image_y)
            index = _nearest_index(geometry, lat, lon)
            if haversine_km((lat, lon), geometry[index]) > settings.gpx_point_snap_tolerance_km:
                continue
            points.append(
                SelectedShapingPoint(
                    index=index,
                    lat=geometry[index][0],
                    lon=geometry[index][1],
                    kind="scenic",
                    label="Schöner Abschnitt",
                    reason=str(item.get("reason", "")).strip()[:300],
                )
            )

    assembled = _assemble_points(
        points,
        candidates,
        geometry,
        elevations,
        min_spacing_km=settings.gpx_min_point_spacing_km,
        max_gap_km=settings.gpx_max_gap_km,
        max_points=settings.gpx_max_points,
    )
    return ShapingSelection(
        points=assembled,
        summary=str(raw.get("summary", "")).strip()[:400],
        model=model,
    )


def _shaping_prompt(
    context: dict[str, Any],
    candidates: list[ShapingCandidate],
) -> str:
    payload = [
        {
            "nr": candidate.order,
            "km": round(candidate.km, 1),
            "lat": round(candidate.lat, 5),
            "lon": round(candidate.lon, 5),
            "kind": candidate.kind,
            "label": candidate.label,
        }
        for candidate in candidates
    ]
    return (
        "Wähle die GPX-Wegpunkte für die folgende Route.\n\n"
        "Bildlegende: Die limettenfarbene Linie ist die Route, S ist der Start, "
        "Z ist das Ziel, nummerierte Punkte sind die Kandidaten, größere "
        "farbige Punkte sind Highlights. Die Karte ist nordorientiert.\n\n"
        f"Anfrage und Routendaten:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"Kandidaten:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


async def select_gpx_points(
    route_map: RouteMap,
    context: dict[str, Any],
    candidates: list[ShapingCandidate],
    geometry: list[tuple[float, float]],
    elevations: list[float] | None,
    settings: Settings,
) -> ShapingSelection:
    completion = await complete_json(
        settings,
        [
            {
                "role": "system",
                "content": _SHAPING_SYSTEM.replace("{max_add}", "3"),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _shaping_prompt(context, candidates)},
                    {"type": "image_url", "image_url": {"url": route_map.data_url()}},
                ],
            },
        ],
        require_image=True,
        temperature=0.2,
        timeout_s=settings.route_review_timeout_s,
    )
    return sanitize_shaping_selection(
        completion.data,
        route_map=route_map,
        geometry=geometry,
        elevations=elevations,
        candidates=candidates,
        settings=settings,
        model=completion.model,
    )


def _corridor_ok(
    point: tuple[float, float],
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    round_trip: bool,
    radius_km: float | None,
    corridor_width_m: int,
) -> bool:
    if round_trip:
        distance = haversine_km(start, point)
        radius = radius_km or 0.0
        return radius * 0.25 <= distance <= radius * 1.35
    distance = distance_point_to_segment_km(point, start, end)
    return distance <= max(1.0, corridor_width_m / 1000.0 * 1.4)


def _progress_fraction(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Fraction of the start-end line, using a local equirectangular projection."""
    lat0 = math.radians(start[0])
    kx = 111.320 * math.cos(lat0)
    ky = 110.574
    bx = (end[1] - start[1]) * kx
    by = (end[0] - start[0]) * ky
    px = (point[1] - start[1]) * kx
    py = (point[0] - start[0]) * ky
    denominator = bx * bx + by * by
    if denominator <= 0:
        return 0.0
    return (px * bx + py * by) / denominator


def _draft_prompt(
    context: DraftContext,
    highlights: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    request = context.model_dump()
    return (
        "Entwirf die Route anhand der Karte.\n\n"
        "Bildlegende: Die gestrichelte Linie zeigt den gewünschten Korridor bzw. "
        "die Rundtour. S ist der Start, Z das Ziel, nummerierte Punkte sind "
        "Highlights und POI-Kandidaten. Die Karte ist nordorientiert.\n\n"
        f"Anfrage:\n{json.dumps(request, ensure_ascii=False, indent=2)}\n\n"
        f"Bereits gesetzte Highlights:\n"
        f"{json.dumps(highlights, ensure_ascii=False, indent=2)}\n\n"
        f"Weitere POI-Kandidaten:\n"
        f"{json.dumps(candidates, ensure_ascii=False, indent=2)}"
    )


def sanitize_draft(
    raw: dict[str, Any],
    *,
    route_map: RouteMap,
    existing: list[tuple[str, str]],
    candidates_by_id: dict[str, dict[str, Any]],
    start: tuple[float, float],
    end: tuple[float, float],
    round_trip: bool,
    radius_km: float | None,
    corridor_width_m: int,
    max_highlights: int,
    max_shaping: int,
    model: str = "",
) -> RouteDraft:
    existing_ids = [osm_id for osm_id, _reason in existing]
    existing_reasons = {osm_id: reason for osm_id, reason in existing}
    ordered: list[DraftHighlight] = []
    seen: set[str] = set()
    shaping: list[DraftShaping] = []
    anchor = "start"
    last_progress = -math.inf

    def add_highlight(osm_id: str, reason: str) -> None:
        nonlocal anchor
        if osm_id in seen or osm_id not in candidates_by_id:
            return
        ordered.append(
            DraftHighlight(
                osm_id=osm_id,
                reason=reason or existing_reasons.get(osm_id, ""),
            )
        )
        seen.add(osm_id)
        anchor = osm_id

    def progress(point: tuple[float, float]) -> float:
        return _progress_fraction(point, start, end)

    raw_waypoints = raw.get("waypoints")
    if isinstance(raw_waypoints, list):
        for item in raw_waypoints[:40]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            if kind == "highlight":
                add_highlight(
                    str(item.get("osm_id", "")).strip(),
                    str(item.get("reason", "")).strip()[:300],
                )
                continue
            if kind != "shaping":
                continue
            image_x = _as_float(item.get("image_x"), math.nan)
            image_y = _as_float(item.get("image_y"), math.nan)
            if not math.isfinite(image_x) or not math.isfinite(image_y):
                continue
            if image_x > 1.0 or image_y > 1.0:
                if 0.0 <= image_x <= 100.0 and 0.0 <= image_y <= 100.0:
                    image_x /= 100.0
                    image_y /= 100.0
            if not (0.01 <= image_x <= 0.99 and 0.01 <= image_y <= 0.99):
                continue
            if len(shaping) >= max_shaping:
                continue
            lat, lon = route_map.normalized_to_latlon(image_x, image_y)
            if haversine_km((lat, lon), start) < 0.3:
                continue
            if haversine_km((lat, lon), end) < 0.3:
                continue
            if not _corridor_ok(
                (lat, lon),
                start=start,
                end=end,
                round_trip=round_trip,
                radius_km=radius_km,
                corridor_width_m=corridor_width_m,
            ):
                continue
            if shaping and haversine_km((lat, lon), (shaping[-1].lat, shaping[-1].lon)) < 1.5:
                continue
            if not round_trip:
                current = progress((lat, lon))
                if current < last_progress - 2.0:
                    continue
                last_progress = current
            shaping.append(
                DraftShaping(
                    after=anchor,
                    lat=lat,
                    lon=lon,
                    reason=str(item.get("reason", "")).strip()[:300],
                )
            )

    # Never silently drop a highlight the router already selected.
    for osm_id in existing_ids:
        if osm_id not in seen and osm_id in candidates_by_id:
            ordered.append(
                DraftHighlight(
                    osm_id=osm_id,
                    reason=existing_reasons.get(osm_id, ""),
                )
            )
            seen.add(osm_id)

    limit = max(max_highlights, len(existing_ids))
    ordered = ordered[:limit]

    valid_ids = {item.osm_id for item in ordered}
    for item in shaping:
        if item.after not in valid_ids and item.after not in ("start", "end"):
            item.after = "start"

    return RouteDraft(
        highlights=ordered,
        shaping=shaping,
        summary=str(raw.get("summary", "")).strip()[:400],
        model=model,
    )


async def draft_route(
    route_map: RouteMap,
    context: DraftContext,
    existing: list[tuple[str, str]],
    candidates: list[dict[str, Any]],
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    round_trip: bool,
    radius_km: float | None,
    corridor_width_m: int,
    settings: Settings,
) -> RouteDraft:
    candidates_by_id = {str(item["osm_id"]): item for item in candidates}
    highlights_payload = [
        {
            "osm_id": osm_id,
            "name": candidates_by_id.get(osm_id, {}).get("name", ""),
            "category": candidates_by_id.get(osm_id, {}).get("category", ""),
            "reason": reason,
        }
        for osm_id, reason in existing
    ]
    completion = await complete_json(
        settings,
        [
            {
                "role": "system",
                "content": _DRAFT_SYSTEM.replace(
                    "{max_highlights}", str(settings.max_highlights)
                ).replace(
                    "{max_shaping}", str(settings.vision_route_draft_max_waypoints)
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _draft_prompt(context, highlights_payload, candidates),
                    },
                    {"type": "image_url", "image_url": {"url": route_map.data_url()}},
                ],
            },
        ],
        require_image=True,
        temperature=0.25,
        timeout_s=settings.vision_route_draft_timeout_s,
    )
    return sanitize_draft(
        completion.data,
        route_map=route_map,
        existing=existing,
        candidates_by_id=candidates_by_id,
        start=start,
        end=end,
        round_trip=round_trip,
        radius_km=radius_km,
        corridor_width_m=corridor_width_m,
        max_highlights=settings.max_highlights,
        max_shaping=settings.vision_route_draft_max_waypoints,
        model=completion.model,
    )


__all__ = [
    "DraftContext",
    "DraftHighlight",
    "DraftShaping",
    "RouteDraft",
    "SelectedShapingPoint",
    "ShapingCandidate",
    "ShapingSelection",
    "build_shaping_candidates",
    "draft_route",
    "fallback_shaping_points",
    "sanitize_draft",
    "sanitize_shaping_selection",
    "select_gpx_points",
]
