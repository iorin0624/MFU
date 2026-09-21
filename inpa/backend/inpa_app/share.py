"""Share-link lifecycle and server-side visibility filtering."""

from __future__ import annotations

import secrets
from datetime import time, timedelta

import segno
from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import get_session, hash_secret, new_public_id, verify_csrf
from .db import get_engine
from .privacy import effective_fields, load_matrix, viewer_audience

bp = Blueprint("share", __name__, url_prefix="/api/v1")
TOKEN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
VISIBLE_FIELDS = {
    "date": (), "park": ("park",), "memo": ("park", "memo"),
    "full": ("park", "arrival_time", "costume", "memo"),
}


def _cipher() -> Fernet:
    return Fernet(current_app.config["MAIL_ENCRYPTION_KEY"].encode("ascii"))


def _share_artifacts(token: str) -> dict[str, str]:
    short_url = f"{current_app.config['PUBLIC_ORIGIN'].rstrip('/')}/{token}"
    return {
        "url": short_url,
        "qr_svg": segno.make_qr(short_url, error="h").svg_inline(scale=4),
    }


def _time_text(value: time | timedelta | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return value.isoformat()


def visibility_allows(
    visibility: str, *, owner: bool, has_link: bool, logged_in: bool,
    owner_follows_viewer: bool, viewer_follows_owner: bool, blocked: bool,
) -> bool:
    """Evaluate the documented sharing policy without database access."""
    if owner:
        return True
    if blocked or visibility == "private":
        return False
    if visibility == "link":
        return has_link
    if visibility == "logged_in":
        return logged_in
    if visibility == "following":
        return logged_in and owner_follows_viewer
    if visibility == "mutual":
        return logged_in and owner_follows_viewer and viewer_follows_owner
    return False


def _viewer_session():
    return get_session(request.cookies.get(current_app.config["SESSION_COOKIE_NAME"]))


def _relationship(connection, owner_id: int, viewer_id: int | None) -> tuple[bool, bool, bool]:
    if not viewer_id or viewer_id == owner_id:
        return False, False, False
    blocked = connection.execute(
        text(
            "SELECT 1 FROM blocks WHERE "
            "(blocker_user_id=:owner AND blocked_user_id=:viewer) OR "
            "(blocker_user_id=:viewer AND blocked_user_id=:owner) LIMIT 1"
        ),
        {"owner": owner_id, "viewer": viewer_id},
    ).first()
    follows = connection.execute(
        text(
            "SELECT follower_user_id, followed_user_id FROM follows WHERE "
            "(follower_user_id=:owner AND followed_user_id=:viewer) OR "
            "(follower_user_id=:viewer AND followed_user_id=:owner)"
        ),
        {"owner": owner_id, "viewer": viewer_id},
    ).all()
    pairs = {(int(row[0]), int(row[1])) for row in follows}
    return bool(blocked), (owner_id, viewer_id) in pairs, (viewer_id, owner_id) in pairs


def _render_visits(owner_id: int, rows, viewer_id: int | None, relationship, privacy_matrix):
    blocked, owner_follows, viewer_follows = relationship
    audience = viewer_audience(
        owner=viewer_id == owner_id,
        logged_in=viewer_id is not None,
        owner_follows_viewer=owner_follows,
        viewer_follows_owner=viewer_follows,
        blocked=blocked,
    )
    fields = effective_fields(privacy_matrix, audience)
    if not fields:
        return []
    result = []
    for row in rows:
        item = {}
        if "date" in fields:
            item["visit_date"] = row["visit_date"].isoformat()
        for key in ("park", "costume", "memo"):
            if key in fields:
                item[key] = row[key]
        if any(value not in (None, "") for value in item.values()):
            result.append(item)
    return result


@bp.get("/share/<token>")
def share(token: str):
    token_hash = hash_secret(token)
    viewer = _viewer_session()
    viewer_id = int(viewer["user_id"]) if viewer else None
    with get_engine().begin() as connection:
        owner_id = connection.execute(
            text("SELECT user_id FROM share_tokens WHERE token_hash=:hash AND status='active'"),
            {"hash": token_hash},
        ).scalar()
        if not owner_id:
            return _error("not_found", "共有URLが無効です。", 404)
        owner_id = int(owner_id)
        connection.execute(
            text("UPDATE share_tokens SET last_used_at=UTC_TIMESTAMP(6) WHERE token_hash=:hash"),
            {"hash": token_hash},
        )
        user = connection.execute(
            text(
                "SELECT public_id,connection_id,display_name,x_handle,instagram_handle,"
                "x_handle_visible,instagram_handle_visible FROM users WHERE id=:id"
            ),
            {"id": owner_id},
        ).mappings().one()
        rows = connection.execute(
            text(
                "SELECT visit_date,park,costume,memo "
                "FROM visits WHERE user_id=:id ORDER BY visit_date"
            ),
            {"id": owner_id},
        ).mappings().all()
        relationship = _relationship(connection, owner_id, viewer_id)
        privacy_matrix = load_matrix(connection, owner_id)
    profile = {
        "public_id": user["public_id"], "connection_id": user["connection_id"],
        "display_name": user["display_name"],
    }
    if viewer_id == owner_id or (user["x_handle_visible"] and user["x_handle"]):
        profile["x_handle"] = user["x_handle"]
    if viewer_id == owner_id or (user["instagram_handle_visible"] and user["instagram_handle"]):
        profile["instagram_handle"] = user["instagram_handle"]
    return jsonify(
        profile=profile,
        visits=_render_visits(owner_id, rows, viewer_id, relationship, privacy_matrix),
    )


def _owner(*, csrf: bool = False):
    session, error = _require_session()
    if error:
        return None, error
    if csrf and not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return None, _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    return session, None


@bp.get("/share-token")
def share_token_status():
    session, error = _owner()
    if error:
        return error
    with get_engine().connect() as connection:
        connection_id = connection.execute(
            text("SELECT connection_id FROM users WHERE id=:id"),
            {"id": session["user_id"]},
        ).scalar_one()
        row = connection.execute(
            text(
                "SELECT token_last4, token_ciphertext, created_at, last_used_at FROM share_tokens "
                "WHERE user_id=:id AND status='active'"
            ),
            {"id": session["user_id"]},
        ).mappings().first()
    if not row:
        return jsonify(active=False, connection_id=connection_id)
    result = {
        "active": True, "connection_id": connection_id, "token_last4": row["token_last4"],
        "created_at": row["created_at"].isoformat(),
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    }
    if row["token_ciphertext"]:
        try:
            token = _cipher().decrypt(row["token_ciphertext"]).decode("ascii")
            result.update(_share_artifacts(token))
        except InvalidToken:
            pass
    return jsonify(result)


def _rotate_token(user_id: int):
    token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(20))
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "UPDATE share_tokens SET status='revoked', revoked_at=UTC_TIMESTAMP(6), "
                "revoke_reason='rotated' WHERE user_id=:id AND status='active'"
            ),
            {"id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO share_tokens (public_id,user_id,token_hash,token_last4,token_ciphertext) "
                "VALUES (:public_id,:user_id,:hash,:last4,:ciphertext)"
            ),
            {"public_id": new_public_id(), "user_id": user_id,
             "hash": hash_secret(token), "last4": token[-4:],
             "ciphertext": _cipher().encrypt(token.encode("ascii"))},
        )
    return jsonify(_share_artifacts(token)), 201


@bp.post("/share-token")
@bp.post("/share-token/rotate")
def create_or_rotate_share_token():
    session, error = _owner(csrf=True)
    if error:
        return error
    return _rotate_token(int(session["user_id"]))


@bp.delete("/share-token")
def revoke_share_token():
    session, error = _owner(csrf=True)
    if error:
        return error
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "UPDATE share_tokens SET status='revoked', revoked_at=UTC_TIMESTAMP(6), "
                "revoke_reason='user_revoked' WHERE user_id=:id AND status='active'"
            ),
            {"id": session["user_id"]},
        )
    return "", 204
