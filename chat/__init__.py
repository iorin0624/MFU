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
import uuid
from collections import deque
from urllib.parse import urlencode
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

try:
    from redis import Redis
except Exception:  # pragma: no cover
    Redis = None


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
RATE_LIMIT_PER_SECOND = 1
RATE_LIMIT_PER_MINUTE = 30
RATE_LIMIT_ERROR_MESSAGE = "送信が速すぎます。少し待ってください"
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
CHAT_ROOMS_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_ROOMS_SCHEMA_READY: bool | None = None
CHAT_ROOM_MEMBERS_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_ROOM_MEMBERS_SCHEMA_READY: bool | None = None
CHAT_MESSAGES_ROOM_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_MESSAGES_ROOM_SCHEMA_READY: bool | None = None
CHAT_EDIT_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_EDIT_SCHEMA_READY: bool | None = None
CHAT_SEARCH_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_SEARCH_SCHEMA_READY: bool | None = None
CHAT_THREAD_SCHEMA_CHECK_LOCK = threading.Lock()
CHAT_THREAD_SCHEMA_READY: bool | None = None
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
CHAT_TYPING_STATE_LOCK = threading.Lock()
CHAT_TYPING_STATE: dict[str, dict[str, Any]] = {}
CHAT_TYPING_TTL_SECONDS = 3

CHAT_RATE_LIMIT_LOCK = threading.Lock()
CHAT_RATE_LIMIT_MEMORY: dict[str, deque[float]] = {}
CHAT_RATE_LIMIT_MEMORY_MAX_KEYS = 5000
CHAT_RATE_LIMIT_MEMORY_WINDOW_SECONDS = 60
CHAT_RATE_LIMIT_REDIS_CLIENT: Any | None = None
CHAT_RATE_LIMIT_REDIS_INIT_ATTEMPTED = False


def _actor_log_id(actor: dict[str, Any] | None) -> str:
    if not actor:
        return "unknown"
    return f"{actor.get('actor_type', 'unknown')}:{actor.get('actor_id', 'unknown')}"


def _audit_log(action: str, **fields: Any) -> None:
    normalized: list[str] = [f"action={action}"]
    for key in sorted(fields.keys()):
        value = fields.get(key)
        if value is None:
            continue
        text = str(value).replace("\n", " ").replace("\r", " ").strip()
        normalized.append(f"{key}={text}")
    current_app.logger.info("chat_audit %s", " ".join(normalized))


def _get_rate_limit_redis_client() -> Any | None:
    global CHAT_RATE_LIMIT_REDIS_CLIENT, CHAT_RATE_LIMIT_REDIS_INIT_ATTEMPTED
    if CHAT_RATE_LIMIT_REDIS_INIT_ATTEMPTED:
        return CHAT_RATE_LIMIT_REDIS_CLIENT
    CHAT_RATE_LIMIT_REDIS_INIT_ATTEMPTED = True

    redis_url = (os.getenv("CHAT_RATE_LIMIT_REDIS_URL") or "").strip()
    if not redis_url or Redis is None:
        return None
    try:
        client = Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        CHAT_RATE_LIMIT_REDIS_CLIENT = client
    except Exception:
        current_app.logger.warning("chat rate-limit redis init failed", exc_info=True)
        CHAT_RATE_LIMIT_REDIS_CLIENT = None
    return CHAT_RATE_LIMIT_REDIS_CLIENT


