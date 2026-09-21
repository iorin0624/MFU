"""SQLAlchemy engine lifecycle. Domain repositories are added in later phases."""

from __future__ import annotations

from flask import Flask, current_app
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def init_app(app: Flask) -> None:
    app.config.setdefault("DB_POOL_PRE_PING", True)


def get_engine() -> Engine:
    engine = current_app.extensions.get("inpa_db_engine")
    if engine is None:
        engine = create_engine(
            current_app.config["DATABASE_URL"],
            pool_pre_ping=current_app.config["DB_POOL_PRE_PING"],
            future=True,
        )
        current_app.extensions["inpa_db_engine"] = engine
    return engine


def ping() -> None:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
