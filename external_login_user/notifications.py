from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlparse

from flask import abort, current_app, jsonify, render_template, request, session

from . import bp
from .utils import _require_ext_login
from app.utils.db import get_db

try:
    from app.chat.socketio_ext import socketio
except Exception:  # pragma: no cover
    socketio = None


_READ_NOTIFICATIONS_KEEP_LIMIT = 30


def _parse_chat_room_context(row: dict[str, Any]) -> tuple[int | None, str | None]:
    chat_room_id = str(row.get("chat_room_id") or "").strip()
    chat_event_id = int(row.get("chat_event_id") or 0) or None
    if chat_room_id:
        return chat_event_id, chat_room_id

    target_url = str(row.get("target_url") or "").strip()
    event_id = int(row.get("event_id") or 0) or chat_event_id
    if not target_url:
        return event_id, None
    try:
        parsed = urlparse(target_url)
        if not parsed.path.startswith("/chat/events/"):
            return event_id, None
        qs = parse_qs(parsed.query or "")
        room_id = (qs.get("room_id") or [None])[0]
        if room_id:
            room_id = str(room_id)
        if not event_id:
            ev = (qs.get("event_id") or [None])[0]
            if ev:
                event_id = int(ev)
        return event_id, room_id
    except Exception:
        return event_id, None


def _is_notification_visible_for_external(cur, uid: int, row: dict[str, Any], room_cache: dict[str, tuple[int, str]]) -> tuple[bool, str | None]:
    event_id, room_id = _parse_chat_room_context(row)
    if not room_id:
        return True, None

    cache_key = f"{event_id}:{room_id}"
    if cache_key in room_cache:
        is_main, room_name = room_cache[cache_key]
    else:
        cur.execute(
            "SELECT is_main, room_name FROM chat_rooms WHERE event_id=%s AND room_id=%s LIMIT 1",
            (event_id, room_id),
        )
        room = cur.fetchone() or {}
        if not room:
            room_cache[cache_key] = (0, "")
            return False, None
        is_main = int(room.get("is_main") or 0)
        room_name = str(room.get("room_name") or "")
        room_cache[cache_key] = (is_main, room_name)

    if is_main == 1:
        return True, room_name or "メイン"

    cur.execute(
        """
        SELECT 1
          FROM chat_room_members
         WHERE room_id=%s
           AND actor_type='line'
           AND actor_id=%s
         LIMIT 1
        """,
        (room_id, str(uid)),
    )
    allowed = bool(cur.fetchone())
    return allowed, (room_name if allowed else None)


