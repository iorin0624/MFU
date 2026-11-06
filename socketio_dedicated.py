"""Gunicorn entry point for the dedicated Socket.IO process.

Only Engine.IO traffic is admitted here. All normal Flask URLs remain on the
existing HTTP Gunicorn service. Keep a single worker: Engine.IO polling needs
all requests for a session to reach the same process.
"""

from __future__ import annotations

import os


if os.environ.get("MFU_SOCKETIO_DEDICATED", "").strip().lower() not in {"1", "true", "yes", "on"}:
    raise RuntimeError("MFU_SOCKETIO_DEDICATED=1 is required for this entry point")

from app import app  # noqa: E402
from app.utils.socketio_wsgi_gate import SocketIOOnlyWSGI

if not app.secret_key:
    raise RuntimeError("Socket.IO service needs the same SECRET_KEY as the HTTP service")


def _health_status() -> bool:
    try:
        import redis

        url = app.config["SOCKETIO_MESSAGE_QUEUE"]
        return bool(redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1).ping())
    except Exception:
        return False


application = SocketIOOnlyWSGI(app.wsgi_app, _health_status)
