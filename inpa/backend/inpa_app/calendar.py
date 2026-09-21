"""Integrated monthly calendar for the owner and registered people."""

from __future__ import annotations

import calendar as month_calendar
from datetime import date

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import normalize_connection_id
from .db import get_engine
from .holidays import japanese_holidays
from .privacy import effective_fields, load_matrix, viewer_audience
from .restrictions import serialize_restriction

bp = Blueprint("calendar", __name__, url_prefix="/api/v1")


def _park_counts(entries: list[dict[str, object]]) -> dict[str, int]:
    return {
        park: sum(entry.get("park") == park for entry in entries)
        for park in ("both", "land", "sea", "undecided")
    }


@bp.get("/calendar")
def integrated_calendar():
    session, error = _require_session()
    if error:
        return error
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
        start = date(year, month, 1)
    except ValueError:
        return _error("invalid_month", "年月を確認してください。", 400)
    end = date(year, month, month_calendar.monthrange(year, month)[1])
    season_public_id = request.args.get("season_id", "").strip()
    person_value = request.args.get("person_id", "")
    person_connection_id = normalize_connection_id(person_value) if person_value else ""
    if person_value and not person_connection_id:
        return _error("invalid_person", "つながりIDを確認してください。", 400)
    myself_only = request.args.get("myself_only") == "1"
    viewer_id = int(session["user_id"])
    with get_engine().connect() as connection:
        season = None
        if season_public_id:
            season = connection.execute(
                text("SELECT id,public_id,name FROM seasons WHERE public_id=:id AND is_active=1"),
                {"id": season_public_id},
            ).mappings().first()
            if not season:
                return _error("invalid_season", "シーズンを確認してください。", 400)
        rows = connection.execute(
            text(
                "SELECT v.user_id,u.public_id AS user_public_id,u.display_name,"
                "u.x_handle,u.instagram_handle,u.x_handle_visible,u.instagram_handle_visible,"
                "v.visit_date,v.park,v.costume,v.memo "
                "FROM visits v JOIN users u ON u.id=v.user_id JOIN seasons s ON s.id=v.season_id "
                "WHERE s.is_active=1 AND (:season IS NULL OR v.season_id=:season) "
                "AND v.visit_date BETWEEN :start AND :end "
                "AND (v.user_id=:viewer OR ("
                ":myself_only=0 AND v.user_id IN (SELECT followed_user_id FROM follows WHERE follower_user_id=:viewer))) "
                "AND (:person_id='' OR u.connection_id=:person_id) "
                "ORDER BY v.visit_date,u.display_name,v.id"
            ),
            {"season": season["id"] if season else None, "start": start, "end": end, "viewer": viewer_id,
             "myself_only": int(myself_only), "person_id": person_connection_id},
        ).mappings().all()
        follows = connection.execute(
            text(
                "SELECT follower_user_id,followed_user_id FROM follows WHERE "
                "follower_user_id=:viewer OR followed_user_id=:viewer"
            ),
            {"viewer": viewer_id},
        ).all()
        blocks = connection.execute(
            text("SELECT blocker_user_id,blocked_user_id FROM blocks WHERE blocker_user_id=:viewer OR blocked_user_id=:viewer"),
            {"viewer": viewer_id},
        ).all()
        restrictions = connection.execute(
            text(
                "SELECT r.public_id,r.name,r.restriction_type,r.start_date,r.end_date,"
                "r.park_scope,r.description,r.enforcement FROM restriction_periods r "
                "JOIN seasons s ON s.id=r.season_id WHERE s.is_active=1 "
                "AND (:season IS NULL OR r.season_id=:season) AND r.is_active=1 "
                "AND r.start_date<=:end AND r.end_date>=:start ORDER BY r.start_date,r.id"
            ),
            {"season": season["id"] if season else None, "start": start, "end": end},
        ).mappings().all()
        privacy_matrices = {
            owner_id: load_matrix(connection, owner_id)
            for owner_id in {int(row["user_id"]) for row in rows}
        }
    follow_pairs = {(int(row[0]), int(row[1])) for row in follows}
    block_pairs = {(int(row[0]), int(row[1])) for row in blocks}
    days: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        owner_id = int(row["user_id"])
        owner = owner_id == viewer_id
        blocked = (owner_id, viewer_id) in block_pairs or (viewer_id, owner_id) in block_pairs
        audience = viewer_audience(
            owner=owner, logged_in=True,
            owner_follows_viewer=(owner_id, viewer_id) in follow_pairs,
            viewer_follows_owner=(viewer_id, owner_id) in follow_pairs, blocked=blocked,
        )
        fields = effective_fields(privacy_matrices[owner_id], audience)
        if "date" not in fields:
            continue
        entry: dict[str, object] = {
            "user_public_id": row["user_public_id"], "display_name": row["display_name"],
        }
        for key in ("park", "costume", "memo"):
            if key in fields:
                entry[key] = row[key]
        if owner or (row["x_handle_visible"] and row["x_handle"]):
            entry["x_handle"] = row["x_handle"]
        if owner or (row["instagram_handle_visible"] and row["instagram_handle"]):
            entry["instagram_handle"] = row["instagram_handle"]
        days.setdefault(row["visit_date"].isoformat(), []).append(entry)
    result = []
    for day, entries in days.items():
        result.append({
            "date": day, "count": len(entries),
            "land_count": sum(entry.get("park") in {"land", "both"} for entry in entries),
            "sea_count": sum(entry.get("park") in {"sea", "both"} for entry in entries),
            "park_counts": _park_counts(entries),
            "entries": entries,
        })
    return jsonify(
        season={"public_id": season["public_id"], "name": season["name"]} if season else None,
        days=result,
        holidays=[
            {"date": holiday.isoformat(), "name": name}
            for holiday, name in japanese_holidays(year).items()
            if holiday.month == month
        ],
        restrictions=[serialize_restriction(row) for row in restrictions],
    )
