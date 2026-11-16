"""Render a route map image for the vision review.

The image is based on the same Esri World Topo Map that the frontend uses. The
ArcGIS export endpoint returns a single map image without an API key, which is
much more reliable than stitching tiles ourselves. Pillow draws the route,
markers, legend and scale bar on top.
"""

from __future__ import annotations

import base64
import io
import math
import os
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import Settings

_MERCATOR_R = 6_378_137.0
_MAX_LAT = 85.05112878
_EXPORT_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Topo_Map/MapServer/export"
)

_CATEGORY_COLORS: dict[str, str] = {
    "viewpoint": "#2b8fb8",
    "peak": "#7c5bc7",
    "lake": "#2f7fc9",
    "historic": "#b97a12",
    "mountain_pass": "#d24d63",
    "mtb_trail": "#3d8f2e",
    "mtb_route": "#3d8f2e",
    "attraction": "#c94f8d",
    "picnic_site": "#7b9a14",
    "park": "#1f8f68",
    "track": "#a98606",
    "poi": "#5f7268",
}

_FONT_CANDIDATES = {
    False: (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ),
    True: (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
}

_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


class MapImageError(RuntimeError):
    """Raised when a route map preview cannot be produced."""


@dataclass(frozen=True)
class MapMarker:
    name: str
    category: str
    lat: float
    lon: float
    order: int = 0


@dataclass(frozen=True)
class MapCandidate:
    """Small numbered dot for a possible GPX shaping point."""

    lat: float
    lon: float
    order: int
    label: str = ""


@dataclass(frozen=True)
class RouteMap:
    image: bytes
    width: int
    height: int
    # Web Mercator bounds: minx, miny, maxx, maxy
    bounds: tuple[float, float, float, float]
    scale_label: str = ""

    def data_url(self) -> str:
        encoded = base64.b64encode(self.image).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def normalized_to_latlon(self, x: float, y: float) -> tuple[float, float]:
        """Map a 0..1 image coordinate (top-left origin) back to WGS84."""
        minx, miny, maxx, maxy = self.bounds
        nx = min(max(float(x), 0.0), 1.0)
        ny = min(max(float(y), 0.0), 1.0)
        mx = minx + nx * (maxx - minx)
        my = maxy - ny * (maxy - miny)
        return mercator_to_latlon(mx, my)


def lonlat_to_mercator(lat: float, lon: float) -> tuple[float, float]:
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    x = math.radians(lon) * _MERCATOR_R
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * _MERCATOR_R
    return x, y


def mercator_to_latlon(x: float, y: float) -> tuple[float, float]:
    lon = math.degrees(x / _MERCATOR_R)
    lat = math.degrees(2 * math.atan(math.exp(y / _MERCATOR_R)) - math.pi / 2)
    return lat, lon


def _map_bounds(
    points: list[tuple[float, float]], width: int, height: int, margin: float = 0.08
) -> tuple[float, float, float, float]:
    merc = [lonlat_to_mercator(lat, lon) for lat, lon in points]
    if not merc:
        raise MapImageError("Keine Geometrie für das Routenbild vorhanden.")

    xs = [point[0] for point in merc]
    ys = [point[1] for point in merc]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)

    span_x = maxx - minx
    span_y = maxy - miny
    if span_x < 1.0:
        minx -= 500.0
        maxx += 500.0
        span_x = maxx - minx
    if span_y < 1.0:
        miny -= 500.0
        maxy += 500.0
        span_y = maxy - miny

    pad_x = span_x * margin
    pad_y = span_y * margin
    minx -= pad_x
    maxx += pad_x
    miny -= pad_y
    maxy += pad_y

    target_aspect = width / max(1, height)
    span_x = maxx - minx
    span_y = maxy - miny
    if span_x / span_y < target_aspect:
        needed = span_y * target_aspect - span_x
        minx -= needed / 2
        maxx += needed / 2
    else:
        needed = span_x / target_aspect - span_y
        miny -= needed / 2
        maxy += needed / 2

    return minx, miny, maxx, maxy


