from __future__ import annotations

from datetime import datetime, timedelta
from ipaddress import ip_address, ip_network
from typing import Any

import bcrypt
from flask import jsonify, request, session

from app.chat import (
    CHAT_ALLOWED_REACTION_EMOJIS,
    CHAT_UPLOAD_MAX_BYTES,
    CHAT_UPLOAD_MAX_FILES,
    MESSAGE_MAX_LEN,
    _accessible_events,
    _build_admin_dm_inbox_items,
    _build_chat_notification_context,
    _build_read_room_key,
    _can_access_event,
    _can_access_room,
    _can_manage_rooms,
    _chat_csrf,
    _default_avatar_url,
    _dm_message_to_payload,
    _ensure_chat_delete_schema,
    _ensure_chat_dm_delete_schema,
    _ensure_chat_dm_message_images_schema,
    _ensure_chat_dm_reaction_schema,
    _ensure_chat_dm_schema,
    _ensure_chat_edit_schema,
    _ensure_chat_message_images_schema,
    _ensure_chat_messages_room_schema,
    _ensure_chat_read_state_room_schema,
    _ensure_chat_read_state_v2_schema,
    _ensure_chat_reaction_schema,
    _ensure_chat_reply_schema,
    _ensure_chat_rooms_schema,
    _ensure_chat_room_members_schema,
    _ensure_chat_search_schema,
    _ensure_chat_thread_schema,
    _fallback_images_from_message,
    _format_jst_labels,
    _get_dm_actor_key,
    _get_dm_conversation_by_uuid,
    _get_event,
    _is_chat_admin_actor,
    _list_accessible_rooms,
    _load_chat_read_state_v2_snapshot,
    _load_dm_messages,
    _load_message_images_by_message_ids,
    _load_messages,
    _load_my_reactions_by_message_ids,
    _load_reactions_by_message_ids,
    _present_message,
    _build_plain_excerpt,
    can_access_dm,
    chat_bp,
    get_chat_actor,
    get_chat_actor_key,
)
from app.utils.db import get_db
from app.utils.logs import write_login_log
from app.utils.totp_util import get_totp_status


def _json_error(error: str, status: int = 400, **extra: Any):
    payload = {"ok": False, "error": error}
    payload.update(extra)
    return jsonify(payload), status


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return value


def _limits() -> dict[str, int]:
    return {
        "message_max_len": MESSAGE_MAX_LEN,
        "upload_max_files": CHAT_UPLOAD_MAX_FILES,
        "upload_max_bytes": CHAT_UPLOAD_MAX_BYTES,
    }


def _actor_payload(actor: dict[str, Any] | None) -> dict[str, Any] | None:
    if not actor:
        return None
    return {
        "actor_type": str(actor.get("actor_type") or ""),
        "actor_id": str(actor.get("actor_id") or ""),
        "display_name": str(actor.get("display_name") or actor.get("actor_id") or ""),
        "actor_key": get_chat_actor_key(actor),
        "is_chat_admin_alias": bool(actor.get("is_chat_admin_alias")),
    }


def _ensure_event_runtime_schema() -> None:
    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    _ensure_chat_messages_room_schema()
    _ensure_chat_thread_schema()
    _ensure_chat_read_state_room_schema()
    _ensure_chat_read_state_v2_schema()
    _ensure_chat_delete_schema()
    _ensure_chat_edit_schema()
    _ensure_chat_reaction_schema()
    _ensure_chat_message_images_schema()


