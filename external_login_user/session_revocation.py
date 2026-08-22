from __future__ import annotations

import os
import threading
import time
from typing import Any

from flask import current_app

from app.utils.db import get_db

try:
    from redis import Redis
except Exception:  # pragma: no cover
    Redis = None


EXTERNAL_USER_STATUS_CACHE_TTL_SECONDS = max(
    int(os.getenv("EXTERNAL_USER_STATUS_CACHE_TTL_SECONDS", "10")),
    1,
)
EXTERNAL_USER_SOCKET_TTL_SECONDS = max(
    int(os.getenv("EXTERNAL_USER_SOCKET_TTL_SECONDS", str(7 * 24 * 60 * 60))),
    60,
)

_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: dict[int, tuple[bool, float]] = {}
_REDIS_LOCK = threading.Lock()
_REDIS_CLIENT: Any | None = None
_REDIS_INIT_ATTEMPTED = False


def _deleted_key(user_id: int) -> str:
    return f"mfu:external-user:deleted:{int(user_id)}"


def _socket_key(user_id: int) -> str:
    return f"mfu:external-user:sockets:{int(user_id)}"


def _get_redis_client():
    global _REDIS_CLIENT, _REDIS_INIT_ATTEMPTED
    if _REDIS_INIT_ATTEMPTED:
        return _REDIS_CLIENT
    with _REDIS_LOCK:
        if _REDIS_INIT_ATTEMPTED:
            return _REDIS_CLIENT
        _REDIS_INIT_ATTEMPTED = True
        redis_url = (
            current_app.config.get("SOCKETIO_MESSAGE_QUEUE")
            or os.getenv("SOCKETIO_MESSAGE_QUEUE")
            or os.getenv("CHAT_RATE_LIMIT_REDIS_URL")
            or ""
        ).strip()
        if not redis_url or Redis is None:
            return None
        try:
            client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            _REDIS_CLIENT = client
        except Exception:
            current_app.logger.warning(
                "external user revocation redis unavailable",
                exc_info=True,
            )
            _REDIS_CLIENT = None
        return _REDIS_CLIENT


def _cache_status(user_id: int, active: bool) -> None:
    expires_at = time.monotonic() + EXTERNAL_USER_STATUS_CACHE_TTL_SECONDS
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[int(user_id)] = (bool(active), expires_at)


def _cached_status(user_id: int) -> bool | None:
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get(int(user_id))
        if not cached:
            return None
        active, expires_at = cached
        if expires_at <= now:
            _STATUS_CACHE.pop(int(user_id), None)
            return None
        return bool(active)


def mark_external_user_deleted(user_id: int) -> None:
    uid = int(user_id or 0)
    if uid <= 0:
        return
    _cache_status(uid, False)
    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_client.set(_deleted_key(uid), "1")
        except Exception:
            current_app.logger.warning(
                "external user deleted marker write failed user_id=%s",
                uid,
                exc_info=True,
            )


def is_external_user_active(user_id: int, *, force_refresh: bool = False) -> bool:
    uid = int(user_id or 0)
    if uid <= 0:
        return False

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            if redis_client.exists(_deleted_key(uid)):
                _cache_status(uid, False)
                return False
        except Exception:
            current_app.logger.warning(
                "external user deleted marker read failed user_id=%s",
                uid,
                exc_info=True,
            )

    if not force_refresh:
        cached = _cached_status(uid)
        if cached is not None:
            return cached

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, COALESCE(is_deleted, 0) AS is_deleted
              FROM external_login_user
             WHERE id=%s
             LIMIT 1
            """,
            (uid,),
        )
        row = cur.fetchone() or {}
        active = bool(row) and int(row.get("is_deleted") or 0) == 0
    finally:
        cur.close()
        db.close()

    _cache_status(uid, active)
    if not active:
        mark_external_user_deleted(uid)
    return active


def register_external_user_socket(user_id: int, sid: str) -> None:
    uid = int(user_id or 0)
    socket_id = str(sid or "").strip()
    if uid <= 0 or not socket_id:
        return
    redis_client = _get_redis_client()
    if redis_client is None:
        return
    try:
        key = _socket_key(uid)
        redis_client.sadd(key, socket_id)
        redis_client.expire(key, EXTERNAL_USER_SOCKET_TTL_SECONDS)
    except Exception:
        current_app.logger.warning(
            "external user socket registration failed user_id=%s",
            uid,
            exc_info=True,
        )


def unregister_external_user_socket(user_id: int, sid: str) -> None:
    uid = int(user_id or 0)
    socket_id = str(sid or "").strip()
    if uid <= 0 or not socket_id:
        return
    redis_client = _get_redis_client()
    if redis_client is None:
        return
    try:
        redis_client.srem(_socket_key(uid), socket_id)
    except Exception:
        current_app.logger.warning(
            "external user socket unregister failed user_id=%s",
            uid,
            exc_info=True,
        )


def _external_user_socket_ids(user_id: int) -> list[str]:
    uid = int(user_id or 0)
    if uid <= 0:
        return []
    redis_client = _get_redis_client()
    if redis_client is None:
        return []
    try:
        return sorted(str(value) for value in (redis_client.smembers(_socket_key(uid)) or set()) if value)
    except Exception:
        current_app.logger.warning(
            "external user socket lookup failed user_id=%s",
            uid,
            exc_info=True,
        )
        return []


def revoke_external_user_sessions(
    user_id: int,
    *,
    message: str = "このアカウントは退会処理済みのためログアウトしました。",
) -> int:
    uid = int(user_id or 0)
    if uid <= 0:
        return 0

    mark_external_user_deleted(uid)

    from app.chat.socketio_ext import socketio

    socket_ids = _external_user_socket_ids(uid)
    payload = {
        "reason": "account_deleted",
        "message": message,
        "redirect": "/external-login/",
    }
    try:
        socketio.emit(
            "force_logout",
            payload,
            to=f"external_user:{uid}",
            namespace="/",
        )
    except Exception:
        current_app.logger.warning(
            "external user force logout emit failed user_id=%s",
            uid,
            exc_info=True,
        )

    disconnected = 0
    for sid in socket_ids:
        try:
            socketio.server.disconnect(sid, namespace="/", ignore_queue=False)
            disconnected += 1
        except Exception:
            current_app.logger.warning(
                "external user socket disconnect failed user_id=%s sid=%s",
                uid,
                sid,
                exc_info=True,
            )

    redis_client = _get_redis_client()
    if redis_client is not None:
        try:
            redis_client.delete(_socket_key(uid))
        except Exception:
            current_app.logger.warning(
                "external user socket cleanup failed user_id=%s",
                uid,
                exc_info=True,
            )

    current_app.logger.info(
        "external user sessions revoked user_id=%s sockets=%s",
        uid,
        disconnected,
    )
    return disconnected
