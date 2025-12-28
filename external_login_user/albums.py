# -*- coding: utf-8 -*-
from __future__ import annotations
import os, uuid, secrets
from . import bp
from app.utils.db import get_db

# MySQL エラーコード（任意）
try:
    from mysql.connector import errors as mysql_errors
except Exception:
    mysql_errors = None  # type: ignore

def _ensure_album_schema():
    """albums に event_id / access_mode を追加（競合安全）"""
    db = get_db(); cur = db.cursor()
    def _has(col):
        cur.execute("""SELECT COUNT(*) FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='albums' AND COLUMN_NAME=%s""", (col,))
        return bool(cur.fetchone()[0])

    try:
        if not _has('event_id'):
            try:
                cur.execute("ALTER TABLE albums ADD COLUMN event_id BIGINT UNSIGNED NULL AFTER owner")
            except Exception as e:
                if not (mysql_errors and getattr(e, "errno", None) == 1060):
                    raise
            try:
                cur.execute("CREATE INDEX idx_albums_event_id ON albums(event_id)")
            except Exception as e:
                if not (mysql_errors and getattr(e, "errno", None) == 1061):
                    pass
        if not _has('access_mode'):
            try:
                cur.execute("ALTER TABLE albums ADD COLUMN access_mode ENUM('token','event') NOT NULL DEFAULT 'token' AFTER event_id")
            except Exception as e:
                if not (mysql_errors and getattr(e, "errno", None) == 1060):
                    raise
        db.commit()
    finally:
        cur.close(); db.close()

def create_event_album(*, title: str, event_id: int) -> str:
    """イベント専用アルバムを作成し、'event'モードで保護"""
    _ensure_album_schema()
    album_id = str(uuid.uuid4())
    access_token = secrets.token_bytes(32).hex()
    db = get_db(); cur = db.cursor()
    cur.execute("""INSERT INTO albums (id, album_name, owner, access_token, event_id, access_mode)
                   VALUES (%s,%s,%s,%s,%s,'event')""",
                (album_id, f"[イベント] {title}", 'system', access_token, event_id))
    db.commit(); cur.close(); db.close()
    return album_id