def _check_rate_limit_memory(actor_key: str, now_ts: float) -> bool:
    with CHAT_RATE_LIMIT_LOCK:
        history = CHAT_RATE_LIMIT_MEMORY.get(actor_key)
        if history is None:
            history = deque()
            CHAT_RATE_LIMIT_MEMORY[actor_key] = history

        threshold_minute = now_ts - CHAT_RATE_LIMIT_MEMORY_WINDOW_SECONDS
        while history and history[0] <= threshold_minute:
            history.popleft()

        second_count = 0
        for ts in reversed(history):
            if now_ts - ts < RATE_LIMIT_SECONDS:
                second_count += 1
            else:
                break

        if second_count >= RATE_LIMIT_PER_SECOND or len(history) >= RATE_LIMIT_PER_MINUTE:
            return False

        history.append(now_ts)

        if len(CHAT_RATE_LIMIT_MEMORY) > CHAT_RATE_LIMIT_MEMORY_MAX_KEYS:
            stale_before = now_ts - (CHAT_RATE_LIMIT_MEMORY_WINDOW_SECONDS * 2)
            stale_keys = [k for k, dq in CHAT_RATE_LIMIT_MEMORY.items() if not dq or dq[-1] < stale_before]
            for stale_key in stale_keys[: max(1, len(CHAT_RATE_LIMIT_MEMORY) // 4)]:
                CHAT_RATE_LIMIT_MEMORY.pop(stale_key, None)
    return True


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


def _ensure_chat_edit_schema() -> bool:
    global CHAT_EDIT_SCHEMA_READY
    if CHAT_EDIT_SCHEMA_READY is not None:
        return CHAT_EDIT_SCHEMA_READY

    with CHAT_EDIT_SCHEMA_CHECK_LOCK:
        if CHAT_EDIT_SCHEMA_READY is not None:
            return CHAT_EDIT_SCHEMA_READY

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            definitions = {
                "edited_flag": "ALTER TABLE chat_messages ADD COLUMN edited_flag TINYINT NOT NULL DEFAULT 0 AFTER body",
                "edited_at": "ALTER TABLE chat_messages ADD COLUMN edited_at DATETIME NULL AFTER edited_flag",
            }
            for column_name, ddl in definitions.items():
                cur.execute(f"SHOW COLUMNS FROM chat_messages LIKE '{column_name}'")
                if cur.fetchone():
                    continue
                try:
                    cur.execute(ddl)
                except Exception:
                    current_app.logger.warning("chat edit schema ensure column failed column=%s", column_name, exc_info=True)

            db.commit()
            missing_columns: list[str] = []
            for column_name in definitions:
                cur.execute(f"SHOW COLUMNS FROM chat_messages LIKE '{column_name}'")
                if not cur.fetchone():
                    missing_columns.append(column_name)
            CHAT_EDIT_SCHEMA_READY = len(missing_columns) == 0
            if missing_columns:
                current_app.logger.warning("chat edit schema ensure missing columns=%s", ",".join(missing_columns))
            return CHAT_EDIT_SCHEMA_READY
        except Exception:
            current_app.logger.warning("chat edit schema ensure failed", exc_info=True)
            CHAT_EDIT_SCHEMA_READY = False
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


def _log_auto_migration_error(name: str, sql: str, event_id: int | None = None) -> None:
    current_app.logger.warning(
        "chat auto-migration failed name=%s event_id=%s sql=%s",
        name,
        event_id,
        sql,
        exc_info=True,
    )


def _ensure_chat_rooms_schema() -> bool:
    global CHAT_ROOMS_SCHEMA_READY
    if CHAT_ROOMS_SCHEMA_READY is not None:
        return CHAT_ROOMS_SCHEMA_READY
    with CHAT_ROOMS_SCHEMA_CHECK_LOCK:
        if CHAT_ROOMS_SCHEMA_READY is not None:
            return CHAT_ROOMS_SCHEMA_READY
        db = get_db()
        cur = db.cursor()
        try:
            sql = """
            CREATE TABLE IF NOT EXISTS chat_rooms (
              room_id VARCHAR(36) PRIMARY KEY,
              event_id BIGINT NOT NULL,
              room_name VARCHAR(80) NOT NULL,
              is_main TINYINT NOT NULL DEFAULT 0,
              is_archived TINYINT NOT NULL DEFAULT 0,
              created_at DATETIME NOT NULL,
              created_by_actor_type VARCHAR(16) NOT NULL,
              created_by_actor_id VARCHAR(64) NOT NULL,
              updated_at DATETIME NOT NULL,
              updated_by_actor_type VARCHAR(16) NOT NULL,
              updated_by_actor_id VARCHAR(64) NOT NULL,
              UNIQUE KEY uq_chat_rooms_event_main (event_id, is_main),
              KEY idx_chat_rooms_event (event_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            cur.execute(sql)
            db.commit()
            CHAT_ROOMS_SCHEMA_READY = True
            return True
        except Exception:
            _log_auto_migration_error("_ensure_chat_rooms_schema", sql)
            CHAT_ROOMS_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_room_members_schema() -> bool:
    global CHAT_ROOM_MEMBERS_SCHEMA_READY
    if CHAT_ROOM_MEMBERS_SCHEMA_READY is not None:
        return CHAT_ROOM_MEMBERS_SCHEMA_READY
    with CHAT_ROOM_MEMBERS_SCHEMA_CHECK_LOCK:
        if CHAT_ROOM_MEMBERS_SCHEMA_READY is not None:
            return CHAT_ROOM_MEMBERS_SCHEMA_READY
        db = get_db()
        cur = db.cursor()
        try:
            sql = """
            CREATE TABLE IF NOT EXISTS chat_room_members (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              room_id VARCHAR(36) NOT NULL,
              actor_type VARCHAR(16) NOT NULL,
              actor_id VARCHAR(64) NOT NULL,
              added_at DATETIME NOT NULL,
              added_by_actor_type VARCHAR(16) NOT NULL,
              added_by_actor_id VARCHAR(64) NOT NULL,
              UNIQUE KEY uq_chat_room_member (room_id, actor_type, actor_id),
              KEY idx_chat_room_member_room (room_id),
              KEY idx_chat_room_member_actor (actor_type, actor_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            cur.execute(sql)
            db.commit()
            CHAT_ROOM_MEMBERS_SCHEMA_READY = True
            return True
        except Exception:
            _log_auto_migration_error("_ensure_chat_room_members_schema", sql)
            CHAT_ROOM_MEMBERS_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_main_room(event_id: int) -> str | None:
    if not _ensure_chat_rooms_schema():
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT room_id FROM chat_rooms WHERE event_id=%s AND is_main=1 LIMIT 1", (event_id,))
        row = cur.fetchone()
        if row and row.get("room_id"):
            return str(row["room_id"])
        room_id = str(uuid.uuid4())
        now = datetime.utcnow()
        cur.execute(
            """
            INSERT INTO chat_rooms (
              room_id, event_id, room_name, is_main, is_archived,
              created_at, created_by_actor_type, created_by_actor_id,
              updated_at, updated_by_actor_type, updated_by_actor_id
            ) VALUES (%s,%s,%s,1,0,%s,'system','migration',%s,'system','migration')
            """,
            (room_id, event_id, "メイン", now, now),
        )
        db.commit()
        return room_id
    except Exception:
        _log_auto_migration_error("_ensure_main_room", "INSERT chat_rooms is_main", event_id)
        return None
    finally:
        cur.close()
        db.close()


def _ensure_chat_messages_room_schema() -> bool:
    global CHAT_MESSAGES_ROOM_SCHEMA_READY
    if CHAT_MESSAGES_ROOM_SCHEMA_READY is not None:
        return CHAT_MESSAGES_ROOM_SCHEMA_READY
    with CHAT_MESSAGES_ROOM_SCHEMA_CHECK_LOCK:
        if CHAT_MESSAGES_ROOM_SCHEMA_READY is not None:
            return CHAT_MESSAGES_ROOM_SCHEMA_READY
        if not _ensure_chat_rooms_schema():
            CHAT_MESSAGES_ROOM_SCHEMA_READY = False
            return False
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'room_id'")
            if not cur.fetchone():
                sql = "ALTER TABLE chat_messages ADD COLUMN room_id VARCHAR(36) NULL AFTER event_id"
                try:
                    cur.execute(sql)
                except Exception:
                    _log_auto_migration_error("_ensure_chat_messages_room_schema", sql)

            cur.execute("SELECT DISTINCT event_id FROM chat_messages")
            for row in cur.fetchall() or []:
                event_id = int(row.get("event_id") or 0)
                if event_id > 0:
                    _ensure_main_room(event_id)

            sql = """
            UPDATE chat_messages m
            JOIN chat_rooms r ON r.event_id=m.event_id AND r.is_main=1
            SET m.room_id=r.room_id
            WHERE m.room_id IS NULL
            """
            try:
                cur.execute(sql)
            except Exception:
                _log_auto_migration_error("_ensure_chat_messages_room_schema", sql)

            try:
                sql = "ALTER TABLE chat_messages MODIFY COLUMN room_id VARCHAR(36) NOT NULL"
                cur.execute(sql)
            except Exception:
                _log_auto_migration_error("_ensure_chat_messages_room_schema", sql)

            try:
                sql = "ALTER TABLE chat_messages ADD KEY idx_chat_messages_room (room_id, id)"
                cur.execute(sql)
            except Exception:
                pass

            db.commit()
            CHAT_MESSAGES_ROOM_SCHEMA_READY = True
            return True
        except Exception:
            _log_auto_migration_error("_ensure_chat_messages_room_schema", "chat_messages room migration")
            CHAT_MESSAGES_ROOM_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_search_schema() -> bool:
    global CHAT_SEARCH_SCHEMA_READY
    if CHAT_SEARCH_SCHEMA_READY is not None:
        return CHAT_SEARCH_SCHEMA_READY

    with CHAT_SEARCH_SCHEMA_CHECK_LOCK:
        if CHAT_SEARCH_SCHEMA_READY is not None:
            return CHAT_SEARCH_SCHEMA_READY

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SHOW INDEX FROM chat_messages WHERE Key_name='ft_chat_body'")
            if cur.fetchone():
                CHAT_SEARCH_SCHEMA_READY = True
                return True

            try:
                cur.execute("ALTER TABLE chat_messages ADD FULLTEXT KEY ft_chat_body (body)")
                db.commit()
            except Exception:
                current_app.logger.warning("chat search schema ensure fulltext failed", exc_info=True)

            cur.execute("SHOW INDEX FROM chat_messages WHERE Key_name='ft_chat_body'")
            CHAT_SEARCH_SCHEMA_READY = bool(cur.fetchone())
            return CHAT_SEARCH_SCHEMA_READY
        except Exception:
            current_app.logger.warning("chat search schema ensure failed", exc_info=True)
            CHAT_SEARCH_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_thread_schema() -> bool:
    global CHAT_THREAD_SCHEMA_READY
    if CHAT_THREAD_SCHEMA_READY is not None:
        return CHAT_THREAD_SCHEMA_READY

    with CHAT_THREAD_SCHEMA_CHECK_LOCK:
        if CHAT_THREAD_SCHEMA_READY is not None:
            return CHAT_THREAD_SCHEMA_READY

        _ensure_chat_messages_room_schema()
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'thread_root_id'")
            thread_column_rows = cur.fetchall() or []
            if not thread_column_rows:
                try:
                    cur.execute("ALTER TABLE chat_messages ADD COLUMN thread_root_id BIGINT NULL AFTER reply_to_message_id")
                    db.commit()
                except Exception:
                    current_app.logger.warning("chat auto-migration: add thread_root_id failed", exc_info=True)
                    CHAT_THREAD_SCHEMA_READY = False
                    return False

            for idx_name, sql in (
                (
                    "idx_chat_thread_root",
                    "ALTER TABLE chat_messages ADD KEY idx_chat_thread_root (event_id, room_id, thread_root_id, id)",
                ),
                (
                    "idx_chat_thread_room",
                    "ALTER TABLE chat_messages ADD KEY idx_chat_thread_room (event_id, room_id, id)",
                ),
            ):
                try:
                    cur.execute(f"SHOW INDEX FROM chat_messages WHERE Key_name='{idx_name}'")
                    index_rows = cur.fetchall() or []
                    if not index_rows:
                        cur.execute(sql)
                        db.commit()
                except Exception:
                    current_app.logger.warning("chat auto-migration: add %s failed", idx_name, exc_info=True)

            try:
                cur.execute(
                    """
                    UPDATE chat_messages child
                    LEFT JOIN chat_messages parent ON parent.id = child.reply_to_message_id
                    SET child.thread_root_id = COALESCE(parent.thread_root_id, child.reply_to_message_id)
                    WHERE child.reply_to_message_id IS NOT NULL
                      AND child.thread_root_id IS NULL
                    """
                )
                db.commit()
            except Exception:
                current_app.logger.warning("chat auto-migration: backfill thread_root_id failed", exc_info=True)

            cur.execute("SHOW COLUMNS FROM chat_messages LIKE 'thread_root_id'")
            verified_rows = cur.fetchall() or []
            CHAT_THREAD_SCHEMA_READY = bool(verified_rows)
            return CHAT_THREAD_SCHEMA_READY
        except Exception:
            current_app.logger.warning("chat thread schema ensure failed", exc_info=True)
            CHAT_THREAD_SCHEMA_READY = False
            return False
        finally:
            cur.close()
            db.close()


def _ensure_chat_read_state_room_schema() -> bool:
    if not _ensure_chat_read_state_schema() or not _ensure_chat_messages_room_schema():
        return False
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SHOW COLUMNS FROM chat_read_state LIKE 'room_id'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE chat_read_state ADD COLUMN room_id VARCHAR(36) NULL AFTER event_id")

        cur.execute("SELECT DISTINCT event_id FROM chat_read_state WHERE room_id IS NULL")
        for row in cur.fetchall() or []:
            event_id = int(row.get("event_id") or 0)
            if event_id <= 0:
                continue
            main_room_id = _ensure_main_room(event_id)
            if main_room_id:
                cur.execute(
                    "UPDATE chat_read_state SET room_id=%s WHERE event_id=%s AND room_id IS NULL",
                    (main_room_id, event_id),
                )
        try:
            cur.execute("ALTER TABLE chat_read_state MODIFY COLUMN room_id VARCHAR(36) NOT NULL")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE chat_read_state DROP INDEX uq_chat_read_state_actor")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE chat_read_state ADD UNIQUE KEY uq_chat_read_state_actor_room (event_id, room_id, actor_type, actor_id)")
        except Exception:
            pass
        db.commit()
        return True
    except Exception:
        _log_auto_migration_error("_ensure_chat_read_state_room_schema", "chat_read_state room migration")
        return False
    finally:
        cur.close()
        db.close()


def _get_room(event_id: int, room_id: str | None, *, allow_archived: bool = False) -> dict[str, Any] | None:
    if not _ensure_chat_rooms_schema():
        return None
    target_room_id = room_id or _ensure_main_room(event_id)
    if not target_room_id:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        sql = "SELECT room_id,event_id,room_name,is_main,is_archived FROM chat_rooms WHERE event_id=%s AND room_id=%s LIMIT 1"
        cur.execute(sql, (event_id, target_room_id))
        room = cur.fetchone()
        if not room:
            return None
        if not allow_archived and int(room.get("is_archived") or 0) == 1:
            return None
        return room
    finally:
        cur.close()
        db.close()


def _is_room_member(room_id: str, actor: dict[str, Any]) -> bool:
    if not _ensure_chat_room_members_schema():
        return False
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT 1 FROM chat_room_members WHERE room_id=%s AND actor_type=%s AND actor_id=%s LIMIT 1",
            (room_id, actor.get("actor_type"), str(actor.get("actor_id") or "")),
        )
        return bool(cur.fetchone())
    finally:
        cur.close()
        db.close()


def _can_manage_rooms(event_id: int, actor: dict[str, Any]) -> bool:
    if actor.get("actor_type") == "admin":
        return True
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if actor.get("actor_type") == "line":
            cur.execute(
                "SELECT 1 FROM mfu_event_member WHERE event_id=%s AND user_id=%s AND (COALESCE(is_host,0)=1 OR COALESCE(is_subhost,0)=1) LIMIT 1",
                (event_id, actor.get("actor_id")),
            )
            if cur.fetchone():
                return True
        cur.execute(
            "SELECT 1 FROM mfu_event_admin_acl WHERE event_id=%s AND username=%s LIMIT 1",
            (event_id, actor.get("actor_id")),
        )
        return bool(cur.fetchone())
    finally:
        cur.close()
        db.close()


def _can_access_room(event_id: int, room_id: str | None, actor: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any] | None]:
    if not _can_access_event(event_id, actor):
        return False, None, None
    room = _get_room(event_id, room_id)
    if not room:
        return False, None, None
    rid = str(room.get("room_id") or "")
    if int(room.get("is_main") or 0) == 1:
        return True, rid, room
    if _is_room_member(rid, actor):
        return True, rid, room
    return False, rid, room


def _list_accessible_rooms(event_id: int, actor: dict[str, Any]) -> list[dict[str, Any]]:
    main_room = _ensure_main_room(event_id)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT room_id, room_name, is_main
              FROM chat_rooms
             WHERE event_id=%s
               AND is_archived=0
               AND (
                    is_main=1
                    OR room_id IN (
                        SELECT room_id
                          FROM chat_room_members
                         WHERE actor_type=%s AND actor_id=%s
                    )
               )
             ORDER BY is_main DESC, room_name ASC
            """,
            (event_id, actor.get("actor_type"), str(actor.get("actor_id") or "")),
        )
        rooms = cur.fetchall() or []
        if main_room and not any(str(r.get("room_id")) == main_room for r in rooms):
            rooms.insert(0, {"room_id": main_room, "room_name": "メイン", "is_main": 1})
        return rooms
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


def _load_event_read_state_snapshot(event_id: int, room_id: str) -> list[dict[str, Any]]:
    if not _ensure_chat_read_state_room_schema():
        return []

    participants = _build_chat_participants(event_id)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT actor_type, actor_id, last_read_message_id, updated_at
              FROM chat_read_state
             WHERE event_id=%s AND room_id=%s
            """,
            (event_id, room_id),
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


def _upsert_chat_read_state(event_id: int, room_id: str, actor: dict[str, Any], last_seen_message_id: int) -> int:
    if not _ensure_chat_read_state_room_schema():
        return 0

    now = datetime.utcnow()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            INSERT INTO chat_read_state (event_id, room_id, actor_type, actor_id, last_read_message_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              last_read_message_id = GREATEST(IFNULL(last_read_message_id, 0), VALUES(last_read_message_id)),
              updated_at = VALUES(updated_at)
            """,
            (event_id, room_id, actor["actor_type"], actor["actor_id"], last_seen_message_id, now),
        )
        cur.execute(
            """
            SELECT last_read_message_id
              FROM chat_read_state
             WHERE event_id=%s AND room_id=%s AND actor_type=%s AND actor_id=%s
             LIMIT 1
            """,
            (event_id, room_id, actor["actor_type"], actor["actor_id"]),
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
    can_edit = False
    if not deleted_flag:
        if actor_is_admin:
            can_delete = True
        elif is_me and isinstance(msg.get("created_at"), datetime):
            can_delete = msg["created_at"] + timedelta(hours=12) >= datetime.utcnow()
        if is_me and isinstance(msg.get("created_at"), datetime):
            can_edit = msg["created_at"] + timedelta(hours=12) >= datetime.utcnow()

    edited_flag = 1 if int(msg.get("edited_flag") or 0) == 1 else 0

    return {
        "id": msg["id"],
        "event_id": msg["event_id"],
        "sender_id": sender_id,
        "sender_display_name": msg["sender_display_name"],
        "sender_avatar_url": _resolve_sender_avatar_url(sender_actor_type, sender_actor_id, avatar_cache=avatar_cache),
        "body": body_value,
        "body_html": body_html,
        "edited_flag": edited_flag,
        "edited_at": msg.get("edited_at").isoformat() if msg.get("edited_at") else None,
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
        "thread_root_id": msg.get("thread_root_id"),
        "thread_reply_count": int(msg.get("thread_reply_count") or 0),
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
        "can_edit": can_edit,
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


def _typing_state_key(event_id: int, room_id: str, actor_type: str, actor_id: str) -> str:
    return f"{event_id}:{room_id}:{actor_type}:{actor_id}"


def _emit_typing_state(event_id: int, room_id: str, actor: dict[str, Any], is_typing: bool) -> None:
    socketio.emit(
        "chat_typing_update",
        {
            "actor_type": actor["actor_type"],
            "actor_id": str(actor["actor_id"]),
            "display_name": actor.get("display_name") or str(actor["actor_id"]),
            "is_typing": bool(is_typing),
            "event_id": event_id,
            "room_id": room_id,
        },
        room=f"event:{event_id}:room:{room_id}",
    )


def _cleanup_stale_typing_states(event_id: int | None = None, room_id: str | None = None) -> None:
    now = time.monotonic()
    stale_keys: list[str] = []
    stale_payloads: list[tuple[int, str, dict[str, Any]]] = []

    with CHAT_TYPING_STATE_LOCK:
        for key, item in list(CHAT_TYPING_STATE.items()):
            if not item.get("is_typing"):
                stale_keys.append(key)
                continue
            if event_id is not None and item.get("event_id") != event_id:
                continue
            if room_id is not None and item.get("room_id") != room_id:
                continue
            last_ts = float(item.get("last_ts") or 0)
            if now - last_ts >= CHAT_TYPING_TTL_SECONDS:
                stale_keys.append(key)
                stale_payloads.append((int(item.get("event_id") or 0), str(item.get("room_id") or ""), dict(item.get("actor") or {})))

        for key in stale_keys:
            CHAT_TYPING_STATE.pop(key, None)

    for stale_event_id, stale_room_id, stale_actor in stale_payloads:
        if stale_event_id <= 0 or not stale_room_id or not stale_actor:
            continue
        _emit_typing_state(stale_event_id, stale_room_id, stale_actor, False)


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


def _load_messages(event_id: int, room_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    _ensure_chat_messages_room_schema()
    effective_room_id = room_id or _ensure_main_room(event_id)
    has_reply_schema = _ensure_chat_reply_schema()
    has_image_schema = _ensure_chat_image_schema()
    has_delete_schema = _ensure_chat_delete_schema()
    has_edit_schema = _ensure_chat_edit_schema()
    has_thread_schema = _ensure_chat_thread_schema()
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
    edit_columns = (
        "m.edited_flag, m.edited_at"
        if has_edit_schema
        else "0 AS edited_flag, NULL AS edited_at"
    )
    thread_column = "m.thread_root_id" if has_thread_schema else "NULL AS thread_root_id"
    timeline_thread_filter = "AND m.thread_root_id IS NULL" if has_thread_schema else ""
    timeline_thread_filter_plain = "AND thread_root_id IS NULL" if has_thread_schema else ""
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
                       {edit_columns},
                       {thread_column},
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
                   AND (m.room_id=%s OR (m.room_id IS NULL AND %s IS NOT NULL))
                   {timeline_thread_filter}
                 ORDER BY m.created_at DESC
                 LIMIT %s
                """,
                (event_id, effective_room_id, effective_room_id, limit),
            )
            rows = cur.fetchall() or []
            if has_thread_schema:
                _apply_thread_reply_count(event_id, effective_room_id, rows)
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
                   {'deleted_flag, deleted_at, deleted_by_actor_type, deleted_by_actor_id' if has_delete_schema else '0 AS deleted_flag, NULL AS deleted_at, NULL AS deleted_by_actor_type, NULL AS deleted_by_actor_id'},
                   {'edited_flag, edited_at' if has_edit_schema else '0 AS edited_flag, NULL AS edited_at'},
                   {'thread_root_id' if has_thread_schema else 'NULL AS thread_root_id'}
              FROM chat_messages
             WHERE event_id=%s
               AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL))
               {timeline_thread_filter_plain}
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (event_id, effective_room_id, effective_room_id, limit),
        )
        rows = cur.fetchall() or []
        if has_thread_schema:
            _apply_thread_reply_count(event_id, effective_room_id, rows)
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
    room_id: str,
    actor: dict[str, Any],
    body: str,
    reply_to_message_id: int | None = None,
    thread_root_id: int | None = None,
    image_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    _ensure_chat_messages_room_schema()
    has_reply_schema = _ensure_chat_reply_schema()
    has_image_schema = _ensure_chat_image_schema()
    has_thread_schema = _ensure_chat_thread_schema()
    _ensure_chat_delete_schema()
    _ensure_chat_edit_schema()
    image_meta = image_meta or {}
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        insert_columns = ["event_id", "room_id", "sender_actor_type", "sender_actor_id", "sender_display_name", "body", "created_at"]
        insert_values: list[Any] = [event_id, room_id, actor["actor_type"], actor["actor_id"], actor["display_name"], body, now]

        if has_reply_schema:
            insert_columns.append("reply_to_message_id")
            insert_values.append(reply_to_message_id)
        else:
            reply_to_message_id = None

        if has_thread_schema:
            insert_columns.append("thread_root_id")
            insert_values.append(thread_root_id)
        else:
            thread_root_id = None

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
            "room_id": room_id,
            "sender_actor_type": actor["actor_type"],
            "sender_actor_id": actor["actor_id"],
            "sender_display_name": actor["display_name"],
            "body": body,
            "created_at": now,
            "reply_to_message_id": reply_to_message_id,
            "thread_root_id": thread_root_id,
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
            "edited_flag": 0,
            "edited_at": None,
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


