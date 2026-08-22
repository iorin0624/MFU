from __future__ import annotations

import threading
from datetime import datetime
from typing import Iterable

from app.utils.db import get_db


FILE_KIND_IMAGE = "image"
_SCHEMA_LOCK = threading.Lock()
_schema_ready = False


def ensure_layer_reply_schema(*, db_factory=get_db) -> None:
    global _schema_ready
    db = db_factory()
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS layer_upload_replies (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                upload_id INT NOT NULL,
                reply_uuid CHAR(32) NOT NULL,
                title_snapshot TEXT NOT NULL,
                comment TEXT NULL,
                posted_at DATETIME(6) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uniq_layer_reply_uuid (reply_uuid),
                KEY idx_layer_reply_upload_posted (upload_id, posted_at, id),
                CONSTRAINT fk_layer_reply_upload
                    FOREIGN KEY (upload_id) REFERENCES uploads (id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS layer_upload_reply_files (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                reply_id BIGINT UNSIGNED NOT NULL,
                file_kind ENUM('image', 'zip') NOT NULL,
                filename VARCHAR(512) NOT NULL,
                sort_order INT UNSIGNED NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uniq_layer_reply_file (reply_id, file_kind, filename),
                KEY idx_layer_reply_file_order (reply_id, file_kind, sort_order, id),
                CONSTRAINT fk_layer_reply_file_reply
                    FOREIGN KEY (reply_id) REFERENCES layer_upload_replies (id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
        if db_factory is get_db:
            _schema_ready = True
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def _ensure_schema_once(db_factory=get_db) -> None:
    if db_factory is not get_db:
        ensure_layer_reply_schema(db_factory=db_factory)
        return
    if _schema_ready:
        return
    with _SCHEMA_LOCK:
        if _schema_ready:
            return
        ensure_layer_reply_schema()


def _clean_filenames(filenames: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for filename in filenames:
        value = str(filename or "").strip()
        if not value or value in seen:
            continue
        cleaned.append(value)
        seen.add(value)
    return cleaned


def create_layer_reply(
    *,
    upload_id: int,
    reply_uuid: str,
    title_snapshot: str,
    comment: str,
    posted_at: datetime,
    image_filenames: Iterable[str],
    db_factory=get_db,
) -> int:
    _ensure_schema_once(db_factory)
    images = _clean_filenames(image_filenames)
    db = db_factory()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO layer_upload_replies (
                upload_id, reply_uuid, title_snapshot, comment, posted_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                int(upload_id),
                str(reply_uuid).strip(),
                str(title_snapshot or "").strip(),
                str(comment or ""),
                posted_at,
            ),
        )
        reply_id = int(cur.lastrowid)
        file_rows = [
            (reply_id, FILE_KIND_IMAGE, filename, index)
            for index, filename in enumerate(images, start=1)
        ]
        if file_rows:
            cur.executemany(
                """
                INSERT INTO layer_upload_reply_files (
                    reply_id, file_kind, filename, sort_order
                ) VALUES (%s, %s, %s, %s)
                """,
                file_rows,
            )
        db.commit()
        return reply_id
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def layer_reply_exists(reply_uuid: str, *, db_factory=get_db) -> bool:
    _ensure_schema_once(db_factory)
    db = db_factory()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM layer_upload_replies WHERE reply_uuid=%s LIMIT 1",
            (str(reply_uuid).strip(),),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()
        db.close()


def get_layer_reply(reply_uuid: str, *, db_factory=get_db) -> dict | None:
    _ensure_schema_once(db_factory)
    db = db_factory()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT reply.*, upload.uuid AS upload_uuid
              FROM layer_upload_replies AS reply
              JOIN uploads AS upload ON upload.id=reply.upload_id
             WHERE reply.reply_uuid=%s
             LIMIT 1
            """,
            (str(reply_uuid).strip(),),
        )
        reply = cur.fetchone()
        if not reply:
            return None
        cur.execute(
            """
            SELECT file_kind, filename, sort_order
              FROM layer_upload_reply_files
             WHERE reply_id=%s AND file_kind='image'
             ORDER BY sort_order, id
            """,
            (reply["id"],),
        )
        files = cur.fetchall()
        reply["images"] = [row["filename"] for row in files]
        reply["filenames"] = list(reply["images"])
        reply["title"] = reply.get("title_snapshot") or ""
        reply["created"] = reply.get("posted_at")
        return reply
    finally:
        cur.close()
        db.close()


def list_layer_reply_groups(upload_id: int, *, db_factory=get_db) -> list[dict]:
    _ensure_schema_once(db_factory)
    db = db_factory()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, reply_uuid, title_snapshot, comment, posted_at
              FROM layer_upload_replies
             WHERE upload_id=%s
             ORDER BY posted_at DESC, id DESC
            """,
            (int(upload_id),),
        )
        replies = cur.fetchall()
        if not replies:
            return []
        reply_ids = [int(reply["id"]) for reply in replies]
        placeholders = ",".join(["%s"] * len(reply_ids))
        cur.execute(
            f"""
            SELECT reply_id, file_kind, filename, sort_order
              FROM layer_upload_reply_files
             WHERE reply_id IN ({placeholders}) AND file_kind='image'
             ORDER BY reply_id, sort_order, id
            """,
            reply_ids,
        )
        files_by_reply: dict[int, list[str]] = {reply_id: [] for reply_id in reply_ids}
        for row in cur.fetchall():
            files_by_reply[int(row["reply_id"])].append(row["filename"])

        groups = []
        for reply in replies:
            images = files_by_reply[int(reply["id"])]
            groups.append(
                {
                    "reply_id": int(reply["id"]),
                    "folder_name": reply["reply_uuid"],
                    "reply_uuid": reply["reply_uuid"],
                    "title_snapshot": reply.get("title_snapshot") or "",
                    "comment": reply.get("comment") or "",
                    "posted_at": reply.get("posted_at"),
                    "updated_at": reply.get("posted_at"),
                    "images": list(images),
                }
            )
        return groups
    finally:
        cur.close()
        db.close()


def get_layer_reply_summary(upload_id: int, *, db_factory=get_db) -> dict:
    _ensure_schema_once(db_factory)
    db = db_factory()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS folder_count, MAX(posted_at) AS latest_mtime
              FROM layer_upload_replies
             WHERE upload_id=%s
            """,
            (int(upload_id),),
        )
        summary = cur.fetchone() or {}
        folder_count = int(summary.get("folder_count") or 0)
        return {
            "has_layer_upload": folder_count > 0,
            "folder_count": folder_count,
            "latest_mtime": summary.get("latest_mtime"),
        }
    finally:
        cur.close()
        db.close()


def layer_reply_file_exists(
    *,
    upload_id: int,
    reply_uuid: str,
    filename: str,
    db_factory=get_db,
) -> bool:
    _ensure_schema_once(db_factory)
    db = db_factory()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT 1
              FROM layer_upload_reply_files AS file
              JOIN layer_upload_replies AS reply ON reply.id=file.reply_id
             WHERE reply.upload_id=%s
               AND reply.reply_uuid=%s
               AND file.file_kind='image'
               AND file.filename=%s
             LIMIT 1
            """,
            (
                int(upload_id),
                str(reply_uuid).strip(),
                str(filename).strip(),
            ),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()
        db.close()


def delete_layer_replies(upload_id: int, *, db=None, cursor=None, db_factory=get_db) -> int:
    owns_connection = db is None or cursor is None
    if owns_connection:
        _ensure_schema_once(db_factory)
    connection = db or db_factory()
    cur = cursor or connection.cursor()
    try:
        cur.execute("DELETE FROM layer_upload_replies WHERE upload_id=%s", (int(upload_id),))
        deleted = max(0, int(cur.rowcount or 0))
        if owns_connection:
            connection.commit()
        return deleted
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            cur.close()
            connection.close()
