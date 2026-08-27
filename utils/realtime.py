"""Small, dependency-light helpers for MFU realtime progress events."""

from __future__ import annotations

from typing import Any


def emit_admin_event(event: str, payload: dict[str, Any], *, room: str) -> None:
    """Emit through the shared Redis-backed Socket.IO queue.

    Progress persistence remains authoritative.  Realtime delivery is best effort,
    so a temporary Redis/Socket.IO problem must never fail the underlying job.
    """
    try:
        from app.chat.socketio_ext import socketio

        socketio.emit(event, payload, namespace="/admin-system", room=room)
    except Exception:
        return


def emit_download_event(event: str, payload: dict[str, Any], *, room: str) -> None:
    try:
        from app.chat.socketio_ext import socketio

        socketio.emit(event, payload, namespace="/download-progress", room=room)
    except Exception:
        return

