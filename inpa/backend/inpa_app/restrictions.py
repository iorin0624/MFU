"""Public warning-only restriction period lookup."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error
from .db import get_engine

bp = Blueprint("restrictions", __name__, url_prefix="/api/v1")


def matching_restrictions(connection, season_public_id: str, visit_date: date, park: str | None):
    return connection.execute(
        text(
            "SELECT r.public_id,r.name,r.restriction_type,r.start_date,r.end_date,"
            "r.park_scope,r.description,r.enforcement FROM restriction_periods r "
            "JOIN seasons s ON s.id=r.season_id WHERE s.public_id=:season "
            "AND r.is_active=1 AND :visit_date BETWEEN r.start_date AND r.end_date "
            "AND (r.park_scope='all' OR r.park_scope=:park) ORDER BY r.start_date,r.id"
        ),
        {"season": season_public_id, "visit_date": visit_date, "park": park or ""},
    ).mappings().all()


def serialize_restriction(row) -> dict[str, object]:
    value = dict(row)
    value["start_date"] = value["start_date"].isoformat()
    value["end_date"] = value["end_date"].isoformat()
    return value


@bp.get("/restrictions")
def restrictions():
    season = request.args.get("season_id", "")
    date_value = request.args.get("date", "")
    park = request.args.get("park") or None
    if not season or not date_value:
        return _error("invalid_request", "シーズンと日付を指定してください。", 400)
    try:
        visit_date = date.fromisoformat(date_value)
    except ValueError:
        return _error("invalid_request", "日付を確認してください。", 400)
    if park not in {None, "land", "sea", "both", "undecided"}:
        return _error("invalid_request", "パークを確認してください。", 400)
    with get_engine().connect() as connection:
        rows = matching_restrictions(connection, season, visit_date, park)
        if park == "both":
            extra = connection.execute(
                text(
                    "SELECT r.public_id,r.name,r.restriction_type,r.start_date,r.end_date,"
                    "r.park_scope,r.description,r.enforcement FROM restriction_periods r "
                    "JOIN seasons s ON s.id=r.season_id WHERE s.public_id=:season AND r.is_active=1 "
                    "AND :visit_date BETWEEN r.start_date AND r.end_date AND r.park_scope IN ('land','sea') "
                    "ORDER BY r.start_date,r.id"
                ),
                {"season": season, "visit_date": visit_date},
            ).mappings().all()
            known = {row["public_id"] for row in rows}
            rows = [*rows, *(row for row in extra if row["public_id"] not in known)]
    return jsonify(restrictions=[serialize_restriction(row) for row in rows])
