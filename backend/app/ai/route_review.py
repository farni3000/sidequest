"""Vision-based route review and feedback-driven correction prompts."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..geo import distance_point_to_path_km
from ..map_image import RouteMap
from .deepseek import DeepSeekError, complete_json

_REVIEW_SYSTEM = """Du bist ein strenger, aber fairer Prüfer für Radtouren.
Du bekommst eine topografische Karte mit einer eingezeichneten Route, dazu die
ursprüngliche Anfrage und die berechneten Routendaten. Prüfe ausschließlich, was
auf der Karte und in den Daten tatsächlich erkennbar ist.

Bewerte:
- Stimmen Start, Ziel und Rundtour-Form mit der Anfrage überein?
- Ist die Streckenform plausibel (keine sinnlosen Schleifen, Ausreißer oder Umwege)?
- Passt die tatsächliche Distanz grob zur Wunschdistanz?
- Liegen die Highlights an der Route und passen sie zur Beschreibung?
- Sind Freitext-Wünsche, soweit aus der Karte erkennbar, berücksichtigt?

Erfinde nichts über Straßenbelag, Verkehr, Wetter oder Sperrungen. Wenn etwas auf
einer topografischen Karte nicht erkennbar ist, darf es kein Grund für eine
Ablehnung sein. Melde nur konkrete, actionable Abweichungen.

Antworte ausschließlich mit JSON in genau dieser Form:
{
  "score": 82,
  "uncertainty": 0.18,
  "verdict": "ok",
  "summary": "Ein bis zwei kurze Sätze auf Deutsch.",
  "issues": [
    {
      "severity": "high",
      "category": "distance",
      "description": "Konkrete Abweichung auf Deutsch.",
      "fix_hint": "Was konkret geändert werden sollte."
    }
  ]
}

Regeln:
- score: 0 bis 100. 100 = perfekt passend.
- uncertainty: 0 bis 1. Wie unsicher bist du bei dieser Beurteilung?
- verdict: "ok", wenn die Route insgesamt passt, sonst "revise".
- severity: "high", "medium" oder "low".
- category: eine von distance, shape, highlights, preferences, start_end, other.
- Maximal 4 issues, nach Wichtigkeit sortiert.
- Wenn alles passt: verdict "ok", issues leer.
- Keine Markdown-Codezäune, nur das JSON-Objekt."""

_REVISION_SYSTEM = """Du bist ein Radtouren-Planer und korrigierst eine bestehende Route
anhand eines Prüfberichts. Du bekommst die ursprüngliche Anfrage, die aktuelle
Route, das Routenbild, den Prüfbericht und die verfügbaren POI-Kandidaten.

Du darfst nur diese Änderungen vorschlagen:
- Highlights entfernen, hinzufügen oder umsortieren (nur IDs aus den Kandidaten).
- Bei Rundtouren den Radius und die Drehung der Schleife anpassen.
- Zusätzliche freie Zwischenwegpunkte setzen, indem du eine Stelle im BILD
  angibst: image_x und image_y sind Werte von 0 bis 1, Ursprung oben links.

Setze keine neuen Start- oder Endpunkte. Erfinde keine POI-IDs. Wenn der
Prüfbericht nicht mit den verfügbaren Mitteln behoben werden kann, setze
"can_fix": false und ändere nichts.

Antworte ausschließlich mit JSON in genau dieser Form:
{
  "can_fix": true,
  "reason": "Kurze Begründung auf Deutsch.",
  "remove_highlights": ["osm_id"],
  "add_highlights": [
    {"osm_id": "osm_id", "rationale": "Kurze Begründung auf Deutsch."}
  ],
  "highlight_order": ["osm_id"],
  "loop_radius_factor": 1.0,
  "loop_rotation_deg": 0,
  "free_waypoints": [
    {
      "after": "start",
      "image_x": 0.42,
      "image_y": 0.63,
      "reason": "Kurze Begründung auf Deutsch."
    }
  ]
}

