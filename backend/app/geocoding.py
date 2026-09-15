"""Address/coordinate resolution with ORS geocoding and Nominatim fallback."""

from __future__ import annotations

import re

import httpx

from .config import Settings

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")


class GeocodingError(RuntimeError):
    pass


def parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORD_RE.match(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


async def _geocode_ors(text: str, settings: Settings) -> tuple[float, float, str] | None:
    if not settings.has_ors:
        return None
    url = f"{settings.ors_base_url.rstrip('/')}/geocode/search"
    params: dict[str, str | int] = {"api_key": settings.ors_api_key or "", "text": text, "size": 1}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_s) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    features = payload.get("features") or []
    if not features:
        return None
    feature = features[0]
    lon, lat = feature["geometry"]["coordinates"][:2]
    label = feature.get("properties", {}).get("label") or text
    return float(lat), float(lon), label


async def _geocode_nominatim(text: str, settings: Settings) -> tuple[float, float, str] | None:
    url = f"{settings.nominatim_url.rstrip('/')}/search"
    params: dict[str, str | int] = {"q": text, "format": "jsonv2", "limit": 1}
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_s) as client:
            response = await client.get(
                url, params=params, headers={"User-Agent": settings.geocoding_user_agent}
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not payload:
        return None
    entry = payload[0]
    return float(entry["lat"]), float(entry["lon"]), entry.get("display_name") or text


async def geocode(text: str, settings: Settings) -> tuple[float, float, str]:
    """Resolve ``text`` to (lat, lon, label); raises GeocodingError if unresolvable."""
    coords = parse_coordinates(text)
    if coords is not None:
        return coords[0], coords[1], text.strip()

    for resolver in (_geocode_ors, _geocode_nominatim):
        result = await resolver(text, settings)
        if result is not None:
            return result

    raise GeocodingError(f"Ort konnte nicht gefunden werden: '{text}'")
