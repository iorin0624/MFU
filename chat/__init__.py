from __future__ import annotations

import html
import io
import hashlib
import json
import os
import random
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import disconnect, emit, join_room
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError as RequestsSSLError
from requests.exceptions import Timeout as RequestsTimeout

from app.chat.socketio_ext import socketio
from app.utils.db import get_db


HEIC_UNSUPPORTED_MESSAGE = "iPhoneのHEIC画像は未対応です。設定→カメラ→フォーマット→互換性優先(JPEG)にするか、JPEGで送ってください"
HEIF_OPENER_AVAILABLE = False
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_OPENER_AVAILABLE = True
except Exception:
    HEIF_OPENER_AVAILABLE = False

chat_bp = Blueprint(
    "chat",
    __name__,
    template_folder="templates",
    static_folder="static",
    url_prefix="/chat",
)

MESSAGE_MAX_LEN = 2000
RATE_LIMIT_SECONDS = 1
JST = ZoneInfo("Asia/Tokyo")

CHAT_REPLY_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_REPLY_SCHEMA_READY: bool | None = None
CHAT_PUSH_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_PUSH_SCHEMA_READY: bool | None = None
CHAT_NOTIFICATION_LOG_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_NOTIFICATION_LOG_SCHEMA_READY: bool | None = None
CHAT_READ_STATE_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_READ_STATE_SCHEMA_READY: bool | None = None
CHAT_REACTION_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_REACTION_SCHEMA_READY: bool | None = None
CHAT_IMAGE_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_IMAGE_SCHEMA_READY: bool | None = None
CHAT_MESSAGE_IMAGES_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_MESSAGE_IMAGES_SCHEMA_READY: bool | None = None
CHAT_DELETE_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_DELETE_SCHEMA_READY: bool | None = None
DEFAULT_AVATAR_URL = "/static/img/avatar_default.png"
CHAT_ALLOWED_REACTION_EMOJIS = ("💕", "👍", "😆", "😭", "😢", "🫶")
CHAT_UPLOAD_DIR = os.getenv("CHAT_UPLOAD_DIR", "/mnt/mfu/chat_uploads")
CHAT_UPLOAD_MAX_BYTES = max(int(os.getenv("CHAT_UPLOAD_MAX_BYTES", "10485760")), 1)
PUSH_REQUEST_TIMEOUT_SECONDS = 6
PUSH_RETRY_BACKOFF_SECONDS = (0.3, 1.0)
PUSH_LOG_SUPPRESSION_SECONDS = 300
PUSH_ENDPOINT_ERROR_LOCK = threading.Lock()
PUSH_ENDPOINT_ERROR_STATS: dict[str, dict[str, float | int]] = {}
PUSH_ASYNC_MAX_WORKERS = max(int(os.getenv("CHAT_PUSH_ASYNC_MAX_WORKERS", "4")), 1)
PUSH_ASYNC_EXECUTOR = ThreadPoolExecutor(max_workers=PUSH_ASYNC_MAX_WORKERS, thread_name_prefix="chat-push")
PUSH_ASYNC_INFLIGHT_LOCK = threading.Lock()
PUSH_ASYNC_INFLIGHT = 0
_LINKIFY_RE = re.compile(
    r"(?P<url>(?:https?://|www\.)[^\s<]+)|(?P<email>[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)
_LINKIFY_TRAILING_CHARS = ".,!?)]}、。！？）】」』〉》〕＞\"'”’"


def _default_avatar_url() -> str:
    return DEFAULT_AVATAR_URL


def _build_ext_avatar_url(row: dict[str, Any] | None) -> str:
    if not row:
        return _default_avatar_url()
    avatar_file = (row.get("avatar_file") or "").strip()
    if avatar_file:
        try:
            return url_for("external_login_user.avatar_file", name=avatar_file)
        except Exception:
            current_app.logger.warning("chat avatar url_for failed name=%s", avatar_file, exc_info=True)
    avatar_url = (row.get("avatar_url") or "").strip()
    if avatar_url:
        return avatar_url
    return _default_avatar_url()


def _resolve_sender_avatar_url(actor_type: str, actor_id: str, avatar_cache: dict[str, str] | None = None) -> str:
    key = f"{actor_type}:{actor_id}"
    if avatar_cache is not None and key in avatar_cache:
        return avatar_cache[key]

    avatar_url = _default_avatar_url()
    if actor_type == "line":
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT avatar_file, avatar_url FROM external_login_user WHERE id=%s LIMIT 1",
                (actor_id,),
            )
            avatar_url = _build_ext_avatar_url(cur.fetchone())
        except Exception:
            current_app.logger.warning("chat avatar lookup failed actor=%s:%s", actor_type, actor_id, exc_info=True)
        finally:
            cur.close()
            db.close()

    if avatar_cache is not None:
        avatar_cache[key] = avatar_url
    return avatar_url


