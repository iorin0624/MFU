from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta

import requests
from flask import current_app

from app.utils.db import get_db

DEFAULT_POSTFIX_STATUS_API_BASE_URL = "http://192.168.103.15:18080"
DEFAULT_MESSAGE_ID_DOMAIN = "mail.iori0624.jp"
DEFAULT_POLL_LIMIT_HOURS = 24
DEFAULT_HTTP_TIMEOUT_SEC = 30
MAX_HTTP_TIMEOUT_SEC = 120
SUCCESS_STATUSES = {"sent", "delivered", "success", "ok"}
SUCCESS_DETAIL_PATTERNS = ("250", "2.0.0", "2.0.0 ok", "queued as", "status=sent")
QUEUED_AS_PATTERN = re.compile(r"queued\s+as\s+([A-Z0-9._-]+)", re.IGNORECASE)


def _get_config_value(name: str, default, *legacy_names: str):
    value = None
    try:
        value = current_app.config.get(name)
    except Exception:
        value = None
    if value is None:
        value = os.environ.get(name)
    if value is None:
        for legacy_name in legacy_names:
            try:
                value = current_app.config.get(legacy_name)
            except Exception:
                value = None
            if value is None:
                value = os.environ.get(legacy_name)
            if value is not None:
                break
    return default if value is None else value


