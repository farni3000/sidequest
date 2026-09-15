"""Overpass API adapter: discover POIs inside the route corridor."""

from __future__ import annotations

import math

import httpx
from pydantic import BaseModel, Field

from ..config import Settings
from ..geo import distance_point_to_path_km, haversine_km
from ..routing.base import BikeType

# (Overpass filter, canonical category)
_COMMON_FILTERS: list[tuple[str, str]] = [
    ("[tourism=viewpoint][name]", "viewpoint"),
    ("[natural=peak][name]", "peak"),
    ("[historic][name]", "historic"),
    ("[natural=water][water=lake][name]", "lake"),
]

_CATEGORY_FILTERS: dict[BikeType, list[tuple[str, str]]] = {
    BikeType.road: [
        ("[mountain_pass=yes][name]", "mountain_pass"),
        ("[tourism=attraction][name]", "attraction"),
    ],
    BikeType.gravel: [
        ("[tourism=attraction][name]", "attraction"),
        ("[highway=track][name]", "track"),
    ],
    BikeType.mtb: [
        ('["mtb:scale"][name]', "mtb_trail"),
        ("[route=mtb][name]", "mtb_route"),
        ("[tourism=attraction][name]", "attraction"),
    ],
    BikeType.trekking: [
        ("[tourism=attraction][name]", "attraction"),
        ("[amenity=picnic_site][name]", "picnic_site"),
        ("[leisure=park][name]", "park"),
    ],
}


class POI(BaseModel):
    osm_id: str
    name: str
    category: str
    lat: float
    lon: float
    distance_to_route_km: float = 0.0
    detour_km: float = 0.0
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lon)


def filters_for(bike_type: BikeType) -> list[tuple[str, str]]:
    return [*_COMMON_FILTERS, *_CATEGORY_FILTERS.get(bike_type, [])]


def infer_category(tags: dict[str, str]) -> str:
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("mtb:scale") is not None:
        return "mtb_trail"
    if tags.get("route") == "mtb":
        return "mtb_route"
    if tags.get("mountain_pass") in ("yes", "1"):
        return "mountain_pass"
    if tags.get("natural") == "water" or tags.get("water"):
        return "lake"
    if tags.get("tourism") == "attraction":
        return "attraction"
    if tags.get("amenity") == "picnic_site":
        return "picnic_site"
    if tags.get("leisure") == "park":
        return "park"
    if tags.get("highway") == "track":
        return "track"
    if tags.get("historic"):
        return "historic"
    return "poi"


def _bbox(points: list[tuple[float, float]], width_m: int) -> tuple[float, float, float, float]:
    """Bounding box around ``points`` expanded by ``width_m`` on all sides."""
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    mean_lat = sum(lats) / len(lats)
    dlat = width_m / 111_320.0
    dlon = width_m / (111_320.0 * max(0.1, math.cos(math.radians(mean_lat))))
    return min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon


def build_query(
    points: list[tuple[float, float]],
    width_m: int,
    bike_type: BikeType,
    timeout_s: int = 25,
    limit: int = 80,
) -> str:
    south, west, north, east = _bbox(points, width_m)
    bbox = f"({south:.5f},{west:.5f},{north:.5f},{east:.5f})"
    lines = []
    # A bbox is much cheaper for Overpass than a multi-point `around`; the exact
    # corridor distance is enforced locally afterwards.
    for filter_expr, _ in filters_for(bike_type):
        for selector in ("node", "way"):
            lines.append(f"  {selector}{bbox}{filter_expr};")
    body = "\n".join(lines)
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout center {limit};"


def _element_coords(element: dict) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if center:
        return float(center["lat"]), float(center["lon"])
    return None


def parse_elements(elements: list[dict], reference_path: list[tuple[float, float]]) -> list[POI]:
    pois: list[POI] = []
    seen: set[str] = set()
    for element in elements:
        tags = element.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        coords = _element_coords(element)
        if coords is None:
            continue
        osm_id = f"{element.get('type', 'node')}/{element.get('id')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)

        if len(reference_path) <= 1:
            dist = haversine_km(coords, reference_path[0]) if reference_path else 0.0
        else:
            dist = distance_point_to_path_km(coords, reference_path)

        pois.append(
            POI(
                osm_id=osm_id,
                name=name,
                category=infer_category(tags),
                lat=coords[0],
                lon=coords[1],
                distance_to_route_km=round(dist, 3),
                detour_km=round(dist * 2, 3),
                tags={k: v for k, v in tags.items() if k in ("name", "tourism", "natural", "historic", "mtb:scale", "route", "amenity", "leisure", "highway", "water", "mountain_pass")},
            )
        )

    pois.sort(key=lambda p: p.detour_km)
    return pois


async def fetch_pois(
    points: list[tuple[float, float]],
    width_m: int,
    bike_type: BikeType,
    settings: Settings,
    reference_path: list[tuple[float, float]] | None = None,
) -> list[POI]:
    """Query Overpass (with mirror fallback) and return POIs sorted by detour."""
    query = build_query(points, width_m, bike_type, limit=settings.max_pois)
    endpoints = [settings.overpass_url]
    if settings.overpass_fallback_url:
        endpoints.append(settings.overpass_fallback_url)

    last_error: Exception | None = None
    last_url = endpoints[-1]
    payload: dict | None = None
    for url in endpoints:
        last_url = url
        try:
            async with httpx.AsyncClient(timeout=settings.http_timeout_s) as client:
                response = await client.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": settings.geocoding_user_agent},
                )
                response.raise_for_status()
                payload = response.json()
            break
        except httpx.HTTPError as exc:
            last_error = exc
        except ValueError as exc:
            last_error = exc

    if payload is None:
        reason = f"{type(last_error).__name__}: {last_error}" if last_error else "unbekannt"
        raise OverpassError(
            f"Overpass nicht erreichbar ({last_url} -> {reason}). "
            "Versuche es später erneut oder setze OVERPASS_URL."
        )

    elements = payload.get("elements", [])
    ref = reference_path if reference_path is not None else points
    pois = parse_elements(elements, ref)

    max_distance_km = width_m / 1000.0 * 1.05
    return [poi for poi in pois if poi.distance_to_route_km <= max_distance_km]


class OverpassError(RuntimeError):
    """Raised when the Overpass API cannot be queried."""
