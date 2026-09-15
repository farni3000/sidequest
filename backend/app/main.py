"""FastAPI application entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import Session, select

from .config import get_settings
from .db import create_db_engine, init_db
from .geocoding import GeocodingError
from .gpx_export import build_gpx, gpx_filename
from .models import Highlight, RoutePlan
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
    session: Annotated[Session, Depends(get_session)],
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
        form = PlanForm.model_validate(
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
    except ValidationError as exc:
        return templates.TemplateResponse(
            request=request,
            name="partials/error.html",
            context={"request": request, "error": str(exc)},
        )

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
    )
    return Response(
        content=gpx_xml,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{gpx_filename(name)}"'},
    )