def _clamp(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= max_len else text[:max_len]


def ensure_mail_delivery_schema() -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mfu_mail_delivery_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            mfu_mail_uuid CHAR(36) NOT NULL,
            message_id VARCHAR(255) NOT NULL,
            to_addresses VARCHAR(1024) NULL,
            subject VARCHAR(255) NULL,
            submit_status VARCHAR(32) NOT NULL,
            submit_at DATETIME NOT NULL,
            last_delivery_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
            last_delivery_detail VARCHAR(1000) NULL,
            last_delivery_queue_id VARCHAR(64) NULL,
            last_delivery_checked_at DATETIME NULL,
            external_login_user_id BIGINT NULL,
            mail_kind VARCHAR(64) NULL,
            UNIQUE KEY uniq_message_id (message_id),
            KEY idx_submit_at (submit_at),
            KEY idx_last_delivery_status (last_delivery_status),
            KEY idx_ext_user_submit (external_login_user_id, submit_at),
            KEY idx_mail_kind_submit (mail_kind, submit_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    _ensure_mail_delivery_columns(cur)
    db.commit()
    db.close()


def _ensure_mail_delivery_columns(cur) -> None:
    cur.execute("SHOW COLUMNS FROM mfu_mail_delivery_log")
    existing = {row[0] if isinstance(row, tuple) else row.get("Field") for row in cur.fetchall()}
    cur.execute("SHOW INDEX FROM mfu_mail_delivery_log")
    existing_indexes = {
        row[2] if isinstance(row, tuple) else row.get("Key_name") for row in cur.fetchall()
    }
    if "external_login_user_id" not in existing:
        cur.execute("ALTER TABLE mfu_mail_delivery_log ADD COLUMN external_login_user_id BIGINT NULL")
    if "mail_kind" not in existing:
        cur.execute("ALTER TABLE mfu_mail_delivery_log ADD COLUMN mail_kind VARCHAR(64) NULL")
    if "idx_ext_user_submit" not in existing_indexes:
        try:
            cur.execute(
                "ALTER TABLE mfu_mail_delivery_log ADD KEY idx_ext_user_submit (external_login_user_id, submit_at)"
            )
        except Exception:
            pass
    if "idx_mail_kind_submit" not in existing_indexes:
        try:
            cur.execute("ALTER TABLE mfu_mail_delivery_log ADD KEY idx_mail_kind_submit (mail_kind, submit_at)")
        except Exception:
            pass


def generate_message_id() -> tuple[str, str]:
    mfu_mail_uuid = str(uuid.uuid4())
    domain = _get_config_value(
        "DEFAULT_MESSAGE_ID_DOMAIN",
        DEFAULT_MESSAGE_ID_DOMAIN,
        "MFU_MAIL_MESSAGE_ID_DOMAIN",
    )
    domain = (domain or DEFAULT_MESSAGE_ID_DOMAIN).strip() or DEFAULT_MESSAGE_ID_DOMAIN
    message_id = f"{mfu_mail_uuid}@{domain}"
    return mfu_mail_uuid, message_id


def record_mail_submission(
    *,
    mfu_mail_uuid: str,
    message_id: str,
    to_addresses: str,
    subject: str,
    submit_status: str,
    last_delivery_status: str,
    last_delivery_detail: str | None = None,
    external_login_user_id: int | None = None,
    mail_kind: str | None = None,
) -> None:
    ensure_mail_delivery_schema()
    submit_at = datetime.now()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO mfu_mail_delivery_log (
            mfu_mail_uuid,
            message_id,
            to_addresses,
            subject,
            submit_status,
            submit_at,
            last_delivery_status,
            last_delivery_detail,
            last_delivery_queue_id,
            last_delivery_checked_at,
            external_login_user_id,
            mail_kind
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s)
        ON DUPLICATE KEY UPDATE
            submit_status = VALUES(submit_status),
            submit_at = VALUES(submit_at),
            last_delivery_status = VALUES(last_delivery_status),
            last_delivery_detail = VALUES(last_delivery_detail),
            external_login_user_id = VALUES(external_login_user_id),
            mail_kind = VALUES(mail_kind)
        """,
        (
            mfu_mail_uuid,
            message_id,
            _clamp(to_addresses, 1024),
            _clamp(subject, 255),
            submit_status,
            submit_at,
            last_delivery_status,
            _clamp(last_delivery_detail, 1000),
            external_login_user_id,
            _clamp(mail_kind, 64),
        ),
    )
    db.commit()
    db.close()


def fetch_latest_mail_delivery_for_external_user(
    *,
    external_login_user_id: int,
    email: str,
    mail_kind: str | None = None,
) -> dict | None:
    ensure_mail_delivery_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        where_sql = "WHERE external_login_user_id = %s AND to_addresses LIKE %s"
        params: list = [external_login_user_id, f"%{email}%"]
        if mail_kind:
            where_sql += " AND mail_kind = %s"
            params.append(mail_kind)
        cur.execute(
            f"""
            SELECT mfu_mail_uuid,
                   message_id,
                   to_addresses,
                   subject,
                   submit_status,
                   submit_at,
                   last_delivery_status,
                   last_delivery_detail,
                   last_delivery_queue_id,
                   last_delivery_checked_at,
                   external_login_user_id,
                   mail_kind
              FROM mfu_mail_delivery_log
              {where_sql}
             ORDER BY submit_at DESC, id DESC
             LIMIT 1
            """,
            params,
        )
        return cur.fetchone()
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def build_mail_delivery_display(
    submit_status: str | None,
    delivery_status: str | None,
) -> tuple[str, str | None]:
    submit_status_norm = (submit_status or "").lower()
    delivery_status_norm = (delivery_status or "").lower()

    submit_map = {
        "accepted": "送信受付済み",
        "sent": "送信受付済み",
        "success": "送信受付済み",
        "queued": "送信受付済み",
        "failed": "送信失敗",
        "error": "送信失敗",
    }
    delivery_map = {
        "sent": "配信完了",
        "delivered": "配信完了",
        "bounced": "配信失敗（宛先不達の可能性）",
        "failed": "配信失敗",
        "deferred": "遅延中（再試行中）",
        "queued": "確認中",
        "unknown": "確認中",
    }

    submit_ja = submit_map.get(submit_status_norm, "未送信")
    delivery_ja = delivery_map.get(delivery_status_norm, "確認中")

    display = f"{submit_ja}／配信状況：{delivery_ja}"
    message_ja = None
    if delivery_status_norm == "deferred":
        message_ja = "配送が遅延しているため、再試行しています。"
    elif delivery_status_norm in ("bounced", "failed"):
        message_ja = "宛先不達の可能性があります。メールアドレスをご確認ください。"
    return display, message_ja


def _extract_queue_id(detail: str | None) -> str | None:
    if not detail:
        return None
    match = QUEUED_AS_PATTERN.search(str(detail))
    if not match:
        return None
    return match.group(1)


def _has_successful_delivery_detail(detail: str | None) -> bool:
    if not detail:
        return False
    detail_lower = str(detail).lower()
    return any(pattern in detail_lower for pattern in SUCCESS_DETAIL_PATTERNS)


def _normalize_delivery_status(
    status: str | None,
    detail: str | None,
    queue_id: str | None,
) -> tuple[str, str | None, str | None]:
    """
    Normalize Postfix API results to app-level statuses so that
    "queued + 250 OK" responses are stored as successful delivery.
    """
    status_norm = (status or "").strip().lower()
    detail_text = None if detail is None else str(detail)
    queue_id_text = None if queue_id in (None, "") else str(queue_id)

    if not queue_id_text:
        queue_id_text = _extract_queue_id(detail_text)

    if status_norm in SUCCESS_STATUSES:
        return "sent", detail_text, queue_id_text
    if status_norm == "queued" and _has_successful_delivery_detail(detail_text):
        return "sent", detail_text, queue_id_text
    if status_norm in ("deferred", "bounced", "failed"):
        return status_norm, detail_text, queue_id_text
    if _has_successful_delivery_detail(detail_text):
        return "sent", detail_text, queue_id_text
    if status_norm == "queued":
        return "queued", detail_text, queue_id_text
    return "unknown", detail_text, queue_id_text


def _fetch_poll_targets(limit_hours: int, max_rows: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=limit_hours)
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, message_id, last_delivery_status, submit_at
          FROM mfu_mail_delivery_log
         WHERE submit_status IN ('queued', 'sent')
           -- Successful / terminal results should not be polled again.
           AND last_delivery_status NOT IN ('sent', 'delivered', 'bounced', 'failed')
           AND submit_at >= %s
         ORDER BY submit_at ASC
         LIMIT %s
        """,
        (cutoff, max_rows),
    )
    rows = cur.fetchall() or []
    db.close()
    return rows


def _update_delivery_row(
    *,
    row_id: int,
    status: str,
    detail: str | None,
    queue_id: str | None,
    checked_at: datetime,
) -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE mfu_mail_delivery_log
           SET last_delivery_status = %s,
               last_delivery_detail = %s,
               last_delivery_queue_id = %s,
               last_delivery_checked_at = %s
         WHERE id = %s
        """,
        (
            status,
            _clamp(detail, 1000),
            _clamp(queue_id, 64),
            checked_at,
            row_id,
        ),
    )
    db.commit()
    db.close()


def _touch_delivery_row(
    *,
    row_id: int,
    detail: str | None,
    checked_at: datetime,
) -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE mfu_mail_delivery_log
           SET last_delivery_detail = %s,
               last_delivery_checked_at = %s
         WHERE id = %s
        """,
        (
            _clamp(detail, 1000),
            checked_at,
            row_id,
        ),
    )
    db.commit()
    db.close()


