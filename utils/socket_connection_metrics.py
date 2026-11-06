"""Cross-worker Socket.IO connection metrics.

Each worker refreshes the connections it owns in Redis.  Entries disappear
automatically when a worker dies because snapshots ignore stale timestamps.
No user, IP address, or session information is stored.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime
from typing import Any

from flask import current_app, request


_ZSET_KEY = "mfu:socket-connections:active"
_TRANSPORT_KEY = "mfu:socket-connections:transport"
_EVENTS_KEY = "mfu:socket-connections:events"
_STALE_AFTER_SECONDS = 75
_REFRESH_SECONDS = 20
_registry: dict[str, dict[str, str]] = {}
_registry_lock = threading.Lock()
_refresh_started = False
_event_counts: dict[str, int] = {}


def record_connection_event(kind: str, namespace: str = "/") -> None:
    """Count rejected connections and client-reported reconnects, without IDs."""

    if kind not in {"rejected", "reconnected"}:
        raise ValueError("unsupported Socket.IO metric event")
    key = f"{kind}:{namespace}"
    with _registry_lock:
        _event_counts[key] = _event_counts.get(key, 0) + 1
    client = _redis_client(current_app.config.get("SOCKETIO_MESSAGE_QUEUE"))
    if client is not None:
        try:
            client.hincrby(_EVENTS_KEY, key, 1)
        except Exception:
            current_app.logger.warning("Socket.IO metric Redis write failed", exc_info=True)


def _redis_client(queue_url: str | None):
    if not queue_url:
        return None
    try:
        import redis

        return redis.Redis.from_url(
            queue_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
    except Exception:
        return None


def _connection_details(socketio: Any, namespace: str) -> tuple[str, str]:
    sid = str(request.sid)
    try:
        eio_sid = str(socketio.server.manager.eio_sid_from_sid(sid, namespace) or sid)
    except Exception:
        eio_sid = sid
    connection_id = f"{socket.gethostname()}:{os.getpid()}:{eio_sid}"
    try:
        transport = str(socketio.server.eio.transport(eio_sid) or "unknown")
    except Exception:
        transport = str(request.args.get("transport") or "unknown")
    return connection_id, transport


def _member(connection_id: str, namespace: str) -> str:
    return f"{connection_id}|{namespace}"


def _refresh_redis(queue_url: str | None, items: dict[str, dict[str, str]]) -> None:
    client = _redis_client(queue_url)
    if client is None:
        return
    now = time.time()
    stale_members = client.zrangebyscore(
        _ZSET_KEY, "-inf", now - _STALE_AFTER_SECONDS
    )
    pipeline = client.pipeline(transaction=False)
    for member, details in items.items():
        pipeline.zadd(_ZSET_KEY, {member: now})
        pipeline.hset(_TRANSPORT_KEY, member, details.get("transport", "unknown"))
    pipeline.zremrangebyscore(_ZSET_KEY, "-inf", now - _STALE_AFTER_SECONDS)
    if stale_members:
        pipeline.hdel(_TRANSPORT_KEY, *stale_members)
    pipeline.execute()


def _refresh_loop(socketio: Any, queue_url: str | None) -> None:
    while True:
        with _registry_lock:
            items = {key: dict(value) for key, value in _registry.items()}
        # Re-read the Engine.IO transport so polling -> WebSocket upgrades are
        # reflected without relying on browser-specific client events.
        for details in items.values():
            try:
                details["transport"] = str(
                    socketio.server.eio.transport(details["eio_sid"]) or "unknown"
                )
            except Exception:
                pass
        try:
            _refresh_redis(queue_url, items)
        except Exception:
            pass
        socketio.sleep(_REFRESH_SECONDS)


def register_connection(socketio: Any, namespace: str) -> None:
    """Register the current Socket.IO connection after authentication."""

    global _refresh_started
    connection_id, transport = _connection_details(socketio, namespace)
    try:
        eio_sid = str(socketio.server.manager.eio_sid_from_sid(request.sid, namespace) or request.sid)
    except Exception:
        eio_sid = str(request.sid)
    member = _member(connection_id, namespace)
    with _registry_lock:
        _registry[member] = {
            "connection_id": connection_id,
            "eio_sid": eio_sid,
            "namespace": namespace,
            "transport": transport,
        }
        should_start = not _refresh_started
        if should_start:
            _refresh_started = True
    queue_url = current_app.config.get("SOCKETIO_MESSAGE_QUEUE")
    try:
        _refresh_redis(queue_url, {member: dict(_registry[member])})
    except Exception:
        current_app.logger.debug("Socket connection metric registration failed", exc_info=True)
    if should_start:
        socketio.start_background_task(_refresh_loop, socketio, queue_url)


def unregister_connection(socketio: Any, namespace: str) -> None:
    """Remove the current namespace connection immediately."""

    connection_id, _transport = _connection_details(socketio, namespace)
    member = _member(connection_id, namespace)
    with _registry_lock:
        _registry.pop(member, None)
    client = _redis_client(current_app.config.get("SOCKETIO_MESSAGE_QUEUE"))
    if client is not None:
        try:
            pipeline = client.pipeline(transaction=False)
            pipeline.zrem(_ZSET_KEY, member)
            pipeline.hdel(_TRANSPORT_KEY, member)
            pipeline.execute()
        except Exception:
            current_app.logger.debug("Socket connection metric removal failed", exc_info=True)


def connection_snapshot(queue_url: str | None) -> dict[str, Any]:
    """Return anonymized unique Engine.IO connection counts."""

    now = time.time()
    members: list[str] = []
    transports: dict[str, str] = {}
    event_counts: dict[str, int] = {}
    source = "local"
    redis_error = False
    client = _redis_client(queue_url)
    if client is not None:
        try:
            cutoff = now - _STALE_AFTER_SECONDS
            stale_members = client.zrangebyscore(_ZSET_KEY, "-inf", cutoff)
            pipeline = client.pipeline(transaction=False)
            pipeline.zremrangebyscore(_ZSET_KEY, "-inf", cutoff)
            if stale_members:
                pipeline.hdel(_TRANSPORT_KEY, *stale_members)
            pipeline.zrangebyscore(_ZSET_KEY, cutoff, "+inf")
            results = pipeline.execute()
            members = results[-1]
            if members:
                values = client.hmget(_TRANSPORT_KEY, members)
                transports = {
                    member: str(value or "unknown")
                    for member, value in zip(members, values)
                }
            event_counts = {key: int(value) for key, value in client.hgetall(_EVENTS_KEY).items()}
            source = "redis"
        except Exception:
            members = []
            transports = {}
            redis_error = True
    elif queue_url:
        redis_error = True

    if not members:
        with _registry_lock:
            members = list(_registry)
            transports = {
                member: details.get("transport", "unknown")
                for member, details in _registry.items()
            }
            if members:
                source = "local"
            if source == "local":
                event_counts = dict(_event_counts)

    namespaces: dict[str, int] = {}
    for member in members:
        namespace = member.rsplit("|", 1)[-1]
        namespaces[namespace] = namespaces.get(namespace, 0) + 1

    unique: dict[str, str] = {}
    for member in members:
        connection_id = member.rsplit("|", 1)[0]
        transport = transports.get(member, "unknown")
        # One browser tab may connect to multiple namespaces. Prefer WebSocket
        # if any namespace on the shared Engine.IO connection has upgraded.
        if connection_id not in unique or transport == "websocket":
            unique[connection_id] = transport

    websocket_count = sum(value == "websocket" for value in unique.values())
    polling_count = sum(value == "polling" for value in unique.values())
    return {
        "total": len(unique),
        "websocket": websocket_count,
        "polling": polling_count,
        "other": max(0, len(unique) - websocket_count - polling_count),
        "namespaces": namespaces,
        "rejected": sum(value for key, value in event_counts.items() if key.startswith("rejected:")),
        "reconnected": sum(value for key, value in event_counts.items() if key.startswith("reconnected:")),
        "redis_error": redis_error,
        "source": source,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
