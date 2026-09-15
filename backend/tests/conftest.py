"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.routing.base import RouteRequest, RouteResult, RoutingError


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        brouter_url="http://brouter.test",
        ors_api_key=None,
        graphhopper_api_key=None,
        deepseek_api_key=None,
        http_timeout_s=5.0,
    )


@pytest.fixture
def session(settings: Settings):
    engine = create_db_engine(settings)
    init_db(engine)
    with Session(engine) as db_session:
        yield db_session


class MockProvider:
    """Deterministic routing provider for tests."""

    def __init__(self, name: str = "mock", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[RouteRequest] = []

    async def route(self, req: RouteRequest) -> RouteResult:
        self.calls.append(req)
        if self.fail:
            raise RoutingError(f"{self.name} down")
        geometry = [(lat, lon) for lat, lon in req.waypoints]
        return RouteResult(
            geometry=geometry,
            distance_km=42.0,
            elevation_gain_m=500.0,
            elevation_loss_m=480.0,
            provider=self.name,
            profile="mock",
            elevations=[100.0 + i for i in range(len(geometry))],
        )
