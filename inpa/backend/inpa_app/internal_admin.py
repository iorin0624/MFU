"""Internal API exposed only through the permission-restricted Unix socket."""

from __future__ import annotations

import re
from datetime import date

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .auth.service import new_public_id
from .db import get_engine

bp = Blueprint("internal_admin", __name__, url_prefix="/internal/admin/v1")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _error(message: str, status: int = 400):
    return jsonify(error={"message": message}), status


def _season(row):
    value = dict(row)
    value["start_date"] = value["start_date"].isoformat()
    value["end_date"] = value["end_date"].isoformat()
    value["is_active"] = bool(value["is_active"])
    return value


@bp.get("/bootstrap-status")
def bootstrap_status():
    return jsonify(service="inpa", status="internal_api_ready")


@bp.get("/seasons")
def list_seasons():
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT public_id,name,slug,start_date,end_date,is_active "
            "FROM seasons ORDER BY start_date DESC"
        )).mappings().all()
    return jsonify(seasons=[_season(row) for row in rows])


def _values(data: object, current: dict | None = None):
    if not isinstance(data, dict):
        raise TypeError("JSON形式で送信してください。")
    merged = {**(current or {}), **data}
    name, slug = merged.get("name"), merged.get("slug")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("シーズン名は1〜80文字で指定してください。")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("slugは英小文字・数字・ハイフンで指定してください。")
    try:
        start, end = date.fromisoformat(merged["start_date"]), date.fromisoformat(merged["end_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("開始日と終了日を指定してください。") from exc
    if start > end:
        raise ValueError("開始日は終了日以前にしてください。")
    active = merged.get("is_active", True)
    if not isinstance(active, bool):
        raise TypeError("is_activeはtrueまたはfalseで指定してください。")
    return {"name": name.strip(), "slug": slug, "start_date": start, "end_date": end, "is_active": active}


@bp.post("/seasons")
def create_season():
    try:
        values = _values(request.get_json(silent=True))
        public_id = new_public_id()
        with get_engine().begin() as connection:
            connection.execute(text(
                "INSERT INTO seasons (public_id,name,slug,start_date,end_date,is_active) "
                "VALUES (:public_id,:name,:slug,:start_date,:end_date,:is_active)"
            ), {**values, "public_id": public_id})
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except IntegrityError:
        return _error("同じslugのシーズンが存在します。", 409)
    return jsonify(public_id=public_id), 201


@bp.patch("/seasons/<public_id>")
def update_season(public_id: str):
    with get_engine().connect() as connection:
        row = connection.execute(text(
            "SELECT name,slug,start_date,end_date,is_active FROM seasons WHERE public_id=:id"
        ), {"id": public_id}).mappings().first()
    if not row:
        return _error("シーズンが見つかりません。", 404)
    current = _season(row)
    try:
        values = _values(request.get_json(silent=True), current)
        with get_engine().begin() as connection:
            connection.execute(text(
                "UPDATE seasons SET name=:name,slug=:slug,start_date=:start_date,"
                "end_date=:end_date,is_active=:is_active WHERE public_id=:public_id"
            ), {**values, "public_id": public_id})
    except (TypeError, ValueError) as exc:
        return _error(str(exc))
    except IntegrityError:
        return _error("同じslugのシーズンが存在します。", 409)
    return jsonify(public_id=public_id)
