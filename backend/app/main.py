"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import Session, select

from .ai import route_shaping
from .config import get_settings
from .db import create_db_engine, init_db
from .geocoding import GeocodingError
from .gpx_export import build_gpx, gpx_filename
from .models import Highlight, PlanShapingPoint, RoutePlan
from .routing.base import BIKE_TYPE_LABELS, MTB_DIFFICULTY_LABELS, BikeType, MtbDifficulty
from .schemas import PlanForm
from .services.planner import PlanError, create_plan

BASE_DIR = Path(__file__).resolve().parent

settings = get_settings()


async def _lifespan(app: FastAPI):
    engine = create_db_engine(settings)
    init_db(engine)
    app.state.engine = engine
    yield


app = FastAPI(title="sidequest", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_BIKE_OPTIONS = [
    {"value": bike.value, "label": BIKE_TYPE_LABELS[bike]} for bike in BikeType
]
_MTB_OPTIONS = [
    {"value": diff.value, "label": MTB_DIFFICULTY_LABELS[diff]} for diff in MtbDifficulty
]


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "bike_options": _BIKE_OPTIONS,
        "mtb_options": _MTB_OPTIONS,
    }


def _render_template(name: str, request: Request, context: dict[str, Any]) -> str:
    return templates.get_template(name).render({"request": request, **context})


def _wants_stream(request: Request) -> bool:
    return "application/x-ndjson" in request.headers.get("accept", "")


def _ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _form_from_request(
    bike_type: str,
    mtb_difficulty: str | None,
    start: str,
    end: str | None,
    round_trip: bool,
    distance_km: float | None,
    heat: int,
    freetext: str | None,
) -> PlanForm:
    return PlanForm.model_validate(
        {
            "bike_type": bike_type,
            "mtb_difficulty": mtb_difficulty,
            "start": start,
            "end": end,
            "round_trip": round_trip,
            "distance_km": distance_km,
            "heat": heat,
            "freetext": freetext,
        }
    )


async def _plan_stream(request: Request, form: PlanForm) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def emit(payload: dict[str, Any]) -> None:
        await queue.put(payload)

    async def runner() -> None:
        try:
            with Session(request.app.state.engine) as session:
                plan = await create_plan(form, session, settings, on_event=emit)
            html = _render_template("partials/result.html", request, {"plan": plan})
            await queue.put({"type": "result", "html": html})
        except (PlanError, GeocodingError) as exc:
            html = _render_template("partials/error.html", request, {"error": str(exc)})
            await queue.put({"type": "error", "html": html})
        except Exception as exc:  # noqa: BLE001 - the UI needs a readable error
            html = _render_template(
                "partials/error.html",
                request,
                {"error": f"Unerwarteter Fehler bei der Routenplanung: {exc}"},
            )
            await queue.put({"type": "error", "html": html})

    task = asyncio.create_task(runner())
    try:
        while True:
            payload = await queue.get()
            yield _ndjson(payload)
            if payload.get("type") in ("result", "error"):
                break
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _single_event(payload: dict[str, Any]) -> AsyncIterator[str]:
    yield _ndjson(payload)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=_base_context(request),
    )


@app.post("/plan", response_class=HTMLResponse)
async def plan_route(
    request: Request,
    bike_type: str = Form(...),
    mtb_difficulty: str | None = Form(None),
    start: str = Form(...),
    end: str | None = Form(None),
    round_trip: bool = Form(False),
    distance_km: float | None = Form(None),
    heat: int = Form(50),
    freetext: str | None = Form(None),
):
    try:
        form = _form_from_request(
            bike_type, mtb_difficulty, start, end, round_trip, distance_km, heat, freetext
        )
    except ValidationError as exc:
        html = _render_template("partials/error.html", request, {"error": str(exc)})
        if _wants_stream(request):
            return StreamingResponse(
                _single_event({"type": "error", "html": html}),
                media_type="application/x-ndjson",
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/error.html",
            context={"request": request, "error": str(exc)},
        )

    if _wants_stream(request):
        return StreamingResponse(
            _plan_stream(request, form),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    with Session(request.app.state.engine) as session:
        try:
            plan = await create_plan(form, session, settings)
        except (PlanError, GeocodingError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="partials/error.html",
                context={"request": request, "error": str(exc)},
            )

    return templates.TemplateResponse(
        request=request,
        name="partials/result.html",
        context={"request": request, "plan": plan},
    )


@app.get("/plan/{plan_id}/gpx")
async def download_gpx(plan_id: int, session: Annotated[Session, Depends(get_session)]):
    plan = session.get(RoutePlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Route nicht gefunden")

    highlights = session.exec(
        select(Highlight)
        .where(Highlight.plan_id == plan_id)
        .order_by(Highlight.order_index)  # type: ignore[arg-type]
    ).all()

    shaping_rows = session.exec(
        select(PlanShapingPoint)
        .where(PlanShapingPoint.plan_id == plan_id)
        .order_by(PlanShapingPoint.order_index)  # type: ignore[arg-type]
    ).all()
    shaping_points = [
        {
            "lat": point.lat,
            "lon": point.lon,
            "elevation": point.elevation,
        }
        for point in shaping_rows
    ]
    if not shaping_points and settings.gpx_shaping_enabled:
        # Old plans (or plans created before this feature) still get a sparse
        # fallback instead of a dense track.
        candidates = route_shaping.build_shaping_candidates(
            [(lat, lon) for lat, lon in plan.geometry],
            plan.elevations,
            [
                {
                    "osm_id": highlight.osm_id,
                    "name": highlight.name,
                    "lat": highlight.lat,
                    "lon": highlight.lon,
                    "rationale": highlight.rationale,
                }
                for highlight in highlights
            ],
            spacing_km=max(
                3.0,
                settings.gpx_point_spacing_km,
                plan.distance_km / max(1, settings.gpx_max_points),
            ),
            max_points=settings.gpx_max_points,
            min_spacing_km=settings.gpx_min_point_spacing_km,
        )
        shaping_points = [
            {
                "lat": point.lat,
                "lon": point.lon,
                "elevation": point.elevation,
            }
            for point in route_shaping.fallback_shaping_points(candidates)
        ]

    name = f"sidequest {plan.start_label or ''} -> {plan.end_label or ''}".strip()
    gpx_xml = build_gpx(
        name=name,
        geometry=[(lat, lon) for lat, lon in plan.geometry],
        elevations=plan.elevations,
        highlights=[
            {
                "name": highlight.name,
                "lat": highlight.lat,
                "lon": highlight.lon,
                "description": highlight.rationale,
            }
            for highlight in highlights
        ],
        shaping_points=shaping_points,
    )
    return Response(
        content=gpx_xml,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{gpx_filename(name)}"'},
    )