def _looks_like_mpo_bytes(raw_bytes: bytes) -> bool:
    if len(raw_bytes) < 64:
        return False
    return raw_bytes[:2] == b"\xff\xd8" and b"MPF\x00" in raw_bytes[:4096]


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
    looks_like_mpo = lower_name.endswith(".mpo") or _looks_like_mpo_bytes(raw_bytes)

    if looks_like_heif and not HEIF_OPENER_AVAILABLE:
        raise ValueError(HEIC_UNSUPPORTED_MESSAGE)

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
        raise ValueError("画像ファイルとして読み取れません。Live Photo/連写画像やHEIC形式の可能性があります") from exc
    except OSError as exc:
        if looks_like_heif and not HEIF_OPENER_AVAILABLE:
            raise ValueError(HEIC_UNSUPPORTED_MESSAGE) from exc
        if looks_like_mpo or "MPO" in str(exc).upper():
            raise ValueError("MPO画像の読み込みに失敗しました。Live Photo/連写画像の可能性があります。互換性優先で再撮影をお試しください") from exc
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


def _validate_reply_to_message_id(event_id: int, room_id: str, raw_value: Any) -> int | None:
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
        cur.execute("SELECT id FROM chat_messages WHERE id=%s AND event_id=%s AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL)) LIMIT 1", (reply_to_message_id, event_id, room_id, room_id))
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


