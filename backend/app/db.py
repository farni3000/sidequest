"""Database engine, session handling and table creation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from .config import Settings


def _prepare_sqlite_path(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url[len(prefix) :]
    if raw_path in ("", ":memory:"):
        return
    path = Path(raw_path)
    if str(path.parent) in ("", "."):
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(settings: Settings):
    database_url = settings.database_url
    _prepare_sqlite_path(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, echo=False)


def init_db(engine) -> None:
    # Importing models registers them on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def session_scope(engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def default_data_dir() -> str:
    return os.environ.get("SIDEQUEST_DATA_DIR", "./data")
