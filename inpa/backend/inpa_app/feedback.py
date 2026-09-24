"""Authenticated user feedback endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import new_public_id, verify_csrf
from .db import get_engine
from .mfu_notification import send_feedback_notification

bp = Blueprint("feedback", __name__, url_prefix="/api/v1")
_CATEGORIES = {"bug", "feature", "usability", "wording", "other"}


def _source_path(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("送信元ページが長すぎます。")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or value.startswith("//"):
        raise ValueError("送信元ページが正しくありません。")
    return value


@bp.post("/feedback")
def create_feedback():
    session, error = _require_session()
    if error:
        return error
    if not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error("invalid_request", "JSON形式で送信してください。", 400)
    category = data.get("category")
    message = data.get("message")
    if category not in _CATEGORIES:
        return _error("invalid_request", "項目を選択してください。", 400)
    if not isinstance(message, str) or not 10 <= len(message.strip()) <= 2000:
        return _error("invalid_request", "内容は10〜2000文字で入力してください。", 400)
    try:
        source_path = _source_path(data.get("source_path"))
    except ValueError as exc:
        return _error("invalid_request", str(exc), 400)

    now = datetime.now(UTC).replace(tzinfo=None)
    public_id = new_public_id()
    user_agent = request.user_agent.string[:512] or None
    with get_engine().begin() as connection:
        lock = "" if connection.dialect.name == "sqlite" else " FOR UPDATE"
        sender = connection.execute(text(
            "SELECT public_id,connection_id,display_name,email_normalized FROM users WHERE id=:id" + lock
        ), {"id": session["user_id"]}).mappings().first()
        if not sender:
            return _error("authentication_required", "Sign in to continue.", 401)
        recent = connection.execute(text(
            "SELECT COUNT(*) FROM feedbacks WHERE user_id=:id AND created_at>=:threshold"
        ), {"id": session["user_id"], "threshold": now - timedelta(minutes=5)}).scalar_one()
        if int(recent) >= 3:
            return _error(
                "feedback_rate_limited", "短時間に送信できる上限に達しました。5分ほど待ってからお試しください。", 429
            )
        connection.execute(text(
            "INSERT INTO feedbacks (public_id,user_id,sender_public_id,sender_connection_id,"
            "sender_display_name,sender_email,category,message,source_path,user_agent,status,created_at,updated_at) "
            "VALUES (:public_id,:user_id,:sender_public_id,:sender_connection_id,:sender_display_name,"
            ":sender_email,:category,:message,:source_path,:user_agent,'new',:now,:now)"
        ), {
            "public_id": public_id, "user_id": session["user_id"],
            "sender_public_id": sender["public_id"], "sender_connection_id": sender["connection_id"],
            "sender_display_name": sender["display_name"], "sender_email": sender["email_normalized"],
            "category": category, "message": message.strip(), "source_path": source_path,
            "user_agent": user_agent, "now": now,
        })
    try:
        send_feedback_notification({
            "event_id": public_id,
            "sender_public_id": sender["public_id"],
            "sender_connection_id": sender["connection_id"],
            "sender_display_name": sender["display_name"],
            "sender_email": sender["email_normalized"],
            "category": category,
            "message": message.strip(),
            "source_path": source_path,
            "created_at": now.isoformat(),
        })
    except Exception:
        current_app.logger.exception("Could not deliver feedback notification to MFU")
    return jsonify(public_id=public_id, message="フィードバックを送信しました。"), 201
