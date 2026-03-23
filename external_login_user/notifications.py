from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlparse

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session

from . import bp
from .utils import _require_ext_login
from app.utils.db import get_db
from app.utils.mail import send_external_unread_reminder_mail

try:
    from app.chat.socketio_ext import socketio
except Exception:  # pragma: no cover
    socketio = None


_READ_NOTIFICATIONS_KEEP_LIMIT = 30
_JST = timezone(timedelta(hours=9))
mfu_notifications_bp = Blueprint("mfu_notifications", __name__)
_EXTERNAL_UNREAD_REMINDER_COLUMN = "notification_unread_reminder_last_sent_at"


def _notification_recipient_key(user_kind: str, user_id: int, recipient_key: str | None = None) -> str:
    if str(user_kind) == "mfu":
        return (recipient_key or "").strip()
    return str(int(user_id or 0))


def _notification_storage_user_id(user_kind: str, user_id: int, recipient_key: str | None = None) -> int:
    if str(user_kind) != "mfu":
        return int(user_id or 0)
    recipient = (recipient_key or "").strip()
    if not recipient:
        return 0
    digest = hashlib.sha1(recipient.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)

def _get_chat_admin_alias_ext_user_row(ext_user_id: int) -> dict[str, Any] | None:
    if int(ext_user_id or 0) <= 0:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, social_id, COALESCE(chat_admin_alias, 0) AS chat_admin_alias
              FROM external_login_user
             WHERE id=%s
             LIMIT 1
            """,
            (int(ext_user_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        if int(row.get("chat_admin_alias") or 0) != 1:
            return None
        return row
    finally:
        cur.close()
        db.close()


def _resolve_mfu_notification_recipient_for_session() -> tuple[str | None, str | None]:
    username = str(session.get("user") or "").strip()
    if username:
        role = "admin" if username == "admin" else "acl"
        return username, role

    ext_user_id = int(session.get("ext_user_id") or 0)
    if ext_user_id <= 0:
        return None, None
    alias_row = _get_chat_admin_alias_ext_user_row(ext_user_id)
    if not alias_row:
        return None, None
    current_app.logger.info(
        "mfu notifications admin alias ext_user_id=%s social_id=%s",
        ext_user_id,
        alias_row.get("social_id"),
    )
    return "admin", "alias"


def _resolve_notification_scope_for_session() -> str | None:
    if str(session.get("user") or "").strip():
        return "mfu"

    ext_user_id = int(session.get("ext_user_id") or 0)
    if ext_user_id <= 0:
        return None

    alias_row = _get_chat_admin_alias_ext_user_row(ext_user_id)
    if alias_row:
        return "mfu"
    return "external"


def _is_notification_scope_mfu() -> bool:
    return _resolve_notification_scope_for_session() == "mfu"


def _to_unread_only(value: Any, *, default: bool = False) -> bool:
    unread_arg = str(value or "").strip().lower()
    if not unread_arg:
        return bool(default)
    return unread_arg in {"1", "true", "yes", "on"}


def _resolve_notification_api_mode_for_session() -> dict[str, Any] | None:
    scope = _resolve_notification_scope_for_session()
    if scope == "external":
        return {
            "scope": "external",
            "urls": {
                "list": "/external-login/api/notifications",
                "unreadCount": "/external-login/api/notifications/unread-count",
                "updates": "/external-login/api/notifications/updates",
                "readOneBase": "/external-login/api/notifications",
                "readAll": "/external-login/api/notifications/read-all",
            },
        }
    if scope == "mfu":
        return {
            "scope": "mfu",
            "urls": {
                "list": "/api/mfu-notifications",
                "unreadCount": "/api/mfu-notifications/unread-count",
                "updates": "/api/mfu-notifications/updates",
                "readOneBase": "/api/mfu-notifications",
                "readAll": "/api/mfu-notifications/read-all",
            },
        }
    return None


@bp.app_context_processor
def _inject_notification_ui_config() -> dict[str, Any]:
    return {
        "notification_config": _resolve_notification_api_mode_for_session(),
    }


def _is_mfu_notification_user(username: str) -> bool:
    name = (username or "").strip()
    if not name:
        return False
    if name == "admin":
        return True
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT 1 FROM mfu_event_admin_acl WHERE username=%s LIMIT 1", (name,))
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        cur.close()
        db.close()


def _require_mfu_admin_acl() -> tuple[str | None, Any | None]:
    username, role = _resolve_mfu_notification_recipient_for_session()
    if not username:
        current_app.logger.info("mfu notification guard denied reason=login_required")
        return None, (jsonify({"ok": False, "reason": "login_required"}), 401)
    if not _is_mfu_notification_user(username):
        current_app.logger.info("mfu notification guard denied reason=forbidden username=%s role=%s", username, role)
        return None, (jsonify({"ok": False, "reason": "forbidden"}), 403)
    current_app.logger.info("mfu notification guard pass username=%s role=%s", username, role)
    return username, None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _can_send_external_unread_reminder(last_sent_at: datetime | None, now_utc: datetime | None) -> bool:
    last_sent_utc = _as_utc(last_sent_at)
    current_utc = _as_utc(now_utc)
    if current_utc is None:
        return False
    if last_sent_utc is None:
        return True
    return current_utc >= (last_sent_utc + timedelta(days=2))


def _relative_time_from(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = max(0, int((_now_utc() - dt).total_seconds()))
    if delta < 60:
        return "たった今"
    if delta < 3600:
        return f"{delta // 60}分前"
    if delta < 86400:
        return f"{delta // 3600}時間前"
    if delta < 86400 * 7:
        return f"{delta // 86400}日前"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


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
    if str(room_id).startswith("dm:"):
        return True, "DM"

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
  recipient_key VARCHAR(191) NULL,
  kind VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  body TEXT NULL,
  target_url VARCHAR(512) NOT NULL,
  event_id BIGINT UNSIGNED NULL,
  chat_event_id BIGINT UNSIGNED NULL,
  chat_room_id VARCHAR(64) NULL,
  room_type VARCHAR(32) NULL,
  room_id VARCHAR(64) NULL,
  sender_label VARCHAR(255) NULL,
  created_at DATETIME NOT NULL,
  read_at DATETIME NULL,
  dedup_key VARCHAR(191) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_mfu_notifications_dedup (user_kind, user_id, dedup_key),
  KEY idx_mfu_notifications_unread (user_kind, user_id, read_at, created_at),
  KEY idx_mfu_notifications_created (user_kind, user_id, created_at),
  KEY idx_mfu_notifications_chat_room_unread (user_kind, user_id, kind, chat_room_id, read_at),
  KEY idx_mfu_notifications_unread_kind (user_kind, user_id, read_at, kind),
  KEY idx_mfu_notifications_recipient_unread (user_kind, recipient_key, read_at, created_at),
  KEY idx_mfu_notifications_recipient_created (user_kind, recipient_key, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_NOTIFICATION_DELIVERY_DDL = """
