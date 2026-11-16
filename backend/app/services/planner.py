"""End-to-end route planning orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session

from .. import map_image
from ..ai import route_shaping
from ..ai.route_review import (
    RouteContext,
    RouteReview,
    RouteRevision,
    describe_revision,
    review_route,
    revise_route,
)
from ..config import Settings
from ..corridor import corridor_points, corridor_width_m, loop_waypoints, roundtrip_radius_km
from ..geocoding import geocode
from ..models import Highlight, PlanShapingPoint, RoutePlan
from ..poi.overpass import POI, OverpassError, fetch_pois
from ..poi.ranking import RankingContext, RankingResult, rank_pois
from ..routing.base import (
    BIKE_TYPE_LABELS,
    MTB_DIFFICULTY_LABELS,
    RouteRequest,
    RoutingError,
    RoutingProvider,
)
from ..routing.selector import providers_for, route_with_fallback
from ..schemas import PlanForm

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class PlanError(RuntimeError):
    pass


class HighlightView(BaseModel):
    osm_id: str = ""
    name: str
    category: str
    lat: float
    lon: float
    detour_km: float = 0.0
    rationale: str | None = None
    order_index: int = 0


class ReviewIssueView(BaseModel):
    severity: str = "medium"
    category: str = "other"
    description: str
    fix_hint: str | None = None


class ReviewView(BaseModel):
    checked: bool = False
    accepted: bool = False
    score: int | None = None
    uncertainty: float | None = None
    rounds: int = 0
    summary: str | None = None
    issues: list[ReviewIssueView] = Field(default_factory=list)
    model: str | None = None


class PlanView(BaseModel):
    id: int | None = None
    provider_used: str
    profile: str | None = None
    bike_type: str
    bike_type_label: str
    mtb_difficulty: str | None = None
    start_label: str
    end_label: str
    round_trip: bool = False
    requested_distance_km: float | None = None
    distance_km: float
    elevation_gain_m: float
    elevation_loss_m: float
    heat: int
    freetext: str | None = None
    summary: str | None = None
    ai_used: bool = False
    geometry: list[list[float]] = Field(default_factory=list)
    elevations: list[float] = Field(default_factory=list)
    highlights: list[HighlightView] = Field(default_factory=list)
    review: ReviewView | None = None
    warnings: list[str] = Field(default_factory=list)
    gpx_url: str = "#"
    gpx_point_count: int = 0


def _detour_budget(form: PlanForm, width_m: int) -> float:
    if form.distance_km:
        return round(form.distance_km * (0.1 + 0.4 * form.heat / 100.0), 1)
    return round(max(2.0, (width_m / 1000.0) * 2.0), 1)


def _waypoints(
    start: tuple[float, float],
    end: tuple[float, float],
    selected: list[tuple[POI, str]],
    round_trip: bool,
    loop_pts: list[tuple[float, float]],
    free_waypoints: list[Any] | None = None,
) -> list[tuple[float, float]]:
    """Build the ordered routing waypoints, including free shaping points."""
    nodes: list[tuple[str, tuple[float, float]]] = [("start", start)]
    for poi, _rationale in selected:
        nodes.append((poi.osm_id, poi.coords))
    if round_trip:
        if not selected:
            nodes.extend((f"loop-{index}", point) for index, point in enumerate(loop_pts))
        nodes.append(("end", start))
    else:
        nodes.append(("end", end))

    grouped: dict[str, list[tuple[float, float]]] = {}
    for waypoint in free_waypoints or []:
        grouped.setdefault(str(getattr(waypoint, "after", "end")), []).append(
            (float(waypoint.lat), float(waypoint.lon))
        )

    route: list[tuple[float, float]] = []
    for key, coords in nodes:
        if key == "end":
            route.extend(grouped.get("end", []))
            route.append(coords)
        else:
            route.append(coords)
            route.extend(grouped.get(key, []))
    return route


def _prune_free_waypoints(
    free_waypoints: list[Any], selected: list[tuple[POI, str]]
) -> list[Any]:
    """Drop shaping points whose anchor highlight was removed."""
    anchors = {poi.osm_id for poi, _rationale in selected}
    return [
        waypoint
        for waypoint in free_waypoints
        if getattr(waypoint, "after", "start") in anchors
        or getattr(waypoint, "after", "start") in ("start", "end")
    ]


def _highlight_payload(selected: list[tuple[POI, str]]) -> list[dict[str, Any]]:
    return [
        {
            "osm_id": poi.osm_id,
            "name": poi.name,
            "category": poi.category,
            "detour_km": poi.detour_km,
            "rationale": rationale,
        }
        for poi, rationale in selected
    ]


def _highlight_markers(selected: list[tuple[POI, str]]) -> list[map_image.MapMarker]:
    return [
        map_image.MapMarker(
            name=poi.name,
            category=poi.category,
            lat=poi.lat,
            lon=poi.lon,
            order=index,
        )
        for index, (poi, _rationale) in enumerate(selected, start=1)
    ]


def _apply_revision(
    revision: RouteRevision,
    selected: list[tuple[POI, str]],
    candidate_by_id: dict[str, POI],
) -> tuple[list[tuple[POI, str]], list[str], list[str], bool]:
    remove_set = set(revision.remove_highlights)
    removed_names = [poi.name for poi, _ in selected if poi.osm_id in remove_set]
    kept = [(poi, rationale) for poi, rationale in selected if poi.osm_id not in remove_set]
    old_ids = [poi.osm_id for poi, _ in kept]

    existing = {poi.osm_id for poi, _ in kept}
    added_names: list[str] = []
    for added in revision.add_highlights:
        poi = candidate_by_id.get(added.osm_id)
        if poi is None or poi.osm_id in existing:
            continue
        kept.append((poi, added.rationale))
        existing.add(poi.osm_id)
        added_names.append(poi.name)

    order_changed = False
    if revision.highlight_order:
        by_id = {poi.osm_id: (poi, rationale) for poi, rationale in kept}
        ordered: list[tuple[POI, str]] = []
        seen: set[str] = set()
        for osm_id in revision.highlight_order:
            item = by_id.get(osm_id)
            if item is not None and osm_id not in seen:
                ordered.append(item)
                seen.add(osm_id)
        for item in kept:
            if item[0].osm_id not in seen:
                ordered.append(item)
        order_changed = [poi.osm_id for poi, _ in ordered] != old_ids
        kept = ordered

    return kept, removed_names, added_names, order_changed


def _review_context(
    form: PlanForm,
    start_label: str,
    end_label: str,
    result: Any,
    selected: list[tuple[POI, str]],
    ranking: RankingResult,
) -> RouteContext:
    return RouteContext(
        bike_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
        mtb_label=(
            MTB_DIFFICULTY_LABELS.get(form.mtb_difficulty) if form.mtb_difficulty else None
        ),
        start_label=start_label,
        end_label=end_label,
        round_trip=form.round_trip,
        requested_distance_km=form.distance_km,
        heat=form.heat,
        freetext=form.freetext,
        distance_km=result.distance_km,
        elevation_gain_m=result.elevation_gain_m,
        elevation_loss_m=result.elevation_loss_m,
        provider=result.provider,
        summary=ranking.summary,
        highlights=[
            {
                "osm_id": poi.osm_id,
                "name": poi.name,
                "category": poi.category,
                "detour_km": poi.detour_km,
                "rationale": rationale,
                "order": index,
            }
            for index, (poi, rationale) in enumerate(selected, start=1)
        ],
    )


def _review_event(review: RouteReview, round_no: int) -> dict[str, Any]:
    return {
        "type": "review",
        "round": round_no,
        "score": review.score,
        "uncertainty": review.uncertainty,
        "verdict": review.verdict,
        "accepted": review.accepted,
        "summary": review.summary,
        "issues": [issue.model_dump() for issue in review.issues],
        "model": review.model,
    }


def _review_view(review: RouteReview | None, rounds: int) -> ReviewView | None:
    if review is None:
        return None
    return ReviewView(
        checked=True,
        accepted=review.accepted,
        score=review.score,
        uncertainty=review.uncertainty,
        rounds=rounds,
        summary=review.summary or None,
        issues=[
            ReviewIssueView(
                severity=issue.severity,
                category=issue.category,
                description=issue.description,
                fix_hint=issue.fix_hint,
            )
            for issue in review.issues
        ],
        model=review.model,
    )


async def _select_gpx_points(
    form: PlanForm,
    result: Any,
    selected: list[tuple[POI, str]],
    free_waypoints: list[Any],
    settings: Settings,
    warnings: list[str],
    on_event: EventCallback | None,
    start_label: str,
    end_label: str,
) -> list[route_shaping.SelectedShapingPoint]:
    """Reduce the routed geometry to the sparse waypoints used by the GPX."""
    if not settings.gpx_shaping_enabled or not result.geometry:
        return []

    spacing_km = max(
        3.0,
        settings.gpx_point_spacing_km,
        result.distance_km / max(1, settings.gpx_max_points),
    )
    candidates = route_shaping.build_shaping_candidates(
        [(lat, lon) for lat, lon in result.geometry],
        result.elevations,
        _highlight_payload(selected),
        anchors=[
            {
                "lat": waypoint.lat,
                "lon": waypoint.lon,
                "reason": getattr(waypoint, "reason", ""),
            }
            for waypoint in free_waypoints
        ],
        spacing_km=spacing_km,
        max_points=settings.gpx_max_points,
        min_spacing_km=settings.gpx_min_point_spacing_km,
    )
    if not candidates:
        return []

    if not settings.has_deepseek:
        return route_shaping.fallback_shaping_points(candidates)

    await _emit(
        on_event,
        {"type": "status", "message": "GPX-Wegpunkte werden per Bildmodell gewählt …"},
    )
    try:
        candidate_map = await map_image.render_route_map(
            [(lat, lon) for lat, lon in result.geometry],
            _highlight_markers(selected),
            settings,
            distance_km=result.distance_km,
            requested_distance_km=form.distance_km,
            elevation_gain_m=result.elevation_gain_m,
            bike_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
            round_trip=form.round_trip,
            start_label=start_label,
            end_label=end_label,
            candidates=[
                map_image.MapCandidate(
                    lat=candidate.lat,
                    lon=candidate.lon,
                    order=candidate.order,
                    label=candidate.label,
                )
                for candidate in candidates
            ],
            title="sidequest · GPX-Wegpunkte",
            headline=f"{result.distance_km:.1f} km · {len(candidates)} Kandidaten",
        )
        context = {
            "radtyp": BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
            "mtb_haerte": (
                MTB_DIFFICULTY_LABELS.get(form.mtb_difficulty)
                if form.mtb_difficulty
                else None
            ),
            "distanz_km": round(result.distance_km, 1),
            "wunsch_km": form.distance_km,
            "heat": form.heat,
            "freitext": form.freetext,
            "rundtour": form.round_trip,
            "highlights": [
                {"name": poi.name, "category": poi.category}
                for poi, _rationale in selected
            ],
        }
        selection = await route_shaping.select_gpx_points(
            candidate_map,
            context,
            candidates,
            [(lat, lon) for lat, lon in result.geometry],
            result.elevations,
            settings,
        )
        if selection.points:
            return selection.points
        return route_shaping.fallback_shaping_points(candidates)
    except Exception as exc:  # noqa: BLE001 - GPX export must still work
        warnings.append(
            f"GPX-Wegpunkte konnten nicht per Bildmodell gewählt werden ({exc}); "
            "eine gleichmäßige Auswahl wird verwendet."
        )
        return route_shaping.fallback_shaping_points(candidates)


async def _emit(on_event: EventCallback | None, payload: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        await on_event(payload)
    except Exception:  # noqa: BLE001 - a broken progress stream must not stop planning
        pass


async def create_plan(
    form: PlanForm,
    session: Session,
    settings: Settings,
    providers: list[RoutingProvider] | None = None,
    on_event: EventCallback | None = None,
) -> PlanView:
    warnings: list[str] = []

    await _emit(on_event, {"type": "status", "message": "Start und Ziel werden aufgelöst …"})
    start_lat, start_lon, start_label = await geocode(form.start, settings)
    start = (start_lat, start_lon)
    if form.round_trip:
        end = start
        end_label = f"Rundtour ab {start_label}"
    else:
        if not form.end:
            raise PlanError("Für eine Punkt-zu-Punkt-Route wird ein Ziel benötigt.")
        end_lat, end_lon, end_label = await geocode(form.end, settings)
        end = (end_lat, end_lon)

    width_m = corridor_width_m(form.heat, settings)
    if form.round_trip:
        radius = roundtrip_radius_km(form.distance_km, form.heat)
        loop_pts = loop_waypoints(start, radius, 4)
        reference_path = [start, *loop_pts, start]
        query_points = [start, *loop_pts]
    else:
        loop_pts = []
        reference_path = corridor_points(start, end, samples=4)
        query_points = reference_path

    await _emit(on_event, {"type": "status", "message": "Interessante Orte werden gesucht …"})
    pois: list[POI] = []
    try:
        pois = await fetch_pois(
            query_points, width_m, form.bike_type, settings, reference_path=reference_path
        )
    except OverpassError as exc:
        warnings.append(f"Keine Highlight-Suche möglich ({exc}). Route wird ohne POIs berechnet.")

    context = RankingContext(
        bike_type=form.bike_type,
        mtb_difficulty=form.mtb_difficulty,
        freetext=form.freetext,
        heat=form.heat,
        requested_distance_km=form.distance_km,
        max_highlights=settings.max_highlights,
        budget_km=_detour_budget(form, width_m),
    )
    await _emit(
        on_event,
        {
            "type": "status",
            "message": (
                "KI wählt Highlights aus …" if settings.has_deepseek else "Highlights werden gewählt …"
            ),
        },
    )
    ranking = await rank_pois(pois, context, settings)
    if settings.has_deepseek and ranking.source != "deepseek":
        warnings.append("KI-Auswahl nicht verfügbar – nächstgelegene Highlights wurden gewählt.")

    poi_by_id = {poi.osm_id: poi for poi in pois}
    selected: list[tuple[POI, str]] = [
        (poi_by_id[item.osm_id], item.rationale)
        for item in ranking.items
        if item.osm_id in poi_by_id
    ]

    provider_list = providers if providers is not None else providers_for(form.bike_type, settings)
    if not provider_list:
        raise PlanError(
            "Kein Routing-Provider konfiguriert. Bitte ORS_API_KEY setzen oder BRouter starten."
        )

    # --- Vision route draft: let the image model shape the route -----------
    draft_free: list[Any] = []
    if settings.vision_route_draft_enabled and settings.has_deepseek:
        await _emit(
            on_event,
            {"type": "status", "message": "Bildmodell entwirft die Route …"},
        )
        try:
            selected_ids = {poi.osm_id for poi, _rationale in selected}
            extras = sorted(
                (poi for poi in pois if poi.osm_id not in selected_ids),
                key=lambda poi: poi.detour_km,
            )
            candidate_pool = [poi for poi, _rationale in selected]
            candidate_pool.extend(extras[: max(0, 24 - len(candidate_pool))])
            candidate_pool = candidate_pool[:24]
            candidates_payload = [
                {
                    "osm_id": poi.osm_id,
                    "name": poi.name,
                    "category": poi.category,
                    "detour_km": poi.detour_km,
                    "distance_to_route_km": poi.distance_to_route_km,
                    "lat": poi.lat,
                    "lon": poi.lon,
                }
                for poi in candidate_pool
            ]
            markers = [
                map_image.MapMarker(
                    name=poi.name,
                    category=poi.category,
                    lat=poi.lat,
                    lon=poi.lon,
                    order=index,
                )
                for index, poi in enumerate(candidate_pool, start=1)
            ]
            area_map = await map_image.render_area_map(
                start,
                end,
                markers,
                settings,
                reference_path=(
                    [start, *loop_pts, start] if form.round_trip else reference_path
                ),
                round_trip=form.round_trip,
                requested_distance_km=form.distance_km,
                bike_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
                start_label=start_label,
                end_label=end_label,
            )
            draft = await route_shaping.draft_route(
                area_map,
                route_shaping.DraftContext(
                    bike_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
                    mtb_label=(
                        MTB_DIFFICULTY_LABELS.get(form.mtb_difficulty)
                        if form.mtb_difficulty
                        else None
                    ),
                    start_label=start_label,
                    end_label=end_label,
                    round_trip=form.round_trip,
                    requested_distance_km=form.distance_km,
                    heat=form.heat,
                    freetext=form.freetext,
                    summary=ranking.summary,
                ),
                [(poi.osm_id, rationale) for poi, rationale in selected],
                candidates_payload,
                start=start,
                end=end,
                round_trip=form.round_trip,
                radius_km=(
                    roundtrip_radius_km(form.distance_km, form.heat)
                    if form.round_trip
                    else None
                ),
                corridor_width_m=width_m,
                settings=settings,
            )
            if draft.highlights:
                drafted: list[tuple[POI, str]] = []
                for entry in draft.highlights:
                    poi = poi_by_id.get(entry.osm_id)
                    if poi is None:
                        continue
                    rationale = entry.reason or next(
                        (
                            reason
                            for existing_poi, reason in selected
                            if existing_poi.osm_id == entry.osm_id
                        ),
                        "Vom Bildmodell gewählt.",
                    )
                    drafted.append((poi, rationale))
                if drafted:
                    selected = drafted
            draft_free = list(draft.shaping)
        except Exception as exc:  # noqa: BLE001 - draft is an enhancement
            warnings.append(
                f"Bildmodell-Routenentwurf nicht verfügbar ({exc}). "
                "Die bisherige Auswahl bleibt."
            )

    async def _attempt(
        candidate: list[tuple[POI, str]],
        loop: list[tuple[float, float]],
        free: list[Any] | None = None,
    ):
        effective_loop = [] if (free and not candidate) else loop
        return await route_with_fallback(
            provider_list,
            RouteRequest(
                waypoints=_waypoints(
                    start, end, candidate, form.round_trip, effective_loop, free
                ),
                bike_type=form.bike_type,
                mtb_difficulty=form.mtb_difficulty,
            ),
        )

    await _emit(on_event, {"type": "status", "message": "Strecke wird berechnet …"})
    result = None
    route_warnings: list[str] = []
    last_error: RoutingError | None = None

    free_waypoints: list[Any] = list(draft_free)
    for free_option in ([free_waypoints, []] if free_waypoints else [[]]):
        for drop in range(len(selected) + 1):
            candidate = selected[: len(selected) - drop]
            try:
                result, route_warnings = await _attempt(candidate, loop_pts, free_option)
            except RoutingError as exc:
                last_error = exc
                continue
            selected = candidate
            free_waypoints = _prune_free_waypoints(free_option, selected)
            if drop:
                warnings.append(
                    f"{drop} nicht anfahrbare(s) Highlight(s) wurden ausgelassen."
                )
            if free_option and not free_waypoints:
                warnings.append(
                    "Die Wegpunkte des Bildmodells lagen hinter entfernten Highlights "
                    "und wurden ausgelassen."
                )
            break
        if result is not None:
            break
        if free_option:
            warnings.append(
                "Die Wegpunkte des Bildmodells waren nicht anfahrbar und wurden verworfen."
            )

    if result is None and form.round_trip:
        radius = roundtrip_radius_km(form.distance_km, form.heat)
        for factor in (0.6, 1.4, 0.35):
            alternative = loop_waypoints(start, radius * factor, 3)
            try:
                result, route_warnings = await _attempt([], alternative)
                loop_pts = alternative
                selected = []
                free_waypoints = []
                warnings.append("Rundtour ohne Highlights (POIs waren nicht anfahrbar).")
                break
            except RoutingError as exc:
                last_error = exc
                continue

    if result is None:
        raise PlanError(str(last_error) if last_error else "Routing fehlgeschlagen.")

    warnings.extend(route_warnings)
    await _emit(
        on_event,
        {
            "type": "status",
            "message": (
                f"Route berechnet: {result.distance_km:.1f} km · "
                f"{result.elevation_gain_m:.0f} hm"
            ),
        },
    )

    if (
        form.distance_km
        and (selected or free_waypoints)
        and result.distance_km > form.distance_km * 1.4
    ):
        removed = False
        while (
            result.distance_km > form.distance_km * 1.4
            and (free_waypoints or selected)
        ):
            if free_waypoints:
                free_waypoints = free_waypoints[:-1]
            else:
                selected = selected[:-1]
            try:
                result, _ = await _attempt(selected, loop_pts, free_waypoints or None)
            except RoutingError:
                break
            removed = True
        if removed:
            warnings.append(
                "Die Route überschritt die Wunschdistanz deutlich; "
                "Zwischenpunkte/Highlights wurden entfernt."
            )

    # --- Vision review + up to two correction rounds -----------------------
    review_history: list[RouteReview] = []
    best_review: RouteReview | None = None
    best_result = result
    best_selected = list(selected)
    best_loop = list(loop_pts)
    best_free: list[Any] = list(free_waypoints)
    best_score = -1

    max_rounds = min(3, max(0, settings.route_review_max_rounds))
    if settings.route_review_enabled and settings.has_deepseek and max_rounds:
        current_result = result
        current_selected = list(selected)
        current_loop = list(loop_pts)

        for round_no in range(1, max_rounds + 1):
            await _emit(
                on_event,
                {
                    "type": "status",
                    "message": f"Routenbild für Prüfung {round_no} wird erzeugt …",
                },
            )
            try:
                route_map = await map_image.render_route_map(
                    [(lat, lon) for lat, lon in current_result.geometry],
                    _highlight_markers(current_selected),
                    settings,
                    distance_km=current_result.distance_km,
                    requested_distance_km=form.distance_km,
                    elevation_gain_m=current_result.elevation_gain_m,
                    bike_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
                    round_trip=form.round_trip,
                    start_label=start_label,
                    end_label=end_label,
                )
            except Exception as exc:  # noqa: BLE001 - review is an enhancement
                warnings.append(
                    f"Automatische Streckenprüfung nicht verfügbar ({exc}). "
                    "Die Route wurde trotzdem berechnet."
                )
                break

            await _emit(
                on_event,
                {"type": "status", "message": f"Prüfung {round_no} läuft …"},
            )
            review_context = _review_context(
                form, start_label, end_label, current_result, current_selected, ranking
            )
            try:
                review = await review_route(route_map, review_context, settings)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"Automatische Streckenprüfung nicht verfügbar ({exc}). "
                    "Die Route wurde trotzdem berechnet."
                )
                break

            review_history.append(review)
            await _emit(on_event, _review_event(review, round_no))

            if review.accepted:
                best_review = review
                best_score = review.score
                best_result = current_result
                best_selected = list(current_selected)
                best_loop = list(current_loop)
                best_free = list(free_waypoints)
                break

            if review.score > best_score:
                best_review = review
                best_score = review.score
                best_result = current_result
                best_selected = list(current_selected)
                best_loop = list(current_loop)
                best_free = list(free_waypoints)
            elif round_no > 1:
                warnings.append(
                    "Die Korrektur hat die Route nicht verbessert; die vorherige Route bleibt."
                )
                break

            if round_no >= max_rounds:
                break

            await _emit(
                on_event,
                {"type": "status", "message": f"Korrektur nach Prüfung {round_no} …"},
            )
            candidates = [
                {
                    "osm_id": poi.osm_id,
                    "name": poi.name,
                    "category": poi.category,
                    "detour_km": poi.detour_km,
                    "distance_to_route_km": poi.distance_to_route_km,
                }
                for poi in pois
            ]
            selected_payload = [
                {
                    "osm_id": poi.osm_id,
                    "name": poi.name,
                    "category": poi.category,
                    "detour_km": poi.detour_km,
                    "rationale": rationale,
                }
                for poi, rationale in current_selected
            ]
            try:
                revision = await revise_route(
                    route_map,
                    review_context,
                    review,
                    selected_payload,
                    candidates,
                    [(lat, lon) for lat, lon in current_result.geometry],
                    settings,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Automatische Korrektur nicht verfügbar ({exc}).")
                break

            if not revision.can_fix or not revision.has_actions:
                await _emit(
                    on_event,
                    {
                        "type": "status",
                        "message": "Keine sinnvolle automatische Korrektur gefunden.",
                    },
                )
                break

            new_selected, removed_names, added_names, order_changed = _apply_revision(
                revision, current_selected, poi_by_id
            )
            new_loop = list(current_loop)
            loop_changed = False
            if (
                form.round_trip
                and not new_selected
                and (
                    abs(revision.loop_radius_factor - 1.0) > 0.01
                    or abs(revision.loop_rotation_deg) > 0.5
                )
            ):
                radius = roundtrip_radius_km(form.distance_km, form.heat)
                new_loop = loop_waypoints(
                    start,
                    radius * revision.loop_radius_factor,
                    4,
                    rotation_deg=revision.loop_rotation_deg,
                )
                loop_changed = True

            if new_selected == current_selected and not revision.free_waypoints and not loop_changed:
                await _emit(
                    on_event,
                    {
                        "type": "status",
                        "message": "Keine wirksame Änderung gefunden; die Route bleibt.",
                    },
                )
                break

            description = describe_revision(
                revision, removed_names, added_names, order_changed=order_changed
            )
            await _emit(
                on_event,
                {
                    "type": "revision",
                    "round": round_no,
                    "message": description,
                    "reason": revision.reason,
                    "model": revision.model,
                },
            )
            await _emit(
                on_event,
                {"type": "status", "message": "Route wird mit den Änderungen neu berechnet …"},
            )

            existing_free = _prune_free_waypoints(free_waypoints, new_selected)
            used_free = [*existing_free, *revision.free_waypoints]
            try:
                new_result, new_warnings = await _attempt(
                    new_selected, new_loop, used_free
                )
            except RoutingError:
                used_free = existing_free
                try:
                    new_result, new_warnings = await _attempt(
                        new_selected, new_loop, used_free or None
                    )
                    warnings.append(
                        "Neue freie Zwischenwegpunkte waren nicht anfahrbar und "
                        "wurden verworfen."
                    )
                except RoutingError as exc:
                    warnings.append(
                        "Die korrigierte Route war nicht berechenbar; "
                        "die vorherige Route bleibt."
                    )
                    last_error = exc
                    break

            warnings.extend(new_warnings)
            current_result = new_result
            current_selected = new_selected
            current_loop = new_loop
            free_waypoints = used_free
            await _emit(
                on_event,
                {
                    "type": "status",
                    "message": (
                        f"Neue Route berechnet: {current_result.distance_km:.1f} km · "
                        f"{current_result.elevation_gain_m:.0f} hm"
                    ),
                },
            )

        if best_review is not None:
            result = best_result
            selected = best_selected
            loop_pts = best_loop
            free_waypoints = best_free

    review_view = _review_view(best_review, len(review_history))
    gpx_points = await _select_gpx_points(
        form,
        result,
        selected,
        free_waypoints,
        settings,
        warnings,
        on_event,
        start_label,
        end_label,
    )

    plan = RoutePlan(
        bike_type=form.bike_type.value,
        mtb_difficulty=form.mtb_difficulty.value if form.mtb_difficulty else None,
        start_label=start_label,
        start_lat=start[0],
        start_lon=start[1],
        end_label=end_label,
        end_lat=end[0],
        end_lon=end[1],
        round_trip=form.round_trip,
        requested_distance_km=form.distance_km,
        heat=form.heat,
        freetext=form.freetext,
        provider_used=result.provider,
        distance_km=result.distance_km,
        elevation_gain_m=result.elevation_gain_m,
        elevation_loss_m=result.elevation_loss_m,
    )
    plan.set_geometry(result.geometry)
    plan.set_elevations(result.elevations)
    plan.set_warnings(warnings)
    session.add(plan)
    session.commit()
    session.refresh(plan)

    for index, (poi, rationale) in enumerate(selected, start=1):
        session.add(
            Highlight(
                plan_id=plan.id,
                osm_id=poi.osm_id,
                name=poi.name,
                category=poi.category,
                lat=poi.lat,
                lon=poi.lon,
                detour_km=poi.detour_km,
                rationale=rationale,
                order_index=index,
            )
        )
    for index, point in enumerate(gpx_points, start=1):
        session.add(
            PlanShapingPoint(
                plan_id=plan.id,
                order_index=index,
                lat=point.lat,
                lon=point.lon,
                elevation=point.elevation,
                kind=point.kind,
                label=point.label or None,
                reason=point.reason or None,
                osm_id=point.osm_id or None,
            )
        )
    session.commit()

    highlights = [
        HighlightView(
            osm_id=poi.osm_id,
            name=poi.name,
            category=poi.category,
            lat=poi.lat,
            lon=poi.lon,
            detour_km=poi.detour_km,
            rationale=rationale,
            order_index=index,
        )
        for index, (poi, rationale) in enumerate(selected, start=1)
    ]

    return PlanView(
        id=plan.id,
        provider_used=result.provider,
        profile=result.profile,
        bike_type=form.bike_type.value,
        bike_type_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
        mtb_difficulty=form.mtb_difficulty.value if form.mtb_difficulty else None,
        start_label=start_label,
        end_label=end_label,
        round_trip=form.round_trip,
        requested_distance_km=form.distance_km,
        distance_km=result.distance_km,
        elevation_gain_m=result.elevation_gain_m,
        elevation_loss_m=result.elevation_loss_m,
        heat=form.heat,
        freetext=form.freetext,
        summary=ranking.summary,
        ai_used=ranking.source == "deepseek",
        geometry=[[lat, lon] for lat, lon in result.geometry],
        elevations=result.elevations,
        highlights=highlights,
        review=review_view,
        warnings=warnings,
        gpx_url=f"/plan/{plan.id}/gpx",
        gpx_point_count=(
            len(gpx_points)
            if settings.gpx_shaping_enabled and gpx_points
            else 0
        ),
    )
