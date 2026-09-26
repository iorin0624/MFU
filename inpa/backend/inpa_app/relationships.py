"""User lookup, follow, and block APIs used by relationship-based visibility."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import (
    RegistrationError,
    normalize_connection_id,
    normalize_social_handle,
    verify_csrf,
)
from .db import get_engine

bp = Blueprint("relationships", __name__, url_prefix="/api/v1")


def _session(*, csrf: bool = False):
    session, error = _require_session()
    if error:
        return None, error
    if csrf and not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return None, _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    return session, None


def _target(connection, public_id: str):
    return connection.execute(
        text(
            "SELECT id, public_id, connection_id, display_name, x_handle, instagram_handle, "
            "x_handle_visible, instagram_handle_visible FROM users "
            "WHERE public_id=:public_id AND status='active'"
        ),
        {"public_id": public_id},
    ).mappings().first()


def _target_by_connection_id(connection, connection_id: str):
    return connection.execute(
        text(
            "SELECT id, public_id, connection_id, display_name, x_handle, instagram_handle, "
            "x_handle_visible, instagram_handle_visible FROM users "
            "WHERE connection_id=:connection_id AND status='active'"
        ),
        {"connection_id": connection_id},
    ).mappings().first()


def _normalize_social_query(value: str) -> str:
    for service in ("x", "instagram"):
        try:
            _, normalized = normalize_social_handle(value, service)
        except RegistrationError:
            continue
        if normalized:
            return normalized
    return ""


def _search_targets(connection, query: str):
    connection_id = normalize_connection_id(query)
    social_id = _normalize_social_query(query)
    if not connection_id and not social_id:
        return []
    return connection.execute(
        text(
            "SELECT id,public_id,connection_id,display_name,x_handle,instagram_handle,"
            "x_handle_visible,instagram_handle_visible FROM users WHERE status='active' AND ("
            "(:connection_id<>'' AND connection_id=:connection_id) OR "
            "(:social_id<>'' AND x_handle_visible=1 AND x_handle_normalized=:social_id) OR "
            "(:social_id<>'' AND instagram_handle_visible=1 "
            "AND instagram_handle_normalized=:social_id)) "
            "ORDER BY CASE WHEN connection_id=:connection_id AND :connection_id<>'' THEN 0 "
            "WHEN x_handle_normalized=:social_id AND x_handle_visible=1 THEN 1 ELSE 2 END,id "
            "LIMIT 3"
        ),
        {"connection_id": connection_id, "social_id": social_id},
    ).mappings().all()


def _consume_lookup_limit(connection, owner_id: int) -> bool:
    """Allow at most ten connection-ID lookups per user/IP in each minute."""
    remote = request.remote_addr or "unknown"
    bucket_key = hashlib.sha256(f"{owner_id}:{remote}".encode()).hexdigest()
    window = datetime.now(UTC).replace(second=0, microsecond=0, tzinfo=None)
    params = {
        "bucket": bucket_key, "action": "connection_lookup", "window": window,
        "seconds": 60,
    }
    if connection.dialect.name == "sqlite":
        connection.execute(text(
            "INSERT INTO rate_limit_counters "
            "(bucket_key,action_name,window_started_at,window_seconds,request_count) "
            "VALUES (:bucket,:action,:window,:seconds,1) "
            "ON CONFLICT(bucket_key,action_name,window_started_at) "
            "DO UPDATE SET request_count=request_count+1"
        ), params)
    else:
        connection.execute(text(
            "INSERT INTO rate_limit_counters "
            "(bucket_key,action_name,window_started_at,window_seconds,request_count) "
            "VALUES (:bucket,:action,:window,:seconds,1) "
            "ON DUPLICATE KEY UPDATE request_count=request_count+1"
        ), params)
    count = connection.execute(text(
        "SELECT request_count FROM rate_limit_counters WHERE bucket_key=:bucket "
        "AND action_name=:action AND window_started_at=:window"
    ), params).scalar_one()
    return int(count) <= 10


def _person_result(connection, owner_id: int, user):
    target_id = int(user["id"])
    blocked = connection.execute(
        text(
            "SELECT 1 FROM blocks WHERE "
            "(blocker_user_id=:owner AND blocked_user_id=:target) OR "
            "(blocker_user_id=:target AND blocked_user_id=:owner) LIMIT 1"
        ),
        {"owner": owner_id, "target": target_id},
    ).first()
    if blocked:
        return None
    follows = connection.execute(
        text(
            "SELECT follower_user_id, followed_user_id FROM follows WHERE "
            "(follower_user_id=:owner AND followed_user_id=:target) OR "
            "(follower_user_id=:target AND followed_user_id=:owner)"
        ),
        {"owner": owner_id, "target": target_id},
    ).all()
    pairs = {(int(row[0]), int(row[1])) for row in follows}
    result = {
        "public_id": user["public_id"], "connection_id": user["connection_id"],
        "display_name": user["display_name"],
        "following": (owner_id, target_id) in pairs, "follows_me": (target_id, owner_id) in pairs,
    }
    if user["x_handle_visible"] and user["x_handle"]:
        result["x_handle"] = user["x_handle"]
    if user["instagram_handle_visible"] and user["instagram_handle"]:
        result["instagram_handle"] = user["instagram_handle"]
    return result


@bp.get("/people/by-connection-id/<connection_id>")
def person_by_connection_id(connection_id: str):
    session, error = _session()
    if error:
        return error
    normalized = normalize_connection_id(connection_id)
    owner_id = int(session["user_id"])
    with get_engine().begin() as connection:
        if not _consume_lookup_limit(connection, owner_id):
            return _error("rate_limited", "検索回数が多すぎます。しばらく待ってからお試しください。", 429)
        user = _target_by_connection_id(connection, normalized) if normalized else None
        result = _person_result(connection, owner_id, user) if user else None
    if not result:
        return _error("not_found", "利用者が見つかりません。", 404)
    return jsonify(person=result)


@bp.get("/people/search")
def search_people():
    session, error = _session()
    if error:
        return error
    query = request.args.get("q", "").strip()
    if not query:
        return _error("invalid_query", "検索するIDを入力してください。", 400)
    owner_id = int(session["user_id"])
    with get_engine().begin() as connection:
        if not _consume_lookup_limit(connection, owner_id):
            return _error("rate_limited", "検索回数が多すぎます。しばらく待ってからお試しください。", 429)
        people = [
            result
            for user in _search_targets(connection, query)
            if (result := _person_result(connection, owner_id, user)) is not None
        ]
    return jsonify(people=people)


@bp.get("/people/<public_id>")
def person(public_id: str):
    session, error = _session()
    if error:
        return error
    owner_id = int(session["user_id"])
    with get_engine().connect() as connection:
        user = _target(connection, public_id)
        if not user:
            return _error("not_found", "利用者が見つかりません。", 404)
        result = _person_result(connection, owner_id, user)
        if not result:
            return _error("not_found", "利用者が見つかりません。", 404)
    return jsonify(person=result)


@bp.get("/follows")
def list_follows():
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT u.public_id,u.connection_id,u.display_name,u.x_handle,u.instagram_handle,"
                "u.x_handle_visible,u.instagram_handle_visible,"
                "EXISTS(SELECT 1 FROM follows reverse_follow "
                "WHERE reverse_follow.follower_user_id=f.followed_user_id "
                "AND reverse_follow.followed_user_id=f.follower_user_id) AS mutual "
                "FROM follows f JOIN users u ON u.id=f.followed_user_id "
                "WHERE f.follower_user_id=:id ORDER BY u.display_name,u.id"
            ),
            {"id": session["user_id"]},
        ).mappings().all()
    people = []
    for row in rows:
        item = {
            "public_id": row["public_id"], "connection_id": row["connection_id"],
            "display_name": row["display_name"], "mutual": bool(row["mutual"]),
        }
        if row["x_handle_visible"] and row["x_handle"]:
            item["x_handle"] = row["x_handle"]
        if row["instagram_handle_visible"] and row["instagram_handle"]:
            item["instagram_handle"] = row["instagram_handle"]
        people.append(item)
    return jsonify(people=people)


@bp.get("/followers")
def list_followers():
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT u.public_id,u.connection_id,u.display_name,u.x_handle,u.instagram_handle,"
                "u.x_handle_visible,u.instagram_handle_visible,"
                "EXISTS(SELECT 1 FROM follows owner_follow "
                "WHERE owner_follow.follower_user_id=f.followed_user_id "
                "AND owner_follow.followed_user_id=f.follower_user_id) AS mutual "
                "FROM follows f JOIN users u ON u.id=f.follower_user_id "
                "WHERE f.followed_user_id=:id AND u.status='active' ORDER BY u.display_name,u.id"
            ),
            {"id": session["user_id"]},
        ).mappings().all()
    people = []
    for row in rows:
        item = {
            "public_id": row["public_id"], "connection_id": row["connection_id"],
            "display_name": row["display_name"], "mutual": bool(row["mutual"]),
            "following": bool(row["mutual"]), "follows_me": True,
        }
        if row["x_handle_visible"] and row["x_handle"]:
            item["x_handle"] = row["x_handle"]
        if row["instagram_handle_visible"] and row["instagram_handle"]:
            item["instagram_handle"] = row["instagram_handle"]
        people.append(item)
    return jsonify(people=people)


@bp.post("/follows/<public_id>")
def follow(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    owner_id = int(session["user_id"])
    with get_engine().begin() as connection:
        user = _target(connection, public_id)
        if not user:
            return _error("not_found", "利用者が見つかりません。", 404)
        target_id = int(user["id"])
        if target_id == owner_id:
            return _error("invalid_target", "自分自身は登録できません。", 400)
        blocked = connection.execute(
            text(
                "SELECT 1 FROM blocks WHERE "
                "(blocker_user_id=:owner AND blocked_user_id=:target) OR "
                "(blocker_user_id=:target AND blocked_user_id=:owner) LIMIT 1"
            ),
            {"owner": owner_id, "target": target_id},
        ).first()
        if blocked:
            return _error("blocked", "この利用者は登録できません。", 409)
        connection.execute(
            text("INSERT IGNORE INTO follows (follower_user_id,followed_user_id) VALUES (:owner,:target)"),
            {"owner": owner_id, "target": target_id},
        )
    return jsonify(following=True)


@bp.delete("/follows/<public_id>")
def unfollow(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    with get_engine().begin() as connection:
        user = _target(connection, public_id)
        if user:
            connection.execute(
                text("DELETE FROM follows WHERE follower_user_id=:owner AND followed_user_id=:target"),
                {"owner": session["user_id"], "target": user["id"]},
            )
    return "", 204


@bp.get("/blocks")
def list_blocks():
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT u.public_id,u.connection_id,u.display_name FROM blocks b "
                "JOIN users u ON u.id=b.blocked_user_id "
                "WHERE b.blocker_user_id=:id ORDER BY u.display_name,u.id"
            ),
            {"id": session["user_id"]},
        ).mappings().all()
    return jsonify(people=[dict(row) for row in rows])


@bp.post("/blocks/<public_id>")
def block(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    owner_id = int(session["user_id"])
    with get_engine().begin() as connection:
        user = _target(connection, public_id)
        if not user:
            return _error("not_found", "利用者が見つかりません。", 404)
        target_id = int(user["id"])
        if target_id == owner_id:
            return _error("invalid_target", "自分自身はブロックできません。", 400)
        connection.execute(
            text("INSERT IGNORE INTO blocks (blocker_user_id,blocked_user_id) VALUES (:owner,:target)"),
            {"owner": owner_id, "target": target_id},
        )
        connection.execute(
            text(
                "DELETE FROM follows WHERE "
                "(follower_user_id=:owner AND followed_user_id=:target) OR "
                "(follower_user_id=:target AND followed_user_id=:owner)"
            ),
            {"owner": owner_id, "target": target_id},
        )
    return jsonify(blocked=True)


@bp.delete("/blocks/<public_id>")
def unblock(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    with get_engine().begin() as connection:
        user = _target(connection, public_id)
        if user:
            connection.execute(
                text("DELETE FROM blocks WHERE blocker_user_id=:owner AND blocked_user_id=:target"),
                {"owner": session["user_id"], "target": user["id"]},
            )
    return "", 204