CREATE TABLE IF NOT EXISTS mfu_notification_deliveries (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  notification_id BIGINT UNSIGNED NULL,
  dedup_key VARCHAR(191) NOT NULL,
  recipient_type VARCHAR(32) NOT NULL,
  recipient_value VARCHAR(191) NOT NULL,
  channel VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  detail TEXT NULL,
  created_at DATETIME NOT NULL,
  sent_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY idx_mfu_notification_deliveries_notification_id (notification_id),
  KEY idx_mfu_notification_deliveries_dedup (dedup_key),
  KEY idx_mfu_notification_deliveries_recipient (recipient_type, recipient_value, channel),
  KEY idx_mfu_notification_deliveries_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def _ensure_notification_delivery_schema(cur) -> None:
    cur.execute(_NOTIFICATION_DELIVERY_DDL)
    cur.execute("SHOW INDEX FROM mfu_notification_deliveries")
    existing_indexes = {str(r.get("Key_name") or "") for r in (cur.fetchall() or [])}
    index_map = {
        "idx_mfu_notification_deliveries_notification_id": "ALTER TABLE mfu_notification_deliveries ADD KEY idx_mfu_notification_deliveries_notification_id (notification_id)",
        "idx_mfu_notification_deliveries_dedup": "ALTER TABLE mfu_notification_deliveries ADD KEY idx_mfu_notification_deliveries_dedup (dedup_key)",
        "idx_mfu_notification_deliveries_recipient": "ALTER TABLE mfu_notification_deliveries ADD KEY idx_mfu_notification_deliveries_recipient (recipient_type, recipient_value, channel)",
        "idx_mfu_notification_deliveries_created": "ALTER TABLE mfu_notification_deliveries ADD KEY idx_mfu_notification_deliveries_created (created_at)",
    }
    for key_name, ddl in index_map.items():
        if key_name not in existing_indexes:
            cur.execute(ddl)


def _ensure_external_unread_reminder_schema(cur) -> None:
    cur.execute("SHOW COLUMNS FROM external_login_user")
    existing = {str(r.get("Field") or "") for r in (cur.fetchall() or [])}
    if _EXTERNAL_UNREAD_REMINDER_COLUMN not in existing:
        cur.execute(
            f"""
            ALTER TABLE external_login_user
            ADD COLUMN {_EXTERNAL_UNREAD_REMINDER_COLUMN} DATETIME NULL AFTER chat_admin_alias
            """
        )


def _ensure_notification_schema() -> None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(_NOTIFICATION_DDL)
        _ensure_notification_delivery_schema(cur)
        _ensure_external_unread_reminder_schema(cur)

        cur.execute("SHOW COLUMNS FROM mfu_notifications")
        existing = {str(r.get("Field") or "") for r in (cur.fetchall() or [])}

        if "chat_event_id" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN chat_event_id BIGINT UNSIGNED NULL AFTER event_id")
        if "chat_room_id" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN chat_room_id VARCHAR(64) NULL AFTER chat_event_id")
        if "recipient_key" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN recipient_key VARCHAR(191) NULL AFTER user_id")
        if "room_type" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN room_type VARCHAR(32) NULL AFTER chat_room_id")
        if "room_id" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN room_id VARCHAR(64) NULL AFTER room_type")
        if "sender_label" not in existing:
            cur.execute("ALTER TABLE mfu_notifications ADD COLUMN sender_label VARCHAR(255) NULL AFTER room_id")

        cur.execute(
            """
            UPDATE mfu_notifications
               SET recipient_key=CAST(user_id AS CHAR)
             WHERE user_kind='external'
               AND (recipient_key IS NULL OR recipient_key='')
            """
        )

        index_map = {
            "idx_mfu_notifications_chat_room_unread": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_chat_room_unread (user_kind, user_id, kind, chat_room_id, read_at)",
            "idx_mfu_notifications_unread_kind": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_unread_kind (user_kind, user_id, read_at, kind)",
            "idx_mfu_notifications_recipient_unread": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_recipient_unread (user_kind, recipient_key, read_at, created_at)",
            "idx_mfu_notifications_recipient_created": "ALTER TABLE mfu_notifications ADD KEY idx_mfu_notifications_recipient_created (user_kind, recipient_key, created_at)",
        }
        cur.execute("SHOW INDEX FROM mfu_notifications")
        existing_indexes = {str(r.get("Key_name") or "") for r in (cur.fetchall() or [])}
        for key_name, ddl in index_map.items():
            if key_name not in existing_indexes:
                cur.execute(ddl)

        last_id = 0
        scanned_count = 0
        updated_count = 0
        skipped_count = 0
        parse_failed_count = 0
        while True:
            cur.execute(
                """
                SELECT id, target_url, event_id
                  FROM mfu_notifications
                 WHERE kind='chat_message'
                   AND (chat_room_id IS NULL OR chat_room_id='')
                   AND id > %s
                 ORDER BY id ASC
                 LIMIT 200
                """
                ,
                (int(last_id),),
            )
            rows = cur.fetchall() or []
            if not rows:
                break

            for row in rows:
                row_id = int(row.get("id") or 0)
                last_id = max(last_id, row_id)
                scanned_count += 1
                try:
                    event_id, room_id = _parse_chat_room_context(row)
                except Exception:
                    parse_failed_count += 1
                    continue
                if not room_id:
                    skipped_count += 1
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
                updated_count += int(cur.rowcount or 0)

        current_app.logger.info(
            "notifications backfill chat_room_id scanned=%s updated=%s skipped=%s parse_failed=%s",
            scanned_count,
            updated_count,
            skipped_count,
            parse_failed_count,
        )
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
    result = create_notification_dispatch_result(
        recipient_type="external_user_id",
        recipient_value=int(user_id),
        kind=kind,
        title=title,
        body=body,
        target_url=target_url or "/external-login/",
        dedup_key=dedup_key,
        event_id=event_id,
        chat_event_id=chat_event_id,
        chat_room_id=chat_room_id,
    )
    if not result.get("ok"):
        current_app.logger.warning(
            "create_notification_external failed user_id=%s reason=%s",
            user_id,
            result.get("reason"),
        )
    return bool(result.get("created"))


def send_external_unread_reminder_emails(*, now_utc: datetime | None = None) -> dict[str, int]:
    _ensure_notification_schema()
    now_utc = _as_utc(now_utc) or _now_utc()
    summary = {
        "candidates": 0,
        "sent": 0,
        "failed": 0,
        "skipped_no_mail": 0,
        "skipped_too_soon": 0,
        "skipped_no_unread": 0,
        "skipped_invalid_user": 0,
    }

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT id, email, {_EXTERNAL_UNREAD_REMINDER_COLUMN} AS last_sent_at
              FROM external_login_user
             WHERE id > 0
             ORDER BY id ASC
            """
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    update_db = get_db()
    update_cur = update_db.cursor()
    try:
        summary["candidates"] = len(rows)
        current_app.logger.info(
            "external unread reminder job started candidates=%s now_utc=%s now_jst=%s",
            summary["candidates"],
            now_utc.isoformat(),
            now_utc.astimezone(_JST).isoformat(),
        )

        for row in rows:
            user_id = int(row.get("id") or 0)
            email = str(row.get("email") or "").strip()
            last_sent_at = _as_utc(row.get("last_sent_at"))

            if user_id <= 0:
                summary["skipped_invalid_user"] += 1
                current_app.logger.info(
                    "external unread reminder skipped user_id=%s email=%s unread_count=%s reason=invalid_user",
                    user_id,
                    email or "-",
                    0,
                )
                continue

            if not email:
                summary["skipped_no_mail"] += 1
                current_app.logger.info(
                    "external unread reminder skipped user_id=%s email=%s unread_count=%s reason=mailなし",
                    user_id,
                    email or "-",
                    0,
                )
                continue

            try:
                unread_count = _compute_unread_count_external(user_id)
            except Exception:
                summary["failed"] += 1
                current_app.logger.exception(
                    "external unread reminder unread count failed user_id=%s email=%s",
                    user_id,
                    email,
                )
                continue

            if unread_count <= 0:
                summary["skipped_no_unread"] += 1
                current_app.logger.info(
                    "external unread reminder skipped user_id=%s email=%s unread_count=%s reason=unreadなし",
                    user_id,
                    email,
                    unread_count,
                )
                continue

            if not _can_send_external_unread_reminder(last_sent_at, now_utc):
                summary["skipped_too_soon"] += 1
                current_app.logger.info(
                    "external unread reminder skipped user_id=%s email=%s unread_count=%s reason=last sent < 2 days ago last_sent_at=%s",
                    user_id,
                    email,
                    unread_count,
                    last_sent_at.isoformat() if last_sent_at else None,
                )
                continue

            try:
                send_external_unread_reminder_mail(email, external_login_user_id=user_id)
                try:
                    update_cur.execute(
                        f"""
                        UPDATE external_login_user
                           SET {_EXTERNAL_UNREAD_REMINDER_COLUMN}=%s
                         WHERE id=%s
                        """,
                        (now_utc.replace(tzinfo=None), user_id),
                    )
                    update_db.commit()
                except Exception:
                    update_db.rollback()
                    raise
                summary["sent"] += 1
                current_app.logger.info(
                    "external unread reminder sent user_id=%s email=%s unread_count=%s result=success",
                    user_id,
                    email,
                    unread_count,
                )
            except Exception:
                summary["failed"] += 1
                current_app.logger.exception(
                    "external unread reminder failed user_id=%s email=%s unread_count=%s result=failed",
                    user_id,
                    email,
                    unread_count,
                )
    finally:
        update_cur.close()
        update_db.close()

    current_app.logger.info(
        "external unread reminder job finished candidates=%s sent=%s failed=%s skipped_no_mail=%s skipped_too_soon=%s skipped_no_unread=%s skipped_invalid_user=%s",
        summary["candidates"],
        summary["sent"],
        summary["failed"],
        summary["skipped_no_mail"],
        summary["skipped_too_soon"],
        summary["skipped_no_unread"],
        summary["skipped_invalid_user"],
    )
    return summary


def _compute_unread_count_mfu(username: str) -> int:
    recipient = (username or "").strip()
    if not recipient:
        return 0
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM mfu_notifications
             WHERE user_kind='mfu'
               AND recipient_key=%s
               AND read_at IS NULL
            """,
            (recipient,),
        )
        return int((cur.fetchone() or {}).get("cnt") or 0)
    finally:
        cur.close()
        db.close()


