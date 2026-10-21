"""Download audit history for normal uploads.

The history is intentionally scoped to the normal ``/view/<uuid>`` download
flow.  Layer reply uploads are not recorded here.  History rows are deleted
when the normal upload is deleted.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from flask import Request

from app.utils.db import get_db


DOWNLOAD_KIND_LABELS = {
    "selected_zip": "選択ZIPダウンロード",
    "all_zip": "ZIP一括ダウンロード",
    "mobile_app": "写真アプリに保存",
    "ios_shortcut": "SCでDL",
}
DOWNLOAD_STATUS_LABELS = {
    "started": "配信開始",
    "completed": "配信完了",
    "failed": "中断・失敗",
}
DOWNLOAD_AUDIT_EVENT_LABELS = {
    "prepare_started": "ZIP生成を開始",
    "prepare_completed": "ZIP生成が完了",
    "prepare_rejected": "ZIP生成を拒否",
    "prepare_failed": "ZIP生成に失敗",
    "download_clicked": "ダウンロードボタンを押下",
    "download_intent_rejected": "ダウンロード操作を拒否",
    "download_started": "ZIP配信を開始",
    "download_rejected": "ZIP配信を拒否",
    "stream_finished": "ZIP配信が終了",
}
DOWNLOAD_AUDIT_DETAIL_LABELS = {
    "job_ref": "ジョブ参照ID",
    "attempt_ref": "配信試行ID",
    "intent_ref": "操作ID",
    "client_ip_hash": "IP識別子（匿名化）",
    "user_agent": "ブラウザー",
    "reason": "理由",
    "error_type": "エラー種別",
    "requested_files": "指定枚数",
    "valid_files": "有効枚数",
    "bad_paths_count": "対象外枚数",
    "file_count": "配信枚数",
    "sequence_rename": "連番リネーム",
    "http_status": "HTTP状態",
    "archive_bytes": "ZIPサイズ（バイト）",
    "elapsed_ms": "所要時間（ミリ秒）",
    "range_requested": "範囲指定リクエスト",
    "response_bytes": "応答予定（バイト）",
    "history_event_id": "履歴ID",
    "result": "配信結果",
    "bytes_yielded": "送信量（バイト）",
    "expected_bytes": "予定送信量（バイト）",
    "stream_error": "配信エラー",
}
_AUDIT_DETAIL_KEYS = frozenset(DOWNLOAD_AUDIT_DETAIL_LABELS)
_AUDIT_DISPLAY_ORDER = (
    "result", "reason", "http_status", "file_count", "requested_files",
    "valid_files", "bad_paths_count", "bytes_yielded", "expected_bytes",
    "archive_bytes", "response_bytes", "elapsed_ms", "error_type",
    "stream_error", "sequence_rename", "range_requested", "job_ref",
    "attempt_ref", "intent_ref", "history_event_id", "client_ip_hash", "user_agent",
)
_SCHEMA_LOCK = threading.Lock()
_schema_ready = False
_AUDIT_LOGGER = logging.getLogger("mfu.download_audit")
_AUDIT_LOGGER.setLevel(logging.INFO)
_AUDIT_LOGGER.propagate = False
if not _AUDIT_LOGGER.handlers:
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _AUDIT_LOGGER.addHandler(_audit_handler)


def log_download_audit(event: str, **details) -> None:
    """Write structured transfer diagnostics to stderr/systemd journal."""
    _AUDIT_LOGGER.info("DOWNLOAD_AUDIT %s", json.dumps({
        "event": event, **details,
    }, ensure_ascii=False, sort_keys=True))
    if event in DOWNLOAD_AUDIT_EVENT_LABELS and details.get("upload_id"):
        try:
            record_upload_download_audit(
                upload_id=int(details["upload_id"]), event=event, details=details,
            )
        except Exception:
            # Diagnostics must never interrupt ZIP creation or response streaming.
            _AUDIT_LOGGER.exception("DOWNLOAD_AUDIT database insert failed event=%s", event)


def ensure_upload_download_history_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _SCHEMA_LOCK:
        if _schema_ready:
            return
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_download_events (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  upload_id INT NOT NULL,
                  event_key VARCHAR(191) NOT NULL,
                  download_kind VARCHAR(32) NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'started',
                  ip_address VARCHAR(64) NOT NULL,
                  user_agent VARCHAR(512) NULL,
                  file_count INT UNSIGNED NOT NULL DEFAULT 0,
                  requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                  completed_at DATETIME(6) NULL,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_upload_download_event_key (event_key),
                  KEY idx_upload_download_upload_time (upload_id, requested_at),
                  KEY idx_upload_download_upload_ip (upload_id, ip_address),
                  CONSTRAINT fk_upload_download_event_upload
                    FOREIGN KEY (upload_id) REFERENCES uploads (id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_download_event_files (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  event_id BIGINT UNSIGNED NOT NULL,
                  file_id INT NULL,
                  filename VARCHAR(1024) NOT NULL,
                  display_order INT UNSIGNED NOT NULL DEFAULT 0,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_upload_download_event_file (event_id, display_order),
                  KEY idx_upload_download_file_id (file_id),
                  KEY idx_upload_download_filename (filename(191)),
                  CONSTRAINT fk_upload_download_file_event
                    FOREIGN KEY (event_id) REFERENCES upload_download_events (id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_download_audit_events (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  upload_id INT NOT NULL,
                  job_ref CHAR(16) NULL,
                  event_name VARCHAR(40) NOT NULL,
                  details_json TEXT NOT NULL,
                  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                  PRIMARY KEY (id),
                  KEY idx_download_audit_upload_time (upload_id, created_at, id),
                  KEY idx_download_audit_job (job_ref, id),
                  CONSTRAINT fk_download_audit_upload
                    FOREIGN KEY (upload_id) REFERENCES uploads (id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            # ios_shortcut が履歴種別へ追加される前は selected_zip に正規化
            # されていたため、イベントキーから安全に判別できる既存行を補正する。
            cur.execute(
                """
                UPDATE upload_download_events
                   SET download_kind='ios_shortcut'
                 WHERE event_key LIKE 'ios-shortcut:%'
                   AND download_kind='selected_zip'
                """
            )
            db.commit()
        finally:
            db.close()
        _schema_ready = True


def record_upload_download_audit(*, upload_id: int, event: str, details: dict) -> None:
    """Persist only allowlisted diagnostics, never the reusable ZIP download key."""
    if upload_id <= 0 or event not in DOWNLOAD_AUDIT_EVENT_LABELS:
        return
    safe_details = {
        key: value for key, value in details.items()
        if key in _AUDIT_DETAIL_KEYS and value is not None
        and isinstance(value, (str, int, float, bool))
    }
    for key in ("user_agent", "reason", "error_type", "stream_error"):
        if key in safe_details:
            safe_details[key] = str(safe_details[key])[:180]
    ensure_upload_download_history_schema()
    db = get_db(retries=1)
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO upload_download_audit_events
                (upload_id, job_ref, event_name, details_json, created_at)
            VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6))
            """,
            (upload_id, safe_details.get("job_ref"), event,
             json.dumps(safe_details, ensure_ascii=False)),
        )
        db.commit()
    finally:
        db.close()