def _resolve_thread_root_id(event_id: int, room_id: str, reply_to_message_id: int | None) -> int | None:
    if not reply_to_message_id:
        return None
    if not _ensure_chat_thread_schema():
        return None

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, thread_root_id
              FROM chat_messages
             WHERE id=%s AND event_id=%s AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL))
             LIMIT 1
            """,
            (reply_to_message_id, event_id, room_id, room_id),
        )
        parent = cur.fetchone()
        if not parent:
            raise ValueError("返信先メッセージが見つかりません")
        return int(parent.get("thread_root_id") or parent["id"])
    finally:
        cur.close()
        db.close()


def _apply_thread_reply_count(event_id: int, room_id: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    root_ids = {int(row.get("id") or 0) for row in rows}
    root_ids.discard(0)
    if not root_ids:
        return

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        placeholders = ", ".join(["%s"] * len(root_ids))
        cur.execute(
            f"""
            SELECT thread_root_id, COUNT(*) AS cnt
              FROM chat_messages
             WHERE event_id=%s
               AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL))
               AND COALESCE(deleted_flag, 0)=0
               AND thread_root_id IN ({placeholders})
             GROUP BY thread_root_id
            """,
            (event_id, room_id, room_id, *list(root_ids)),
        )
        counts = {int(row.get("thread_root_id") or 0): int(row.get("cnt") or 0) for row in (cur.fetchall() or [])}
    except Exception:
        current_app.logger.warning("chat thread count aggregation failed", exc_info=True)
        counts = {}
    finally:
        cur.close()
        db.close()

    for row in rows:
        row_id = int(row.get("id") or 0)
        row["thread_reply_count"] = counts.get(row_id, 0) if row_id > 0 else 0


def _check_rate_limit(actor: dict[str, Any], *, route: str, event_id: int | None = None, room_id: str | None = None) -> bool:
    actor_key = _actor_log_id(actor)
    now_ts = time.time()
    redis_client = _get_rate_limit_redis_client()

    if redis_client is not None:
        sec_key = f"chat:rl:sec:{actor_key}"
        min_key = f"chat:rl:min:{actor_key}"
        try:
            sec_count = int(redis_client.incr(sec_key))
            if sec_count == 1:
                redis_client.expire(sec_key, RATE_LIMIT_SECONDS)

            min_count = int(redis_client.incr(min_key))
            if min_count == 1:
                redis_client.expire(min_key, CHAT_RATE_LIMIT_MEMORY_WINDOW_SECONDS)

            allowed = sec_count <= RATE_LIMIT_PER_SECOND and min_count <= RATE_LIMIT_PER_MINUTE
            if not allowed:
                _audit_log(
                    "rate_limit",
                    actor=actor_key,
                    event_id=event_id,
                    room_id=room_id,
                    route=route,
                    result="deny",
                )
            return allowed
        except Exception:
            current_app.logger.warning("chat rate-limit redis check failed", exc_info=True)

    allowed = _check_rate_limit_memory(actor_key, now_ts)
    if not allowed:
        _audit_log(
            "rate_limit",
            actor=actor_key,
            event_id=event_id,
            room_id=room_id,
            route=route,
            result="deny",
        )
    return allowed


def _extract_mentions(body: str) -> list[str]:
    return re.findall(r"@([\w\-ぁ-んァ-ン一-龥ー]+)", body)


def _lookup_mention_targets(event_id: int, room_id: str, names: list[str]) -> list[dict[str, Any]]:
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
        return [
            {"actor_type": "line", "actor_id": str(r["id"]), "display_name": r["nickname"]}
            for r in rows
            if _can_notify_actor_in_room(event_id, room_id, "line", str(r["id"]))
        ]
    finally:
        cur.close()
        db.close()


def _can_notify_actor_in_room(event_id: int, room_id: str, actor_type: str, actor_id: str) -> bool:
    room = _get_room(event_id, room_id, allow_archived=True)
    if not room:
        return False
    if int(room.get("is_main") or 0) == 1:
        return True
    return _is_room_member(room_id, {"actor_type": actor_type, "actor_id": actor_id})


def _build_chat_message_push_targets(event_id: int, room_id: str, sender_actor: dict[str, Any]) -> list[tuple[str, str]]:
    sender_key = _actor_sender_id(sender_actor["actor_type"], str(sender_actor["actor_id"]))
    targets: set[tuple[str, str]] = set()
    room = _get_room(event_id, room_id, allow_archived=True)
    if not room:
        return []

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if int(room.get("is_main") or 0) == 1:
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
        else:
            cur.execute(
                "SELECT actor_type, actor_id FROM chat_room_members WHERE room_id=%s",
                (room_id,),
            )
            for row in cur.fetchall() or []:
                actor_type = str(row.get("actor_type") or "")
                actor_id = str(row.get("actor_id") or "")
                if actor_type and actor_id:
                    targets.add((actor_type, actor_id))
    finally:
        cur.close()
        db.close()

    if int(room.get("is_main") or 0) == 1:
        targets.add(("admin", "admin"))
    return [t for t in targets if _actor_sender_id(t[0], t[1]) != sender_key and _can_notify_actor_in_room(event_id, room_id, t[0], t[1])]


def _create_external_chat_notification(
    *,
    recipient_user_id: int,
    kind: str,
    title: str,
    body: str,
    event_id: int,
    room_id: str | None,
    room_name: str | None,
    dedup_key: str,
) -> None:
    if not dedup_key:
        return
    from app.external_login_user.notifications import create_notification_external

    params = {"event_id": event_id}
    if room_id:
        params["room_id"] = room_id
    target_url = f"/chat/events/{event_id}?{urlencode(params)}"
    title_text = title
    if room_name:
        title_text = f"[{room_name}] {title}"

    create_notification_external(
        user_id=recipient_user_id,
        kind=kind,
        title=title_text,
        body=_build_plain_excerpt(body, max_len=300),
        target_url=target_url,
        dedup_key=dedup_key,
        event_id=event_id,
    )


def _send_chat_message_push_async(
    app: Any,
    event_id: int,
    room_id: str,
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
                "room_id": room_id,
                "url": f"/chat/events/{event_id}?{urlencode({'event_id': event_id, 'room_id': room_id})}",
                "title": "イベントチャット",
                "body": payload_body,
            }

            event = _get_event(event_id)
            if event and event.get("title"):
                payload["title"] = str(event.get("title"))
            room = _get_room(event_id, room_id, allow_archived=True)
            room_name = str(room.get("room_name") or "") if room else ""
            if room_name:
                payload["title"] = f"[{room_name}] {payload['title']}"

            sent_count = 0
            for actor_type, actor_id in _build_chat_message_push_targets(event_id, room_id, sender_actor):
                if not _can_notify_actor_in_room(event_id, room_id, actor_type, actor_id):
                    continue
                actor_push_metrics["target_actors"] += 1
                sent_count += _send_push_to_actor(actor_type, actor_id, payload, metrics=actor_push_metrics)
                if actor_type == "line":
                    inserted = create_notification_external(
                        user_id=int(actor_id),
                        kind="chat_message",
                        title=str(payload.get("title") or "イベントチャット"),
                        body=str(payload.get("body") or message_body),
                        target_url=f"/chat/events/{event_id}?{urlencode({'event_id': event_id, 'room_id': room_id})}",
                        dedup_key=f"chat:{event_id}:{room_id}:{message_id}:{actor_id}",
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
    room_id: str,
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
        room_id,
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
        _audit_log("push_notify", actor=f"{actor_type}:{actor_id}", event_id=payload.get("event_id"), room_id=payload.get("room_id"), result="ok", sent_count=sent)
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
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
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

    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    _ensure_chat_messages_room_schema()
    _ensure_chat_thread_schema()
    _ensure_chat_read_state_room_schema()
    _ensure_chat_delete_schema()
    _ensure_chat_edit_schema()

    requested_room_id = (request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, active_room = _can_access_room(event_id, requested_room_id, actor)
    if not allowed or not effective_room_id or not active_room:
        abort(403)

    event = _get_event(event_id)
    if not event:
        abort(404)
    avatar_cache: dict[str, str] = {}
    raw_messages = _load_messages(event_id, effective_room_id)
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
    accessible_rooms = _list_accessible_rooms(event_id, actor)
    return render_template(
        "chat/room.html",
        actor=actor,
        current_user_id=_actor_sender_id(actor["actor_type"], str(actor["actor_id"])),
        event=event,
        messages=messages,
        vapid_public_key=os.getenv("CHAT_VAPID_PUBLIC_KEY", ""),
        csrf_token=_chat_csrf(),
        can_broadcast=can_broadcast,
        can_manage_rooms=_can_manage_rooms(event_id, actor),
        accessible_rooms=accessible_rooms,
        active_room=active_room,
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
    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    _ensure_chat_messages_room_schema()
    _ensure_chat_thread_schema()
    _ensure_chat_edit_schema()
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
    room_id = (request.form.get("room_id") or request.args.get("room_id") or (request.get_json(silent=True) or {}).get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not allowed or not effective_room_id:
        return jsonify({"ok": False, "error": "このルームにはアクセスできません"}), 403
    if not _ensure_chat_delete_schema():
        return jsonify({"ok": False, "error": "メッセージ削除機能の初期化に失敗しました"}), 500
    if not _ensure_chat_image_schema():
        return jsonify({"ok": False, "error": "画像機能の初期化に失敗しました"}), 500
    if not _ensure_chat_message_images_schema():
        return jsonify({"ok": False, "error": "画像機能の初期化に失敗しました"}), 500

    payload = request.get_json(silent=True) or {}
    if not _check_rate_limit(actor, route="upload_image", event_id=event_id, room_id=effective_room_id):
        return jsonify({"ok": False, "error": RATE_LIMIT_ERROR_MESSAGE}), 429

    token = (request.form.get("csrf_token") or payload.get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        current_app.logger.info(
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
        reply_to_message_id = _validate_reply_to_message_id(event_id, effective_room_id, request.form.get("reply_to_message_id"))
        thread_root_id = _resolve_thread_root_id(event_id, effective_room_id, reply_to_message_id)
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

    message = _enrich_reply_fields(_save_message(event_id, effective_room_id, actor, body, reply_to_message_id, thread_root_id, image_meta=image_metas[0]))
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
    socketio.emit("chat_message", message_payload, to=f"event:{event_id}:room:{effective_room_id}")

    app = current_app._get_current_object()
    now = time.monotonic()
    _submit_chat_message_push_async(
        app,
        event_id,
        effective_room_id,
        actor,
        actor["display_name"],
        f"[スレッド] {body}" if message.get("thread_root_id") else body,
        int(message_payload["id"]),
        {"t0": now, "t1": now, "t2": now},
        has_image=True,
    )

    _audit_log("upload_image", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_payload.get("id"), result="ok")
    return jsonify({"ok": True, "message": message_payload, "room_id": effective_room_id})


@chat_bp.post("/api/events/<int:event_id>/messages/<int:message_id>/delete")
def delete_message(event_id: int, message_id: int):
    _ensure_chat_messages_room_schema()
    _ensure_chat_thread_schema()
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or request.form.get("room_id") or request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not allowed or not effective_room_id:
        return jsonify({"ok": False, "error": "このルームにはアクセスできません"}), 403
    if not _ensure_chat_delete_schema():
        return jsonify({"ok": False, "error": "メッセージ削除機能の初期化に失敗しました"}), 500

    payload = request.get_json(silent=True) or {}
    if not _check_rate_limit(actor, route="chat_delete", event_id=event_id, room_id=effective_room_id):
        return jsonify({"ok": False, "error": RATE_LIMIT_ERROR_MESSAGE}), 429

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
             WHERE id=%s AND event_id=%s AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL))
             LIMIT 1
            """,
            (message_id, event_id, effective_room_id, effective_room_id),
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

    _audit_log("chat_delete", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_id, result="ok")
    socketio.emit(
        "chat_delete_update",
        {
            "event_id": event_id,
            "message_id": message_id,
            "deleted_flag": 1,
            "deleted_at": deleted_at.isoformat() if deleted_at else None,
            "deleted_by_actor_type": deleted_by_actor_type,
        },
        room=f"event:{event_id}:room:{effective_room_id}",
    )
    return jsonify({"ok": True})