def _present_event_messages(event_id: int, room_id: str, actor: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    raw_messages = _load_messages(event_id, room_id, limit=limit)
    message_ids = [int(m.get("id") or 0) for m in raw_messages if int(m.get("id") or 0) > 0]
    reactions = _load_reactions_by_message_ids(message_ids)
    my_reactions = _load_my_reactions_by_message_ids(message_ids, actor)
    images = _load_message_images_by_message_ids(message_ids)
    avatar_cache: dict[str, str] = {}
    messages: list[dict[str, Any]] = []
    for message in raw_messages:
        message_id = int(message.get("id") or 0)
        message["reactions_summary"] = reactions.get(message_id, [])
        message["my_reaction"] = my_reactions.get(message_id)
        message["images"] = images.get(message_id) or _fallback_images_from_message(message)
        messages.append(_present_message(message, actor, avatar_cache=avatar_cache))
    return messages


def _dm_inbox_for(actor: dict[str, Any]) -> list[dict[str, Any]]:
    actor_key = _get_dm_actor_key(actor)
    if not actor_key or not _ensure_chat_dm_schema():
        return []
    if _is_chat_admin_actor(actor):
        rows = _build_admin_dm_inbox_items(actor_key)
    else:
        # Non-admin users normally have a single admin DM opened through /chat/dm.
        rows = []
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT c.id, c.uuid, c.dm_type, c.last_message_at,
                       (SELECT body_text FROM chat_dm_messages m WHERE m.id = c.last_message_id LIMIT 1) AS last_message,
                       (SELECT actor_key FROM chat_dm_participants pp
                         WHERE pp.conversation_id=c.id AND pp.actor_key<>%s
                         LIMIT 1) AS peer_actor_key
                  FROM chat_dm_conversations c
                  JOIN chat_dm_participants p ON p.conversation_id=c.id AND p.actor_key=%s
                 ORDER BY c.last_message_at DESC
                """,
                (actor_key, actor_key),
            )
            rows = cur.fetchall() or []
        finally:
            cur.close()
            db.close()
        for row in rows:
            row["peer_display_name"] = str(row.get("peer_actor_key") or "")
            row["unread_count"] = 0
    conversations: list[dict[str, Any]] = []
    for row in rows:
        conversations.append(
            {
                "dm_uuid": str(row.get("uuid") or ""),
                "peer_actor_key": str(row.get("peer_actor_key") or ""),
                "peer_display_name": str(row.get("peer_display_name") or row.get("peer_actor_key") or ""),
                "last_message": str(row.get("last_message") or ""),
                "last_message_at": _json_safe(row.get("last_message_at")),
                "unread_count": int(row.get("unread_count") or 0),
            }
        )
    return conversations


@chat_bp.get("/api/gui/session")
def gui_session():
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": True, "authenticated": False})
    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "actor": _actor_payload(actor),
            "csrf_token": _chat_csrf(),
            "default_avatar_url": _default_avatar_url(),
            "limits": _limits(),
            "reaction_emojis": list(CHAT_ALLOWED_REACTION_EMOJIS),
        }
    )


@chat_bp.post("/api/gui/login")
def gui_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return _json_error("username_password_required", 400)

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT password_hash, nickname FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()
    if not row or not bcrypt.checkpw(password.encode(), str(row.get("password_hash") or "").encode()):
        return _json_error("login_failed", 401)

    remote_addr = request.remote_addr or "0.0.0.0"
    try:
        client_ip = ip_address(remote_addr)
        is_local = (
            client_ip in ip_network("192.168.103.0/24")
            or client_ip in ip_network("fe80::/10")
            or client_ip in ip_network("2404:7a81:bc40:2a00::/64")
            or client_ip in ip_network("2404:7a81:8ac1:1000::/64")
        )
    except ValueError:
        is_local = False

    totp_status = get_totp_status(username)
    if totp_status.get("enabled") and totp_status.get("has_secret") and not is_local:
        session.clear()
        session["preauth_user"] = username
        session["preauth_expires_at"] = datetime.now() + timedelta(minutes=5)
        return _json_error("mfa_required", 401, mfa_required=True)

    session.clear()
    session["user"] = username
    session["nickname"] = row.get("nickname")
    session.permanent = True
    write_login_log(username, request.remote_addr)
    actor = get_chat_actor()
    return jsonify({"ok": True, "actor": _actor_payload(actor), "csrf_token": _chat_csrf()})


@chat_bp.get("/api/gui/bootstrap")
def gui_bootstrap():
    actor = get_chat_actor()
    if not actor:
        return _json_error("forbidden", 403)
    _ensure_chat_dm_schema()
    return jsonify(
        _json_safe(
            {
                "ok": True,
                "actor": _actor_payload(actor),
                "csrf_token": _chat_csrf(),
                "accessible_events": _accessible_events(actor),
                "dm_inbox": _dm_inbox_for(actor),
                "notification_context": _build_chat_notification_context(actor),
                "default_avatar_url": _default_avatar_url(),
                "limits": _limits(),
                "reaction_emojis": list(CHAT_ALLOWED_REACTION_EMOJIS),
            }
        )
    )


@chat_bp.get("/api/gui/events")
def gui_events():
    actor = get_chat_actor()
    if not actor:
        return _json_error("forbidden", 403)
    return jsonify(_json_safe({"ok": True, "events": _accessible_events(actor)}))


@chat_bp.get("/api/gui/events/<int:event_id>/snapshot")
def gui_event_snapshot(event_id: int):
    actor = get_chat_actor()
    if not actor:
        return _json_error("forbidden", 403)
    if not _can_access_event(event_id, actor):
        return _json_error("forbidden", 403)
    _ensure_event_runtime_schema()
    requested_room_id = (request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, active_room = _can_access_room(event_id, requested_room_id, actor)
    if not allowed or not effective_room_id or not active_room:
        return _json_error("forbidden", 403)
    event = _get_event(event_id)
    if not event:
        return _json_error("not_found", 404)
    room_key = _build_read_room_key(event_id=event_id, room_id=effective_room_id)
    return jsonify(
        _json_safe(
            {
                "ok": True,
                "event": event,
                "active_room": active_room,
                "accessible_rooms": _list_accessible_rooms(event_id, actor),
                "can_manage_rooms": _can_manage_rooms(event_id, actor),
                "messages": _present_event_messages(event_id, effective_room_id, actor),
                "read_states": _load_chat_read_state_v2_snapshot(room_key),
                "csrf_token": _chat_csrf(),
            }
        )
    )


@chat_bp.get("/api/gui/events/<int:event_id>/messages")
def gui_event_messages(event_id: int):
    from app.chat import api_older_messages

    return api_older_messages(event_id)


@chat_bp.get("/api/gui/events/<int:event_id>/search")
def gui_event_search(event_id: int):
    from app.chat import search_messages

    return search_messages(event_id)


@chat_bp.get("/api/gui/events/<int:event_id>/rooms")
def gui_event_rooms(event_id: int):
    actor = get_chat_actor()
    if not actor or not _can_access_event(event_id, actor):
        return _json_error("forbidden", 403)
    requested_room_id = (request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, active_room = _can_access_room(event_id, requested_room_id, actor)
    if not allowed or not effective_room_id:
        return _json_error("forbidden", 403)
    return jsonify(
        _json_safe(
            {
                "ok": True,
                "accessible_rooms": _list_accessible_rooms(event_id, actor),
                "active_room": active_room,
                "can_manage_rooms": _can_manage_rooms(event_id, actor),
            }
        )
    )


@chat_bp.get("/api/gui/dm/inbox")
def gui_dm_inbox():
    actor = get_chat_actor()
    if not actor:
        return _json_error("forbidden", 403)
    return jsonify(_json_safe({"ok": True, "conversations": _dm_inbox_for(actor)}))


@chat_bp.get("/api/gui/dm/<dm_uuid>/snapshot")
def gui_dm_snapshot(dm_uuid: str):
    actor = get_chat_actor()
    actor_key = _get_dm_actor_key(actor)
    if not actor or not actor_key:
        return _json_error("forbidden", 403)
    if not can_access_dm(dm_uuid, actor_key):
        return _json_error("forbidden", 403)
    if not _ensure_chat_dm_reaction_schema():
        return _json_error("reaction_schema_unavailable", 500)
    conversation = _get_dm_conversation_by_uuid(dm_uuid)
    if not conversation:
        return _json_error("not_found", 404)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT actor_key FROM chat_dm_participants WHERE conversation_id=%s AND actor_key<>%s LIMIT 1",
            (conversation["id"], actor_key),
        )
        peer = cur.fetchone() or {}
    finally:
        cur.close()
        db.close()
    room_key = _build_read_room_key(dm_uuid=dm_uuid)
    return jsonify(
        _json_safe(
            {
                "ok": True,
                "dm_uuid": dm_uuid,
                "room_id": f"dm:{dm_uuid}",
                "peer_actor_key": str(peer.get("actor_key") or ""),
                "peer_display_name": str(peer.get("actor_key") or ""),
                "messages": _load_dm_messages(int(conversation["id"]), actor_key, dm_uuid, limit=200),
                "read_states": _load_chat_read_state_v2_snapshot(room_key),
                "csrf_token": _chat_csrf(),
            }
        )
    )


@chat_bp.get("/api/gui/dm/<dm_uuid>/messages")
def gui_dm_messages(dm_uuid: str):
    actor = get_chat_actor()
    actor_key = _get_dm_actor_key(actor)
    if not actor or not actor_key:
        return _json_error("forbidden", 403)
    if not can_access_dm(dm_uuid, actor_key):
        return _json_error("forbidden", 403)
    conversation = _get_dm_conversation_by_uuid(dm_uuid)
    if not conversation:
        return _json_error("not_found", 404)
    before_id = int(request.args.get("before_id") or 0)
    limit = max(1, min(int(request.args.get("limit") or 50), 100))
    if before_id <= 0:
        return jsonify({"ok": True, "messages": _load_dm_messages(int(conversation["id"]), actor_key, dm_uuid, limit=limit), "has_more": False})

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, sender_actor_key, body_type, body_text, created_at,
                   COALESCE(deleted_flag, 0) AS deleted_flag,
                   deleted_at, deleted_by_actor_key,
                   COALESCE(edited_flag, 0) AS edited_flag,
                   edited_at, edited_by_actor_key
              FROM chat_dm_messages
             WHERE conversation_id=%s AND id < %s
             ORDER BY id DESC
             LIMIT %s
            """,
            (conversation["id"], before_id, limit + 1),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    messages = []
    for row in rows:
        row["dm_uuid"] = dm_uuid
        row["images"] = []
        row["reactions_summary"] = []
        row["my_reaction"] = None
        messages.append(_dm_message_to_payload(row, actor_key))
    return jsonify(_json_safe({"ok": True, "messages": messages, "has_more": has_more}))


@chat_bp.get("/api/gui/dm/<dm_uuid>/search")
def gui_dm_search(dm_uuid: str):
    actor = get_chat_actor()
    actor_key = _get_dm_actor_key(actor)
    if not actor or not actor_key:
        return _json_error("forbidden", 403)
    if not can_access_dm(dm_uuid, actor_key):
        return _json_error("forbidden", 403)
    conversation = _get_dm_conversation_by_uuid(dm_uuid)
    if not conversation:
        return _json_error("not_found", 404)
    q = str(request.args.get("q") or "").strip()
    if not q:
        return _json_error("query_required", 400)
    if len(q) > 100:
        return _json_error("query_too_long", 400)
    limit = max(1, min(int(request.args.get("limit") or 50), 100))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id AS message_id, sender_actor_key, body_text, created_at
              FROM chat_dm_messages
             WHERE conversation_id=%s
               AND COALESCE(deleted_flag, 0)=0
               AND body_text LIKE %s
             ORDER BY id DESC
             LIMIT %s
            """,
            (conversation["id"], f"%{q}%", limit),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()
    results = []
    for row in rows:
        _iso, _date_label, time_label = _format_jst_labels(row.get("created_at") or datetime.utcnow())
        results.append(
            {
                "message_id": int(row.get("message_id") or 0),
                "sender": str(row.get("sender_actor_key") or ""),
                "time": time_label,
                "excerpt": _build_plain_excerpt(str(row.get("body_text") or ""), max_len=80),
            }
        )
    return jsonify(_json_safe({"ok": True, "results": results}))


@chat_bp.get("/api/gui/dm/<dm_uuid>/messages/<int:message_id>/reactions")
def gui_dm_reaction_details(dm_uuid: str, message_id: int):
    actor = get_chat_actor()
    actor_key = _get_dm_actor_key(actor)
    if not actor or not actor_key:
        return _json_error("forbidden", 403)
    if not can_access_dm(dm_uuid, actor_key):
        return _json_error("forbidden", 403)
    conversation = _get_dm_conversation_by_uuid(dm_uuid)
    if not conversation:
        return _json_error("not_found", 404)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id FROM chat_dm_messages WHERE id=%s AND conversation_id=%s LIMIT 1",
            (message_id, conversation["id"]),
        )
        if not cur.fetchone():
            return _json_error("not_found", 404)
        cur.execute(
            """
            SELECT r.emoji, r.actor_key, r.created_at, p.display_name_cache
              FROM chat_dm_message_reactions r
              LEFT JOIN chat_dm_participants p
                ON p.conversation_id=r.conversation_id AND p.actor_key=r.actor_key
             WHERE r.conversation_id=%s AND r.message_id=%s
             ORDER BY r.created_at ASC
            """,
            (conversation["id"], message_id),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        emoji = str(row.get("emoji") or "").strip()
        if not emoji:
            continue
        if emoji not in grouped:
            grouped[emoji] = {"emoji": emoji, "count": 0, "actors": []}
            order.append(emoji)
        actor_key_value = str(row.get("actor_key") or "")
        grouped[emoji]["count"] += 1
        grouped[emoji]["actors"].append(
            {
                "actor_key": actor_key_value,
                "display_name": str(row.get("display_name_cache") or actor_key_value),
            }
        )
    return jsonify(_json_safe({"ok": True, "groups": [grouped[k] for k in order]}))
