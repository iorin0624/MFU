"""Socket.IO deployment settings shared by the HTTP and dedicated processes."""

from __future__ import annotations

import importlib
import os


def dedicated_mode() -> bool:
    return os.environ.get("MFU_SOCKETIO_DEDICATED", "").strip().lower() in {"1", "true", "yes", "on"}


def queue_required() -> bool:
    return dedicated_mode() or os.environ.get("SOCKETIO_QUEUE_REQUIRED", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_message_queue(logger) -> str | None:
    """Fail startup in split mode instead of silently losing cross-process emits."""

    queue_url = (os.environ.get("SOCKETIO_MESSAGE_QUEUE") or "").strip()
    if not queue_url:
        if queue_required():
            raise RuntimeError("SOCKETIO_MESSAGE_QUEUE is required for split Socket.IO mode")
        return None
    try:
        redis = importlib.import_module("redis")
        redis.Redis.from_url(queue_url, socket_connect_timeout=1, socket_timeout=1).ping()
    except Exception as exc:
        if queue_required():
            raise RuntimeError("Socket.IO Redis queue is unavailable; refusing local-only mode") from exc
        logger.warning("SOCKETIO_MESSAGE_QUEUE unavailable; fallback to local mode (%s)", type(exc).__name__)
        return None
    return queue_url


def allowed_origins() -> list[str] | str:
    raw = (os.environ.get("SOCKETIO_CORS_ALLOWED_ORIGINS") or "").strip()
    origins = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    if dedicated_mode() and (not origins or "*" in origins):
        raise RuntimeError("Dedicated Socket.IO requires explicit SOCKETIO_CORS_ALLOWED_ORIGINS")
    return origins or "*"
