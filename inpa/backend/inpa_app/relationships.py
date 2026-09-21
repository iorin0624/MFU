"""User lookup, follow, and block APIs used by relationship-based visibility."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import verify_csrf
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
            "SELECT id, public_id, display_name, x_handle, instagram_handle, "
            "x_handle_visible, instagram_handle_visible FROM users "
            "WHERE public_id=:public_id AND status='active'"
        ),
        {"public_id": public_id},
    ).mappings().first()


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
            return _error("not_found", "利用者が見つかりません。", 404)
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
        "public_id": user["public_id"], "display_name": user["display_name"],
        "following": (owner_id, target_id) in pairs, "follows_me": (target_id, owner_id) in pairs,
    }
    if user["x_handle_visible"] and user["x_handle"]:
        result["x_handle"] = user["x_handle"]
    if user["instagram_handle_visible"] and user["instagram_handle"]:
        result["instagram_handle"] = user["instagram_handle"]
    return jsonify(person=result)


@bp.get("/follows")
def list_follows():
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT u.public_id,u.display_name,u.x_handle,u.instagram_handle,"
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
        item = {"public_id": row["public_id"], "display_name": row["display_name"], "mutual": bool(row["mutual"])}
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
                "SELECT u.public_id,u.display_name FROM blocks b JOIN users u ON u.id=b.blocked_user_id "
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