def _ensure_chat_reply_schema() -> bool:
    global CHAT_REPLY_SCHEMA_READY
    if CHAT_REPLY_SCHEMA_READY is not None:
        return CHAT_REPLY_SCHEMA_READY

    with CHAT_REPLY_SCHEMA_CHECK_LOCK:
        if CHAT_REPLY_SCHEMA_READY is not None:
            return CHAT_REPLY_SCHEMA_READY

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'reply_to_message_id'")
            if cur.fetchone():
                CHAT_REPLY_SCHEMA_READY = True
                return True

            try:
                cur.execute(
                    "ALTER TABLE chat_messages ADD COLUMN reply_to_message_id BIGINT NULL AFTER body"
                )
            except Exception:
                current_app.logger.warning('chat auto-migration: add reply_to_message_id failed', exc_info=True)

            try:
                cur.execute(
                    "ALTER TABLE chat_messages ADD KEY idx_chat_messages_reply_to (reply_to_message_id)"
                )
            except Exception:
                current_app.logger.warning('chat auto-migration: add idx_chat_messages_reply_to failed', exc_info=True)

            try:
                cur.execute(
                    """
                    ALTER TABLE chat_messages
                    ADD CONSTRAINT fk_chat_messages_reply_to
                    FOREIGN KEY (reply_to_message_id) REFERENCES chat_messages(id)
                    ON DELETE SET NULL
                    """
                )
            except Exception:
                current_app.logger.warning('chat auto-migration: add fk_chat_messages_reply_to failed', exc_info=True)

            db.commit()
            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'reply_to_message_id'")
            CHAT_REPLY_SCHEMA_READY = bool(cur.fetchone())
            return CHAT_REPLY_SCHEMA_READY
        except Exception:
            current_app.logger.warning('chat schema check failed', exc_info=True)
            CHAT_REPLY_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_push_schema() -> bool:
    global CHAT_PUSH_SCHEMA_READY
    if CHAT_PUSH_SCHEMA_READY is not None:
        return CHAT_PUSH_SCHEMA_READY

    with CHAT_PUSH_SCHEMA_CHECK_LOCK:
        if CHAT_PUSH_SCHEMA_READY is not None:
            return CHAT_PUSH_SCHEMA_READY

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  actor_type VARCHAR(16) NOT NULL,
                  actor_id VARCHAR(64) NOT NULL,
                  endpoint TEXT NOT NULL,
                  endpoint_hash CHAR(64) NOT NULL,
                  sw_scope VARCHAR(255) NOT NULL DEFAULT '/',
                  p256dh VARCHAR(255) NOT NULL,
                  auth VARCHAR(255) NOT NULL,
                  user_agent VARCHAR(255) NULL,
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL,
                  UNIQUE KEY uq_chat_push (actor_type, actor_id, endpoint_hash),
                  KEY idx_chat_push_actor (actor_type, actor_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            try:
                cur.execute("SHOW COLUMNS FROM chat_push_subscriptions LIKE 'sw_scope'")
                has_sw_scope = bool(cur.fetchone())
                if not has_sw_scope:
                    cur.execute(
                        "ALTER TABLE chat_push_subscriptions ADD COLUMN sw_scope VARCHAR(255) NOT NULL DEFAULT '/' AFTER endpoint_hash"
                    )
            except Exception:
                current_app.logger.warning("chat push schema ensure sw_scope failed", exc_info=True)

            try:
                cur.execute("SHOW COLUMNS FROM chat_push_subscriptions LIKE 'endpoint_hash'")
                has_endpoint_hash = bool(cur.fetchone())
                if not has_endpoint_hash:
                    cur.execute(
                        "ALTER TABLE chat_push_subscriptions ADD COLUMN endpoint_hash CHAR(64) NOT NULL AFTER endpoint"
                    )
                    cur.execute(
                        "UPDATE chat_push_subscriptions SET endpoint_hash=SHA2(endpoint, 256) WHERE endpoint_hash='' OR endpoint_hash IS NULL"
                    )
            except Exception:
                current_app.logger.warning("chat push schema ensure endpoint_hash failed", exc_info=True)

            try:
                cur.execute(
                    "ALTER TABLE chat_push_subscriptions ADD UNIQUE KEY uq_chat_push (actor_type, actor_id, endpoint_hash)"
                )
            except Exception:
                pass

            try:
                cur.execute("ALTER TABLE chat_push_subscriptions ADD KEY idx_chat_push_actor (actor_type, actor_id)")
            except Exception:
                pass

            db.commit()
            CHAT_PUSH_SCHEMA_READY = True
            return True
        except Exception:
            current_app.logger.warning("chat push schema ensure failed", exc_info=True)
            CHAT_PUSH_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_notification_log_schema() -> bool:
    global CHAT_NOTIFICATION_LOG_SCHEMA_READY
    if CHAT_NOTIFICATION_LOG_SCHEMA_READY is not None:
        return CHAT_NOTIFICATION_LOG_SCHEMA_READY

    with CHAT_NOTIFICATION_LOG_SCHEMA_CHECK_LOCK:
        if CHAT_NOTIFICATION_LOG_SCHEMA_READY is not None:
            return CHAT_NOTIFICATION_LOG_SCHEMA_READY

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_notification_log (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  event_id BIGINT NOT NULL,
                  kind VARCHAR(32) NOT NULL,
                  payload_json TEXT NOT NULL,
                  sent_count INT NOT NULL DEFAULT 0,
                  created_at DATETIME NOT NULL,
                  KEY idx_chat_notification_event (event_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            try:
                cur.execute("ALTER TABLE chat_notification_log ADD KEY idx_chat_notification_event (event_id, created_at)")
            except Exception:
                pass

            db.commit()
            CHAT_NOTIFICATION_LOG_SCHEMA_READY = True
            return True
        except Exception:
            current_app.logger.warning("chat notification log schema ensure failed", exc_info=True)
            CHAT_NOTIFICATION_LOG_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_reaction_schema() -> bool:
    global CHAT_REACTION_SCHEMA_READY
    if CHAT_REACTION_SCHEMA_READY is not None:
        return CHAT_REACTION_SCHEMA_READY

    with CHAT_REACTION_SCHEMA_CHECK_LOCK:
        if CHAT_REACTION_SCHEMA_READY is not None:
            return CHAT_REACTION_SCHEMA_READY

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_message_reactions (
                  event_id BIGINT NOT NULL,
                  message_id BIGINT NOT NULL,
                  actor_type VARCHAR(16) NOT NULL,
                  actor_id VARCHAR(64) NOT NULL,
                  emoji VARCHAR(16) NOT NULL,
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL,
                  UNIQUE KEY uq_chat_message_reactions_message_actor (message_id, actor_type, actor_id),
                  KEY idx_chat_message_reactions_event_message (event_id, message_id),
                  KEY idx_chat_message_reactions_message (message_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            db.commit()
            CHAT_REACTION_SCHEMA_READY = True
            return True
        except Exception:
            current_app.logger.warning("chat reaction schema ensure failed", exc_info=True)
            CHAT_REACTION_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_image_schema() -> bool:
    global CHAT_IMAGE_SCHEMA_READY
    if CHAT_IMAGE_SCHEMA_READY is not None:
        return CHAT_IMAGE_SCHEMA_READY

    with CHAT_IMAGE_SCHEMA_CHECK_LOCK:
        if CHAT_IMAGE_SCHEMA_READY is not None:
            return CHAT_IMAGE_SCHEMA_READY

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            columns = {
                "image_file": "ALTER TABLE chat_messages ADD COLUMN image_file VARCHAR(255) NULL AFTER body",
                "image_thumb_file": "ALTER TABLE chat_messages ADD COLUMN image_thumb_file VARCHAR(255) NULL AFTER image_file",
                "image_mime": "ALTER TABLE chat_messages ADD COLUMN image_mime VARCHAR(64) NULL AFTER image_thumb_file",
                "image_size": "ALTER TABLE chat_messages ADD COLUMN image_size BIGINT NULL AFTER image_mime",
                "image_width": "ALTER TABLE chat_messages ADD COLUMN image_width INT NULL AFTER image_size",
                "image_height": "ALTER TABLE chat_messages ADD COLUMN image_height INT NULL AFTER image_width",
            }
            for column_name, ddl in columns.items():
                try:
                    cur.execute(f"SHOW COLUMNS FROM chat_messages LIKE '{column_name}'")
                    if not cur.fetchone():
                        cur.execute(ddl)
                except Exception:
                    current_app.logger.warning("chat image schema ensure %s failed", column_name, exc_info=True)

            db.commit()
            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'image_file'")
            CHAT_IMAGE_SCHEMA_READY = bool(cur.fetchone())
            return CHAT_IMAGE_SCHEMA_READY
        except Exception:
            current_app.logger.warning("chat image schema ensure failed", exc_info=True)
            CHAT_IMAGE_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_message_images_schema() -> bool:
    global CHAT_MESSAGE_IMAGES_SCHEMA_READY
    if CHAT_MESSAGE_IMAGES_SCHEMA_READY is not None:
        return CHAT_MESSAGE_IMAGES_SCHEMA_READY

    with CHAT_MESSAGE_IMAGES_SCHEMA_CHECK_LOCK:
        if CHAT_MESSAGE_IMAGES_SCHEMA_READY is not None:
            return CHAT_MESSAGE_IMAGES_SCHEMA_READY

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_message_images (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  event_id BIGINT NOT NULL,
                  message_id BIGINT NOT NULL,
                  seq INT NOT NULL,
                  image_file VARCHAR(255) NOT NULL,
                  image_thumb_file VARCHAR(255) NOT NULL,
                  image_mime VARCHAR(64) NOT NULL,
                  image_size BIGINT NOT NULL,
                  image_width INT NOT NULL,
                  image_height INT NOT NULL,
                  created_at DATETIME NOT NULL,
                  UNIQUE KEY uq_chat_message_images_message_seq (message_id, seq),
                  KEY idx_chat_message_images_event_message (event_id, message_id),
                  KEY idx_chat_message_images_message (message_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            db.commit()
            CHAT_MESSAGE_IMAGES_SCHEMA_READY = True
            return True
        except Exception:
            current_app.logger.warning("chat message images schema ensure failed", exc_info=True)
            CHAT_MESSAGE_IMAGES_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_delete_schema() -> bool:
    global CHAT_DELETE_SCHEMA_READY
    if CHAT_DELETE_SCHEMA_READY is not None:
        return CHAT_DELETE_SCHEMA_READY

    with CHAT_DELETE_SCHEMA_CHECK_LOCK:
        if CHAT_DELETE_SCHEMA_READY is not None:
            return CHAT_DELETE_SCHEMA_READY

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            definitions = {
                "deleted_flag": "ALTER TABLE chat_messages ADD COLUMN deleted_flag TINYINT NOT NULL DEFAULT 0 AFTER created_at",
                "deleted_at": "ALTER TABLE chat_messages ADD COLUMN deleted_at DATETIME NULL AFTER deleted_flag",
                "deleted_by_actor_type": "ALTER TABLE chat_messages ADD COLUMN deleted_by_actor_type VARCHAR(16) NULL AFTER deleted_at",
                "deleted_by_actor_id": "ALTER TABLE chat_messages ADD COLUMN deleted_by_actor_id VARCHAR(64) NULL AFTER deleted_by_actor_type",
            }
            for column_name, ddl in definitions.items():
                cur.execute(f"SHOW COLUMNS FROM chat_messages LIKE '{column_name}'")
                if cur.fetchone():
                    continue
                try:
                    cur.execute(ddl)
                except Exception:
                    current_app.logger.warning("chat delete schema ensure column failed column=%s", column_name, exc_info=True)

            db.commit()
            missing_columns: list[str] = []
            for column_name in definitions:
                cur.execute(f"SHOW COLUMNS FROM chat_messages LIKE '{column_name}'")
                if not cur.fetchone():
                    missing_columns.append(column_name)
            CHAT_DELETE_SCHEMA_READY = len(missing_columns) == 0
            if missing_columns:
                current_app.logger.warning("chat delete schema ensure missing columns=%s", ",".join(missing_columns))
            return CHAT_DELETE_SCHEMA_READY
        except Exception:
            current_app.logger.warning("chat delete schema ensure failed", exc_info=True)
            CHAT_DELETE_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_read_state_schema() -> bool:
    global CHAT_READ_STATE_SCHEMA_READY
    if CHAT_READ_STATE_SCHEMA_READY is not None:
        return CHAT_READ_STATE_SCHEMA_READY

    with CHAT_READ_STATE_SCHEMA_CHECK_LOCK:
        if CHAT_READ_STATE_SCHEMA_READY is not None:
            return CHAT_READ_STATE_SCHEMA_READY

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_read_state (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  event_id BIGINT NOT NULL,
                  actor_type VARCHAR(16) NOT NULL,
                  actor_id VARCHAR(64) NOT NULL,
                  last_read_message_id BIGINT NOT NULL DEFAULT 0,
                  updated_at DATETIME NOT NULL,
                  UNIQUE KEY uq_chat_read_state_actor (event_id, actor_type, actor_id),
                  KEY idx_chat_read_state_event_message (event_id, last_read_message_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            db.commit()
            CHAT_READ_STATE_SCHEMA_READY = True
            return True
        except Exception:
            current_app.logger.warning("chat read_state schema ensure failed", exc_info=True)
            CHAT_READ_STATE_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _build_chat_participants(event_id: int) -> dict[str, dict[str, str]]:
    participants: dict[str, dict[str, str]] = {"admin:admin": {"actor_type": "admin", "actor_id": "admin", "display_name": "admin"}}

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT DISTINCT u.id, u.nickname
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id = m.user_id
             WHERE m.event_id=%s
            """,
            (event_id,),
        )
        for row in cur.fetchall() or []:
            actor_id = str(row["id"])
            key = f"line:{actor_id}"
            participants[key] = {
                "actor_type": "line",
                "actor_id": actor_id,
                "display_name": str(row.get("nickname") or f"LINE-{actor_id}"),
            }

        cur.execute("SELECT DISTINCT username FROM mfu_event_admin_acl WHERE event_id=%s", (event_id,))
        acl_rows = [str(row["username"]) for row in (cur.fetchall() or [])]
        non_admin_acls = [name for name in acl_rows if name != "admin"]
        if non_admin_acls:
            placeholders = ",".join(["%s"] * len(non_admin_acls))
            cur.execute(
                f"SELECT username FROM users WHERE username IN ({placeholders})",
                tuple(non_admin_acls),
            )
            for row in cur.fetchall() or []:
                username = str(row["username"])
                participants[f"acl:{username}"] = {
                    "actor_type": "acl",
                    "actor_id": username,
                    "display_name": username,
                }

        if "admin" in acl_rows:
            participants["admin:admin"] = {"actor_type": "admin", "actor_id": "admin", "display_name": "admin"}
    finally:
        cur.close()
        db.close()

    return participants


def _load_event_read_state_snapshot(event_id: int) -> list[dict[str, Any]]:
    if not _ensure_chat_read_state_schema():
        return []

    participants = _build_chat_participants(event_id)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT actor_type, actor_id, last_read_message_id, updated_at
              FROM chat_read_state
             WHERE event_id=%s
            """,
            (event_id,),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    snapshot: list[dict[str, Any]] = []
    for row in rows:
        actor_type = str(row.get("actor_type") or "")
        actor_id = str(row.get("actor_id") or "")
        key = f"{actor_type}:{actor_id}"
        participant = participants.get(key)
        if not participant:
            continue
        snapshot.append(
            {
                "actor_type": actor_type,
                "actor_id": actor_id,
                "display_name": participant["display_name"],
                "last_read_message_id": int(row.get("last_read_message_id") or 0),
                "updated_at_iso": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            }
        )
    return snapshot


def _upsert_chat_read_state(event_id: int, actor: dict[str, Any], last_seen_message_id: int) -> int:
    if not _ensure_chat_read_state_schema():
        return 0

    now = datetime.utcnow()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            INSERT INTO chat_read_state (event_id, actor_type, actor_id, last_read_message_id, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              last_read_message_id = GREATEST(IFNULL(last_read_message_id, 0), VALUES(last_read_message_id)),
              updated_at = VALUES(updated_at)
            """,
            (event_id, actor["actor_type"], actor["actor_id"], last_seen_message_id, now),
        )
        cur.execute(
            """
            SELECT last_read_message_id
              FROM chat_read_state
             WHERE event_id=%s AND actor_type=%s AND actor_id=%s
             LIMIT 1
            """,
            (event_id, actor["actor_type"], actor["actor_id"]),
        )
        row = cur.fetchone() or {}
        db.commit()
        return int(row.get("last_read_message_id") or 0)
    finally:
        cur.close()
        db.close()


def _actor_sender_id(actor_type: str, actor_id: str) -> str:
    return f"{actor_type}:{actor_id}"


def _to_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))


def _format_jst_labels(created_at: Any) -> tuple[str, str, str]:
    dt_utc = _to_utc_datetime(created_at)
    dt_jst = dt_utc.astimezone(JST)
    date_label = f"{dt_jst.year}/{dt_jst.month}/{dt_jst.day}({['月', '火', '水', '木', '金', '土', '日'][dt_jst.weekday()]})"
    time_label = f"{dt_jst.hour}:{dt_jst.minute:02d}"
    return dt_utc.isoformat(), date_label, time_label


def _present_message(
    msg: dict[str, Any],
    current_actor: dict[str, Any],
    avatar_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    sender_actor_type = str(msg["sender_actor_type"])
    sender_actor_id = str(msg["sender_actor_id"])
    sender_id = _actor_sender_id(sender_actor_type, sender_actor_id)
    created_at_iso, date_label, time_label = _format_jst_labels(msg["created_at"])
    deleted_flag = int(msg.get("deleted_flag") or 0) == 1
    deleted_by_actor_type = str(msg.get("deleted_by_actor_type") or "")
    deleted_text = "管理者により、削除されました" if deleted_by_actor_type == "admin" else "このメッセージは削除されました"

    reply_to_sender_actor_type = msg.get("reply_to_sender_actor_type")
    reply_to_sender_actor_id = msg.get("reply_to_sender_actor_id")
    reply_to_sender_avatar_url = None
    if reply_to_sender_actor_type and reply_to_sender_actor_id:
        reply_to_sender_avatar_url = _resolve_sender_avatar_url(
            str(reply_to_sender_actor_type),
            str(reply_to_sender_actor_id),
            avatar_cache=avatar_cache,
        )

    raw_images = [] if deleted_flag else (msg.get("images") or _fallback_images_from_message(msg))
    images: list[dict[str, Any]] = []
    for image in raw_images:
        image_file = str(image.get("image_file") or "").strip()
        image_thumb_file = str(image.get("image_thumb_file") or "").strip()
        if not image_file or not image_thumb_file:
            continue
        images.append(
            {
                "seq": int(image.get("seq") or (len(images) + 1)),
                "url": url_for("chat.chat_image", event_id=msg["event_id"], name=image_file),
                "thumb_url": url_for("chat.chat_image", event_id=msg["event_id"], name=image_thumb_file),
                "mime": image.get("image_mime"),
                "size": image.get("image_size"),
                "width": image.get("image_width"),
                "height": image.get("image_height"),
            }
        )

    has_image = bool(images)
    first_image = images[0] if images else None
    body_value = "" if deleted_flag else (msg.get("body") or "")
    body_html = deleted_text if deleted_flag else _linkify_escaped_text(body_value).replace("\n", "<br>")
    reply_excerpt = msg.get("reply_to_body_plain_excerpt")
    if not reply_excerpt and msg.get("reply_to_message_id"):
        reply_excerpt = "元メッセージが見つかりません"

    actor_is_admin = _is_admin_actor(current_actor)
    is_me = str(sender_id) == str(_actor_sender_id(current_actor["actor_type"], str(current_actor["actor_id"])))
    can_delete = False
    if not deleted_flag:
        if actor_is_admin:
            can_delete = True
        elif is_me and isinstance(msg.get("created_at"), datetime):
            can_delete = msg["created_at"] + timedelta(hours=12) >= datetime.utcnow()

    return {
        "id": msg["id"],
        "event_id": msg["event_id"],
        "sender_id": sender_id,
        "sender_display_name": msg["sender_display_name"],
        "sender_avatar_url": _resolve_sender_avatar_url(sender_actor_type, sender_actor_id, avatar_cache=avatar_cache),
        "body": body_value,
        "body_html": body_html,
        "deleted_flag": 1 if deleted_flag else 0,
        "deleted_at": msg.get("deleted_at").isoformat() if msg.get("deleted_at") else None,
        "deleted_by_actor_type": deleted_by_actor_type,
        "deleted_by_actor_id": msg.get("deleted_by_actor_id"),
        "deleted_text": deleted_text,
        "created_at_iso": created_at_iso,
        "created_at_jst_date_label": date_label,
        "created_at_jst_time_hm": time_label,
        "body_plain_excerpt": deleted_text if deleted_flag else _build_plain_excerpt(body_value),
        "reply_to_message_id": msg.get("reply_to_message_id"),
        "reply_to_sender_display_name": msg.get("reply_to_sender_display_name"),
        "reply_to_body_plain_excerpt": reply_excerpt,
        "reply_to_sender_avatar_url": reply_to_sender_avatar_url or _default_avatar_url(),
        "reactions_summary": [] if deleted_flag else (msg.get("reactions_summary") or []),
        "my_reaction": None if deleted_flag else msg.get("my_reaction"),
        "has_image": has_image,
        "images": images,
        "image_url": first_image.get("url") if first_image else None,
        "image_thumb_url": first_image.get("thumb_url") if first_image else None,
        "image_mime": first_image.get("mime") if first_image else None,
        "image_size": first_image.get("size") if first_image else None,
        "image_width": first_image.get("width") if first_image else None,
        "image_height": first_image.get("height") if first_image else None,
        "is_me": is_me,
        "can_delete": can_delete,
    }


def _linkify_escaped_text(text: str) -> str:
    value = str(text or "")

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        trimmed = token
        while trimmed and trimmed[-1] in _LINKIFY_TRAILING_CHARS:
            trimmed = trimmed[:-1]
        if not trimmed:
            return token

        suffix = token[len(trimmed):]
        if match.group("email"):
            href = f"mailto:{trimmed}"
        elif trimmed.startswith("http://") or trimmed.startswith("https://"):
            href = trimmed
        elif trimmed.startswith("www."):
            href = f"https://{trimmed}"
        else:
            return token

        return (
            f'<a class="chat-link" href="{href}" target="_blank" rel="noopener noreferrer">{trimmed}</a>{suffix}'
        )

    return _LINKIFY_RE.sub(_replace, value)


def _build_plain_excerpt(value: str, max_len: int = 80) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return f"{text[:max_len - 1]}…"


def _chat_csrf() -> str:
    token = session.get("chat_csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["chat_csrf"] = token
    return token


def _is_admin() -> bool:
    return session.get("user") == "admin"


def _is_admin_actor(actor: dict[str, Any] | None) -> bool:
    if not actor:
        return False
    return str(actor.get("actor_type") or "") == "admin"


def get_chat_actor() -> dict[str, Any] | None:
    """admin / acl / line を統一形式へ正規化。"""
    if session.get("user"):
        username = str(session.get("user"))
        actor_type = "admin" if username == "admin" else "acl"
        email = None
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT username, email FROM users WHERE username=%s LIMIT 1", (username,))
            row = cur.fetchone()
            if row:
                email = row.get("email")
        except Exception:
            current_app.logger.warning("chat actor load failed for mfu user=%s", username, exc_info=True)
        finally:
            cur.close()
            db.close()
        return {
            "actor_type": actor_type,
            "actor_id": username,
            "display_name": username,
            "email": email,
        }

    ext_user_id = session.get("ext_user_id")
    if ext_user_id:
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT id, nickname, email FROM external_login_user WHERE id=%s LIMIT 1",
                (ext_user_id,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
            db.close()
        if not row:
            current_app.logger.warning("chat actor load failed for ext_user_id=%s", ext_user_id)
            return None
        return {
            "actor_type": "line",
            "actor_id": str(row["id"]),
            "display_name": row.get("nickname") or f"LINE-{row['id']}",
            "email": row.get("email"),
        }

    return None


def _can_access_event(event_id: int, actor: dict[str, Any]) -> bool:
    if actor["actor_type"] == "admin":
        return True

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if actor["actor_type"] == "line":
            cur.execute(
                """
                SELECT 1
                  FROM mfu_event_member
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
                """,
                (event_id, actor["actor_id"]),
            )
            return bool(cur.fetchone())

        # acl: mfu_event_admin_acl + users(admin系)
        cur.execute(
            """
            SELECT 1
              FROM mfu_event_admin_acl
             WHERE event_id=%s AND username=%s
             LIMIT 1
            """,
            (event_id, actor["actor_id"]),
        )
        return bool(cur.fetchone())
    except Exception:
        current_app.logger.warning("chat access check failed event=%s actor=%s", event_id, actor, exc_info=True)
        return False
    finally:
        cur.close()
        db.close()


def _accessible_events(actor: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if actor["actor_type"] == "admin":
            cur.execute("SELECT id, title, starts_at AS start_at FROM mfu_event ORDER BY starts_at DESC LIMIT 100")
            return cur.fetchall() or []
        if actor["actor_type"] == "line":
            cur.execute(
                """
                SELECT e.id, e.title, e.starts_at AS start_at
                  FROM mfu_event e
                  JOIN mfu_event_member m ON m.event_id = e.id
                 WHERE m.user_id = %s
                 ORDER BY e.starts_at DESC
                 LIMIT 100
                """,
                (actor["actor_id"],),
            )
            return cur.fetchall() or []

        cur.execute(
            """
            SELECT e.id, e.title, e.starts_at AS start_at
              FROM mfu_event e
              JOIN mfu_event_admin_acl a ON a.event_id = e.id
             WHERE a.username = %s
             ORDER BY e.starts_at DESC
             LIMIT 100
            """,
            (actor["actor_id"],),
        )
        return cur.fetchall() or []
    finally:
        cur.close()
        db.close()


def _get_event(event_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, title, starts_at AS start_at FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def _load_messages(event_id: int, limit: int = 100) -> list[dict[str, Any]]:
    has_reply_schema = _ensure_chat_reply_schema()
    has_image_schema = _ensure_chat_image_schema()
    has_delete_schema = _ensure_chat_delete_schema()
    image_columns = (
        "m.image_file, m.image_thumb_file, m.image_mime, m.image_size, m.image_width, m.image_height"
        if has_image_schema
        else "NULL AS image_file, NULL AS image_thumb_file, NULL AS image_mime, NULL AS image_size, NULL AS image_width, NULL AS image_height"
    )
    delete_columns = (
        "m.deleted_flag, m.deleted_at, m.deleted_by_actor_type, m.deleted_by_actor_id"
        if has_delete_schema
        else "0 AS deleted_flag, NULL AS deleted_at, NULL AS deleted_by_actor_type, NULL AS deleted_by_actor_id"
    )
    reply_delete_columns = (
        "p.deleted_flag AS reply_to_deleted_flag, p.deleted_by_actor_type AS reply_to_deleted_by_actor_type"
        if has_delete_schema
        else "0 AS reply_to_deleted_flag, NULL AS reply_to_deleted_by_actor_type"
    )
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if has_reply_schema:
            cur.execute(
                f"""
                SELECT m.id,
                       m.event_id,
                       m.sender_actor_type,
                       m.sender_actor_id,
                       m.sender_display_name,
                       m.body,
                       m.created_at,
                       {image_columns},
                       {delete_columns},
                       m.reply_to_message_id,
                       p.sender_actor_type AS reply_to_sender_actor_type,
                       p.sender_actor_id AS reply_to_sender_actor_id,
                       p.sender_display_name AS reply_to_sender_display_name,
                       p.body AS reply_to_body,
                       {reply_delete_columns}
                  FROM chat_messages m
                  LEFT JOIN chat_messages p
                         ON p.id = m.reply_to_message_id
                        AND p.event_id = m.event_id
                 WHERE m.event_id=%s
                 ORDER BY m.created_at DESC
                 LIMIT %s
                """,
                (event_id, limit),
            )
            rows = cur.fetchall() or []
            for row in rows:
                if row.get("reply_to_message_id") and row.get("reply_to_sender_display_name"):
                    if int(row.get("reply_to_deleted_flag") or 0) == 1:
                        row["reply_to_body_plain_excerpt"] = (
                            "管理者により削除"
                            if str(row.get("reply_to_deleted_by_actor_type") or "") == "admin"
                            else "削除されました"
                        )
                    else:
                        row["reply_to_body_plain_excerpt"] = _build_plain_excerpt(row.get("reply_to_body") or "")
                elif row.get("reply_to_message_id"):
                    row["reply_to_sender_display_name"] = "元メッセージ"
                    row["reply_to_body_plain_excerpt"] = "元メッセージが見つかりません"
                else:
                    row["reply_to_sender_display_name"] = None
                    row["reply_to_body_plain_excerpt"] = None
            return list(reversed(rows))

        cur.execute(
            f"""
            SELECT id, event_id, sender_actor_type, sender_actor_id, sender_display_name, body, created_at,
                   {'image_file, image_thumb_file, image_mime, image_size, image_width, image_height' if has_image_schema else 'NULL AS image_file, NULL AS image_thumb_file, NULL AS image_mime, NULL AS image_size, NULL AS image_width, NULL AS image_height'},
                   {'deleted_flag, deleted_at, deleted_by_actor_type, deleted_by_actor_id' if has_delete_schema else '0 AS deleted_flag, NULL AS deleted_at, NULL AS deleted_by_actor_type, NULL AS deleted_by_actor_id'}
              FROM chat_messages
             WHERE event_id=%s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (event_id, limit),
        )
        rows = cur.fetchall() or []
        for row in rows:
            row["reply_to_message_id"] = None
            row["reply_to_sender_actor_type"] = None
            row["reply_to_sender_actor_id"] = None
            row["reply_to_sender_display_name"] = None
            row["reply_to_body_plain_excerpt"] = None
            row["reply_to_deleted_flag"] = 0
            row["reply_to_deleted_by_actor_type"] = None
        return list(reversed(rows))
    finally:
        cur.close()
        db.close()


def _save_message(
    event_id: int,
    actor: dict[str, Any],
    body: str,
    reply_to_message_id: int | None = None,
    image_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    has_reply_schema = _ensure_chat_reply_schema()
    has_image_schema = _ensure_chat_image_schema()
    _ensure_chat_delete_schema()
    image_meta = image_meta or {}
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        insert_columns = ["event_id", "sender_actor_type", "sender_actor_id", "sender_display_name", "body", "created_at"]
        insert_values: list[Any] = [event_id, actor["actor_type"], actor["actor_id"], actor["display_name"], body, now]

        if has_reply_schema:
            insert_columns.append("reply_to_message_id")
            insert_values.append(reply_to_message_id)
        else:
            reply_to_message_id = None

        if has_image_schema:
            insert_columns.extend(["image_file", "image_thumb_file", "image_mime", "image_size", "image_width", "image_height"])
            insert_values.extend(
                [
                    image_meta.get("image_file"),
                    image_meta.get("image_thumb_file"),
                    image_meta.get("image_mime"),
                    image_meta.get("image_size"),
                    image_meta.get("image_width"),
                    image_meta.get("image_height"),
                ]
            )

        placeholders = ", ".join(["%s"] * len(insert_columns))
        cur.execute(
            f"INSERT INTO chat_messages ({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(insert_values),
        )
        msg_id = cur.lastrowid
        db.commit()
        return {
            "id": msg_id,
            "event_id": event_id,
            "sender_actor_type": actor["actor_type"],
            "sender_actor_id": actor["actor_id"],
            "sender_display_name": actor["display_name"],
            "body": body,
            "created_at": now,
            "reply_to_message_id": reply_to_message_id,
            "image_file": image_meta.get("image_file"),
            "image_thumb_file": image_meta.get("image_thumb_file"),
            "image_mime": image_meta.get("image_mime"),
            "image_size": image_meta.get("image_size"),
            "image_width": image_meta.get("image_width"),
            "image_height": image_meta.get("image_height"),
            "deleted_flag": 0,
            "deleted_at": None,
            "deleted_by_actor_type": None,
            "deleted_by_actor_id": None,
        }
    finally:
        cur.close()
        db.close()


def _load_reactions_by_message_ids(message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not message_ids:
        return {}
    if not _ensure_chat_reaction_schema():
        return {}

    placeholders = ", ".join(["%s"] * len(message_ids))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT message_id, emoji, COUNT(*) AS cnt
              FROM chat_message_reactions
             WHERE message_id IN ({placeholders})
             GROUP BY message_id, emoji
            """,
            tuple(message_ids),
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in cur.fetchall() or []:
            message_id = int(row.get("message_id") or 0)
            if message_id <= 0:
                continue
            grouped.setdefault(message_id, []).append({"emoji": row.get("emoji") or "", "count": int(row.get("cnt") or 0)})
        for values in grouped.values():
            values.sort(key=lambda item: CHAT_ALLOWED_REACTION_EMOJIS.index(item["emoji"]) if item["emoji"] in CHAT_ALLOWED_REACTION_EMOJIS else 999)
        return grouped
    finally:
        cur.close()
        db.close()


def _load_message_images_by_message_ids(message_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not message_ids:
        return {}
    if not _ensure_chat_message_images_schema():
        return {}

    placeholders = ", ".join(["%s"] * len(message_ids))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT message_id, seq, image_file, image_thumb_file, image_mime, image_size, image_width, image_height
              FROM chat_message_images
             WHERE message_id IN ({placeholders})
             ORDER BY message_id ASC, seq ASC
            """,
            tuple(message_ids),
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in cur.fetchall() or []:
            message_id = int(row.get("message_id") or 0)
            if message_id <= 0:
                continue
            grouped.setdefault(message_id, []).append(
                {
                    "seq": int(row.get("seq") or 0),
                    "image_file": row.get("image_file"),
                    "image_thumb_file": row.get("image_thumb_file"),
                    "image_mime": row.get("image_mime"),
                    "image_size": row.get("image_size"),
                    "image_width": row.get("image_width"),
                    "image_height": row.get("image_height"),
                }
            )
        return grouped
    finally:
        cur.close()
        db.close()


def _fallback_images_from_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    image_file = (msg.get("image_file") or "").strip()
    image_thumb_file = (msg.get("image_thumb_file") or "").strip()
    if not image_file or not image_thumb_file:
        return []
    return [
        {
            "seq": 1,
            "image_file": image_file,
            "image_thumb_file": image_thumb_file,
            "image_mime": msg.get("image_mime"),
            "image_size": msg.get("image_size"),
            "image_width": msg.get("image_width"),
            "image_height": msg.get("image_height"),
        }
    ]


def _load_my_reactions_by_message_ids(message_ids: list[int], actor: dict[str, Any]) -> dict[int, str]:
    if not message_ids:
        return {}
    if not _ensure_chat_reaction_schema():
        return {}

    placeholders = ", ".join(["%s"] * len(message_ids))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        params: tuple[Any, ...] = tuple(message_ids) + (actor["actor_type"], str(actor["actor_id"]))
        cur.execute(
            f"""
            SELECT message_id, emoji
              FROM chat_message_reactions
             WHERE message_id IN ({placeholders})
               AND actor_type=%s
               AND actor_id=%s
            """,
            params,
        )
        return {int(row["message_id"]): str(row.get("emoji") or "") for row in (cur.fetchall() or [])}
    finally:
        cur.close()
        db.close()


def _validate_caption_optional(raw: str) -> str:
    caption = (raw or "").strip()
    if len(caption) > MESSAGE_MAX_LEN:
        raise ValueError(f"メッセージは{MESSAGE_MAX_LEN}文字以内です")
    return html.escape(caption)


def _image_extension_and_mime(image_format: str) -> tuple[str, str, bool]:
    fmt = (image_format or "").upper()
    mapping: dict[str, tuple[str, str, bool]] = {
        "JPEG": ("jpg", "image/jpeg", False),
        "PNG": ("png", "image/png", False),
        "WEBP": ("webp", "image/webp", False),
        "MPO": ("jpg", "image/jpeg", True),
        "HEIF": ("jpg", "image/jpeg", True),
        "HEIC": ("jpg", "image/jpeg", True),
    }
    if fmt not in mapping:
        show_fmt = fmt or "unknown"
        raise ValueError(f"未対応の画像形式({show_fmt})です。JPEG/PNG/WEBP/MPO/HEICのみ対応")
    return mapping[fmt]


def _looks_like_heif_bytes(raw_bytes: bytes) -> bool:
    if len(raw_bytes) < 12:
        return False
    if raw_bytes[4:8] != b"ftyp":
        return False
    heif_brands = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
    major_brand = raw_bytes[8:12]
    if major_brand in heif_brands:
        return True
    compatible = raw_bytes[16:64]
    return any(brand in compatible for brand in heif_brands)


def _save_upload_image_files(event_id: int, storage: Any, filename: str = "") -> dict[str, Any]:
    os.makedirs(CHAT_UPLOAD_DIR, exist_ok=True)
    event_dir = os.path.join(CHAT_UPLOAD_DIR, str(event_id))
    os.makedirs(event_dir, exist_ok=True)

    raw_bytes = storage.read()
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raw_bytes = bytes(raw_bytes or b"")
    image_size = len(raw_bytes)
    if image_size <= 0:
        raise ValueError("画像ファイルが空です")
    if image_size > CHAT_UPLOAD_MAX_BYTES:
        raise ValueError("画像サイズが上限を超えています")

    lower_name = (filename or "").lower()
    looks_like_heif = lower_name.endswith(".heic") or lower_name.endswith(".heif") or _looks_like_heif_bytes(raw_bytes)

    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            image_format = (im.format or "").upper()
            if image_format == "MPO":
                try:
                    im.seek(0)
                except Exception:
                    pass
            ext, image_mime, convert_original_to_jpeg = _image_extension_and_mime(image_format)
            normalized = ImageOps.exif_transpose(im)
            width, height = normalized.size

            if convert_original_to_jpeg:
                rgb = normalized.convert("RGB")
                original_buffer = io.BytesIO()
                rgb.save(original_buffer, format="JPEG", quality=92, optimize=True)
                original_bytes = original_buffer.getvalue()
                image_size = len(original_bytes)
            else:
                original_bytes = bytes(raw_bytes)

            thumb_image = normalized.convert("RGBA")
    except UnidentifiedImageError as exc:
        if looks_like_heif:
            raise ValueError(HEIC_UNSUPPORTED_MESSAGE) from exc
        raise ValueError("画像ファイルとして読み取れません") from exc
    except OSError as exc:
        if looks_like_heif and not HEIF_OPENER_AVAILABLE:
            raise ValueError(HEIC_UNSUPPORTED_MESSAGE) from exc
        if "MPO" in str(exc).upper():
            raise ValueError("MPO画像の読み込みに失敗しました。iPhoneのLive/連写画像が原因の可能性があります") from exc
        raise ValueError("画像ファイルとして読み取れません") from exc

    if image_size > CHAT_UPLOAD_MAX_BYTES:
        raise ValueError("画像サイズが上限を超えています")

    base_name = secrets.token_urlsafe(18)
    image_file = f"{base_name}.{ext}"
    image_thumb_file = f"{base_name}_thumb.jpg"
    image_path = os.path.join(event_dir, image_file)
    thumb_path = os.path.join(event_dir, image_thumb_file)

    with open(image_path, "wb") as fp:
        fp.write(original_bytes)

    thumb_image.thumbnail((480, 480))
    bg = Image.new("RGB", thumb_image.size, (255, 255, 255))
    bg.paste(thumb_image, mask=thumb_image.split()[3] if thumb_image.mode == "RGBA" else None)
    bg.save(thumb_path, format="JPEG", quality=85, optimize=True)

    return {
        "image_file": image_file,
        "image_thumb_file": image_thumb_file,
        "image_mime": image_mime,
        "image_size": image_size,
        "image_width": width,
        "image_height": height,
    }


def _validate_body(raw: str) -> str:
    body = (raw or "").strip()
    if not body:
        raise ValueError("メッセージが空です")
    if len(body) > MESSAGE_MAX_LEN:
        raise ValueError(f"メッセージは{MESSAGE_MAX_LEN}文字以内です")
    return html.escape(body)


def _validate_reply_to_message_id(event_id: int, raw_value: Any) -> int | None:
    if raw_value in (None, "", 0, "0"):
        return None
    if not _ensure_chat_reply_schema():
        return None
    try:
        reply_to_message_id = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("返信先メッセージの形式が不正です") from exc
    if reply_to_message_id <= 0:
        raise ValueError("返信先メッセージの形式が不正です")

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT id FROM chat_messages WHERE id=%s AND event_id=%s LIMIT 1", (reply_to_message_id, event_id))
        if not cur.fetchone():
            raise ValueError("返信先メッセージが見つかりません")
        return reply_to_message_id
    finally:
        cur.close()
        db.close()


def _enrich_reply_fields(message: dict[str, Any]) -> dict[str, Any]:
    if not _ensure_chat_reply_schema():
        message["reply_to_message_id"] = None
        message["reply_to_sender_actor_type"] = None
        message["reply_to_sender_actor_id"] = None
        message["reply_to_sender_display_name"] = None
        message["reply_to_body_plain_excerpt"] = None
        return message

    reply_to_message_id = message.get("reply_to_message_id")
    if not reply_to_message_id:
        message["reply_to_sender_actor_type"] = None
        message["reply_to_sender_actor_id"] = None
        message["reply_to_sender_display_name"] = None
        message["reply_to_body_plain_excerpt"] = None
        return message

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT sender_actor_type, sender_actor_id, sender_display_name, body,
                   COALESCE(deleted_flag, 0) AS deleted_flag,
                   deleted_by_actor_type
              FROM chat_messages
             WHERE id=%s AND event_id=%s
             LIMIT 1
            """,
            (reply_to_message_id, message["event_id"]),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if row:
        message["reply_to_sender_actor_type"] = row["sender_actor_type"]
        message["reply_to_sender_actor_id"] = row["sender_actor_id"]
        message["reply_to_sender_display_name"] = row["sender_display_name"]
        if int(row.get("deleted_flag") or 0) == 1:
            message["reply_to_body_plain_excerpt"] = (
                "管理者により削除" if str(row.get("deleted_by_actor_type") or "") == "admin" else "削除されました"
            )
        else:
            message["reply_to_body_plain_excerpt"] = _build_plain_excerpt(row.get("body") or "")
    else:
        message["reply_to_sender_actor_type"] = None
        message["reply_to_sender_actor_id"] = None
        message["reply_to_sender_display_name"] = "元メッセージ"
        message["reply_to_body_plain_excerpt"] = "元メッセージが見つかりません"
    return message


def _check_rate_limit(actor: dict[str, Any]) -> bool:
    key = f"chat_last_post:{actor['actor_type']}:{actor['actor_id']}"
    now = datetime.utcnow()
    prev_iso = session.get(key)
    if prev_iso:
        try:
            prev = datetime.fromisoformat(prev_iso)
            if now - prev < timedelta(seconds=RATE_LIMIT_SECONDS):
                return False
        except Exception:
            pass
    session[key] = now.isoformat()
    return True


def _extract_mentions(body: str) -> list[str]:
    return re.findall(r"@([\w\-ぁ-んァ-ン一-龥ー]+)", body)


def _lookup_mention_targets(event_id: int, names: list[str]) -> list[dict[str, Any]]:
    if not names:
        return []
    uniq_names = sorted(set(names))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        placeholders = ",".join(["%s"] * len(uniq_names))
        cur.execute(
            f"""
            SELECT u.id, u.nickname
              FROM external_login_user u
              JOIN mfu_event_member m ON m.user_id = u.id
             WHERE m.event_id = %s
               AND u.nickname IN ({placeholders})
            """,
            tuple([event_id] + uniq_names),
        )
        rows = cur.fetchall() or []
        return [{"actor_type": "line", "actor_id": str(r["id"]), "display_name": r["nickname"]} for r in rows]
    finally:
        cur.close()
        db.close()


def _build_chat_message_push_targets(event_id: int, sender_actor: dict[str, Any]) -> list[tuple[str, str]]:
    sender_key = _actor_sender_id(sender_actor["actor_type"], str(sender_actor["actor_id"]))
    targets: set[tuple[str, str]] = set()

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT DISTINCT user_id FROM mfu_event_member WHERE event_id=%s", (event_id,))
        for row in cur.fetchall() or []:
            targets.add(("line", str(row["user_id"])))

        cur.execute("SELECT DISTINCT username FROM mfu_event_admin_acl WHERE event_id=%s", (event_id,))
        for row in cur.fetchall() or []:
            username = str(row["username"])
            if username == "admin":
                targets.add(("admin", "admin"))
            else:
                targets.add(("acl", username))
    finally:
        cur.close()
        db.close()

    targets.add(("admin", "admin"))
    return [t for t in targets if _actor_sender_id(t[0], t[1]) != sender_key]


def _create_external_chat_notification(
    *,
    recipient_user_id: int,
    kind: str,
    title: str,
    body: str,
    event_id: int,
    dedup_key: str,
) -> None:
    if not dedup_key:
        return
    from app.external_login_user.notifications import create_notification_external

    create_notification_external(
        user_id=recipient_user_id,
        kind=kind,
        title=title,
        body=_build_plain_excerpt(body, max_len=300),
        target_url=f"/chat/events/{event_id}",
        dedup_key=dedup_key,
        event_id=event_id,
    )


def _send_chat_message_push_async(
    app: Any,
    event_id: int,
    sender_actor: dict[str, Any],
    sender_display_name: str,
    message_body: str,
    message_id: int,
    has_image: bool = False,
    timing: dict[str, float] | None = None,
) -> None:
    with app.app_context():
        from app.external_login_user.notifications import create_notification_external

        t3 = time.monotonic()
        trace = dict(timing or {})
        trace["t3"] = t3
        actor_push_metrics = {
            "target_actors": 0,
            "subscription_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "http_start": None,
            "http_end": None,
            "errors": {},
        }
        notification_inserted = 0
        notification_skipped = 0
        try:
            excerpt = _build_plain_excerpt(message_body, max_len=80)
            if has_image:
                payload_body = f"{sender_display_name}: 📷 画像"
                if excerpt:
                    payload_body = f"{payload_body} {excerpt}"
            else:
                payload_body = f"{sender_display_name}: {excerpt}"

            payload = {
                "type": "chat_message",
                "event_id": event_id,
                "url": f"/chat/events/{event_id}",
                "title": "イベントチャット",
                "body": payload_body,
            }

            event = _get_event(event_id)
            if event and event.get("title"):
                payload["title"] = str(event.get("title"))

            sent_count = 0
            for actor_type, actor_id in _build_chat_message_push_targets(event_id, sender_actor):
                actor_push_metrics["target_actors"] += 1
                sent_count += _send_push_to_actor(actor_type, actor_id, payload, metrics=actor_push_metrics)
                if actor_type == "line":
                    inserted = create_notification_external(
                        user_id=int(actor_id),
                        kind="chat_message",
                        title=str(payload.get("title") or "イベントチャット"),
                        body=str(payload.get("body") or message_body),
                        target_url=f"/chat/events/{event_id}",
                        dedup_key=f"chat:{event_id}:{message_id}:{actor_id}",
                        event_id=event_id,
                    )
                    if inserted:
                        notification_inserted += 1
                    else:
                        notification_skipped += 1

            _log_notification(event_id, "chat_message", payload, sent_count)
        except Exception as exc:
            sender = f"{sender_actor.get('actor_type', '?')}:{sender_actor.get('actor_id', '?')}"
            current_app.logger.warning(
                "chat push async worker recovered event_id=%s sender=%s error=%s",
                event_id,
                sender,
                type(exc).__name__,
            )
            if app.debug:
                current_app.logger.exception("chat push async worker debug traceback")
        finally:
            t_end = time.monotonic()
            t4 = actor_push_metrics.get("http_start")
            t5 = actor_push_metrics.get("http_end")
            current_app.logger.info(
                "chat_push_timeline event_id=%s actor_id=%s targets=%s subs=%s success=%s failure=%s notif_inserted=%s notif_skipped=%s t1_t0=%.3fs t2_t1=%.3fs t3_t2=%.3fs t4_t3=%.3fs t5_t4=%.3fs total=%.3fs errors=%s",
                event_id,
                sender_actor.get("actor_id"),
                actor_push_metrics.get("target_actors", 0),
                actor_push_metrics.get("subscription_count", 0),
                actor_push_metrics.get("success_count", 0),
                actor_push_metrics.get("failure_count", 0),
                notification_inserted,
                notification_skipped,
                (trace.get("t1", t3) - trace.get("t0", t3)),
                (trace.get("t2", t3) - trace.get("t1", t3)),
                (trace.get("t3", t3) - trace.get("t2", t3)),
                ((t4 or t_end) - trace.get("t3", t3)),
                ((t5 or t_end) - (t4 or t_end)),
                (t_end - trace.get("t0", t_end)),
                actor_push_metrics.get("errors", {}),
            )


def _submit_chat_message_push_async(
    app: Any,
    event_id: int,
    sender_actor: dict[str, Any],
    sender_display_name: str,
    message_body: str,
    message_id: int,
    timing: dict[str, float],
    has_image: bool = False,
) -> None:
    global PUSH_ASYNC_INFLIGHT
    with PUSH_ASYNC_INFLIGHT_LOCK:
        PUSH_ASYNC_INFLIGHT += 1
        inflight = PUSH_ASYNC_INFLIGHT

    if inflight > PUSH_ASYNC_MAX_WORKERS * 2:
        current_app.logger.warning(
            "chat push async queue backlog inflight=%s max_workers=%s event_id=%s",
            inflight,
            PUSH_ASYNC_MAX_WORKERS,
            event_id,
        )

    future = PUSH_ASYNC_EXECUTOR.submit(
        _send_chat_message_push_async,
        app,
        event_id,
        sender_actor,
        sender_display_name,
        message_body,
        message_id,
        has_image,
        timing,
    )

    def _done(_fut: Any) -> None:
        global PUSH_ASYNC_INFLIGHT
        with PUSH_ASYNC_INFLIGHT_LOCK:
            PUSH_ASYNC_INFLIGHT = max(PUSH_ASYNC_INFLIGHT - 1, 0)

    future.add_done_callback(_done)


def _log_push_endpoint_warning(endpoint: str, message: str, *args: Any) -> None:
    endpoint_key = endpoint or "(empty)"
    endpoint_hash = hashlib.sha256(endpoint_key.encode("utf-8")).hexdigest()[:12]
    now = time.monotonic()
    suppressed_count = 0
    should_log = True

    with PUSH_ENDPOINT_ERROR_LOCK:
        stat = PUSH_ENDPOINT_ERROR_STATS.get(endpoint_key)
        if not stat:
            stat = {"last_logged": 0.0, "suppressed": 0}
            PUSH_ENDPOINT_ERROR_STATS[endpoint_key] = stat
        last_logged = float(stat.get("last_logged", 0.0) or 0.0)
        if now - last_logged < PUSH_LOG_SUPPRESSION_SECONDS:
            stat["suppressed"] = int(stat.get("suppressed", 0) or 0) + 1
            should_log = False
        else:
            suppressed_count = int(stat.get("suppressed", 0) or 0)
            stat["suppressed"] = 0
            stat["last_logged"] = now

    if not should_log:
        return

    suffix = f" endpoint_hash={endpoint_hash}"
    if suppressed_count > 0:
        suffix += f" suppressed={suppressed_count}"
    current_app.logger.warning(message + suffix, *args)


def _log_notification(event_id: int, kind: str, payload: dict[str, Any], sent_count: int = 0) -> None:
    if not _ensure_chat_notification_log_schema():
        return

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO chat_notification_log (event_id, kind, payload_json, sent_count, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (event_id, kind, json.dumps(payload, ensure_ascii=False), sent_count, datetime.utcnow()),
        )
        db.commit()
    except Exception:
        current_app.logger.warning("chat notification log insert failed", exc_info=True)
    finally:
        cur.close()
        db.close()


def _send_push_to_actor(actor_type: str, actor_id: str, payload: dict[str, Any], metrics: dict[str, Any] | None = None) -> int:
    if not _ensure_chat_push_schema():
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 0

    public = os.getenv("CHAT_VAPID_PUBLIC_KEY")
    private = os.getenv("CHAT_VAPID_PRIVATE_KEY")
    subject = os.getenv("CHAT_VAPID_SUBJECT", "mailto:admin@example.com")
    if not private or not public:
        return 0

    db = get_db()
    cur = db.cursor(dictionary=True)
    sent = 0
    try:
        cur.execute(
            """
            SELECT id, endpoint, p256dh, auth
              FROM chat_push_subscriptions
             WHERE actor_type=%s AND actor_id=%s
            """,
            (actor_type, actor_id),
        )
        subs = cur.fetchall() or []
        if metrics is not None:
            metrics["subscription_count"] = int(metrics.get("subscription_count", 0)) + len(subs)

        for sub in subs:
            endpoint = str(sub.get("endpoint") or "")
            push_success = False
            for attempt in range(len(PUSH_RETRY_BACKOFF_SECONDS) + 1):
                try:
                    req_started = time.monotonic()
                    if metrics is not None and not metrics.get("http_start"):
                        metrics["http_start"] = req_started
                    webpush(
                        subscription_info={
                            "endpoint": endpoint,
                            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                        },
                        data=json.dumps(payload, ensure_ascii=False),
                        vapid_private_key=private,
                        vapid_claims={"sub": subject},
                        timeout=PUSH_REQUEST_TIMEOUT_SECONDS,
                    )
                    sent += 1
                    push_success = True
                    if metrics is not None:
                        metrics["success_count"] = int(metrics.get("success_count", 0)) + 1
                        metrics["http_end"] = time.monotonic()
                    break
                except (RequestsSSLError, RequestsConnectionError, RequestsTimeout) as exc:
                    if metrics is not None:
                        metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
                        errors = metrics.setdefault("errors", {})
                        key = f"network:{type(exc).__name__}"
                        errors[key] = int(errors.get(key, 0)) + 1
                        metrics["http_end"] = time.monotonic()
                    if attempt >= len(PUSH_RETRY_BACKOFF_SECONDS):
                        _log_push_endpoint_warning(
                            endpoint,
                            "chat push temporary network failure actor=%s:%s error=%s",
                            actor_type,
                            actor_id,
                            type(exc).__name__,
                        )
                        break
                    delay = PUSH_RETRY_BACKOFF_SECONDS[attempt] + random.uniform(0.0, 0.2)
                    time.sleep(delay)
                except WebPushException as exc:
                    if metrics is not None:
                        metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
                        errors = metrics.setdefault("errors", {})
                        status_key = getattr(getattr(exc, "response", None), "status_code", None)
                        key = f"webpush:{status_key}"
                        errors[key] = int(errors.get(key, 0)) + 1
                        metrics["http_end"] = time.monotonic()
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in {404, 410}:
                        cur.execute("DELETE FROM chat_push_subscriptions WHERE id=%s", (sub["id"],))
                        _log_push_endpoint_warning(
                            endpoint,
                            "chat push subscription removed actor=%s:%s status=%s",
                            actor_type,
                            actor_id,
                            status,
                        )
                        break
                    if status in {401, 403}:
                        current_app.logger.error(
                            "chat push auth/config failure actor=%s:%s status=%s",
                            actor_type,
                            actor_id,
                            status,
                        )
                        break
                    if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                        if attempt >= len(PUSH_RETRY_BACKOFF_SECONDS):
                            _log_push_endpoint_warning(
                                endpoint,
                                "chat push temporary http failure actor=%s:%s status=%s",
                                actor_type,
                                actor_id,
                                status,
                            )
                            break
                        delay = PUSH_RETRY_BACKOFF_SECONDS[attempt] + random.uniform(0.0, 0.2)
                        time.sleep(delay)
                        continue

                    _log_push_endpoint_warning(
                        endpoint,
                        "chat push failed actor=%s:%s status=%s",
                        actor_type,
                        actor_id,
                        status,
                    )
                    break
                except Exception as exc:
                    if metrics is not None:
                        metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
                        errors = metrics.setdefault("errors", {})
                        key = f"unexpected:{type(exc).__name__}"
                        errors[key] = int(errors.get(key, 0)) + 1
                        metrics["http_end"] = time.monotonic()
                    _log_push_endpoint_warning(
                        endpoint,
                        "chat push unexpected failure actor=%s:%s error=%s",
                        actor_type,
                        actor_id,
                        type(exc).__name__,
                    )
                    if current_app.debug:
                        current_app.logger.exception("chat push unexpected traceback endpoint_hash logging")
                    break

            if not push_success:
                continue
        db.commit()
        return sent
    finally:
        cur.close()
        db.close()


@chat_bp.before_request
def _require_any_login():
    if request.endpoint in {"chat.manifest", "chat.sw", "chat.static"}:
        return None
    actor = get_chat_actor()
    if not actor:
        abort(403)
    return None


@chat_bp.route("/")
def index():
    actor = get_chat_actor()
    if not actor:
        abort(403)
    events = _accessible_events(actor)
    return render_template("chat/index.html", actor=actor, events=events, csrf_token=_chat_csrf())


@chat_bp.route("/events/<int:event_id>")
def room(event_id: int):
    actor = get_chat_actor()
    if not actor:
        abort(403)
    if not _can_access_event(event_id, actor):
        abort(403)

    _ensure_chat_read_state_schema()
    _ensure_chat_delete_schema()

    event = _get_event(event_id)
    if not event:
        abort(404)
    avatar_cache: dict[str, str] = {}
    raw_messages = _load_messages(event_id)
    message_ids = [int(m.get("id") or 0) for m in raw_messages if int(m.get("id") or 0) > 0]
    reaction_summary_by_message = _load_reactions_by_message_ids(message_ids)
    my_reaction_by_message = _load_my_reactions_by_message_ids(message_ids, actor)
    images_by_message = _load_message_images_by_message_ids(message_ids)
    messages = []
    for message in raw_messages:
        message_id = int(message.get("id") or 0)
        message["reactions_summary"] = reaction_summary_by_message.get(message_id, [])
        message["my_reaction"] = my_reaction_by_message.get(message_id)
        message["images"] = images_by_message.get(message_id) or _fallback_images_from_message(message)
        messages.append(_present_message(message, actor, avatar_cache=avatar_cache))
    can_broadcast = actor["actor_type"] in {"admin", "acl"}
    return render_template(
        "chat/room.html",
        actor=actor,
        current_user_id=_actor_sender_id(actor["actor_type"], str(actor["actor_id"])),
        event=event,
        messages=messages,
        vapid_public_key=os.getenv("CHAT_VAPID_PUBLIC_KEY", ""),
        csrf_token=_chat_csrf(),
        can_broadcast=can_broadcast,
        default_avatar_url=_default_avatar_url(),
    )


@chat_bp.get("/events/<int:event_id>/images/<path:name>")
def chat_image(event_id: int, name: str):
    actor = get_chat_actor()
    if not actor:
        abort(403)
    if not _can_access_event(event_id, actor):
        abort(403)
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    directory = os.path.join(CHAT_UPLOAD_DIR, str(event_id))
    return send_from_directory(directory, name)


@chat_bp.post("/api/events/<int:event_id>/upload-image")
def upload_image(event_id: int):
    actor = get_chat_actor()
    if not actor:
        abort(403)
    if not _can_access_event(event_id, actor):
        abort(403)
    if not _ensure_chat_delete_schema():
        return jsonify({"ok": False, "error": "メッセージ削除機能の初期化に失敗しました"}), 500
    if not _ensure_chat_image_schema():
        return jsonify({"ok": False, "error": "画像機能の初期化に失敗しました"}), 500
    if not _ensure_chat_message_images_schema():
        return jsonify({"ok": False, "error": "画像機能の初期化に失敗しました"}), 500

    payload = request.get_json(silent=True) or {}
    token = (request.form.get("csrf_token") or payload.get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        current_app.logger.warning(
            "chat upload-image csrf mismatch event_id=%s actor=%s:%s ua=%s",
            event_id,
            actor.get("actor_type"),
            actor.get("actor_id"),
            request.headers.get("User-Agent", ""),
        )
        return jsonify({"ok": False, "error": "セッションが切れました。ページを更新して再送してください"}), 400

    if request.content_length and request.content_length > CHAT_UPLOAD_MAX_BYTES * 6:
        return jsonify({"ok": False, "error": "画像サイズが上限を超えています"}), 413

    raw_upload_files = [f for f in (request.files.getlist("file") or []) if f]
    if not raw_upload_files:
        return jsonify({"ok": False, "error": "画像ファイルがありません"}), 400
    if len(raw_upload_files) > 6:
        return jsonify({"ok": False, "error": "画像は最大6枚までです"}), 400

    def _fallback_filename(file_idx: int, upload_file: Any) -> str:
        original_name = (getattr(upload_file, "filename", "") or "").strip()
        if original_name:
            return original_name
        mimetype = str(getattr(upload_file, "mimetype", "") or "").lower()
        if mimetype in {"image/jpeg", "image/jpg", "image/heic", "image/heif"}:
            ext = "jpg"
        elif mimetype == "image/png":
            ext = "png"
        elif mimetype == "image/webp":
            ext = "webp"
        else:
            ext = "bin"
        return f"upload_{file_idx}.{ext}"

    upload_files: list[dict[str, Any]] = []
    for idx, upload_file in enumerate(raw_upload_files, start=1):
        normalized_name = _fallback_filename(idx, upload_file)
        mimetype = str(getattr(upload_file, "mimetype", "") or "")
        if mimetype and not mimetype.lower().startswith("image/"):
            return jsonify({"ok": False, "error": f"画像以外が混ざっています: {normalized_name}/{mimetype}"}), 400
        upload_files.append({"idx": idx, "file": upload_file, "name": normalized_name, "mimetype": mimetype})

    try:
        body = _validate_caption_optional(request.form.get("body") or "")
    except ValueError as exc:
        status = 413 if "上限" in str(exc) else 400
        return jsonify({"ok": False, "error": str(exc)}), status

    image_metas: list[dict[str, Any]] = []
    for upload in upload_files:
        idx = int(upload["idx"])
        upload_file = upload["file"]
        filename = str(upload["name"])
        mimetype = str(upload.get("mimetype") or "")
        current_app.logger.info(
            "chat upload-image processing event_id=%s idx=%s filename=%s mimetype=%s size=%s",
            event_id,
            idx,
            filename,
            mimetype,
            getattr(upload_file, "content_length", None),
        )
        try:
            image_metas.append(_save_upload_image_files(event_id, upload_file.stream, filename=filename))
        except ValueError as exc:
            current_app.logger.warning(
                "chat upload-image invalid file event_id=%s idx=%s filename=%s mimetype=%s size=%s error=%s",
                event_id,
                idx,
                filename,
                mimetype,
                getattr(upload_file, "content_length", None),
                str(exc),
            )
            status = 413 if "上限" in str(exc) else 400
            return jsonify({"ok": False, "error": f"{idx}枚目({filename}): {str(exc)}"}), status

    message = _enrich_reply_fields(_save_message(event_id, actor, body, None, image_meta=image_metas[0]))
    message_id = int(message.get("id") or 0)

    db = get_db()
    cur = db.cursor()
    try:
        now = datetime.utcnow()
        for idx, image_meta in enumerate(image_metas, start=1):
            cur.execute(
                """
                INSERT INTO chat_message_images (
                    event_id, message_id, seq,
                    image_file, image_thumb_file, image_mime,
                    image_size, image_width, image_height, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id,
                    message_id,
                    idx,
                    image_meta.get("image_file"),
                    image_meta.get("image_thumb_file"),
                    image_meta.get("image_mime"),
                    image_meta.get("image_size"),
                    image_meta.get("image_width"),
                    image_meta.get("image_height"),
                    now,
                ),
            )
        db.commit()
    finally:
        cur.close()
        db.close()

    message["images"] = [
        {
            "seq": idx,
            "image_file": image_meta.get("image_file"),
            "image_thumb_file": image_meta.get("image_thumb_file"),
            "image_mime": image_meta.get("image_mime"),
            "image_size": image_meta.get("image_size"),
            "image_width": image_meta.get("image_width"),
            "image_height": image_meta.get("image_height"),
        }
        for idx, image_meta in enumerate(image_metas, start=1)
    ]

    message_payload = _present_message(message, actor, avatar_cache={})
    socketio.emit("chat_message", message_payload, to=f"event:{event_id}")

    app = current_app._get_current_object()
    now = time.monotonic()
    _submit_chat_message_push_async(
        app,
        event_id,
        actor,
        actor["display_name"],
        body,
        int(message_payload["id"]),
        {"t0": now, "t1": now, "t2": now},
        has_image=True,
    )

    return jsonify({"ok": True, "message": message_payload})


@chat_bp.post("/api/events/<int:event_id>/messages/<int:message_id>/delete")
def delete_message(event_id: int, message_id: int):
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
    if not _can_access_event(event_id, actor):
        return jsonify({"ok": False, "error": "このイベントにはアクセスできません"}), 403
    if not _ensure_chat_delete_schema():
        return jsonify({"ok": False, "error": "メッセージ削除機能の初期化に失敗しました"}), 500

    payload = request.get_json(silent=True) or {}
    token = (request.form.get("csrf_token") or payload.get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        return jsonify({"ok": False, "error": "セッションが切れました。ページを更新して再試行してください"}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    deleted_at: datetime | None = None
    deleted_by_actor_type = actor.get("actor_type")
    try:
        cur.execute(
            """
            SELECT id, event_id, sender_actor_type, sender_actor_id, created_at,
                   COALESCE(deleted_flag, 0) AS deleted_flag,
                   deleted_at, deleted_by_actor_type
              FROM chat_messages
             WHERE id=%s AND event_id=%s
             LIMIT 1
            """,
            (message_id, event_id),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "対象メッセージが見つかりません"}), 404

        if int(row.get("deleted_flag") or 0) == 1:
            return jsonify({"ok": True})

        is_admin_actor = _is_admin_actor(actor)
        actor_sender_id = _actor_sender_id(str(actor.get("actor_type") or ""), str(actor.get("actor_id") or ""))
        message_sender_id = _actor_sender_id(
            str(row.get("sender_actor_type") or ""),
            str(row.get("sender_actor_id") or ""),
        )

        if not is_admin_actor and actor_sender_id != message_sender_id:
            return jsonify({"ok": False, "error": "このメッセージを削除する権限がありません"}), 403

        if not is_admin_actor:
            created_at = row.get("created_at")
            if not isinstance(created_at, datetime):
                return jsonify({"ok": False, "error": "送信時刻の取得に失敗しました"}), 400
            if created_at + timedelta(hours=12) < datetime.utcnow():
                return jsonify({"ok": False, "error": "送信から12時間を過ぎたため、送信取消できません"}), 403

        cur.execute(
            """
            UPDATE chat_messages
               SET deleted_flag=1,
                   deleted_at=NOW(),
                   deleted_by_actor_type=%s,
                   deleted_by_actor_id=%s
             WHERE id=%s
            """,
            (actor.get("actor_type"), str(actor.get("actor_id") or ""), message_id),
        )
        cur.execute("SELECT deleted_at, deleted_by_actor_type FROM chat_messages WHERE id=%s LIMIT 1", (message_id,))
        latest = cur.fetchone() or {}
        deleted_at = latest.get("deleted_at")
        deleted_by_actor_type = latest.get("deleted_by_actor_type") or deleted_by_actor_type
        db.commit()
    finally:
        cur.close()
        db.close()

    socketio.emit(
        "chat_delete_update",
        {
            "event_id": event_id,
            "message_id": message_id,
            "deleted_flag": 1,
            "deleted_at": deleted_at.isoformat() if deleted_at else None,
            "deleted_by_actor_type": deleted_by_actor_type,
        },
        room=f"event:{event_id}",
    )
    return jsonify({"ok": True})


@chat_bp.get("/api/push/bootstrap")
def push_bootstrap():
    actor = get_chat_actor()
    if not actor:
        abort(403)
    return jsonify(
        {
            "ok": True,
            "csrf_token": _chat_csrf(),
            "vapid_public_key": os.getenv("CHAT_VAPID_PUBLIC_KEY", ""),
            "sw_url": "/sw.js",
        }
    )


@chat_bp.post("/api/push/subscribe")
def push_subscribe():
    actor = get_chat_actor()
    if not actor:
        abort(403)
    token = (request.form.get("csrf_token") or (request.json or {}).get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        abort(400)
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest() if endpoint else ""
    sw_scope = (data.get("sw_scope") or "/").strip() or "/"
    if not sw_scope.startswith("/"):
        sw_scope = "/"
    keys = data.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        abort(400)
    if not _ensure_chat_push_schema():
        abort(500)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO chat_push_subscriptions (
              actor_type, actor_id, endpoint, endpoint_hash, sw_scope, p256dh, auth, user_agent, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE endpoint=VALUES(endpoint), sw_scope=VALUES(sw_scope), p256dh=VALUES(p256dh), auth=VALUES(auth), user_agent=VALUES(user_agent), updated_at=VALUES(updated_at)
            """,
            (
                actor["actor_type"],
                actor["actor_id"],
                endpoint,
                endpoint_hash,
                sw_scope,
                keys["p256dh"],
                keys["auth"],
                request.headers.get("User-Agent"),
                datetime.utcnow(),
                datetime.utcnow(),
            ),
        )
        if sw_scope == "/":
            cur.execute(
                """
                DELETE FROM chat_push_subscriptions
                 WHERE actor_type=%s
                   AND actor_id=%s
                   AND sw_scope <> '/'
                   AND (endpoint_hash=%s OR (user_agent IS NOT NULL AND user_agent=%s))
                """,
                (
                    actor["actor_type"],
                    actor["actor_id"],
                    endpoint_hash,
                    request.headers.get("User-Agent"),
                ),
            )
        db.commit()
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True})


@chat_bp.post("/api/push/unsubscribe")
def push_unsubscribe():
    actor = get_chat_actor()
    if not actor:
        abort(403)
    token = (request.form.get("csrf_token") or (request.json or {}).get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        abort(400)
    endpoint = ((request.get_json(silent=True) or {}).get("endpoint") or "").strip()
    endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest() if endpoint else ""
    if not endpoint:
        abort(400)
    if not _ensure_chat_push_schema():
        abort(500)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            DELETE FROM chat_push_subscriptions
             WHERE actor_type=%s AND actor_id=%s AND endpoint_hash=%s
            """,
            (actor["actor_type"], actor["actor_id"], endpoint_hash),
        )
        db.commit()
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True})


@chat_bp.post("/api/events/<int:event_id>/broadcast")
def broadcast_push(event_id: int):
    actor = get_chat_actor()
    if not actor or actor["actor_type"] == "line":
        abort(403)
    if not _can_access_event(event_id, actor):
        abort(403)
    token = (request.form.get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        abort(400)

    msg = _validate_body(request.form.get("body") or "")
    db = get_db()
    cur = db.cursor(dictionary=True)
    sent_count = 0
    try:
        cur.execute("SELECT user_id FROM mfu_event_member WHERE event_id=%s", (event_id,))
        for row in cur.fetchall() or []:
            sent_count += _send_push_to_actor(
                "line",
                str(row["user_id"]),
                {
                    "title": "イベント通知",
                    "body": msg,
                    "event_id": event_id,
                    "url": f"/chat/events/{event_id}",
                },
            )
    finally:
        cur.close()
        db.close()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT user_id FROM mfu_event_member WHERE event_id=%s", (event_id,))
        for row in cur.fetchall() or []:
            recipient = int(row["user_id"])
            digest = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:16]
            _create_external_chat_notification(
                recipient_user_id=recipient,
                kind="chat_broadcast",
                title="イベント通知",
                body=msg,
                event_id=event_id,
                dedup_key=f"chat:broadcast:{event_id}:{digest}:{recipient}",
            )
    finally:
        cur.close()
        db.close()
    _log_notification(event_id, "broadcast", {"body": msg}, sent_count)
    return jsonify({"ok": True, "sent_count": sent_count})


@chat_bp.get("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "MFU Event Chat",
            "short_name": "MFU Chat",
            "start_url": "/chat/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#0d6efd",
            "icons": [],
        }
    )


@chat_bp.get("/sw.js")
def sw():
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@socketio.on("connect")
def chat_connect():
    actor = get_chat_actor()
    current_app.logger.warning("chat socket connect actor=%s", actor)  # ★追加
    if not actor:
        current_app.logger.warning("chat socket connect denied: no actor")
        return False
    return True

@socketio.on("chat_join")
def on_join(data):
    actor = get_chat_actor()
    event_id = int((data or {}).get("event_id") or 0)
    if not actor or not event_id or not _can_access_event(event_id, actor):
        current_app.logger.warning("chat join denied event=%s actor=%s", event_id, actor)
        disconnect()
        return
    join_room(f"event:{event_id}")
    emit("chat_joined", {"event_id": event_id})
    emit("chat_read_snapshot", {"event_id": event_id, "read_states": _load_event_read_state_snapshot(event_id)})


@socketio.on("chat_seen")
def on_seen(data):
    actor = get_chat_actor()
    if not actor:
        disconnect()
        return

    event_id = int((data or {}).get("event_id") or 0)
    last_seen_message_id = int((data or {}).get("last_seen_message_id") or 0)
    if event_id <= 0 or last_seen_message_id <= 0 or not _can_access_event(event_id, actor):
        disconnect()
        return

    effective_last_read_id = _upsert_chat_read_state(event_id, actor, last_seen_message_id)
    emit(
        "chat_read_update",
        {
            "actor_type": actor["actor_type"],
            "actor_id": actor["actor_id"],
            "display_name": actor["display_name"],
            "last_read_message_id": effective_last_read_id,
        },
        to=f"event:{event_id}",
    )


@socketio.on("chat_send")
def on_send(data):
    actor = get_chat_actor()
    event_id = int((data or {}).get("event_id") or 0)
    raw_body = (data or {}).get("body") or ""
    raw_reply_to_message_id = (data or {}).get("reply_to_message_id")

    t0 = time.monotonic()
    current_app.logger.warning(
        "chat_send recv event_id=%s actor=%s body_len=%s",
        event_id,
        actor,
        len(raw_body),
    )

    if not actor:
        disconnect()
        return
    if not event_id or not _can_access_event(event_id, actor):
        disconnect()
        return
    if not _ensure_chat_delete_schema():
        emit("chat_error", {"error": "メッセージ削除機能の初期化に失敗しました"})
        return
    if not _check_rate_limit(actor):
        emit("chat_error", {"error": "送信間隔が短すぎます"})
        return

    try:
        body = _validate_body(raw_body)
        reply_to_message_id = _validate_reply_to_message_id(event_id, raw_reply_to_message_id)
    except ValueError as exc:
        emit("chat_error", {"error": str(exc)})
        return

    message = _enrich_reply_fields(_save_message(event_id, actor, body, reply_to_message_id))
    t1 = time.monotonic()
    message_payload = _present_message(message, actor, avatar_cache={})
    emit("chat_message", message_payload, to=f"event:{event_id}")

    mention_names = _extract_mentions(body)
    mention_targets = _lookup_mention_targets(event_id, mention_names)
    sent_count = 0
    for target in mention_targets:
        sent_count += _send_push_to_actor(
            target["actor_type"],
            target["actor_id"],
            {
                "title": f"{actor['display_name']}さんからメンション",
                "body": body,
                "event_id": event_id,
                "url": f"/chat/events/{event_id}",
            },
        )
        if target.get("actor_type") == "line":
            _create_external_chat_notification(
                recipient_user_id=int(target["actor_id"]),
                kind="chat_mention",
                title=f"{actor['display_name']}さんからメンション",
                body=body,
                event_id=event_id,
                dedup_key=f"chat:mention:{event_id}:{message_payload['id']}:{target['actor_id']}",
            )
    if mention_targets:
        _log_notification(event_id, "mention", {"names": mention_names, "message_id": message_payload["id"]}, sent_count)

    t2 = time.monotonic()
    app = current_app._get_current_object()
    _submit_chat_message_push_async(
        app,
        event_id,
        actor,
        actor["display_name"],
        body,
        int(message_payload["id"]),
        {"t0": t0, "t1": t1, "t2": t2},
        has_image=bool(message.get("image_file")),
    )


@socketio.on("chat_react")
def on_react(data):
    actor = get_chat_actor()
    if not actor:
        disconnect()
        return
    if not _ensure_chat_reaction_schema():
        emit("chat_error", {"error": "リアクション機能の初期化に失敗しました"})
        return
    if not _ensure_chat_delete_schema():
        emit("chat_error", {"error": "メッセージ削除機能の初期化に失敗しました"})
        return

    try:
        event_id = int((data or {}).get("event_id") or 0)
        message_id = int((data or {}).get("message_id") or 0)
    except (TypeError, ValueError):
        disconnect()
        return

    emoji = str((data or {}).get("emoji") or "")
    if event_id <= 0 or message_id <= 0 or not _can_access_event(event_id, actor):
        disconnect()
        return
    if emoji not in CHAT_ALLOWED_REACTION_EMOJIS:
        emit("chat_error", {"error": "利用できないリアクションです"})
        return

    db = get_db()
    cur = db.cursor(dictionary=True)
    changed_emoji: str | None = emoji
    try:
        cur.execute("SELECT COALESCE(deleted_flag, 0) AS deleted_flag FROM chat_messages WHERE id=%s AND event_id=%s LIMIT 1", (message_id, event_id))
        msg_row = cur.fetchone()
        if not msg_row:
            emit("chat_error", {"error": "対象メッセージが見つかりません"})
            return
        if int(msg_row.get("deleted_flag") or 0) == 1:
            emit("chat_error", {"error": "削除済みメッセージにはリアクションできません"})
            return

        cur.execute(
            """
            SELECT emoji
              FROM chat_message_reactions
             WHERE message_id=%s
               AND actor_type=%s
               AND actor_id=%s
             LIMIT 1
            """,
            (message_id, actor["actor_type"], str(actor["actor_id"])),
        )
        existing = cur.fetchone()
        now = datetime.utcnow()
        if existing and (existing.get("emoji") or "") == emoji:
            cur.execute(
                """
                DELETE FROM chat_message_reactions
                 WHERE message_id=%s
                   AND actor_type=%s
                   AND actor_id=%s
                """,
                (message_id, actor["actor_type"], str(actor["actor_id"])),
            )
            changed_emoji = None
        else:
            cur.execute(
                """
                INSERT INTO chat_message_reactions (
                    event_id, message_id, actor_type, actor_id, emoji, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    emoji=VALUES(emoji),
                    updated_at=VALUES(updated_at)
                """,
                (event_id, message_id, actor["actor_type"], str(actor["actor_id"]), emoji, now, now),
            )

        cur.execute(
            """
            SELECT emoji, COUNT(*) AS cnt
              FROM chat_message_reactions
             WHERE message_id=%s
             GROUP BY emoji
            """,
            (message_id,),
        )
        reactions = [{"emoji": row.get("emoji") or "", "count": int(row.get("cnt") or 0)} for row in (cur.fetchall() or [])]
        reactions.sort(key=lambda item: CHAT_ALLOWED_REACTION_EMOJIS.index(item["emoji"]) if item["emoji"] in CHAT_ALLOWED_REACTION_EMOJIS else 999)
        db.commit()
    finally:
        cur.close()
        db.close()

    emit(
        "chat_reaction_update",
        {
            "event_id": event_id,
            "message_id": message_id,
            "reactions": reactions,
            "changed": {
                "actor_type": actor["actor_type"],
                "actor_id": str(actor["actor_id"]),
                "emoji": changed_emoji,
            },
        },
        to=f"event:{event_id}",
    )


@socketio.on("chat_notify_dm")
def notify_dm(data):
    actor = get_chat_actor()
    if not actor:
        disconnect()
        return
    event_id = int((data or {}).get("event_id") or 0)
    target_actor_type = (data or {}).get("target_actor_type")
    target_actor_id = str((data or {}).get("target_actor_id") or "")
    if not _can_access_event(event_id, actor):
        disconnect()
        return
    dm_body = (data or {}).get("body") or ""
    sent_count = _send_push_to_actor(
        target_actor_type,
        target_actor_id,
        {
            "title": "ダイレクト通知",
            "body": dm_body,
            "event_id": event_id,
            "url": f"/chat/events/{event_id}",
        },
    )
    source_message_id = int((data or {}).get("message_id") or 0)
    dm_fallback = hashlib.sha256(str(dm_body).encode("utf-8")).hexdigest()[:12]
    dedup_suffix = str(source_message_id) if source_message_id > 0 else dm_fallback
    if target_actor_type == "line":
        _create_external_chat_notification(
            recipient_user_id=int(target_actor_id),
            kind="chat_dm",
            title="ダイレクト通知",
            body=str(dm_body),
            event_id=event_id,
            dedup_key=f"chat:dm:{event_id}:{dedup_suffix}:{target_actor_id}",
        )
    _log_notification(event_id, "dm", {"target_actor_type": target_actor_type, "target_actor_id": target_actor_id}, sent_count)
    emit("chat_dm_notified", {"ok": True, "sent_count": sent_count})
