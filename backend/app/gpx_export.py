"""GPX 1.1 export (track + highlight waypoints) for Komoot import."""

from __future__ import annotations

import re
from typing import Any

import gpxpy
import gpxpy.gpx


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "route"


def gpx_filename(name: str) -> str:
    return f"{_slugify(name)}.gpx"


def build_gpx(
    name: str,
    geometry: list[tuple[float, float]],
    elevations: list[float] | None = None,
    highlights: list[dict[str, Any]] | None = None,
    shaping_points: list[dict[str, Any]] | None = None,
) -> str:
    """Render a route to a GPX 1.1 document.

    ``geometry`` is a list of (lat, lon); ``elevations`` an optional parallel
    list. Highlights become ``<wpt>`` entries with name and description.

    When ``shaping_points`` is given, the track only contains those important
    points. komoot can then "match the route to known ways" and replan the
    roads between them. The full geometry stays available in the app.
    """
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "sidequest"
    gpx.name = name

    sparse = shaping_points if shaping_points and len(shaping_points) >= 2 else None

    track = gpxpy.gpx.GPXTrack(name=name)
    track.description = (
        "Erstellt mit sidequest (AI-Radroutenplaner) – ausgedünnte Wegpunkte "
        "für Komoot (Route an bekannte Wege angleichen)"
        if sparse is not None
        else "Erstellt mit sidequest (AI-Radroutenplaner)"
    )
    gpx.tracks.append(track)

    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    if sparse is not None:
        for point in sparse:
            elevation = point.get("elevation")
            segment.points.append(
                gpxpy.gpx.GPXTrackPoint(
                    latitude=float(point["lat"]),
                    longitude=float(point["lon"]),
                    elevation=float(elevation) if elevation is not None else None,
                )
            )
    else:
        for index, (lat, lon) in enumerate(geometry):
            elevation = None
            if elevations and index < len(elevations):
                elevation = elevations[index]
            segment.points.append(
                gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon, elevation=elevation)
            )

    for highlight in highlights or []:
        waypoint = gpxpy.gpx.GPXWaypoint(
            latitude=highlight["lat"],
            longitude=highlight["lon"],
            name=highlight.get("name"),
            description=highlight.get("description") or highlight.get("rationale"),
        )
        waypoint.symbol = "Flag, Red"
        gpx.waypoints.append(waypoint)

    return gpx.to_xml()