@chat_bp.post("/api/events/<int:event_id>/messages/<int:message_id>/edit")
def edit_message(event_id: int, message_id: int):
    _ensure_chat_messages_room_schema()
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
    if not _can_access_event(event_id, actor):
        return jsonify({"ok": False, "error": "このイベントにはアクセスできません"}), 403
    if not _ensure_chat_edit_schema():
        return jsonify({"ok": False, "error": "メッセージ編集機能の初期化に失敗しました"}), 500
    if not _ensure_chat_delete_schema():
        return jsonify({"ok": False, "error": "メッセージ削除機能の初期化に失敗しました"}), 500

    payload = request.get_json(silent=True) or {}
    room_id = str(payload.get("room_id") or request.form.get("room_id") or request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not allowed or not effective_room_id:
        _audit_log("chat_edit", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_id, result="deny", error="room_denied")
        return jsonify({"ok": False, "error": "このルームにはアクセスできません"}), 403

    if not _check_rate_limit(actor, route="chat_edit", event_id=event_id, room_id=effective_room_id):
        return jsonify({"ok": False, "error": RATE_LIMIT_ERROR_MESSAGE}), 429

    token = (request.form.get("csrf_token") or payload.get("csrf_token") or "").strip()
    if token != session.get("chat_csrf"):
        return jsonify({"ok": False, "error": "セッションが切れました。ページを更新して再試行してください"}), 400

    raw_body = payload.get("body") if "body" in payload else request.form.get("body")
    try:
        body = _validate_body(str(raw_body or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    actor_sender_id = _actor_sender_id(str(actor.get("actor_type") or ""), str(actor.get("actor_id") or ""))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, event_id, room_id, sender_actor_type, sender_actor_id, created_at,
                   COALESCE(deleted_flag, 0) AS deleted_flag
              FROM chat_messages
             WHERE id=%s AND event_id=%s AND room_id=%s
             LIMIT 1
            """,
            (message_id, event_id, effective_room_id),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "対象メッセージが見つかりません"}), 404
        if int(row.get("deleted_flag") or 0) == 1:
            return jsonify({"ok": False, "error": "削除済みメッセージは編集できません"}), 403

        message_sender_id = _actor_sender_id(str(row.get("sender_actor_type") or ""), str(row.get("sender_actor_id") or ""))
        if actor_sender_id != message_sender_id:
            return jsonify({"ok": False, "error": "このメッセージを編集する権限がありません"}), 403

        created_at = row.get("created_at")
        if not isinstance(created_at, datetime):
            return jsonify({"ok": False, "error": "送信時刻の取得に失敗しました"}), 400
        if created_at + timedelta(hours=12) < datetime.utcnow():
            return jsonify({"ok": False, "error": "送信から12時間を過ぎたため、編集できません"}), 403

        cur.execute(
            """
            UPDATE chat_messages
               SET body=%s,
                   edited_flag=1,
                   edited_at=NOW()
             WHERE id=%s
            """,
            (body, message_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT *
              FROM chat_messages
             WHERE id=%s AND event_id=%s AND room_id=%s
             LIMIT 1
            """,
            (message_id, event_id, effective_room_id),
        )
        edited_message = cur.fetchone()
    finally:
        cur.close()
        db.close()

    if not edited_message:
        _audit_log("chat_edit", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_id, result="error", error="reload_failed")
        return jsonify({"ok": False, "error": "編集後メッセージの再取得に失敗しました"}), 500

    edited_message = _enrich_reply_fields(edited_message)
    edited_message["reactions_summary"] = _load_reactions_by_message_ids([message_id]).get(message_id, [])
    edited_message["my_reaction"] = _load_my_reactions_by_message_ids([message_id], actor).get(message_id)
    edited_message["images"] = _load_message_images_by_message_ids([message_id]).get(message_id) or _fallback_images_from_message(edited_message)
    payload_message = _present_message(edited_message, actor, avatar_cache={})
    payload_message["message_id"] = payload_message.get("id")

    socketio.emit(
        "chat_edit_update",
        payload_message,
        room=f"event:{event_id}:room:{effective_room_id}",
    )
    _audit_log("chat_edit", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_id, result="ok")
    return jsonify({"ok": True})


@chat_bp.get("/api/events/<int:event_id>/threads/<int:root_message_id>")
def get_thread_messages(event_id: int, root_message_id: int):
    _ensure_chat_messages_room_schema()
    _ensure_chat_thread_schema()
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403

    room_id = str(request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not allowed or not effective_room_id:
        return jsonify({"ok": False, "error": "このルームにはアクセスできません"}), 403

    limit_raw = request.args.get("limit") or 200
    try:
        limit = max(1, min(int(limit_raw), 200))
    except (TypeError, ValueError):
        limit = 200

    has_reply_schema = _ensure_chat_reply_schema()
    has_image_schema = _ensure_chat_image_schema()
    has_delete_schema = _ensure_chat_delete_schema()
    has_edit_schema = _ensure_chat_edit_schema()
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
    edit_columns = (
        "m.edited_flag, m.edited_at"
        if has_edit_schema
        else "0 AS edited_flag, NULL AS edited_at"
    )

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT m.id, m.event_id, m.room_id, m.sender_actor_type, m.sender_actor_id, m.sender_display_name,
                   m.body, m.created_at, {image_columns}, {delete_columns}, {edit_columns},
                   m.reply_to_message_id, m.thread_root_id,
                   p.sender_actor_type AS reply_to_sender_actor_type,
                   p.sender_actor_id AS reply_to_sender_actor_id,
                   p.sender_display_name AS reply_to_sender_display_name,
                   p.body AS reply_to_body,
                   {'p.deleted_flag AS reply_to_deleted_flag, p.deleted_by_actor_type AS reply_to_deleted_by_actor_type' if has_delete_schema else '0 AS reply_to_deleted_flag, NULL AS reply_to_deleted_by_actor_type'}
              FROM chat_messages m
              LEFT JOIN chat_messages p ON p.id = m.reply_to_message_id AND p.event_id = m.event_id
             WHERE m.id=%s AND m.event_id=%s AND (m.room_id=%s OR (m.room_id IS NULL AND %s IS NOT NULL))
             LIMIT 1
            """,
            (root_message_id, event_id, effective_room_id, effective_room_id),
        )
        root_row = cur.fetchone()
        if not root_row:
            return jsonify({"ok": False, "error": "スレッドが見つかりません"}), 404

        if has_reply_schema and root_row.get("reply_to_message_id") and root_row.get("reply_to_sender_display_name"):
            if int(root_row.get("reply_to_deleted_flag") or 0) == 1:
                root_row["reply_to_body_plain_excerpt"] = "管理者により削除" if str(root_row.get("reply_to_deleted_by_actor_type") or "") == "admin" else "削除されました"
            else:
                root_row["reply_to_body_plain_excerpt"] = _build_plain_excerpt(root_row.get("reply_to_body") or "")
        elif has_reply_schema and root_row.get("reply_to_message_id"):
            root_row["reply_to_sender_display_name"] = "元メッセージ"
            root_row["reply_to_body_plain_excerpt"] = "元メッセージが見つかりません"
        else:
            root_row["reply_to_sender_display_name"] = None
            root_row["reply_to_body_plain_excerpt"] = None

        cur.execute(
            f"""
            SELECT m.id, m.event_id, m.room_id, m.sender_actor_type, m.sender_actor_id, m.sender_display_name,
                   m.body, m.created_at, {image_columns}, {delete_columns}, {edit_columns},
                   m.reply_to_message_id, m.thread_root_id,
                   p.sender_actor_type AS reply_to_sender_actor_type,
                   p.sender_actor_id AS reply_to_sender_actor_id,
                   p.sender_display_name AS reply_to_sender_display_name,
                   p.body AS reply_to_body,
                   {'p.deleted_flag AS reply_to_deleted_flag, p.deleted_by_actor_type AS reply_to_deleted_by_actor_type' if has_delete_schema else '0 AS reply_to_deleted_flag, NULL AS reply_to_deleted_by_actor_type'}
              FROM chat_messages m
              LEFT JOIN chat_messages p ON p.id = m.reply_to_message_id AND p.event_id = m.event_id
             WHERE m.event_id=%s
               AND (m.room_id=%s OR (m.room_id IS NULL AND %s IS NOT NULL))
               AND m.thread_root_id=%s
             ORDER BY m.id ASC
             LIMIT %s
            """,
            (event_id, effective_room_id, effective_room_id, root_message_id, limit),
        )
        replies = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    for row in replies:
        if has_reply_schema and row.get("reply_to_message_id") and row.get("reply_to_sender_display_name"):
            if int(row.get("reply_to_deleted_flag") or 0) == 1:
                row["reply_to_body_plain_excerpt"] = "管理者により削除" if str(row.get("reply_to_deleted_by_actor_type") or "") == "admin" else "削除されました"
            else:
                row["reply_to_body_plain_excerpt"] = _build_plain_excerpt(row.get("reply_to_body") or "")
        elif has_reply_schema and row.get("reply_to_message_id"):
            row["reply_to_sender_display_name"] = "元メッセージ"
            row["reply_to_body_plain_excerpt"] = "元メッセージが見つかりません"
        else:
            row["reply_to_sender_display_name"] = None
            row["reply_to_body_plain_excerpt"] = None

    avatar_cache: dict[str, str] = {}
    root_payload = _present_message(root_row, actor, avatar_cache=avatar_cache)
    replies_payload = [_present_message(r, actor, avatar_cache=avatar_cache) for r in replies]
    return jsonify({"ok": True, "root": root_payload, "replies": replies_payload})


@chat_bp.get("/api/events/<int:event_id>/search")
def search_messages(event_id: int):
    _ensure_chat_messages_room_schema()
    _ensure_chat_delete_schema()
    _ensure_chat_search_schema()
    actor = get_chat_actor()
    if not actor:
        return jsonify({"ok": False, "error": "ログインが必要です"}), 403
    if not _can_access_event(event_id, actor):
        return jsonify({"ok": False, "error": "このイベントにはアクセスできません"}), 403

    room_id = str(request.args.get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not allowed or not effective_room_id:
        return jsonify({"ok": False, "error": "このルームにはアクセスできません"}), 403

    q = str(request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "検索キーワードを入力してください"}), 400
    if len(q) > 100:
        return jsonify({"ok": False, "error": "検索キーワードは100文字以内です"}), 400

    raw_limit = request.args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 50
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 100))

    db = get_db()
    cur = db.cursor(dictionary=True)
    rows = []
    search_mode = "fulltext"
    try:
        try:
            cur.execute(
                """
                SELECT id AS message_id,
                       sender_display_name,
                       body,
                       created_at
                  FROM chat_messages
                 WHERE event_id=%s
                   AND room_id=%s
                   AND COALESCE(deleted_flag, 0)=0
                   AND MATCH(body) AGAINST (%s IN NATURAL LANGUAGE MODE)
                 ORDER BY id DESC
                 LIMIT %s
                """,
                (event_id, effective_room_id, q, limit),
            )
            rows = cur.fetchall() or []
        except Exception:
            search_mode = "like_fallback"
            current_app.logger.warning("chat search fulltext failed, fallback to LIKE", exc_info=True)
            cur.execute(
                """
                SELECT id AS message_id,
                       sender_display_name,
                       body,
                       created_at
                  FROM chat_messages
                 WHERE event_id=%s
                   AND room_id=%s
                   AND COALESCE(deleted_flag, 0)=0
                   AND body LIKE %s
                 ORDER BY id DESC
                 LIMIT %s
                """,
                (event_id, effective_room_id, f"%{q}%", limit),
            )
            rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    results: list[dict[str, Any]] = []
    for row in rows:
        _created_at_iso, _date_label, time_label = _format_jst_labels(row.get("created_at") or datetime.utcnow())
        results.append(
            {
                "message_id": int(row.get("message_id") or 0),
                "sender": row.get("sender_display_name") or "Unknown",
                "time": time_label,
                "excerpt": _build_plain_excerpt(str(row.get("body") or ""), max_len=80),
            }
        )

    _audit_log("search", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, result="ok", mode=search_mode, count=len(results))
    return jsonify({"ok": True, "results": results})


def _parse_actor_key(value: str) -> tuple[str, str] | None:
    raw = (value or "").strip()
    if ":" not in raw:
        return None
    actor_type, actor_id = raw.split(":", 1)
    actor_type = actor_type.strip()
    actor_id = actor_id.strip()
    if not actor_type or not actor_id:
        return None
    return actor_type, actor_id


@chat_bp.get("/api/events/<int:event_id>/rooms")
def api_rooms(event_id: int):
    actor = get_chat_actor()
    if not actor or not _can_access_event(event_id, actor):
        return jsonify({"ok": False}), 403
    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    _ensure_chat_messages_room_schema()
    rooms = _list_accessible_rooms(event_id, actor)
    return jsonify({"ok": True, "rooms": rooms})


@chat_bp.post("/api/events/<int:event_id>/rooms/create")
def api_rooms_create(event_id: int):
    actor = get_chat_actor()
    if not actor or not _can_manage_rooms(event_id, actor):
        return jsonify({"ok": False}), 403
    payload = request.get_json(silent=True) or {}
    if (payload.get("csrf_token") or "").strip() != session.get("chat_csrf"):
        return jsonify({"ok": False, "error": "csrf"}), 400
    room_name = str(payload.get("room_name") or "").strip()
    if not (1 <= len(room_name) <= 80):
        return jsonify({"ok": False, "error": "room_nameは1〜80文字です"}), 400
    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    room_id = str(uuid.uuid4())
    now = datetime.utcnow()
    actor_keys = set(payload.get("member_actor_keys") or [])
    actor_keys.add(f"{actor['actor_type']}:{actor['actor_id']}")
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO chat_rooms (
              room_id,event_id,room_name,is_main,is_archived,created_at,created_by_actor_type,created_by_actor_id,updated_at,updated_by_actor_type,updated_by_actor_id
            ) VALUES (%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s)
            """,
            (room_id, event_id, room_name, now, actor["actor_type"], actor["actor_id"], now, actor["actor_type"], actor["actor_id"]),
        )
        for key in actor_keys:
            parsed = _parse_actor_key(str(key))
            if not parsed:
                continue
            at, aid = parsed
            cur.execute(
                """
                INSERT IGNORE INTO chat_room_members (room_id,actor_type,actor_id,added_at,added_by_actor_type,added_by_actor_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (room_id, at, aid, now, actor["actor_type"], actor["actor_id"]),
            )
        db.commit()
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True, "room_id": room_id})


@chat_bp.post("/api/events/<int:event_id>/rooms/<room_id>/update")
def api_rooms_update(event_id: int, room_id: str):
    actor = get_chat_actor()
    if not actor or not _can_manage_rooms(event_id, actor):
        return jsonify({"ok": False}), 403
    payload = request.get_json(silent=True) or {}
    if (payload.get("csrf_token") or "").strip() != session.get("chat_csrf"):
        return jsonify({"ok": False, "error": "csrf"}), 400
    room = _get_room(event_id, room_id, allow_archived=True)
    if not room or int(room.get("is_main") or 0) == 1:
        return jsonify({"ok": False}), 404
    updates = []
    params: list[Any] = []
    if "room_name" in payload:
        name = str(payload.get("room_name") or "").strip()
        if not (1 <= len(name) <= 80):
            return jsonify({"ok": False, "error": "room_nameは1〜80文字です"}), 400
        updates.append("room_name=%s")
        params.append(name)
    if "is_archived" in payload:
        updates.append("is_archived=%s")
        params.append(1 if payload.get("is_archived") else 0)
    if not updates:
        return jsonify({"ok": True})
    updates.extend(["updated_at=%s", "updated_by_actor_type=%s", "updated_by_actor_id=%s"])
    params.extend([datetime.utcnow(), actor["actor_type"], actor["actor_id"], event_id, room_id])
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(f"UPDATE chat_rooms SET {', '.join(updates)} WHERE event_id=%s AND room_id=%s", tuple(params))
        db.commit()
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True})


@chat_bp.get("/api/events/<int:event_id>/rooms/<room_id>/members")
def api_room_members(event_id: int, room_id: str):
    actor = get_chat_actor()
    if not actor or not _can_manage_rooms(event_id, actor):
        return jsonify({"ok": False}), 403
    room = _get_room(event_id, room_id, allow_archived=True)
    if not room:
        return jsonify({"ok": False}), 404
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT m.actor_type, m.actor_id,
                   COALESCE(u.nickname, m.actor_id) AS display_name
              FROM chat_room_members m
              LEFT JOIN external_login_user u
                     ON m.actor_type='line' AND u.id = CAST(m.actor_id AS UNSIGNED)
             WHERE m.room_id=%s
            """,
            (room_id,),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    members = [
        {
            "actor_key": f"{str(r.get('actor_type') or '')}:{str(r.get('actor_id') or '')}",
            "display_name": str(r.get("display_name") or r.get("actor_id") or "")
        }
        for r in rows
        if str(r.get("actor_type") or "") and str(r.get("actor_id") or "")
    ]
    return jsonify({"ok": True, "members": members})


@chat_bp.post("/api/events/<int:event_id>/rooms/<room_id>/members/set")
def api_rooms_members_set(event_id: int, room_id: str):
    actor = get_chat_actor()
    if not actor or not _can_manage_rooms(event_id, actor):
        return jsonify({"ok": False}), 403
    payload = request.get_json(silent=True) or {}
    if (payload.get("csrf_token") or "").strip() != session.get("chat_csrf"):
        return jsonify({"ok": False, "error": "csrf"}), 400
    room = _get_room(event_id, room_id, allow_archived=True)
    if not room or int(room.get("is_main") or 0) == 1:
        return jsonify({"ok": False}), 404
    target_members: set[tuple[str, str]] = set()
    for raw in payload.get("member_actor_keys") or []:
        parsed = _parse_actor_key(str(raw))
        if parsed:
            target_members.add(parsed)
    target_members.add((str(actor["actor_type"]), str(actor["actor_id"])))
    if not target_members:
        return jsonify({"ok": False, "error": "最低1名必要です"}), 400
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT actor_type, actor_id FROM chat_room_members WHERE room_id=%s", (room_id,))
        existing = {(str(r["actor_type"]), str(r["actor_id"])) for r in (cur.fetchall() or [])}
        to_remove = existing - target_members
        to_add = target_members - existing
        for at, aid in to_remove:
            cur.execute("DELETE FROM chat_room_members WHERE room_id=%s AND actor_type=%s AND actor_id=%s", (room_id, at, aid))
        now = datetime.utcnow()
        for at, aid in to_add:
            cur.execute(
                "INSERT IGNORE INTO chat_room_members (room_id,actor_type,actor_id,added_at,added_by_actor_type,added_by_actor_id) VALUES (%s,%s,%s,%s,%s,%s)",
                (room_id, at, aid, now, actor["actor_type"], actor["actor_id"]),
            )
        db.commit()
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True})


@chat_bp.get("/api/events/<int:event_id>/room-member-candidates")
def api_room_member_candidates(event_id: int):
    actor = get_chat_actor()
    if not actor or not _can_manage_rooms(event_id, actor):
        return jsonify({"ok": False}), 403
    db = get_db()
    cur = db.cursor(dictionary=True)
    candidates: dict[str, dict[str, str]] = {"admin:admin": {"actor_key": "admin:admin", "display_name": "admin", "role_hint": "admin"}}
    try:
        cur.execute(
            """
            SELECT u.id, u.nickname
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id=m.user_id
             WHERE m.event_id=%s
            """,
            (event_id,),
        )
        for row in cur.fetchall() or []:
            key = f"line:{row['id']}"
            candidates[key] = {"actor_key": key, "display_name": str(row.get("nickname") or f"LINE-{row['id']}") , "role_hint": "参加者"}
        cur.execute("SELECT username FROM mfu_event_admin_acl WHERE event_id=%s", (event_id,))
        for row in cur.fetchall() or []:
            username = str(row.get("username") or "")
            if not username:
                continue
            if username == "admin":
                continue
            key = f"acl:{username}"
            candidates[key] = {"actor_key": key, "display_name": username, "role_hint": "ACL"}
    finally:
        cur.close()
        db.close()
    return jsonify({"ok": True, "candidates": sorted(candidates.values(), key=lambda x: x["display_name"])})


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
                    "url": f"/chat/events/{event_id}?{urlencode({'event_id': event_id, 'room_id': effective_room_id})}",
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
                room_id=None,
                room_name=None,
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
    _audit_log("room_join", actor=_actor_log_id(actor), result="connect")
    if not actor:
        current_app.logger.info("chat socket connect denied: no actor")
        return False
    return True

@socketio.on("chat_join")
def on_join(data):
    _ensure_chat_rooms_schema()
    _ensure_chat_room_members_schema()
    _ensure_chat_messages_room_schema()
    _ensure_chat_read_state_room_schema()
    _ensure_chat_edit_schema()
    actor = get_chat_actor()
    event_id = int((data or {}).get("event_id") or 0)
    room_id = str((data or {}).get("room_id") or "").strip() or None
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor or {}) if actor else (False, None, None)
    if not actor or not event_id or not allowed or not effective_room_id:
        _audit_log("room_join", actor=_actor_log_id(actor), event_id=event_id, room_id=room_id, result="deny")
        disconnect()
        return
    join_room(f"event:{event_id}:room:{effective_room_id}")
    emit("chat_joined", {"event_id": event_id, "room_id": effective_room_id})
    _audit_log("room_join", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, result="ok")
    emit("chat_read_snapshot", {"event_id": event_id, "room_id": effective_room_id, "read_states": _load_event_read_state_snapshot(event_id, effective_room_id)})


