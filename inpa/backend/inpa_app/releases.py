"""Published application updates and per-user dismissal state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import verify_csrf
from .db import get_engine

bp = Blueprint("releases", __name__, url_prefix="/api/v1/releases")
_JST = timezone(timedelta(hours=9), "JST")


def _release(row) -> dict[str, object]:
    result = dict(row)
    for key in ("published_at", "created_at", "updated_at"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                continue
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            result[key] = value.astimezone(_JST).isoformat()
    return result


def _published_sql() -> str:
    return (
        "SELECT public_id,version,version_major,version_minor,version_patch,change_type,title,"
        "content_markdown,published_at,created_at,updated_at FROM app_releases WHERE status='published' "
    )


@bp.get("")
def release_list():
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            _published_sql() + "ORDER BY version_major DESC,version_minor DESC,version_patch DESC"
        )).mappings().all()
    releases = [_release(row) for row in rows]
    return jsonify(current_version=releases[0]["version"] if releases else None, releases=releases)


@bp.get("/latest")
def latest_release():
    with get_engine().connect() as connection:
        row = connection.execute(text(
            _published_sql() + "ORDER BY version_major DESC,version_minor DESC,version_patch DESC LIMIT 1"
        )).mappings().first()
    return jsonify(current_version=row["version"] if row else None, release=_release(row) if row else None)


@bp.get("/unseen")
def unseen_release():
    session, error = _require_session()
    if error:
        return error
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            _published_sql() + "AND NOT EXISTS (SELECT 1 FROM user_release_dismissals d "
            "WHERE d.user_id=:user_id AND d.release_id=app_releases.id) "
            "ORDER BY version_major DESC,version_minor DESC,version_patch DESC"
        ), {"user_id": session["user_id"]}).mappings().all()
    return jsonify(unseen_count=len(rows), release=_release(rows[0]) if rows else None)


@bp.post("/<public_id>/dismiss")
def dismiss_release(public_id: str):
    session, error = _require_session()
    if error:
        return error
    if not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_engine().begin() as connection:
        selected = connection.execute(text(
            "SELECT version_major,version_minor,version_patch FROM app_releases "
            "WHERE public_id=:id AND status='published'"
        ), {"id": public_id}).mappings().first()
        if not selected:
            return _error("not_found", "アップデート情報が見つかりません。", 404)
        insert = "INSERT OR IGNORE" if connection.dialect.name == "sqlite" else "INSERT IGNORE"
        connection.execute(text(
            f"{insert} INTO user_release_dismissals (user_id,release_id,dismissed_at) "
            "SELECT :user_id,id,:now FROM app_releases WHERE status='published' AND "
            "(version_major<:major OR (version_major=:major AND version_minor<:minor) OR "
            "(version_major=:major AND version_minor=:minor AND version_patch<=:patch))"
        ), {"user_id": session["user_id"], "now": now, "major": selected["version_major"],
             "minor": selected["version_minor"], "patch": selected["version_patch"]})
    return "", 204