def _prune_old_read_notifications(cur, user_id: int) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
          FROM mfu_notifications
         WHERE user_kind=%s
           AND user_id=%s
           AND read_at IS NOT NULL
        """,
        ("external", int(user_id)),
    )
    read_count = int((cur.fetchone() or {}).get("cnt") or 0)
    overflow = max(0, read_count - _READ_NOTIFICATIONS_KEEP_LIMIT)
    if overflow <= 0:
        return 0

    cur.execute(
        """
        DELETE FROM mfu_notifications
         WHERE user_kind=%s
           AND user_id=%s
           AND read_at IS NOT NULL
         ORDER BY created_at ASC, id ASC
         LIMIT %s
        """,
        ("external", int(user_id), overflow),
    )
    return int(cur.rowcount or 0)


def _compute_unread_count_external(uid: int) -> int:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        room_cache: dict[str, tuple[int, str]] = {}
        cur.execute(
            """
            SELECT id, target_url, event_id
                 , chat_event_id, chat_room_id
              FROM mfu_notifications
             WHERE user_kind='external'
               AND user_id=%s
               AND read_at IS NULL
            """,
            (int(uid),),
        )
        unread_rows = cur.fetchall() or []
        count = 0
        for row in unread_rows:
            visible, _room_name = _is_notification_visible_for_external(cur, int(uid), row, room_cache)
            if visible:
                count += 1
        return count
    finally:
        cur.close()
        db.close()


def _emit_notif_unread(uid: int, reason: str = "sync", latest_id: int | None = None) -> None:
    if socketio is None:
        return
    try:
        count = _compute_unread_count_external(int(uid))
        socketio.emit(
            "notif_unread",
            {"count": count, "latest_id": latest_id, "reason": reason},
            room=f"external_user:{int(uid)}",
        )
    except Exception:
        current_app.logger.warning(
            "notifications emit failed user_id=%s reason=%s latest_id=%s",
            int(uid),
            reason,
            latest_id,
            exc_info=True,
        )


_NOTIFICATION_DDL = """
CREATE TABLE IF NOT EXISTS mfu_notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_kind VARCHAR(16) NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  kind VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  body TEXT NULL,
  target_url VARCHAR(512) NOT NULL,
  event_id BIGINT UNSIGNED NULL,
  chat_event_id BIGINT UNSIGNED NULL,
  chat_room_id VARCHAR(64) NULL,
  created_at DATETIME NOT NULL,
  read_at DATETIME NULL,
  dedup_key VARCHAR(191) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_mfu_notifications_dedup (user_kind, user_id, dedup_key),
  KEY idx_mfu_notifications_unread (user_kind, user_id, read_at, created_at),
  KEY idx_mfu_notifications_created (user_kind, user_id, created_at),
  KEY idx_mfu_notifications_chat_room_unread (user_kind, user_id, kind, chat_room_id, read_at),
  KEY idx_mfu_notifications_unread_kind (user_kind, user_id, read_at, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def _ensure_notification_schema() -> None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(_NOTIFICATION_DDL)

        cur.execute("SHOW COLUMNS FROM mfu_notifications")
        existing = {str(r.get("Field") or "") for r in (cur.fetchall() or [])}

        if "chat_event_id" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN chat_event_id BIGINT UNSIGNED NULL AFTER event_id")
        if "chat_room_id" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN chat_room_id VARCHAR(64) NULL AFTER chat_event_id")

        index_map = {
            "idx_mfu_notifications_chat_room_unread": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_chat_room_unread (user_kind, user_id, kind, chat_room_id, read_at)",
            "idx_mfu_notifications_unread_kind": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_unread_kind (user_kind, user_id, read_at, kind)",
        }
        cur.execute("SHOW INDEX FROM mfu_notifications")
        existing_indexes = {str(r.get("Key_name") or "") for r in (cur.fetchall() or [])}
        for key_name, ddl in index_map.items():
            if key_name not in existing_indexes:
                cur.execute(ddl)

        while True:
            cur.execute(
                """
                SELECT id, target_url, event_id
                  FROM mfu_notifications
                 WHERE kind='chat_message'
                   AND (chat_room_id IS NULL OR chat_room_id='')
                 ORDER BY id ASC
                 LIMIT 200
                """
            )
            rows = cur.fetchall() or []
            if not rows:
                break

            updated_in_batch = 0
            for row in rows:
                event_id, room_id = _parse_chat_room_context(row)
                if not room_id:
                    continue
                cur.execute(
                    """
                    UPDATE mfu_notifications
                       SET chat_event_id=COALESCE(chat_event_id, %s),
                           chat_room_id=COALESCE(chat_room_id, %s)
                     WHERE id=%s
                    """,
                    (int(event_id) if event_id else None, str(room_id), int(row["id"])),
                )
                updated_in_batch += int(cur.rowcount or 0)
            if updated_in_batch <= 0:
                break
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("ensure mfu_notifications schema migration failed")
        raise
    finally:
        cur.close()
        db.close()


@bp.record_once
def _ensure_notification_table(state) -> None:
    app = state.app
    with app.app_context():
        try:
            _ensure_notification_schema()
        except Exception:
            app.logger.exception("ensure mfu_notifications failed")


def create_notification_external(
    user_id: int,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    dedup_key: str,
    event_id: int | None = None,
    chat_event_id: int | None = None,
    chat_room_id: str | None = None,
) -> bool:
    _ensure_notification_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        dedup = (dedup_key or "").strip()[:191]
        cur.execute(
            """
            SELECT id, created_at
              FROM mfu_notifications
             WHERE user_kind=%s AND user_id=%s AND dedup_key=%s
             LIMIT 1
            """,
            ("external", int(user_id), dedup),
        )
        existing = cur.fetchone()

        cur.execute(
            """
            INSERT INTO mfu_notifications (
              user_kind, user_id, kind, title, body, target_url,
              event_id, chat_event_id, chat_room_id, dedup_key, created_at, read_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
            ON DUPLICATE KEY UPDATE id=id
            """,
            (
                "external",
                int(user_id),
                (kind or "").strip() or "general",
                (title or "").strip() or "お知らせ",
                (body or "").strip(),
                (target_url or "").strip() or "/external-login/",
                int(event_id) if event_id else None,
                int(chat_event_id) if chat_event_id else None,
                str(chat_room_id).strip() if chat_room_id else None,
                dedup,
                datetime.utcnow(),
            ),
        )
        deleted_old_read = _prune_old_read_notifications(cur, int(user_id))
        inserted = cur.rowcount == 1
        latest_id = int(cur.lastrowid) if inserted and cur.lastrowid else None
        db.commit()
        if inserted:
            _emit_notif_unread(int(user_id), reason="created", latest_id=latest_id)
        current_app.logger.info(
            "notifications insert user_id=%s event_id=%s kind=%s dedup_key=%s inserted=%s existing_id=%s pruned_read=%s",
            int(user_id),
            int(event_id) if event_id else None,
            (kind or "").strip() or "general",
            dedup,
            inserted,
            existing["id"] if existing else None,
            deleted_old_read,
        )
        return inserted
    except Exception:
        current_app.logger.warning("create_notification_external failed user_id=%s", user_id, exc_info=True)
        return False
    finally:
        cur.close()
        db.close()


@bp.get("/notifications")
def notifications_page():
    guard = _require_ext_login()
    if guard:
        return guard
    return render_template("notifications.html")


@bp.get("/api/notifications/unread-count")
def api_notifications_unread_count():
    _ensure_notification_schema()
    uid = int(session.get("ext_user_id") or 0)
    if uid <= 0:
        resp = jsonify({"ok": False, "reason": "login_required"})
        resp.status_code = 401
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    try:
        count = _compute_unread_count_external(uid)
        current_app.logger.info("notifications unread-count user_id=%s read_at_is_null=true count=%s", uid, count)
        resp = jsonify({"count": count})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    except Exception:
        current_app.logger.warning("notifications unread-count failed user_id=%s", uid, exc_info=True)
        raise


@bp.get("/api/notifications")
def api_notifications_list():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    _ensure_notification_schema()
    unread_arg = (request.args.get("unread") or "").strip().lower()
    unread_only = unread_arg not in {"0", "false", "no", "off"}
    page = max(int(request.args.get("page") or 1), 1)
    per_page = 20
    offset = (page - 1) * per_page

    where = ["user_kind='external'", "user_id=%s"]
    params: list[Any] = [uid]
    if unread_only:
        where.append("read_at IS NULL")

    where_sql = " AND ".join(where)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        room_cache: dict[str, tuple[int, str]] = {}
        cur.execute(
            f"""
            SELECT id, kind, title, body, target_url, event_id, created_at, read_at
                 , chat_event_id, chat_room_id
              FROM mfu_notifications
             WHERE {where_sql}
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            tuple(params + [per_page, offset]),
        )
        rows = cur.fetchall() or []

        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM mfu_notifications WHERE {where_sql}",
            tuple(params),
        )
        _ = int((cur.fetchone() or {}).get("cnt") or 0)

        items = []
        for row in rows:
            visible, room_name = _is_notification_visible_for_external(cur, uid, row, room_cache)
            if not visible:
                continue
            items.append(
                {
                    "id": int(row["id"]),
                    "kind": row.get("kind"),
                    "title": row.get("title"),
                    "body": row.get("body") or "",
                    "target_url": row.get("target_url") or "/external-login/",
                    "event_id": row.get("event_id"),
                    "room_name": room_name,
                    "created_at": row.get("created_at").replace(tzinfo=timezone.utc).isoformat() if row.get("created_at") else None,
                    "read_at": row.get("read_at").replace(tzinfo=timezone.utc).isoformat() if row.get("read_at") else None,
                }
            )

        cur.execute(
            f"SELECT id, target_url, event_id, chat_event_id, chat_room_id FROM mfu_notifications WHERE {where_sql}",
            tuple(params),
        )
        all_rows = cur.fetchall() or []
        total = 0
        for row in all_rows:
            visible, _room_name = _is_notification_visible_for_external(cur, uid, row, room_cache)
            if visible:
                total += 1

        latest_id = items[0]["id"] if items else None
        latest_created_at = items[0]["created_at"] if items else None
        current_app.logger.info(
            "notifications list user_id=%s page=%s unread_only=%s returned=%s total=%s latest_id=%s latest_created_at=%s",
            uid,
            page,
            unread_only,
            len(items),
            total,
            latest_id,
            latest_created_at,
        )

        resp = jsonify(
            {
                "items": items,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "has_next": page * per_page < total,
                },
            }
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    finally:
        cur.close()
        db.close()


@bp.get("/api/notifications/updates")
def api_notifications_updates():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    _ensure_notification_schema()
    since_id = max(int(request.args.get("since_id") or 0), 0)
    limit = min(max(int(request.args.get("limit") or 20), 1), 100)

    started = perf_counter()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        db.start_transaction()
        room_cache: dict[str, tuple[int, str]] = {}

        cur.execute(
            """
            SELECT id, title, body, target_url, event_id, created_at, read_at
                 , chat_event_id, chat_room_id
              FROM mfu_notifications
             WHERE user_kind='external'
               AND user_id=%s
               AND id > %s
             ORDER BY id ASC
             LIMIT %s
            """,
            (uid, since_id, limit),
        )
        rows = cur.fetchall() or []

        cur.execute(
            """
            SELECT id, target_url, event_id
                 , chat_event_id, chat_room_id
              FROM mfu_notifications
             WHERE user_kind='external'
               AND user_id=%s
               AND read_at IS NULL
            """,
            (uid,),
        )
        unread_rows = cur.fetchall() or []
        unread_count = 0
        for row in unread_rows:
            visible, _room_name = _is_notification_visible_for_external(cur, uid, row, room_cache)
            if visible:
                unread_count += 1
        db.commit()

        items = []
        latest_id = since_id
        for row in rows:
            visible, room_name = _is_notification_visible_for_external(cur, uid, row, room_cache)
            if not visible:
                continue
            item_id = int(row["id"])
            latest_id = max(latest_id, item_id)
            items.append(
                {
                    "id": item_id,
                    "title": row.get("title"),
                    "body": row.get("body") or "",
                    "target_url": row.get("target_url") or "/external-login/",
                    "room_name": room_name,
                    "created_at": row.get("created_at").replace(tzinfo=timezone.utc).isoformat() if row.get("created_at") else None,
                    "read_at": row.get("read_at").replace(tzinfo=timezone.utc).isoformat() if row.get("read_at") else None,
                }
            )

        elapsed_ms = int((perf_counter() - started) * 1000)
        current_app.logger.info(
            "notifications updates user_id=%s since_id=%s limit=%s returned=%s latest_id=%s unread_count=%s elapsed_ms=%s",
            uid,
            since_id,
            limit,
            len(items),
            latest_id,
            unread_count,
            elapsed_ms,
        )

        resp = jsonify(
            {
                "latest_id": latest_id,
                "unread_count": unread_count,
                "items": items,
            }
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/<int:notification_id>/read")
def api_notifications_mark_read(notification_id: int):
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    _ensure_notification_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=COALESCE(read_at, %s)
             WHERE id=%s
               AND user_kind='external'
               AND user_id=%s
            """,
            (datetime.utcnow(), notification_id, uid),
        )
        db.commit()
        if cur.rowcount == 0:
            abort(404)
        _emit_notif_unread(uid, reason="read")
        return jsonify({"ok": True})
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/read-all")
def api_notifications_mark_all_read():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    _ensure_notification_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=%s
             WHERE user_kind='external'
               AND user_id=%s
               AND read_at IS NULL
            """,
            (datetime.utcnow(), uid),
        )
        updated = int(cur.rowcount or 0)
        db.commit()
        _emit_notif_unread(uid, reason="read_all")
        return jsonify({"ok": True, "updated": updated})
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/read-by-room")
def api_notifications_mark_read_by_room():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    payload = request.get_json(silent=True) or {}
    event_id_raw = payload.get("event_id")
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        resp = jsonify({"ok": False, "reason": "room_id_required"})
        resp.status_code = 400
        return resp

    try:
        event_id = int(event_id_raw)
    except Exception:
        resp = jsonify({"ok": False, "reason": "event_id_required"})
        resp.status_code = 400
        return resp

    _ensure_notification_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        now = datetime.utcnow()
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=%s
             WHERE user_kind='external'
               AND user_id=%s
               AND kind='chat_message'
               AND chat_event_id=%s
               AND chat_room_id=%s
               AND read_at IS NULL
            """,
            (now, uid, event_id, room_id),
        )
        updated_count = int(cur.rowcount or 0)
        db.commit()

        unread_count = _compute_unread_count_external(uid)
        cur.execute(
            """
            SELECT MAX(id) AS latest_id
              FROM mfu_notifications
             WHERE user_kind='external'
               AND user_id=%s
            """,
            (uid,),
        )
        latest_id = int((cur.fetchone() or {}).get("latest_id") or 0)
        _emit_notif_unread(uid, reason="room_read", latest_id=latest_id)

        return jsonify(
            {
                "ok": True,
                "updated_count": updated_count,
                "unread_count": unread_count,
                "latest_id": latest_id,
            }
        )
    finally:
        cur.close()
        db.close()