@socketio.on("chat_seen")
def on_seen(data):
    actor = get_chat_actor()
    if not actor:
        disconnect()
        return

    event_id = int((data or {}).get("event_id") or 0)
    room_id = str((data or {}).get("room_id") or "").strip() or None
    last_seen_message_id = int((data or {}).get("last_seen_message_id") or 0)
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if event_id <= 0 or last_seen_message_id <= 0 or not allowed or not effective_room_id:
        disconnect()
        return

    effective_last_read_id = _upsert_chat_read_state(event_id, effective_room_id, actor, last_seen_message_id)
    _audit_log("chat_seen", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=effective_last_read_id, result="ok")
    emit(
        "chat_read_update",
        {
            "actor_type": actor["actor_type"],
            "actor_id": actor["actor_id"],
            "display_name": actor["display_name"],
            "last_read_message_id": effective_last_read_id,
            "room_id": effective_room_id,
        },
        to=f"event:{event_id}:room:{effective_room_id}",
    )


@socketio.on("chat_typing")
def on_typing(data):
    actor = get_chat_actor()
    if not actor:
        disconnect()
        return

    try:
        event_id = int((data or {}).get("event_id") or 0)
        room_id = str((data or {}).get("room_id") or "").strip() or None
    except (TypeError, ValueError):
        disconnect()
        return
    is_typing = bool((data or {}).get("is_typing"))

    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if event_id <= 0 or not allowed or not effective_room_id:
        disconnect()
        return

    _cleanup_stale_typing_states(event_id, effective_room_id)

    key = _typing_state_key(event_id, effective_room_id, actor["actor_type"], str(actor["actor_id"]))
    if is_typing:
        with CHAT_TYPING_STATE_LOCK:
            CHAT_TYPING_STATE[key] = {
                "event_id": event_id,
                "room_id": effective_room_id,
                "actor": {
                    "actor_type": actor["actor_type"],
                    "actor_id": str(actor["actor_id"]),
                    "display_name": actor.get("display_name") or str(actor["actor_id"]),
                },
                "last_ts": time.monotonic(),
                "is_typing": True,
            }
        _emit_typing_state(event_id, effective_room_id, actor, True)
    else:
        with CHAT_TYPING_STATE_LOCK:
            CHAT_TYPING_STATE.pop(key, None)
        _emit_typing_state(event_id, effective_room_id, actor, False)