def _font(size: int, bold: bool = False):
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None
    for candidate in _FONT_CANDIDATES[bold]:
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _fit_text(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[float, float]:
    box = draw.textbbox((0, 0), text, font=font)
    return float(box[2] - box[0]), float(box[3] - box[1])


async def _fetch_base_map(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    *,
    timeout_s: float = 45.0,
) -> bytes:
    minx, miny, maxx, maxy = bounds
    params = {
        "bbox": f"{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}",
        "bboxSR": "3857",
        "imageSR": "3857",
        "size": f"{width},{height}",
        "format": "jpg",
        "f": "image",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15.0)) as client:
            response = await client.get(
                _EXPORT_URL,
                params=params,
                headers={"User-Agent": "sidequest/0.1 route-review"},
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - caller decides whether to skip review
        raise MapImageError(f"Kartenbild konnte nicht geladen werden: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise MapImageError(f"Kartenbild hat unerwarteten Typ: {content_type or 'unbekannt'}")
    return response.content


def _pixel(points: list[tuple[float, float]], bounds, width: int, height: int):
    minx, miny, maxx, maxy = bounds
    span_x = max(1.0, maxx - minx)
    span_y = max(1.0, maxy - miny)
    return [
        ((x - minx) / span_x * width, (maxy - y) / span_y * height)
        for x, y in points
    ]


def _nice_scale(target_px: int, meters_per_px: float) -> tuple[float, str]:
    target_m = max(1.0, target_px * meters_per_px)
    chosen: tuple[float, str] | None = None
    for km in (0.2, 0.5, 1, 2, 5, 10, 20, 50, 100):
        if km * 1000 <= target_m:
            chosen = (km * 1000, f"{km:g} km")
    if chosen is not None:
        return chosen
    return target_m, f"{target_m / 1000:.1f} km"


def _draw_scale_bar(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    bounds,
    *,
    font,
) -> str:
    minx, _miny, maxx, _maxy = bounds
    center_lat, _ = mercator_to_latlon((minx + maxx) / 2, 0)
    meters_per_px = (maxx - minx) / max(1, width) * math.cos(math.radians(center_lat))
    bar_m, label = _nice_scale(150, meters_per_px)
    bar_px = max(40, int(bar_m / max(0.0001, meters_per_px)))
    bar_px = min(bar_px, max(60, width // 4))

    x1 = width - 24 - bar_px
    y = height - 34
    segment = bar_px / 4
    for index in range(4):
        color = "#101a14" if index % 2 == 0 else "#eef3e7"
        draw.rectangle(
            [x1 + index * segment, y, x1 + (index + 1) * segment, y + 6],
            fill=color,
            outline="#101a14",
        )
    text = label
    text_w, _ = _text_size(draw, text, font)
    draw.text(
        (x1 + (bar_px - text_w) / 2, y - 20),
        text,
        font=font,
        fill="#eef3e7",
        stroke_width=2,
        stroke_fill="#101a14",
    )
    return label


def _draw_legend(
    base: Image.Image,
    *,
    bounds: tuple[float, float, float, float],
    distance_km: float,
    requested_distance_km: float | None,
    elevation_gain_m: float,
    bike_label: str,
    round_trip: bool,
    start_label: str,
    end_label: str,
    markers: list[MapMarker],
    title: str = "sidequest · Routenprüfung",
    headline: str | None = None,
    note: str | None = None,
) -> None:
    width, height = base.size
    title_font = _font(max(15, height // 52), bold=True)
    body_font = _font(max(12, height // 68))
    small_font = _font(max(11, height // 78))
    tiny_font = _font(max(9, height // 96))

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    lines: list[tuple[str, Any]] = [
        (
            headline or f"{distance_km:.1f} km · {elevation_gain_m:.0f} hm",
            body_font,
        ),
        (
            f"Wunsch: {requested_distance_km:.0f} km" if requested_distance_km else "Ohne Distanzwunsch",
            small_font,
        ),
        (
            f"{bike_label}{' · Rundtour' if round_trip else ''}",
            small_font,
        ),
    ]
    for marker in markers[:6]:
        lines.append((f"{marker.order}. {_fit_text(marker.name, 31)}", small_font))
    if note:
        lines.append((note, small_font))
    lines.append(("Karte © Esri, HERE, Garmin, USGS", tiny_font))

    legend_width = min(width - 32, 360)
    line_heights = [_text_size(draw, text, font)[1] + 7 for text, font in lines]
    legend_height = 48 + sum(line_heights) + 16
    x0, y0 = 16, height - legend_height - 16

    draw.rounded_rectangle(
        [x0, y0, x0 + legend_width, y0 + legend_height],
        radius=14,
        fill=(16, 26, 20, 244),
        outline=(238, 243, 231, 45),
        width=1,
    )
    draw.text((x0 + 16, y0 + 12), title, font=title_font, fill="#cdf56f")

    cursor_y = y0 + 42
    for text, font in lines:
        draw.text((x0 + 16, cursor_y), text, font=font, fill="#eef3e7")
        cursor_y += _text_size(draw, text, font)[1] + 7

    # Start/end caption at the top-left, kept short so it does not hide the map.
    caption = (
        f"Start: {_fit_text(start_label, 34)} · Ziel: {_fit_text(end_label, 34)}"
        if not round_trip
        else f"Rundtour ab {_fit_text(start_label, 42)}"
    )
    caption_font = _font(max(11, height // 74))
    caption_w, caption_h = _text_size(draw, caption, caption_font)
    draw.rounded_rectangle(
        [16, 16, 16 + caption_w + 24, 16 + caption_h + 18],
        radius=10,
        fill=(16, 26, 20, 238),
        outline=(238, 243, 231, 40),
        width=1,
    )
    draw.text((28, 25), caption, font=caption_font, fill="#eef3e7")

    scale_font = _font(max(10, height // 84), bold=True)
    _draw_scale_bar(draw, width, height, bounds, font=scale_font)

    # North arrow.
    nx, ny = width - 42, 42
    draw.polygon([(nx, ny - 20), (nx - 9, ny + 8), (nx, ny + 3), (nx + 9, ny + 8)], fill="#eef3e7")
    draw.text((nx, ny + 12), "N", font=scale_font, fill="#eef3e7", anchor="ma")

    base.alpha_composite(overlay)


def _draw_marker(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    label: str,
    *,
    fill: str,
    text_fill: str,
    radius: int,
    font,
) -> None:
    x, y = xy
    draw.ellipse(
        [x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2],
        fill="#f8fff2",
    )
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill)
    draw.text((x, y), label, font=font, fill=text_fill, anchor="mm")


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    *,
    fill: str,
    width: int = 4,
    dash: int = 14,
    gap: int = 10,
) -> None:
    """Draw a simple dashed polyline (Pillow has no native dash support)."""
    if len(points) < 2:
        return
    pattern = dash + gap
    carry = 0.0
    for start, end in zip(points, points[1:], strict=False):
        x1, y1 = start
        x2, y2 = end
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length
        pos = 0.0
        while pos < length:
            phase = (carry + pos) % pattern
            if phase < dash:
                run = min(dash - phase, length - pos)
                draw.line(
                    [
                        (x1 + ux * pos, y1 + uy * pos),
                        (x1 + ux * (pos + run), y1 + uy * (pos + run)),
                    ],
                    fill=fill,
                    width=width,
                )
                pos += run
            else:
                pos += min(pattern - phase, length - pos)
        carry = (carry + length) % pattern


def _draw_candidates(
    draw: ImageDraw.ImageDraw,
    candidates: list[MapCandidate],
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> None:
    font = _font(max(9, height // 96), bold=True)
    radius = max(8, height // 105)
    for candidate in candidates:
        mx, my = _pixel(
            [lonlat_to_mercator(candidate.lat, candidate.lon)], bounds, width, height
        )[0]
        _draw_marker(
            draw,
            (mx, my),
            str(candidate.order),
            fill="#17382a",
            text_fill="#d9ffb0",
            radius=radius,
            font=font,
        )


async def _base_map(bounds, width: int, height: int) -> Image.Image:
    raw = await _fetch_base_map(bounds, width, height)
    try:
        base = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise MapImageError(f"Kartenbild ist nicht lesbar: {exc}") from exc
    if base.size != (width, height):
        base = base.resize((width, height), Image.Resampling.LANCZOS)
    return base


def _map_size(settings: Settings) -> tuple[int, int]:
    width = max(600, min(2000, settings.route_review_image_width))
    height = max(450, min(2000, settings.route_review_image_height))
    return width, height


def _finalize_map(
    base: Image.Image,
    bounds: tuple[float, float, float, float],
    settings: Settings,
    *,
    prefix: str = "route-review",
) -> RouteMap:
    out = io.BytesIO()
    base.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)

    if settings.route_review_save_images:
        directory = os.path.abspath(settings.route_review_data_dir)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(
            directory, f"{prefix}-{int.from_bytes(os.urandom(4), 'big')}.jpg"
        )
        with open(path, "wb") as handle:
            handle.write(out.getvalue())

    return RouteMap(
        image=out.getvalue(),
        width=base.width,
        height=base.height,
        bounds=bounds,
    )


def _route_pixels(
    geometry: list[tuple[float, float]],
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> list[tuple[float, float]]:
    # Downsample very long tracks; drawing every point adds no visible detail.
    step = max(1, len(geometry) // 2500)
    sampled = [lonlat_to_mercator(lat, lon) for lat, lon in geometry[::step]]
    if sampled[-1] != lonlat_to_mercator(*geometry[-1]):
        sampled.append(lonlat_to_mercator(*geometry[-1]))
    return _pixel(sampled, bounds, width, height)


async def render_route_map(
    geometry: list[tuple[float, float]],
    markers: list[MapMarker],
    settings: Settings,
    *,
    distance_km: float,
    requested_distance_km: float | None = None,
    elevation_gain_m: float = 0.0,
    bike_label: str = "",
    round_trip: bool = False,
    start_label: str = "",
    end_label: str = "",
    candidates: list[MapCandidate] | None = None,
    title: str = "sidequest · Routenprüfung",
    headline: str | None = None,
) -> RouteMap:
    """Download the topo basemap and draw the route + highlights on it."""
    if not geometry:
        raise MapImageError("Keine Route für das Kartenbild vorhanden.")

    width, height = _map_size(settings)
    bounds = _map_bounds(geometry, width, height)
    base = await _base_map(bounds, width, height)
    pixels = _route_pixels(geometry, bounds, width, height)

    draw = ImageDraw.Draw(base)
    if len(pixels) > 1:
        draw.line(pixels, fill="#10301a", width=11, joint="curve")
        draw.line(pixels, fill="#a8e63c", width=5, joint="curve")

    if candidates:
        _draw_candidates(draw, candidates, bounds, width, height)

    start_font = _font(max(13, height // 64), bold=True)
    _draw_marker(
        draw,
        pixels[0],
        "S",
        fill="#2fd06a",
        text_fill="#08110c",
        radius=max(12, height // 60),
        font=start_font,
    )
    if not round_trip:
        _draw_marker(
            draw,
            pixels[-1],
            "Z",
            fill="#e45757",
            text_fill="#fff5f2",
            radius=max(12, height // 60),
            font=start_font,
        )

    marker_font = _font(max(10, height // 76), bold=True)
    for marker in markers:
        mx, my = _pixel([lonlat_to_mercator(marker.lat, marker.lon)], bounds, width, height)[0]
        _draw_marker(
            draw,
            (mx, my),
            str(marker.order),
            fill=_CATEGORY_COLORS.get(marker.category, _CATEGORY_COLORS["poi"]),
            text_fill="#ffffff",
            radius=max(10, height // 78),
            font=marker_font,
        )

    canvas = base.convert("RGBA")
    _draw_legend(
        canvas,
        bounds=bounds,
        distance_km=distance_km,
        requested_distance_km=requested_distance_km,
        elevation_gain_m=elevation_gain_m,
        bike_label=bike_label,
        round_trip=round_trip,
        start_label=start_label,
        end_label=end_label,
        markers=markers,
        title=title,
        headline=headline,
    )
    return _finalize_map(canvas, bounds, settings)


async def render_area_map(
    start: tuple[float, float],
    end: tuple[float, float],
    markers: list[MapMarker],
    settings: Settings,
    *,
    reference_path: list[tuple[float, float]],
    round_trip: bool = False,
    requested_distance_km: float | None = None,
    bike_label: str = "",
    start_label: str = "",
    end_label: str = "",
) -> RouteMap:
    """Overview map used by the vision model to draft the route.

    No route exists yet: only the requested corridor/loop and the candidate
    highlights are drawn, so the model can place new shaping points.
    """
    points = [start, end, *reference_path, *[(m.lat, m.lon) for m in markers]]
    width, height = _map_size(settings)
    bounds = _map_bounds(points, width, height)
    base = await _base_map(bounds, width, height)
    draw = ImageDraw.Draw(base)

    path_pixels = _route_pixels(reference_path or [start, end], bounds, width, height)
    _draw_dashed_line(draw, path_pixels, fill="#17382a", width=5)

    start_font = _font(max(13, height // 64), bold=True)
    start_xy = _pixel([lonlat_to_mercator(*start)], bounds, width, height)[0]
    end_xy = _pixel([lonlat_to_mercator(*end)], bounds, width, height)[0]
    _draw_marker(
        draw,
        start_xy,
        "S",
        fill="#2fd06a",
        text_fill="#08110c",
        radius=max(12, height // 60),
        font=start_font,
    )
    if not round_trip:
        _draw_marker(
            draw,
            end_xy,
            "Z",
            fill="#e45757",
            text_fill="#fff5f2",
            radius=max(12, height // 60),
            font=start_font,
        )

    marker_font = _font(max(10, height // 76), bold=True)
    for marker in markers:
        mx, my = _pixel([lonlat_to_mercator(marker.lat, marker.lon)], bounds, width, height)[0]
        _draw_marker(
            draw,
            (mx, my),
            str(marker.order),
            fill=_CATEGORY_COLORS.get(marker.category, _CATEGORY_COLORS["poi"]),
            text_fill="#ffffff",
            radius=max(10, height // 78),
            font=marker_font,
        )

    canvas = base.convert("RGBA")
    _draw_legend(
        canvas,
        bounds=bounds,
        distance_km=0.0,
        requested_distance_km=requested_distance_km,
        elevation_gain_m=0.0,
        bike_label=bike_label,
        round_trip=round_trip,
        start_label=start_label,
        end_label=end_label,
        markers=markers,
        title="sidequest · Routenentwurf",
        headline="Routenentwurf",
        note="Gestrichelt: gewünschter Korridor",
    )
    return _finalize_map(canvas, bounds, settings, prefix="route-draft")