Regeln:
- loop_radius_factor nur bei Rundtouren, zwischen 0.5 und 1.8.
- loop_rotation_deg zwischen -180 und 180.
- free_waypoints: maximal {max_free} Stück. Setze die visuellen Beobachtungen
  des Prüfberichts in konkrete Wegpunkte um, statt nur POIs zu tauschen.
  Mehrere Punkte pro Runde sind ausdrücklich erlaubt, wenn sie die Form oder
  die Straßenwahl wirklich verbessern.
- "after" ist "start", "end" oder die osm_id eines aktuell gewählten Highlights.
- Ändere die Distanz pro Korrektur nur moderat (etwa 20–25 % der aktuellen
  Distanz). Bei zu kurzer Route lieber einen kleinen Umweg, bei zu langer Route
  Umwege/Highlights entfernen. Keine großen Bögen nur zum Verlängern.
- Bevorzuge schöne, ruhige Straßen (Fluss, Wald, Kamm, Aussicht) statt
  offensichtlicher Hauptverkehrsachsen. Erfinde keine Straßen, die auf der
  Karte nicht erkennbar sind.
- Keine Markdown-Codezäune, nur das JSON-Objekt."""


class ReviewIssue(BaseModel):
    severity: str = "medium"
    category: str = "other"
    description: str = ""
    fix_hint: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == "high"


class RouteReview(BaseModel):
    score: int = 0
    uncertainty: float = 0.5
    verdict: str = "ok"
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)
    model: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict == "ok" and not any(issue.is_blocking for issue in self.issues)


class AddedHighlight(BaseModel):
    osm_id: str
    rationale: str = ""


class FreeWaypoint(BaseModel):
    after: str = "end"
    lat: float
    lon: float
    reason: str = ""


class RouteRevision(BaseModel):
    can_fix: bool = False
    reason: str = ""
    remove_highlights: list[str] = Field(default_factory=list)
    add_highlights: list[AddedHighlight] = Field(default_factory=list)
    highlight_order: list[str] = Field(default_factory=list)
    loop_radius_factor: float = 1.0
    loop_rotation_deg: float = 0.0
    free_waypoints: list[FreeWaypoint] = Field(default_factory=list)
    model: str = ""

    @property
    def has_actions(self) -> bool:
        return bool(
            self.remove_highlights
            or self.add_highlights
            or self.highlight_order
            or abs(self.loop_radius_factor - 1.0) > 0.001
            or abs(self.loop_rotation_deg) > 0.001
            or self.free_waypoints
        )


class RouteContext(BaseModel):
    bike_label: str
    mtb_label: str | None = None
    start_label: str
    end_label: str
    round_trip: bool = False
    requested_distance_km: float | None = None
    heat: int = 50
    freetext: str | None = None
    distance_km: float
    elevation_gain_m: float = 0.0
    elevation_loss_m: float = 0.0
    provider: str = ""
    summary: str | None = None
    highlights: list[dict[str, Any]] = Field(default_factory=list)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "ja")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _context_text(context: RouteContext) -> str:
    prefs = {
        "radtyp": context.bike_label,
        "mtb_haerte": context.mtb_label,
        "start": context.start_label,
        "ziel": context.end_label,
        "rundtour": context.round_trip,
        "wunschdistanz_km": context.requested_distance_km,
        "heat": context.heat,
        "freitext_wuensche": context.freetext,
        "tatsaechliche_distanz_km": round(context.distance_km, 1),
        "hoehenmeter_auf": round(context.elevation_gain_m),
        "hoehenmeter_ab": round(context.elevation_loss_m),
        "routing_provider": context.provider,
        "ki_zusammenfassung": context.summary,
        "highlights": context.highlights,
    }
    return json.dumps(prefs, ensure_ascii=False, indent=2)


def sanitize_review(raw: dict[str, Any], model: str) -> RouteReview:
    score = int(round(_clamp(_as_float(raw.get("score"), 0.0), 0.0, 100.0)))
    uncertainty = _clamp(_as_float(raw.get("uncertainty"), 0.5), 0.0, 1.0)

    verdict = str(raw.get("verdict", "")).strip().lower()
    issues: list[ReviewIssue] = []
    raw_issues = raw.get("issues")
    if isinstance(raw_issues, list):
        for item in raw_issues[:4]:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description", "")).strip()
            if not description:
                continue
            severity = str(item.get("severity", "medium")).strip().lower()
            if severity not in ("high", "medium", "low"):
                severity = "medium"
            category = str(item.get("category", "other")).strip().lower() or "other"
            fix_hint = item.get("fix_hint")
            issues.append(
                ReviewIssue(
                    severity=severity,
                    category=category,
                    description=description[:500],
                    fix_hint=str(fix_hint).strip()[:500] if fix_hint else None,
                )
            )

    if verdict not in ("ok", "revise"):
        verdict = "revise" if any(issue.is_blocking for issue in issues) else "ok"

    return RouteReview(
        score=score,
        uncertainty=round(uncertainty, 2),
        verdict=verdict,
        summary=str(raw.get("summary", "")).strip()[:600],
        issues=issues,
        model=model,
    )


async def review_route(
    route_map: RouteMap,
    context: RouteContext,
    settings: Settings,
) -> RouteReview:
    """Ask the vision model whether the rendered route matches the request."""
    prompt = (
        "Hier ist die aktuelle Route. Prüfe sie gegen die ursprüngliche Anfrage.\n\n"
        "Bildlegende: Die limettenfarbene Linie ist die Route, S ist der Start, Z das "
        "Ziel, nummerierte Punkte sind Highlights. Die Karte ist nordorientiert und "
        "enthält Ortsnamen, Straßen und Höhenlinien.\n\n"
        f"Ursprüngliche Anfrage und Routendaten:\n{_context_text(context)}"
    )
    completion = await complete_json(
        settings,
        [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": route_map.data_url()}},
                ],
            },
        ],
        require_image=True,
        temperature=0.1,
        timeout_s=settings.route_review_timeout_s,
    )
    return sanitize_review(completion.data, completion.model)


def _parse_added(raw: Any, valid_ids: set[str]) -> list[AddedHighlight]:
    added: list[AddedHighlight] = []
    if not isinstance(raw, list):
        return added
    for item in raw[:6]:
        if isinstance(item, str):
            osm_id = item
            rationale = "Vom Prüfbericht empfohlen."
        elif isinstance(item, dict):
            osm_id = str(item.get("osm_id", ""))
            rationale = str(item.get("rationale", "")).strip() or "Vom Prüfbericht empfohlen."
        else:
            continue
        if osm_id in valid_ids:
            added.append(AddedHighlight(osm_id=osm_id, rationale=rationale[:300]))
    return added


def sanitize_revision(
    raw: dict[str, Any],
    *,
    route_map: RouteMap,
    current_ids: list[str],
    candidate_ids: set[str],
    current_path: list[tuple[float, float]],
    settings: Settings,
    model: str,
) -> RouteRevision:
    current_set = set(current_ids)
    remove: list[str] = []
    raw_remove = raw.get("remove_highlights")
    if isinstance(raw_remove, list):
        remove = [str(item) for item in raw_remove if str(item) in current_set]

    add = _parse_added(raw.get("add_highlights"), candidate_ids)

    order: list[str] = []
    raw_order = raw.get("highlight_order")
    if isinstance(raw_order, list):
        for item in raw_order:
            osm_id = str(item)
            if osm_id in current_set or osm_id in candidate_ids:
                if osm_id not in order:
                    order.append(osm_id)

    loop_factor = _clamp(_as_float(raw.get("loop_radius_factor"), 1.0), 0.5, 1.8)
    loop_rotation = _clamp(_as_float(raw.get("loop_rotation_deg"), 0.0), -180.0, 180.0)
    if abs(loop_factor - 1.0) < 0.01:
        loop_factor = 1.0
    if abs(loop_rotation) < 0.5:
        loop_rotation = 0.0

    free: list[FreeWaypoint] = []
    raw_free = raw.get("free_waypoints")
    if isinstance(raw_free, list):
        for item in raw_free:
            if len(free) >= settings.route_review_max_free_waypoints:
                break
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
            if not (0.02 <= image_x <= 0.98 and 0.02 <= image_y <= 0.98):
                continue
            lat, lon = route_map.normalized_to_latlon(image_x, image_y)
            if current_path and (
                distance_point_to_path_km((lat, lon), current_path)
                > settings.route_review_max_free_waypoint_offset_km
            ):
                continue
            after = str(item.get("after", "end"))
            if after not in current_set and after not in ("start", "end"):
                after = "end"
            free.append(
                FreeWaypoint(
                    after=after,
                    lat=lat,
                    lon=lon,
                    reason=str(item.get("reason", "")).strip()[:300],
                )
            )

    can_fix = _as_bool(raw.get("can_fix", True))
    revision = RouteRevision(
        can_fix=can_fix,
        reason=str(raw.get("reason", "")).strip()[:500],
        remove_highlights=remove,
        add_highlights=add,
        highlight_order=order,
        loop_radius_factor=loop_factor,
        loop_rotation_deg=loop_rotation,
        free_waypoints=free,
        model=model,
    )
    if not revision.has_actions:
        revision.can_fix = False
    return revision


async def revise_route(
    route_map: RouteMap,
    context: RouteContext,
    review: RouteReview,
    selected_highlights: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    current_path: list[tuple[float, float]],
    settings: Settings,
) -> RouteRevision:
    """Ask the model for constrained, actionable route corrections."""
    current_ids = [str(item["osm_id"]) for item in selected_highlights]
    candidate_ids = {str(item["osm_id"]) for item in candidates}

    prompt = (
        "Korrigiere die Route anhand des Prüfberichts.\n\n"
        f"Bildgröße: {route_map.width}×{route_map.height} Pixel. "
        "Bildkoordinaten für freie Wegpunkte: x/y von 0 bis 1, oben links = 0/0.\n\n"
        f"Ursprüngliche Anfrage und Routendaten:\n{_context_text(context)}\n\n"
        f"Aktuell gewählte Highlights:\n"
        f"{json.dumps(selected_highlights, ensure_ascii=False, indent=2)}\n\n"
        f"Verfügbare POI-Kandidaten:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
        f"Prüfbericht:\n{json.dumps(review.model_dump(), ensure_ascii=False, indent=2)}"
    )
    completion = await complete_json(
        settings,
        [
            {
                "role": "system",
                "content": _REVISION_SYSTEM.replace(
                    "{max_free}", str(settings.route_review_max_free_waypoints)
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": route_map.data_url()}},
                ],
            },
        ],
        require_image=True,
        temperature=0.2,
        timeout_s=settings.route_review_timeout_s,
    )
    return sanitize_revision(
        completion.data,
        route_map=route_map,
        current_ids=current_ids,
        candidate_ids=candidate_ids,
        current_path=current_path,
        settings=settings,
        model=completion.model,
    )


def describe_revision(
    revision: RouteRevision,
    removed_names: list[str],
    added_names: list[str],
    *,
    order_changed: bool = False,
) -> str:
    parts: list[str] = []
    if revision.remove_highlights:
        names = ", ".join(removed_names[:2]) or f"{len(revision.remove_highlights)} Highlight(s)"
        parts.append(f"{names} entfernt")
    if revision.add_highlights:
        names = ", ".join(added_names[:2]) or f"{len(revision.add_highlights)} Highlight(s)"
        parts.append(f"{names} ergänzt")
    if order_changed:
        parts.append("Reihenfolge angepasst")
    if abs(revision.loop_radius_factor - 1.0) > 0.01:
        parts.append(f"Rundtour-Radius ×{revision.loop_radius_factor:.2f}")
    if abs(revision.loop_rotation_deg) > 0.5:
        parts.append(f"Rundtour gedreht ({revision.loop_rotation_deg:+.0f}°)")
    if revision.free_waypoints:
        parts.append(
            f"{len(revision.free_waypoints)} freie(r) Zwischenwegpunkt(e) gesetzt"
        )
    if not parts:
        return "Keine sinnvolle Änderung gefunden."
    return ", ".join(parts) + "."


__all__ = [
    "AddedHighlight",
    "DeepSeekError",
    "FreeWaypoint",
    "ReviewIssue",
    "RouteContext",
    "RouteRevision",
    "RouteReview",
    "describe_revision",
    "review_route",
    "revise_route",
    "sanitize_review",
    "sanitize_revision",
]