def _emit_notif_unread_mfu(username: str, reason: str = "sync", latest_id: int | None = None) -> None:
    if socketio is None:
        return
    recipient = (username or "").strip()
    if not recipient:
        return
    try:
        count = _compute_unread_count_mfu(recipient)
        socketio.emit(
            "notif_unread",
            {"count": count, "latest_id": latest_id, "reason": reason, "scope": "mfu"},
            room=f"mfu_user:{recipient}",
        )
    except Exception:
        current_app.logger.warning(
            "mfu notifications emit failed username=%s reason=%s latest_id=%s",
            recipient,
            reason,
            latest_id,
            exc_info=True,
        )


def _serialize_mfu_notification_item(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row.get("created_at")
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    read_at = row.get("read_at")
    if read_at and read_at.tzinfo is None:
        read_at = read_at.replace(tzinfo=timezone.utc)
    return {
        "id": int(row.get("id") or 0),
        "kind": row.get("kind") or "general",
        "title": row.get("title") or "お知らせ",
        "body": row.get("body") or "",
        "target_url": row.get("target_url") or "/",
        "sender_label": row.get("sender_label") or "",
        "room_type": row.get("room_type") or "",
        "room_id": row.get("room_id") or row.get("chat_room_id") or "",
        "is_read": bool(read_at),
        "created_at": created_at.isoformat() if created_at else None,
        "relative_time": _relative_time_from(created_at),
    }


def _emit_notif_new_mfu(
    recipient: str,
    *,
    notification_id: int | None,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    sender_label: str,
    room_type: str | None,
    room_id: str | None,
) -> None:
    if socketio is None or not recipient:
        return
    socketio.emit(
        "notif_new",
        {"item": _serialize_mfu_notification_item({
            "id": notification_id,
            "kind": kind,
            "title": title,
            "body": body,
            "target_url": target_url,
            "sender_label": sender_label,
            "room_type": room_type,
            "room_id": room_id,
            "read_at": None,
            "created_at": _now_utc(),
        })},
        room=f"mfu_user:{recipient}",
    )


def _record_notification_delivery(
    *,
    notification_id: int | None,
    dedup_key: str,
    recipient_type: str,
    recipient_value: str | int,
    channel: str,
    status: str,
    detail: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        _ensure_notification_delivery_schema(cur)
        cur.execute(
            """
            INSERT INTO mfu_notification_deliveries (
              notification_id, dedup_key, recipient_type, recipient_value,
              channel, status, detail, created_at, sent_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(notification_id) if notification_id else None,
                (dedup_key or "").strip()[:191],
                (recipient_type or "").strip()[:32],
                str(recipient_value or "")[:191],
                (channel or "").strip()[:32],
                (status or "").strip()[:32],
                (detail or "").strip()[:65535] or None,
                datetime.utcnow(),
                sent_at,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.warning(
            "notification delivery log failed dedup_key=%s recipient_type=%s recipient_value=%s channel=%s status=%s",
            (dedup_key or "").strip()[:191],
            (recipient_type or "").strip()[:32],
            str(recipient_value or "")[:191],
            (channel or "").strip()[:32],
            (status or "").strip()[:32],
            exc_info=True,
        )
    finally:
        cur.close()
        db.close()


def _has_notification_delivery_attempt(
    *,
    dedup_key: str,
    recipient_type: str,
    recipient_value: str | int,
    channel: str,
) -> bool:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        _ensure_notification_delivery_schema(cur)
        cur.execute(
            """
            SELECT id
              FROM mfu_notification_deliveries
             WHERE dedup_key=%s
               AND recipient_type=%s
               AND recipient_value=%s
               AND channel=%s
             LIMIT 1
            """,
            (
                (dedup_key or "").strip()[:191],
                (recipient_type or "").strip()[:32],
                str(recipient_value or "")[:191],
                (channel or "").strip()[:32],
            ),
        )
        return bool(cur.fetchone())
    finally:
        cur.close()
        db.close()


def _create_notification_core(
    *,
    user_kind: str,
    user_id: int,
    recipient_key: str | None,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    dedup_key: str,
    event_id: int | None = None,
    chat_event_id: int | None = None,
    chat_room_id: str | None = None,
    room_type: str | None = None,
    room_id: str | None = None,
    sender_label: str = "",
) -> dict[str, Any]:
    _ensure_notification_schema()
    normalized_kind = (kind or "").strip() or "general"
    normalized_recipient_key = _notification_recipient_key(user_kind, user_id, recipient_key)
    storage_user_id = _notification_storage_user_id(user_kind, user_id, normalized_recipient_key)
    normalized_chat_room_id = str(chat_room_id).strip()[:64] if chat_room_id else None
    normalized_room_type = (room_type or "").strip()[:32] or None
    normalized_room_id = (room_id or "").strip()[:64] or normalized_chat_room_id or None
    normalized_sender_label = (sender_label or "").strip()[:255]
    dedup = (dedup_key or "").strip()[:191]
    if not dedup:
        return {"ok": False, "reason": "dedup_key_required"}
    if normalized_kind == "chat_message" and not normalized_chat_room_id:
        return {"ok": False, "reason": "chat_room_id_required"}

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            INSERT INTO mfu_notifications (
              user_kind, user_id, recipient_key, kind, title, body, target_url,
              event_id, chat_event_id, chat_room_id, room_type, room_id, sender_label,
              dedup_key, created_at, read_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """,
            (
                user_kind,
                storage_user_id,
                normalized_recipient_key or None,
                normalized_kind,
                (title or "").strip()[:255] or "お知らせ",
                (body or "").strip(),
                (target_url or "").strip() or "/",
                int(event_id) if event_id else None,
                int(chat_event_id) if chat_event_id else None,
                normalized_chat_room_id,
                normalized_room_type,
                normalized_room_id,
                normalized_sender_label or None,
                dedup,
                datetime.utcnow(),
            ),
        )
        inserted = cur.rowcount == 1
        notification_id = int(cur.lastrowid) if cur.lastrowid else None
        deleted_old_read = 0
        if inserted and user_kind == "external":
            deleted_old_read = _prune_old_read_notifications(cur, int(storage_user_id))
        db.commit()
        if inserted:
            if user_kind == "external":
                _emit_notif_unread(int(storage_user_id), reason="created", latest_id=notification_id)
            elif user_kind == "mfu" and normalized_recipient_key:
                _emit_notif_unread_mfu(normalized_recipient_key, reason="created", latest_id=notification_id)
                _emit_notif_new_mfu(
                    normalized_recipient_key,
                    notification_id=notification_id,
                    kind=normalized_kind,
                    title=(title or "").strip()[:255] or "お知らせ",
                    body=(body or "").strip(),
                    target_url=(target_url or "").strip() or "/",
                    sender_label=normalized_sender_label,
                    room_type=normalized_room_type,
                    room_id=normalized_room_id,
                )
        current_app.logger.info(
            "notifications core user_kind=%s recipient_key=%s storage_user_id=%s kind=%s dedup_key=%s inserted=%s notification_id=%s pruned_read=%s",
            user_kind,
            normalized_recipient_key,
            storage_user_id,
            normalized_kind,
            dedup,
            inserted,
            notification_id,
            deleted_old_read,
        )
        return {
            "ok": True,
            "created": inserted,
            "duplicate": not inserted,
            "notification_id": notification_id if inserted else None,
            "existing_notification_id": notification_id if not inserted else None,
            "user_kind": user_kind,
            "recipient_key": normalized_recipient_key,
            "storage_user_id": storage_user_id,
        }
    except Exception:
        db.rollback()
        current_app.logger.warning(
            "create notification core failed user_kind=%s recipient_key=%s storage_user_id=%s kind=%s",
            user_kind,
            normalized_recipient_key,
            storage_user_id,
            normalized_kind,
            exc_info=True,
        )
        return {"ok": False, "reason": "db_error"}
    finally:
        cur.close()
        db.close()


def create_notification_dispatch_result(
    *,
    recipient_type: str,
    recipient_value: str | int,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    dedup_key: str,
    sender_label: str = "",
    room_type: str | None = None,
    room_id: str | None = None,
    event_id: int | None = None,
    chat_event_id: int | None = None,
    chat_room_id: str | None = None,
) -> dict[str, Any]:
    if recipient_type == "external_user_id":
        return _create_notification_core(
            user_kind="external",
            user_id=int(recipient_value),
            recipient_key=str(int(recipient_value)),
            kind=kind,
            title=title,
            body=body,
            target_url=target_url,
            dedup_key=dedup_key,
            event_id=event_id,
            chat_event_id=chat_event_id,
            chat_room_id=chat_room_id,
            room_type=room_type,
            room_id=room_id,
            sender_label=sender_label,
        )
    if recipient_type == "mfu_username":
        return _create_notification_core(
            user_kind="mfu",
            user_id=0,
            recipient_key=str(recipient_value or "").strip(),
            kind=kind,
            title=title,
            body=body,
            target_url=target_url,
            dedup_key=dedup_key,
            event_id=event_id,
            chat_event_id=chat_event_id,
            chat_room_id=chat_room_id,
            room_type=room_type,
            room_id=room_id,
            sender_label=sender_label,
        )
    return {"ok": False, "reason": "unsupported_recipient_type"}


def create_notification_mfu(
    *,
    recipient_username: str,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    sender_label: str = "",
    room_type: str | None = None,
    room_id: str | None = None,
    dedup_key: str | None = None,
) -> bool:
    recipient = (recipient_username or "").strip()
    if not recipient:
        return False

    dedup = ((dedup_key or "").strip() or f"mfu:{recipient}:{kind}:{hash((title, body, target_url))}")[:191]
    result = create_notification_dispatch_result(
        recipient_type="mfu_username",
        recipient_value=recipient,
        kind=kind,
        title=title,
        body=(body or "").strip()[:300],
        target_url=(target_url or "").strip() or "/",
        sender_label=(sender_label or "").strip()[:255],
        room_type=(room_type or "").strip() or None,
        room_id=(room_id or "").strip() or None,
        dedup_key=dedup,
    )
    if not result.get("ok"):
        current_app.logger.warning(
            "create_notification_mfu failed username=%s reason=%s",
            recipient,
            result.get("reason"),
        )
    return bool(result.get("created"))


def _require_internal_api_key() -> tuple[bool, Any | None]:
    configured_key = str(os.getenv("MFU_INTERNAL_API_KEY") or "").strip()
    if not configured_key:
        return False, (jsonify({"ok": False, "reason": "server_not_configured"}), 503)
    provided_key = str(request.headers.get("X-MFU-Internal-Key") or "").strip()
    if not provided_key:
        return False, (jsonify({"ok": False, "reason": "missing_internal_key"}), 401)
    if not hmac.compare_digest(provided_key, configured_key):
        return False, (jsonify({"ok": False, "reason": "invalid_internal_key"}), 403)
    return True, None


@mfu_notifications_bp.post("/api/internal/push/send")
def api_internal_push_send():
    allowed, error = _require_internal_api_key()
    if not allowed:
        return error

    payload = request.get_json(silent=True) or {}
    try:
        from app.utils.push import PushDispatchError, send_push

        result = send_push(
            recipient_type=payload.get("recipient_type"),
            recipient_value=payload.get("recipient_value"),
            title=payload.get("title"),
            body=payload.get("body"),
            target_url=payload.get("target_url"),
            kind=payload.get("kind", "general"),
            sender_label=payload.get("sender_label"),
            dedup_key=payload.get("dedup_key"),
            room_type=payload.get("room_type"),
            room_id=payload.get("room_id"),
            event_id=payload.get("event_id"),
            chat_event_id=payload.get("chat_event_id"),
            chat_room_id=payload.get("chat_room_id"),
            create_in_app=payload.get("create_in_app", True),
            send_web_push=payload.get("send_web_push", True),
        )
        return jsonify(result)
    except PushDispatchError as exc:
        response = {"ok": False, "reason": exc.reason}
        if exc.detail:
            response["detail"] = exc.detail
        return jsonify(response), int(exc.status_code)
    except Exception:
        current_app.logger.exception("internal push send failed")
        return jsonify({"ok": False, "reason": "internal_error"}), 500


@bp.get("/notifications")
def notifications_page():
    return redirect("/notifications")


@mfu_notifications_bp.get("/notifications")
def notifications_unified_page():
    _ensure_notification_schema()
    scope = _resolve_notification_scope_for_session()
    if scope == "external":
        guard = _require_ext_login()
        if guard:
            return guard
    elif scope == "mfu":
        username, error = _require_mfu_admin_acl()
        if error:
            return error
    else:
        return jsonify({"ok": False, "reason": "login_required"}), 401

    current_app.logger.info("notifications page render scope=%s", scope)
    return render_template(
        "notifications.html",
        notification_scope=scope,
        notification_api_map=_resolve_notification_api_mode_for_session(),
    )


@mfu_notifications_bp.get("/mfu-notifications")
def mfu_notifications_page():
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    current_app.logger.info("notifications page render scope=%s user=%s", "mfu", username)
    return render_template(
        "notifications.html",
        notification_scope="mfu",
        notification_api_map=_resolve_notification_api_mode_for_session(),
        mfu_notification_user=username,
    )


@mfu_notifications_bp.get("/api/mfu-notifications/unread-count")
def api_mfu_notifications_unread_count():
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    return jsonify({"ok": True, "unread_count": _compute_unread_count_mfu(username)})


def _fetch_mfu_notifications(recipient: str, *, limit: int = 20, since_id: int = 0, unread_only: bool = False) -> tuple[list[dict[str, Any]], int | None]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        where_sql = "user_kind='mfu' AND recipient_key=%s"
        params: list[Any] = [recipient]
        if unread_only:
            where_sql += " AND read_at IS NULL"
        if since_id > 0:
            where_sql += " AND id > %s"
            params.append(int(since_id))
            order_sql = "ORDER BY id ASC"
        else:
            order_sql = "ORDER BY id DESC"
        cur.execute(
            f"""
            SELECT id, kind, title, body, target_url, sender_label, room_type, room_id,
                   chat_room_id, created_at, read_at
              FROM mfu_notifications
             WHERE {where_sql}
             {order_sql}
             LIMIT %s
            """,
            tuple(params + [int(limit)]),
        )
        rows = cur.fetchall() or []
        items = [_serialize_mfu_notification_item(r) for r in rows]
        if since_id <= 0:
            items = sorted(items, key=lambda x: int(x.get("id") or 0), reverse=True)
        latest_id = max((int(x.get("id") or 0) for x in items), default=0) or None
        return items, latest_id
    finally:
        cur.close()
        db.close()


@mfu_notifications_bp.get("/api/mfu-notifications")
def api_mfu_notifications_list():
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    unread_only = _to_unread_only(request.args.get("unread"), default=False)
    current_app.logger.info("mfu notifications list user=%s unread_only=%s", username, unread_only)
    items, latest_id = _fetch_mfu_notifications(username, limit=20, since_id=0, unread_only=unread_only)
    return jsonify({
        "ok": True,
        "items": items,
        "unread_count": _compute_unread_count_mfu(username),
        "latest_id": latest_id,
        "pagination": {"has_next": False},
    })


@mfu_notifications_bp.get("/api/mfu-notifications/updates")
def api_mfu_notifications_updates():
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    since_id = max(int(request.args.get("since_id") or 0), 0)
    unread_only = _to_unread_only(request.args.get("unread"), default=False)
    items, latest_id = _fetch_mfu_notifications(username, limit=20, since_id=since_id, unread_only=unread_only)
    return jsonify({
        "ok": True,
        "items": items,
        "unread_count": _compute_unread_count_mfu(username),
        "latest_id": latest_id or since_id,
    })


@mfu_notifications_bp.post("/api/mfu-notifications/<int:notification_id>/read")
def api_mfu_notifications_mark_read(notification_id: int):
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=COALESCE(read_at, %s)
             WHERE id=%s AND user_kind='mfu' AND recipient_key=%s
            """,
            (datetime.utcnow(), int(notification_id), username),
        )
        db.commit()
        if int(cur.rowcount or 0) == 0:
            abort(404)
        _emit_notif_unread_mfu(username, reason="read", latest_id=notification_id)
        current_app.logger.info("notification mark-read success notification_id=%s scope=%s user=%s", notification_id, "mfu", username)
        return jsonify({"ok": True})
    finally:
        cur.close()
        db.close()


@mfu_notifications_bp.post("/api/mfu-notifications/read-all")
def api_mfu_notifications_mark_all_read():
    _ensure_notification_schema()
    username, error = _require_mfu_admin_acl()
    if error:
        return error
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=%s
             WHERE user_kind='mfu' AND recipient_key=%s AND read_at IS NULL
            """,
            (datetime.utcnow(), username),
        )
        updated = int(cur.rowcount or 0)
        db.commit()
        _emit_notif_unread_mfu(username, reason="read_all")
        return jsonify({"ok": True, "updated": updated})
    finally:
        cur.close()
        db.close()


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
    unread_only = _to_unread_only(request.args.get("unread"), default=True)
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
        current_app.logger.info("notification mark-read success notification_id=%s scope=%s user_id=%s", notification_id, "external", uid)
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

    event_id = None
    if event_id_raw not in (None, "", 0, "0"):
        try:
            event_id = int(event_id_raw)
        except Exception:
            resp = jsonify({"ok": False, "reason": "invalid_event_id"})
            resp.status_code = 400
            return resp

    _ensure_notification_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT event_id, is_main FROM chat_rooms WHERE room_id=%s LIMIT 1",
            (room_id,),
        )
        room = cur.fetchone() or {}
        if not room:
            resp = jsonify({"ok": False, "reason": "room_not_found"})
            resp.status_code = 404
            return resp

        room_event_id = int(room.get("event_id") or 0)
        if event_id and room_event_id and event_id != room_event_id:
            resp = jsonify({"ok": False, "reason": "event_room_mismatch"})
            resp.status_code = 400
            return resp

        is_main = int(room.get("is_main") or 0)
        if is_main != 1:
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
            if not cur.fetchone():
                resp = jsonify({"ok": False, "reason": "forbidden"})
                resp.status_code = 403
                return resp

        now = datetime.utcnow()
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=%s
             WHERE user_kind='external'
               AND user_id=%s
               AND kind='chat_message'
               AND chat_room_id=%s
               AND read_at IS NULL
            """,
            (now, uid, room_id),
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
        current_app.logger.info(
            "notifications read-by-room user_id=%s room_id=%s event_id=%s updated_count=%s unread_count=%s latest_id=%s",
            uid,
            room_id,
            event_id,
            updated_count,
            unread_count,
            latest_id,
        )

        return jsonify(
            {
                "ok": True,
                "updated_count": updated_count,
                "unread_count": unread_count,
                "latest_id": latest_id,
            }
        )
    except Exception:
        current_app.logger.warning(
            "notifications read-by-room failed user_id=%s room_id=%s event_id=%s",
            uid,
            room_id,
            event_id,
            exc_info=True,
        )
        raise
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/read-dm-room")
def api_notifications_mark_read_dm_room():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id or not room_id.startswith("dm:"):
        resp = jsonify({"ok": False, "reason": "invalid_room_id"})
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
               AND kind='dm'
               AND chat_room_id=%s
               AND read_at IS NULL
            """,
            (now, uid, room_id),
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
        _emit_notif_unread(uid, reason="dm_room_read", latest_id=latest_id)
        current_app.logger.info(
            "notifications read-dm-room user_id=%s room_id=%s updated_count=%s unread_count=%s latest_id=%s",
            uid,
            room_id,
            updated_count,
            unread_count,
            latest_id,
        )
        return jsonify({
            "ok": True,
            "updated_count": updated_count,
            "unread_count": unread_count,
            "latest_id": latest_id,
        })
    except Exception:
        current_app.logger.warning(
            "notifications read-dm-room failed user_id=%s room_id=%s",
            uid,
            room_id,
            exc_info=True,
        )
        raise
    finally:
        cur.close()
        db.close()
