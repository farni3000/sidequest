"""End-to-end route planning orchestration."""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlmodel import Session

from ..config import Settings
from ..corridor import corridor_points, corridor_width_m, loop_waypoints, roundtrip_radius_km
from ..geocoding import geocode
from ..models import Highlight, RoutePlan
from ..poi.overpass import POI, OverpassError, fetch_pois
from ..poi.ranking import RankingContext, rank_pois
from ..routing.base import (
    BIKE_TYPE_LABELS,
    RouteRequest,
    RoutingError,
    RoutingProvider,
)
from ..routing.selector import providers_for, route_with_fallback
from ..schemas import PlanForm


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
    warnings: list[str] = Field(default_factory=list)
    gpx_url: str = "#"


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
) -> list[tuple[float, float]]:
    if round_trip:
        if selected:
            return [start, *[poi.coords for poi, _ in selected], start]
        return [start, *loop_pts, start]
    return [start, *[poi.coords for poi, _ in selected], end]


async def create_plan(
    form: PlanForm,
    session: Session,
    settings: Settings,
    providers: list[RoutingProvider] | None = None,
) -> PlanView:
    warnings: list[str] = []

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

    async def _attempt(candidate: list[tuple[POI, str]], loop: list[tuple[float, float]]):
        return await route_with_fallback(
            provider_list,
            RouteRequest(
                waypoints=_waypoints(start, end, candidate, form.round_trip, loop),
                bike_type=form.bike_type,
                mtb_difficulty=form.mtb_difficulty,
            ),
        )

    result = None
    route_warnings: list[str] = []
    last_error: RoutingError | None = None

    for drop in range(len(selected) + 1):
        candidate = selected[: len(selected) - drop]
        try:
            result, route_warnings = await _attempt(candidate, loop_pts)
        except RoutingError as exc:
            last_error = exc
            continue
        selected = candidate
        if drop:
            warnings.append(f"{drop} nicht anfahrbare(s) Highlight(s) wurden ausgelassen.")
        break

    if result is None and form.round_trip:
        radius = roundtrip_radius_km(form.distance_km, form.heat)
        for factor in (0.6, 1.4, 0.35):
            alternative = loop_waypoints(start, radius * factor, 3)
            try:
                result, route_warnings = await _attempt([], alternative)
                loop_pts = alternative
                selected = []
                warnings.append("Rundtour ohne Highlights (POIs waren nicht anfahrbar).")
                break
            except RoutingError as exc:
                last_error = exc
                continue

    if result is None:
        raise PlanError(str(last_error) if last_error else "Routing fehlgeschlagen.")

    warnings.extend(route_warnings)

    if (
        form.distance_km
        and selected
        and result.distance_km > form.distance_km * 1.4
    ):
        for _ in range(2):
            selected = selected[:-1]
            waypoints = _waypoints(start, end, selected, form.round_trip, loop_pts)
            try:
                result, _ = await route_with_fallback(
                    provider_list,
                    RouteRequest(
                        waypoints=waypoints,
                        bike_type=form.bike_type,
                        mtb_difficulty=form.mtb_difficulty,
                    ),
                )
            except RoutingError:
                break
            if result.distance_km <= form.distance_km * 1.4:
                break
        warnings.append(
            "Die Route überschritt die Wunschdistanz deutlich; das letzte Highlight wurde entfernt."
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

    end_label_value = end_label
    return PlanView(
        id=plan.id,
        provider_used=result.provider,
        profile=result.profile,
        bike_type=form.bike_type.value,
        bike_type_label=BIKE_TYPE_LABELS.get(form.bike_type, form.bike_type.value),
        mtb_difficulty=form.mtb_difficulty.value if form.mtb_difficulty else None,
        start_label=start_label,
        end_label=end_label_value,
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
        warnings=warnings,
        gpx_url=f"/plan/{plan.id}/gpx",
    )
