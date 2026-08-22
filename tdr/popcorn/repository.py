from __future__ import annotations

import json
from threading import Lock
from typing import Any

from app.utils.db import get_db


LOCK_NAME = "mfu_tdr_popcorn_refresh"
_SCHEMA_LOCK = Lock()
_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tdr_fetch_runs (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    source VARCHAR(64) NOT NULL,
                    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'running',
                    source_status_json LONGTEXT NULL,
                    counts_json LONGTEXT NULL,
                    content_hash CHAR(64) NULL,
                    error_text TEXT NULL,
                    INDEX idx_tdr_fetch_runs_source_started (source, started_at),
                    INDEX idx_tdr_fetch_runs_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tdr_popcorn_snapshots (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    fetch_run_id BIGINT NOT NULL,
                    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    content_hash CHAR(64) NOT NULL,
                    data_json LONGTEXT NOT NULL,
                    is_active TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_tdr_popcorn_content_hash (content_hash),
                    INDEX idx_tdr_popcorn_active (is_active, id),
                    INDEX idx_tdr_popcorn_run (fetch_run_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
            _SCHEMA_READY = True
        finally:
            db.close()


def ensure_nav_item() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT id FROM mfu_nav_items WHERE parent_id IS NULL AND url=%s LIMIT 1",
            ("/tdr/popcorn/",),
        )
        parent = cur.fetchone()
        if parent:
            parent_id = int(parent["id"])
        else:
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (NULL, %s, %s, %s, 1, NULL, 0, 0)
                """,
                ("🎢 TDR情報", "/tdr/popcorn/", 850),
            )
            parent_id = int(cur.lastrowid)

        cur.execute(
            "SELECT id FROM mfu_nav_items WHERE parent_id=%s AND url=%s LIMIT 1",
            (parent_id, "/tdr/popcorn/"),
        )
        child = cur.fetchone()
        if not child:
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (%s, %s, %s, %s, 1, NULL, 0, 0)
                """,
                (parent_id, "🍿 ポップコーン", "/tdr/popcorn/", 10),
            )
        db.commit()
    finally:
        db.close()


def acquire_refresh_lock():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT GET_LOCK(%s, 0)", (LOCK_NAME,))
    row = cur.fetchone()
    if not row or int(row[0] or 0) != 1:
        db.close()
        return None
    return db


def release_refresh_lock(db) -> None:
    if db is None:
        return
    try:
        cur = db.cursor()
        cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    finally:
        db.close()


def begin_run() -> int:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT INTO tdr_fetch_runs (source, status) VALUES (%s, 'running')", ("popcorn",))
        run_id = int(cur.lastrowid)
        db.commit()
        return run_id
    finally:
        db.close()


def mark_run_failed(run_id: int, error_text: str) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE tdr_fetch_runs
               SET status='failed', finished_at=NOW(), error_text=%s
             WHERE id=%s
            """,
            ((error_text or "")[:4000], run_id),
        )
        db.commit()
    finally:
        db.close()


def publish_snapshot(run_id: int, dataset: dict, metadata: dict) -> tuple[int, bool]:
    data_json = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    source_status_json = json.dumps(metadata.get("source_status") or [], ensure_ascii=False)
    counts_json = json.dumps(metadata.get("counts") or {}, ensure_ascii=False)
    content_hash = str(metadata["content_hash"])

    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        db.start_transaction()
        cur.execute(
            "SELECT id FROM tdr_popcorn_snapshots WHERE content_hash=%s LIMIT 1 FOR UPDATE",
            (content_hash,),
        )
        existing = cur.fetchone()
        changed = existing is None
        if existing:
            snapshot_id = int(existing["id"])
        else:
            cur.execute(
                """
                INSERT INTO tdr_popcorn_snapshots
                    (fetch_run_id, fetched_at, content_hash, data_json, is_active)
                VALUES (%s, NOW(), %s, %s, 0)
                """,
                (run_id, content_hash, data_json),
            )
            snapshot_id = int(cur.lastrowid)

        cur.execute("UPDATE tdr_popcorn_snapshots SET is_active=0 WHERE is_active=1 AND id<>%s", (snapshot_id,))
        cur.execute(
            """
            UPDATE tdr_popcorn_snapshots
               SET is_active=1, fetch_run_id=%s, fetched_at=NOW(), data_json=%s
             WHERE id=%s
            """,
            (run_id, data_json, snapshot_id),
        )
        cur.execute(
            """
            UPDATE tdr_fetch_runs
               SET status='success', finished_at=NOW(), source_status_json=%s,
                   counts_json=%s, content_hash=%s, error_text=NULL
             WHERE id=%s
            """,
            (source_status_json, counts_json, content_hash, run_id),
        )
        db.commit()
        return snapshot_id, changed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _decode_json(value: object, default: Any):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def get_active_snapshot() -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, fetch_run_id, fetched_at, content_hash, data_json
              FROM tdr_popcorn_snapshots
             WHERE is_active=1
             ORDER BY id DESC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "fetch_run_id": int(row["fetch_run_id"]),
            "fetched_at": row["fetched_at"],
            "content_hash": row["content_hash"],
            "data": _decode_json(row["data_json"], {}),
        }
    finally:
        db.close()


def get_recent_runs(limit: int = 20) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, source, started_at, finished_at, status,
                   source_status_json, counts_json, content_hash, error_text
              FROM tdr_fetch_runs
             WHERE source='popcorn'
             ORDER BY id DESC
             LIMIT %s
            """,
            (max(1, min(int(limit), 100)),),
        )
        rows = cur.fetchall() or []
        for row in rows:
            row["source_status"] = _decode_json(row.pop("source_status_json", None), [])
            row["counts"] = _decode_json(row.pop("counts_json", None), {})
        return rows
    finally:
        db.close()