def find_upload_id_for_zip_job(job_ref: str) -> int | None:
    """Resolve an expired ZIP attempt to its upload without storing the capability key."""
    if not isinstance(job_ref, str) or len(job_ref) != 16 or any(c not in "0123456789abcdef" for c in job_ref):
        return None
    ensure_upload_download_history_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT upload_id FROM upload_download_audit_events
             WHERE job_ref=%s
             ORDER BY id DESC LIMIT 1
            """,
            (job_ref,),
        )
        row = cur.fetchone()
        return int(row["upload_id"]) if row else None
    finally:
        db.close()


def list_upload_download_audit(*, upload_id: int, page: int = 1, per_page: int = 50) -> dict[str, Any]:
    """Return only this upload's diagnostics, newest first, with a bounded page."""
    ensure_upload_download_history_schema()
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 50)))
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT COUNT(*) AS total FROM upload_download_audit_events WHERE upload_id=%s",
            (upload_id,),
        )
        total = int((cur.fetchone() or {}).get("total") or 0)
        page_count = max(1, (total + per_page - 1) // per_page)
        page = min(page, page_count)
        cur.execute(
            """
            SELECT id, event_name, details_json,
                   CONVERT_TZ(created_at, '+00:00', '+09:00') AS created_at_jst
              FROM upload_download_audit_events
             WHERE upload_id=%s
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            (upload_id, per_page, (page - 1) * per_page),
        )
        events = cur.fetchall()
    finally:
        db.close()
    for row in events:
        try:
            details = json.loads(row.pop("details_json") or "{}")
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        row["label"] = DOWNLOAD_AUDIT_EVENT_LABELS.get(row["event_name"], row["event_name"])
        row["details"] = [
            (DOWNLOAD_AUDIT_DETAIL_LABELS[key],
             ("あり" if details[key] else "なし") if isinstance(details[key], bool) else details[key])
            for key in _AUDIT_DISPLAY_ORDER if key in details
        ]
    return {"events": events, "total": total, "page": page, "page_count": page_count}


def request_ip(flask_request: Request) -> str:
    """Return the ProxyFix-normalized client IP without trusting raw headers."""
    value = str(flask_request.remote_addr or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value[:64] or "-"


def normalize_download_files(files: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()
    for item in files or []:
        filename = str(item.get("filename") or item.get("name") or "").strip()
        if not filename:
            continue
        raw_file_id = item.get("file_id", item.get("id"))
        try:
            file_id = int(raw_file_id) if raw_file_id is not None else None
        except (TypeError, ValueError):
            file_id = None
        if file_id is not None and file_id <= 0:
            file_id = None
        filename = filename[:1024]
        identity = (file_id, filename)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({"file_id": file_id, "filename": filename})
    return normalized


def record_upload_download(
    *,
    upload_id: int,
    event_key: str,
    download_kind: str,
    ip_address: str,
    user_agent: str,
    files: Iterable[dict[str, Any]],
    status: str = "started",
) -> int | None:
    ensure_upload_download_history_schema()
    normalized = normalize_download_files(files)
    if not normalized:
        return None
    kind = download_kind if download_kind in DOWNLOAD_KIND_LABELS else "selected_zip"
    normalized_status = status if status in DOWNLOAD_STATUS_LABELS else "started"
    safe_event_key = str(event_key or "").strip()[:191]
    if not safe_event_key:
        return None

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            INSERT IGNORE INTO upload_download_events
                (upload_id, event_key, download_kind, status, ip_address,
                 user_agent, file_count, requested_at, completed_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6),
                 CASE WHEN %s='completed' THEN UTC_TIMESTAMP(6) ELSE NULL END)
            """,
            (
                int(upload_id),
                safe_event_key,
                kind,
                normalized_status,
                str(ip_address or "-")[:64],
                str(user_agent or "")[:512],
                len(normalized),
                normalized_status,
            ),
        )
        inserted = int(cur.rowcount or 0) > 0
        if inserted:
            event_id = int(cur.lastrowid)
            cur.executemany(
                """
                INSERT INTO upload_download_event_files
                    (event_id, file_id, filename, display_order)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (event_id, item["file_id"], item["filename"], index)
                    for index, item in enumerate(normalized)
                ],
            )
        else:
            cur.execute(
                "SELECT id FROM upload_download_events WHERE event_key=%s LIMIT 1",
                (safe_event_key,),
            )
            row = cur.fetchone()
            event_id = int(row["id"]) if row else None
        db.commit()
        return event_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def mark_upload_download_status(event_id: int | None, status: str) -> None:
    if not event_id or status not in DOWNLOAD_STATUS_LABELS:
        return
    ensure_upload_download_history_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE upload_download_events
               SET status=%s,
                   completed_at=CASE
                       WHEN %s='completed' THEN UTC_TIMESTAMP(6)
                       ELSE completed_at
                   END
             WHERE id=%s
            """,
            (status, status, int(event_id)),
        )
        db.commit()
    finally:
        db.close()


def track_upload_download_response(response, event_id: int | None, *, logger=None, audit=None):
    """Mark completion after streaming and optionally log each transfer attempt."""
    if not event_id and not audit:
        return response
    original_iterable = response.response
    expected_bytes = response.content_length
    started = time.monotonic()

    def tracked_body():
        completed = False
        bytes_yielded = 0
        stream_error = None
        try:
            for chunk in original_iterable:
                bytes_yielded += len(chunk)
                yield chunk
            completed = True
        except BaseException as exc:
            stream_error = type(exc).__name__
            raise
        finally:
            try:
                close = getattr(original_iterable, "close", None)
                if close:
                    close()
            except Exception:
                if logger:
                    logger.exception(
                        "upload download response close failed event_id=%s",
                        event_id,
                    )
            if event_id:
                try:
                    mark_upload_download_status(
                        event_id,
                        "completed" if completed else "failed",
                    )
                except Exception:
                    if logger:
                        logger.exception(
                            "upload download status update failed event_id=%s",
                            event_id,
                        )
            if audit:
                log_download_audit("stream_finished", **{
                    **audit,
                    "result": "completed" if completed else "interrupted",
                    "history_event_id": event_id,
                    "bytes_yielded": bytes_yielded,
                    "expected_bytes": expected_bytes,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "stream_error": stream_error,
                })

    response.response = tracked_body()
    return response


def purge_upload_download_history(upload_id: int, *, db=None, cursor=None) -> int:
    """Delete a normal upload's history, optionally inside the caller transaction."""
    ensure_upload_download_history_schema()
    owns_connection = db is None
    if owns_connection:
        db = get_db()
    owns_cursor = cursor is None
    if owns_cursor:
        cursor = db.cursor()
    try:
        cursor.execute(
            "DELETE FROM upload_download_events WHERE upload_id=%s",
            (int(upload_id),),
        )
        deleted = max(0, int(cursor.rowcount or 0))
        if owns_connection:
            db.commit()
        return deleted
    except Exception:
        if owns_connection:
            db.rollback()
        raise
    finally:
        if owns_cursor:
            cursor.close()
        if owns_connection:
            db.close()


def _parse_date(value: str | None) -> date | None:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def list_upload_download_history(
    *,
    upload_id: int,
    page: int = 1,
    per_page: int = 30,
    ip_address: str = "",
    download_kind: str = "",
    filename: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    ensure_upload_download_history_schema()
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 30)))
    where = ["e.upload_id=%s"]
    params: list[Any] = [int(upload_id)]

    if ip_address:
        where.append("e.ip_address LIKE %s")
        params.append(f"%{str(ip_address).strip()[:64]}%")
    if download_kind in DOWNLOAD_KIND_LABELS:
        where.append("e.download_kind=%s")
        params.append(download_kind)
    parsed_from = _parse_date(date_from)
    if parsed_from:
        where.append("e.requested_at >= %s")
        params.append(
            datetime.combine(parsed_from, datetime.min.time()) - timedelta(hours=9)
        )
    parsed_to = _parse_date(date_to)
    if parsed_to:
        where.append("e.requested_at < %s")
        params.append(
            datetime.combine(parsed_to + timedelta(days=1), datetime.min.time())
            - timedelta(hours=9)
        )
    if filename:
        where.append(
            """
            EXISTS (
              SELECT 1
                FROM upload_download_event_files ef
               WHERE ef.event_id=e.id AND ef.filename LIKE %s
            )
            """
        )
        params.append(f"%{str(filename).strip()[:255]}%")

    where_sql = " AND ".join(where)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM upload_download_events e WHERE {where_sql}",
            tuple(params),
        )
        total = int((cur.fetchone() or {}).get("total") or 0)
        page_count = max(1, (total + per_page - 1) // per_page)
        page = min(page, page_count)
        offset = (page - 1) * per_page
        cur.execute(
            f"""
            SELECT e.*,
                   CONVERT_TZ(e.requested_at, '+00:00', '+09:00') AS requested_at_jst,
                   CONVERT_TZ(e.completed_at, '+00:00', '+09:00') AS completed_at_jst
              FROM upload_download_events e
             WHERE {where_sql}
             ORDER BY e.requested_at DESC, e.id DESC
             LIMIT %s OFFSET %s
            """,
            (*params, per_page, offset),
        )
        events = cur.fetchall()
        event_ids = [int(row["id"]) for row in events]
        files_by_event: dict[int, list[dict[str, Any]]] = {event_id: [] for event_id in event_ids}
        if event_ids:
            placeholders = ",".join(["%s"] * len(event_ids))
            cur.execute(
                f"""
                SELECT ef.event_id, ef.file_id, ef.filename, ef.display_order,
                       f.id AS current_file_id, f.is_hidden
                  FROM upload_download_event_files ef
                  LEFT JOIN files f
                    ON f.id=ef.file_id AND f.upload_id=%s
                 WHERE ef.event_id IN ({placeholders})
                 ORDER BY ef.event_id, ef.display_order
                """,
                (int(upload_id), *event_ids),
            )
            for row in cur.fetchall():
                files_by_event[int(row["event_id"])].append(row)
    finally:
        db.close()

    for event in events:
        event["kind_label"] = DOWNLOAD_KIND_LABELS.get(
            event.get("download_kind"), event.get("download_kind")
        )
        event["status_label"] = DOWNLOAD_STATUS_LABELS.get(
            event.get("status"), event.get("status")
        )
        event["files"] = files_by_event.get(int(event["id"]), [])

    return {
        "events": events,
        "total": total,
        "page": min(page, page_count),
        "page_count": page_count,
        "per_page": per_page,
    }