def poll_mail_delivery_statuses(max_rows: int = 200, timeout_sec: int | None = None) -> dict:
    ensure_mail_delivery_schema()
    base_url = _get_config_value(
        "DEFAULT_POSTFIX_STATUS_API_BASE_URL",
        DEFAULT_POSTFIX_STATUS_API_BASE_URL,
        "MFU_POSTFIX_STATUS_API_BASE_URL",
    )
    api_key = _get_config_value("MFU_POSTFIX_STATUS_API_KEY", "")
    limit_hours = int(
        _get_config_value(
            "DEFAULT_POLL_LIMIT_HOURS",
            DEFAULT_POLL_LIMIT_HOURS,
            "MFU_MAIL_STATUS_POLL_LIMIT_HOURS",
        )
    )
    if timeout_sec is None:
        timeout_sec = int(
            _get_config_value(
                "DEFAULT_HTTP_TIMEOUT_SEC",
                DEFAULT_HTTP_TIMEOUT_SEC,
                "MFU_MAIL_STATUS_HTTP_TIMEOUT_SEC",
            )
        )
    timeout_sec = max(1, min(MAX_HTTP_TIMEOUT_SEC, int(timeout_sec)))

    base_url = (base_url or DEFAULT_POSTFIX_STATUS_API_BASE_URL).rstrip("/")
    headers = {"X-API-Key": api_key} if api_key else {}

    rows = _fetch_poll_targets(limit_hours=limit_hours, max_rows=max_rows)
    now = datetime.now()
    summary = {"checked": 0, "updated": 0, "errors": 0}

    for row in rows:
        summary["checked"] += 1
        message_id = row.get("message_id")
        row_id = row.get("id")
        if not message_id or not row_id:
            continue

        url = f"{base_url}/api/mail/status"
        try:
            resp = requests.get(
                url,
                params={"message_id": message_id},
                headers=headers,
                timeout=timeout_sec,
            )
        except requests.Timeout:
            retry_timeout = min(timeout_sec * 2, MAX_HTTP_TIMEOUT_SEC)
            try:
                resp = requests.get(
                    url,
                    params={"message_id": message_id},
                    headers=headers,
                    timeout=retry_timeout,
                )
            except requests.RequestException as exc:
                summary["errors"] += 1
                _touch_delivery_row(
                    row_id=row_id,
                    detail=f"poll_error: {exc.__class__.__name__}",
                    checked_at=now,
                )
                continue
        except requests.RequestException as exc:
            summary["errors"] += 1
            _touch_delivery_row(
                row_id=row_id,
                detail=f"poll_error: {exc.__class__.__name__}",
                checked_at=now,
            )
            continue

        if resp.status_code != 200:
            summary["errors"] += 1
            _touch_delivery_row(
                row_id=row_id,
                detail=f"poll_http_status: {resp.status_code}",
                checked_at=now,
            )
            continue

        try:
            payload = resp.json()
        except ValueError:
            summary["errors"] += 1
            _touch_delivery_row(
                row_id=row_id,
                detail="poll_error: invalid_json",
                checked_at=now,
            )
            continue

        status, detail, queue_id = _normalize_delivery_status(
            payload.get("status"),
            payload.get("detail"),
            payload.get("queue_id"),
        )

        _update_delivery_row(
            row_id=row_id,
            status=status,
            detail=detail,
            queue_id=queue_id,
            checked_at=now,
        )
        summary["updated"] += 1

    return summary
