"""Durable, user-scoped GPU classification jobs and reviewable suggestions."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .style_tags import normalize_assignment

DEFAULT_DB_PATH = Path(os.environ.get(
    "IMAGE_VIEWER_STYLE_JOB_DB", "/mnt/mfu/data/image_viewer_style_jobs.sqlite3",
))
LEASE_SECONDS = 600


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS image_style_jobs ("
        "id TEXT PRIMARY KEY, owner TEXT NOT NULL, path TEXT NOT NULL, "
        "sha256 TEXT NOT NULL, status TEXT NOT NULL, worker_id INTEGER, "
        "lease_until INTEGER, suggestion_json TEXT, error TEXT, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS image_style_jobs_owner_status "
        "ON image_style_jobs(owner, status, created_at)"
    )
    return conn


def _public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "path": row["path"], "status": row["status"],
        "suggestion": json.loads(row["suggestion_json"]) if row["suggestion_json"] else None,
        "error": row["error"], "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def create(owner: str, path: str, sha256: str, db_path: Path = DEFAULT_DB_PATH) -> dict:
    now = int(time.time())
    job_id = str(uuid.uuid4())
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO image_style_jobs(id,owner,path,sha256,status,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',?,?)",
                (job_id, owner, path, sha256, now, now),
            )
        row = conn.execute("SELECT * FROM image_style_jobs WHERE id=?", (job_id,)).fetchone()
        return _public(row)


def get(job_id: str, owner: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM image_style_jobs WHERE id=? AND owner=?", (job_id, owner),
        ).fetchone()
        return _public(row) if row else None


def latest(path: str, owner: str, sha256: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    if not db_path.is_file():
        return None
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM image_style_jobs WHERE path=? AND owner=? AND sha256=? "
            "ORDER BY created_at DESC LIMIT 1", (path, owner, sha256),
        ).fetchone()
        return _public(row) if row else None


def claim(owner: str, worker_id: int, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    now = int(time.time())
    with closing(_connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM image_style_jobs WHERE owner=? AND (status='pending' "
                "OR (status='running' AND lease_until<?)) "
                "ORDER BY created_at ASC LIMIT 1", (owner, now),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE image_style_jobs SET status='running', worker_id=?, lease_until=?, "
                    "error=NULL, updated_at=? WHERE id=?",
                    (worker_id, now + LEASE_SECONDS, now, row["id"]),
                )
                row = conn.execute("SELECT * FROM image_style_jobs WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return _public(row) if row else None
        except Exception:
            conn.rollback()
            raise


def claimed_row(job_id: str, owner: str, worker_id: int,
                db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM image_style_jobs WHERE id=? AND owner=? AND worker_id=? "
            "AND status='running' AND lease_until>=?",
            (job_id, owner, worker_id, int(time.time())),
        ).fetchone()
        return dict(row) if row else None


def complete(job_id: str, owner: str, worker_id: int, suggestion: object,
             db_path: Path = DEFAULT_DB_PATH) -> bool:
    if not isinstance(suggestion, dict):
        raise ValueError("分類結果の形式が正しくありません。")
    assignment = normalize_assignment(suggestion.get("assignment"))
    scores = suggestion.get("scores")
    if not isinstance(scores, dict) or set(scores) != {"lolita", "jirai", "casual"}:
        raise ValueError("分類スコアが正しくありません。")
    scores = {key: round(float(value), 5) for key, value in scores.items()}
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in scores.values()):
        raise ValueError("分類スコアが正しくありません。")
    value = {"assignment": assignment, "scores": scores,
             "model": str(suggestion.get("model") or "")[:100]}
    now = int(time.time())
    with closing(_connect(db_path)) as conn:
        with conn:
            result = conn.execute(
                "UPDATE image_style_jobs SET status='done', suggestion_json=?, "
                "updated_at=?, lease_until=NULL WHERE id=? AND owner=? AND worker_id=? "
                "AND status='running' AND lease_until>=?",
                (json.dumps(value, ensure_ascii=False), now, job_id, owner, worker_id, now),
            )
        return result.rowcount == 1


def fail(job_id: str, owner: str, worker_id: int, error: str,
         db_path: Path = DEFAULT_DB_PATH) -> bool:
    now = int(time.time())
    with closing(_connect(db_path)) as conn:
        with conn:
            result = conn.execute(
                "UPDATE image_style_jobs SET status='error', error=?, updated_at=?, "
                "lease_until=NULL WHERE id=? AND owner=? AND worker_id=? "
                "AND status='running' AND lease_until>=?",
                (error[:300], now, job_id, owner, worker_id, now),
            )
        return result.rowcount == 1