@socketio.on("chat_send")
def on_send(data):
    actor = get_chat_actor()
    event_id = int((data or {}).get("event_id") or 0)
    room_id = str((data or {}).get("room_id") or "").strip() or None
    raw_body = (data or {}).get("body") or ""
    raw_reply_to_message_id = (data or {}).get("reply_to_message_id")

    t0 = time.monotonic()
    _audit_log("chat_send", actor=_actor_log_id(actor), event_id=event_id, room_id=room_id, result="recv", body_len=len(raw_body))

    if not actor:
        disconnect()
        return
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if not event_id or not allowed or not effective_room_id:
        disconnect()
        return
    if not _ensure_chat_delete_schema():
        emit("chat_error", {"error": "メッセージ削除機能の初期化に失敗しました"})
        return
    if not _ensure_chat_edit_schema():
        emit("chat_error", {"error": "メッセージ編集機能の初期化に失敗しました"})
        return
    if not _ensure_chat_thread_schema():
        emit("chat_error", {"error": "スレッド機能の初期化に失敗しました"})
        return
    if not _check_rate_limit(actor, route="chat_send", event_id=event_id, room_id=effective_room_id):
        emit("chat_error", {"error": RATE_LIMIT_ERROR_MESSAGE})
        return

    try:
        body = _validate_body(raw_body)
        reply_to_message_id = _validate_reply_to_message_id(event_id, effective_room_id, raw_reply_to_message_id)
        thread_root_id = _resolve_thread_root_id(event_id, effective_room_id, reply_to_message_id)
    except ValueError as exc:
        emit("chat_error", {"error": str(exc)})
        return

    message = _enrich_reply_fields(_save_message(event_id, effective_room_id, actor, body, reply_to_message_id, thread_root_id))

    typing_key = _typing_state_key(event_id, effective_room_id, actor["actor_type"], str(actor["actor_id"]))
    with CHAT_TYPING_STATE_LOCK:
        CHAT_TYPING_STATE.pop(typing_key, None)
    _emit_typing_state(event_id, effective_room_id, actor, False)

    t1 = time.monotonic()
    active_room = _get_room(event_id, effective_room_id, allow_archived=True)
    active_room_name = str(active_room.get("room_name") or "") if active_room else None
    message_payload = _present_message(message, actor, avatar_cache={})
    emit("chat_message", message_payload, to=f"event:{event_id}:room:{effective_room_id}")
    _audit_log("chat_send", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_payload.get("id"), result="ok")

    mention_names = _extract_mentions(body)
    mention_targets = _lookup_mention_targets(event_id, effective_room_id, mention_names)
    sent_count = 0
    for target in mention_targets:
        sent_count += _send_push_to_actor(
            target["actor_type"],
            target["actor_id"],
            {
                "title": f"{actor['display_name']}さんからメンション",
                "body": f"[スレッド] {body}" if thread_root_id else body,
                "event_id": event_id,
                "room_id": effective_room_id,
                "url": f"/chat/events/{event_id}?{urlencode({'event_id': event_id, 'room_id': effective_room_id})}",
            },
        )
        if target.get("actor_type") == "line":
            _create_external_chat_notification(
                recipient_user_id=int(target["actor_id"]),
                kind="chat_mention",
                title=f"{actor['display_name']}さんからメンション",
                body=f"[スレッド] {body}" if thread_root_id else body,
                event_id=event_id,
                room_id=effective_room_id,
                room_name=active_room_name,
                dedup_key=f"chat:mention:{event_id}:{effective_room_id}:{message_payload['id']}:{target['actor_id']}",
            )
    if mention_targets:
        _log_notification(event_id, "mention", {"names": mention_names, "message_id": message_payload["id"]}, sent_count)

    t2 = time.monotonic()
    app = current_app._get_current_object()
    _submit_chat_message_push_async(
        app,
        event_id,
        effective_room_id,
        actor,
        actor["display_name"],
        f"[スレッド] {body}" if thread_root_id else body,
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
        room_id = str((data or {}).get("room_id") or "").strip() or None
        message_id = int((data or {}).get("message_id") or 0)
    except (TypeError, ValueError):
        disconnect()
        return

    emoji = str((data or {}).get("emoji") or "")
    allowed, effective_room_id, _room = _can_access_room(event_id, room_id, actor)
    if event_id <= 0 or message_id <= 0 or not allowed or not effective_room_id:
        disconnect()
        return
    if emoji not in CHAT_ALLOWED_REACTION_EMOJIS:
        emit("chat_error", {"error": "利用できないリアクションです"})
        return

    db = get_db()
    cur = db.cursor(dictionary=True)
    changed_emoji: str | None = emoji
    try:
        cur.execute("SELECT COALESCE(deleted_flag, 0) AS deleted_flag FROM chat_messages WHERE id=%s AND event_id=%s AND (room_id=%s OR (room_id IS NULL AND %s IS NOT NULL)) LIMIT 1", (message_id, event_id, effective_room_id, effective_room_id))
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

    _audit_log("chat_react", actor=_actor_log_id(actor), event_id=event_id, room_id=effective_room_id, message_id=message_id, result="ok", emoji=changed_emoji or "removed")
    emit(
        "chat_reaction_update",
        {
            "event_id": event_id,
            "room_id": effective_room_id,
            "message_id": message_id,
            "reactions": reactions,
            "changed": {
                "actor_type": actor["actor_type"],
                "actor_id": str(actor["actor_id"]),
                "emoji": changed_emoji,
            },
        },
        to=f"event:{event_id}:room:{effective_room_id}",
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
            room_id=None,
            room_name=None,
            dedup_key=f"chat:dm:{event_id}:{dedup_suffix}:{target_actor_id}",
        )
    _log_notification(event_id, "dm", {"target_actor_type": target_actor_type, "target_actor_id": target_actor_id}, sent_count)
    emit("chat_dm_notified", {"ok": True, "sent_count": sent_count})
