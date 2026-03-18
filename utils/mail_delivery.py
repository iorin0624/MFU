from __future__ import annotations

import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from email.utils import getaddresses, parseaddr
from typing import Any

import requests
from flask import current_app

from app.utils.db import get_db

DEFAULT_POSTFIX_STATUS_API_BASE_URL = "http://192.168.103.15:18080"
DEFAULT_MESSAGE_ID_DOMAIN = "mail.iori0624.jp"
DEFAULT_POLL_LIMIT_HOURS = 24
DEFAULT_HTTP_TIMEOUT_SEC = 30
MAX_HTTP_TIMEOUT_SEC = 120
SUCCESS_STATUSES = {"sent", "delivered", "success", "ok"}
FAILURE_STATUSES = {"failed", "bounced", "error"}
QUEUED_STATUSES = {"queued", "accepted", "unknown"}
SUCCESS_DETAIL_PATTERNS = ("250", "2.0.0", "2.0.0 ok", "queued as", "status=sent")
QUEUED_AS_PATTERN = re.compile(r"queued\s+as\s+([A-Z0-9._-]+)", re.IGNORECASE)
DERIVED_FROM_PARENT_MARKER = "derived_from_parent"
RECIPIENT_TYPE_LABELS = {"to": "To", "cc": "Cc", "bcc": "Bcc"}
RECIPIENT_TYPE_PRIORITY = ("to", "cc", "bcc")


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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mfu_mail_delivery_recipients (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            mail_log_id BIGINT NOT NULL,
            mfu_mail_uuid VARCHAR(64) NOT NULL,
            message_id VARCHAR(255) NOT NULL,
            recipient VARCHAR(255) NOT NULL,
            recipient_type VARCHAR(16) NOT NULL,
            submit_status VARCHAR(32) NOT NULL DEFAULT 'queued',
            delivery_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
            delivery_detail VARCHAR(1000) NULL,
            delivery_queue_id VARCHAR(64) NULL,
            delivery_checked_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uq_mail_recipient (message_id, recipient, recipient_type),
            KEY idx_mail_log_id (mail_log_id),
            KEY idx_message_id (message_id),
            KEY idx_delivery_status (delivery_status),
            KEY idx_recipient (recipient),
            KEY idx_checked_at (delivery_checked_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    _ensure_mail_delivery_columns(cur)
    _ensure_mail_delivery_recipient_columns(cur)
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


def _ensure_mail_delivery_recipient_columns(cur) -> None:
    cur.execute("SHOW COLUMNS FROM mfu_mail_delivery_recipients")
    existing = {row[0] if isinstance(row, tuple) else row.get("Field") for row in cur.fetchall()}
    cur.execute("SHOW INDEX FROM mfu_mail_delivery_recipients")
    existing_indexes = {
        row[2] if isinstance(row, tuple) else row.get("Key_name") for row in cur.fetchall()
    }

    required_columns = {
        "mail_log_id": "BIGINT NOT NULL",
        "mfu_mail_uuid": "VARCHAR(64) NOT NULL",
        "message_id": "VARCHAR(255) NOT NULL",
        "recipient": "VARCHAR(255) NOT NULL",
        "recipient_type": "VARCHAR(16) NOT NULL",
        "submit_status": "VARCHAR(32) NOT NULL DEFAULT 'queued'",
        "delivery_status": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
        "delivery_detail": "VARCHAR(1000) NULL",
        "delivery_queue_id": "VARCHAR(64) NULL",
        "delivery_checked_at": "DATETIME NULL",
        "created_at": "DATETIME NOT NULL",
        "updated_at": "DATETIME NOT NULL",
    }
    for column_name, ddl in required_columns.items():
        if column_name not in existing:
            cur.execute(f"ALTER TABLE mfu_mail_delivery_recipients ADD COLUMN {column_name} {ddl}")

    index_ddls = {
        "uq_mail_recipient": (
            "ALTER TABLE mfu_mail_delivery_recipients ADD UNIQUE KEY uq_mail_recipient "
            "(message_id, recipient, recipient_type)"
        ),
        "idx_mail_log_id": "ALTER TABLE mfu_mail_delivery_recipients ADD KEY idx_mail_log_id (mail_log_id)",
        "idx_message_id": "ALTER TABLE mfu_mail_delivery_recipients ADD KEY idx_message_id (message_id)",
        "idx_delivery_status": (
            "ALTER TABLE mfu_mail_delivery_recipients ADD KEY idx_delivery_status (delivery_status)"
        ),
        "idx_recipient": "ALTER TABLE mfu_mail_delivery_recipients ADD KEY idx_recipient (recipient)",
        "idx_checked_at": (
            "ALTER TABLE mfu_mail_delivery_recipients ADD KEY idx_checked_at (delivery_checked_at)"
        ),
    }
    for index_name, ddl in index_ddls.items():
        if index_name not in existing_indexes:
            try:
                cur.execute(ddl)
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


def _normalize_email_address(addr: str | None) -> str:
    if addr is None:
        return ""
    display_name, email_address = parseaddr(str(addr).strip())
    candidate = email_address or str(addr)
    candidate = candidate.strip().strip(",;")
    if not candidate and display_name:
        candidate = display_name.strip().strip(",;")
    return candidate.lower()


def _coerce_recipient_values(addresses: list[str] | tuple[str, ...] | set[str] | str | None) -> list[str]:
    if not addresses:
        return []

    raw_values: list[str] = []
    if isinstance(addresses, (list, tuple, set)):
        for value in addresses:
            if value is None:
                continue
            raw_values.append(str(value))
    else:
        raw_values.append(str(addresses))

    parsed = getaddresses(raw_values)
    normalized: list[str] = []
    if parsed:
        for _, email_address in parsed:
            normalized_address = _normalize_email_address(email_address)
            if normalized_address:
                normalized.append(normalized_address)

    if not normalized:
        for value in raw_values:
            normalized_address = _normalize_email_address(value)
            if normalized_address:
                normalized.append(normalized_address)

    return normalized


def _split_recipients(
    to_addrs: list[str] | tuple[str, ...] | set[str] | str | None,
    cc_addrs: list[str] | tuple[str, ...] | set[str] | str | None = None,
    bcc_addrs: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> list[dict[str, str]]:
    recipients: list[dict[str, str]] = []
    seen_global: set[str] = set()

    for recipient_type, values in (
        ("to", to_addrs),
        ("cc", cc_addrs),
        ("bcc", bcc_addrs),
    ):
        seen_local: set[str] = set()
        for recipient in _coerce_recipient_values(values):
            if not recipient or recipient in seen_local:
                continue
            seen_local.add(recipient)
            if recipient in seen_global:
                continue
            seen_global.add(recipient)
            recipients.append({"recipient": recipient, "recipient_type": recipient_type})

    return recipients


def _normalize_submit_status(status: str | None) -> str:
    status_norm = (status or "").strip().lower()
    if status_norm in SUCCESS_STATUSES:
        return "sent"
    if status_norm in FAILURE_STATUSES:
        return "failed"
    if status_norm in QUEUED_STATUSES:
        return "queued"
    if status_norm == "partial":
        return "partial"
    return "unknown"


def _normalize_delivery_status_value(status: str | None) -> str:
    status_norm = (status or "").strip().lower()
    if status_norm in SUCCESS_STATUSES:
        return "sent"
    if status_norm == "deferred":
        return "deferred"
    if status_norm == "bounced":
        return "bounced"
    if status_norm in ("failed", "error"):
        return "failed"
    if status_norm in QUEUED_STATUSES:
        return "queued"
    if status_norm == "partial":
        return "partial"
    return "unknown"


def _aggregate_submit_status(statuses: list[str]) -> str:
    normalized = [_normalize_submit_status(status) for status in statuses if status is not None]
    if not normalized:
        return "queued"
    unique_statuses = set(normalized)
    if unique_statuses == {"sent"}:
        return "sent"
    if any(status == "queued" for status in normalized):
        return "queued"
    if unique_statuses <= {"failed"}:
        return "failed"
    if "sent" in unique_statuses and "failed" in unique_statuses:
        return "partial"
    if len(unique_statuses) > 1:
        return "partial"
    return normalized[0]


def _aggregate_delivery_status(statuses: list[str]) -> str:
    normalized = [_normalize_delivery_status_value(status) for status in statuses if status is not None]
    if not normalized:
        return "queued"
    aggregated = {"failed" if status == "bounced" else status for status in normalized}
    if aggregated == {"sent"}:
        return "sent"
    if aggregated <= {"failed"}:
        return "failed"
    if aggregated == {"deferred"}:
        return "deferred"
    if aggregated <= {"queued", "unknown"}:
        return "queued"
    return "partial"


def _build_parent_delivery_detail(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None

    counter = Counter(
        "failed" if _normalize_delivery_status_value(row.get("delivery_status")) == "bounced"
        else _normalize_delivery_status_value(row.get("delivery_status"))
        for row in rows
    )
    counts_part = ", ".join(
        f"{status}={counter.get(status, 0)}" for status in ("sent", "deferred", "failed", "queued", "unknown")
    )
    issue_parts: list[str] = []
    for row in rows:
        status = _normalize_delivery_status_value(row.get("delivery_status"))
        if status == "sent":
            continue
        recipient_label = f"{RECIPIENT_TYPE_LABELS.get(row.get('recipient_type'), 'Rcpt')}:{row.get('recipient')}"
        detail = (row.get("delivery_detail") or "").strip()
        if detail:
            issue_parts.append(f"{recipient_label}({status}: {detail})")
        else:
            issue_parts.append(f"{recipient_label}({status})")
        if len(issue_parts) >= 3:
            break

    summary = f"recipients={len(rows)}; {counts_part}"
    if issue_parts:
        summary += "; issues=" + " | ".join(issue_parts)
    return _clamp(summary, 1000)


def _recompute_mail_log_summary(mail_log_id: int) -> None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT submit_status,
                   delivery_status,
                   delivery_detail,
                   delivery_queue_id,
                   delivery_checked_at,
                   updated_at,
                   recipient,
                   recipient_type
              FROM mfu_mail_delivery_recipients
             WHERE mail_log_id = %s
             ORDER BY FIELD(recipient_type, 'to', 'cc', 'bcc'), id ASC
            """,
            (mail_log_id,),
        )
        rows = cur.fetchall() or []
        if not rows:
            return

        submit_status = _aggregate_submit_status([row.get("submit_status") for row in rows])
        delivery_status = _aggregate_delivery_status([row.get("delivery_status") for row in rows])
        detail = _build_parent_delivery_detail(rows)

        queue_id = None
        checked_at = None
        for row in sorted(
            rows,
            key=lambda row: row.get("delivery_checked_at") or row.get("updated_at") or datetime.min,
            reverse=True,
        ):
            if checked_at is None:
                checked_at = row.get("delivery_checked_at") or row.get("updated_at")
            if not queue_id and row.get("delivery_queue_id"):
                queue_id = row.get("delivery_queue_id")
            if queue_id and checked_at:
                break

        cur2 = db.cursor()
        cur2.execute(
            """
            UPDATE mfu_mail_delivery_log
               SET submit_status = %s,
                   last_delivery_status = %s,
                   last_delivery_detail = %s,
                   last_delivery_queue_id = %s,
                   last_delivery_checked_at = %s
             WHERE id = %s
            """,
            (
                submit_status,
                delivery_status,
                _clamp(detail, 1000),
                _clamp(queue_id, 64),
                checked_at,
                mail_log_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def _insert_or_update_delivery_recipients(
    *,
    mail_log_id: int,
    mfu_mail_uuid: str,
    message_id: str,
    recipients: list[dict[str, str]],
    submit_status: str = "queued",
    delivery_status: str = "unknown",
    delivery_detail: str | None = None,
    delivery_queue_id: str | None = None,
    delivery_checked_at: datetime | None = None,
) -> int:
    if not recipients:
        return 0

    now = datetime.now()
    normalized_submit_status = _normalize_submit_status(submit_status)
    normalized_delivery_status = _normalize_delivery_status_value(delivery_status)

    db = get_db()
    cur = db.cursor()
    inserted = 0
    try:
        for recipient_info in recipients:
            cur.execute(
                """
                INSERT INTO mfu_mail_delivery_recipients (
                    mail_log_id,
                    mfu_mail_uuid,
                    message_id,
                    recipient,
                    recipient_type,
                    submit_status,
                    delivery_status,
                    delivery_detail,
                    delivery_queue_id,
                    delivery_checked_at,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    mail_log_id = VALUES(mail_log_id),
                    mfu_mail_uuid = VALUES(mfu_mail_uuid),
                    submit_status = VALUES(submit_status),
                    delivery_status = VALUES(delivery_status),
                    delivery_detail = VALUES(delivery_detail),
                    delivery_queue_id = VALUES(delivery_queue_id),
                    delivery_checked_at = VALUES(delivery_checked_at),
                    updated_at = VALUES(updated_at)
                """,
                (
                    mail_log_id,
                    mfu_mail_uuid,
                    message_id,
                    recipient_info["recipient"],
                    recipient_info["recipient_type"],
                    normalized_submit_status,
                    normalized_delivery_status,
                    _clamp(delivery_detail, 1000),
                    _clamp(delivery_queue_id, 64),
                    delivery_checked_at,
                    now,
                    now,
                ),
            )
            inserted += 1
        db.commit()
    finally:
        db.close()

    return inserted


def record_mail_submission_recipients(
    *,
    mail_log_id: int,
    mfu_mail_uuid: str,
    message_id: str,
    to_addresses: list[str] | tuple[str, ...] | set[str] | str,
    cc_addresses: list[str] | tuple[str, ...] | set[str] | str | None = None,
    bcc_addresses: list[str] | tuple[str, ...] | set[str] | str | None = None,
    submit_status: str = "queued",
    delivery_status: str = "unknown",
    delivery_detail: str | None = None,
    delivery_queue_id: str | None = None,
    delivery_checked_at: datetime | None = None,
) -> int:
    recipients = _split_recipients(to_addresses, cc_addresses, bcc_addresses)
    if not recipients:
        return 0

    inserted = _insert_or_update_delivery_recipients(
        mail_log_id=mail_log_id,
        mfu_mail_uuid=mfu_mail_uuid,
        message_id=message_id,
        recipients=recipients,
        submit_status=submit_status,
        delivery_status=delivery_status,
        delivery_detail=delivery_detail,
        delivery_queue_id=delivery_queue_id,
        delivery_checked_at=delivery_checked_at,
    )
    _recompute_mail_log_summary(mail_log_id)
    return inserted


def record_mail_submission(
    *,
    mfu_mail_uuid: str,
    message_id: str,
    to_addresses: str | list[str] | tuple[str, ...] | set[str],
    subject: str,
    submit_status: str,
    last_delivery_status: str,
    last_delivery_detail: str | None = None,
    external_login_user_id: int | None = None,
    mail_kind: str | None = None,
    cc_addresses: str | list[str] | tuple[str, ...] | set[str] | None = None,
    bcc_addresses: str | list[str] | tuple[str, ...] | set[str] | None = None,
) -> int:
    ensure_mail_delivery_schema()
    submit_at = datetime.now()
    if isinstance(to_addresses, str):
        to_address_text = to_addresses.strip()
    elif isinstance(to_addresses, (list, tuple, set)):
        to_address_text = ", ".join(str(address).strip() for address in to_addresses if str(address).strip())
    else:
        to_address_text = ""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mfu_mail_delivery_log (
                id,
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
            ) VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s)
            ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
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
                _clamp(to_address_text, 1024),
                _clamp(subject, 255),
                _normalize_submit_status(submit_status),
                submit_at,
                _normalize_delivery_status_value(last_delivery_status),
                _clamp(last_delivery_detail, 1000),
                external_login_user_id,
                _clamp(mail_kind, 64),
            ),
        )
        mail_log_id = int(cur.lastrowid)
        db.commit()
    finally:
        db.close()

    record_mail_submission_recipients(
        mail_log_id=mail_log_id,
        mfu_mail_uuid=mfu_mail_uuid,
        message_id=message_id,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        bcc_addresses=bcc_addresses,
        submit_status=submit_status,
        delivery_status=last_delivery_status,
        delivery_detail=last_delivery_detail,
    )
    return mail_log_id


def fetch_latest_mail_delivery_for_external_user(
    *,
    external_login_user_id: int,
    email: str,
    mail_kind: str | None = None,
) -> dict | None:
    ensure_mail_delivery_schema()
    email_normalized = _normalize_email_address(email)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        where_sql = "WHERE l.external_login_user_id = %s AND (r.recipient = %s OR l.to_addresses LIKE %s)"
        params: list[Any] = [external_login_user_id, email_normalized, f"%{email}%"]
        if mail_kind:
            where_sql += " AND l.mail_kind = %s"
            params.append(mail_kind)
        cur.execute(
            f"""
            SELECT l.id,
                   l.mfu_mail_uuid,
                   l.message_id,
                   l.to_addresses,
                   l.subject,
                   l.submit_status,
                   l.submit_at,
                   l.last_delivery_status,
                   l.last_delivery_detail,
                   l.last_delivery_queue_id,
                   l.last_delivery_checked_at,
                   l.external_login_user_id,
                   l.mail_kind
              FROM mfu_mail_delivery_log AS l
              LEFT JOIN mfu_mail_delivery_recipients AS r
                ON r.mail_log_id = l.id
             {where_sql}
             ORDER BY l.submit_at DESC, l.id DESC
             LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(
            """
            SELECT recipient,
                   recipient_type,
                   submit_status,
                   delivery_status,
                   delivery_detail,
                   delivery_queue_id,
                   delivery_checked_at
              FROM mfu_mail_delivery_recipients
             WHERE mail_log_id = %s
             ORDER BY FIELD(recipient_type, 'to', 'cc', 'bcc'), id ASC
            """,
            (row["id"],),
        )
        row["recipient_details"] = cur.fetchall() or []
        return row
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
    submit_status_norm = _normalize_submit_status(submit_status)
    delivery_status_norm = _normalize_delivery_status_value(delivery_status)

    submit_map = {
        "sent": "送信受付済み",
        "queued": "送信受付中",
        "failed": "送信失敗",
        "partial": "一部送信失敗",
        "unknown": "送信状況不明",
    }
    delivery_map = {
        "sent": "配信完了",
        "bounced": "配信失敗（宛先不達の可能性）",
        "failed": "配信失敗",
        "deferred": "遅延中（再試行中）",
        "queued": "確認中",
        "unknown": "確認中",
        "partial": "一部成功／一部未達",
    }

    submit_ja = submit_map.get(submit_status_norm, "未送信")
    delivery_ja = delivery_map.get(delivery_status_norm, "確認中")

    display = f"{submit_ja}／配信状況：{delivery_ja}"
    message_ja = None
    if delivery_status_norm == "deferred":
        message_ja = "配送が遅延しているため、再試行しています。"
    elif delivery_status_norm == "partial":
        message_ja = "宛先ごとに配送結果が分かれています。詳細をご確認ください。"
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


def _fetch_recipient_poll_targets(limit_hours: int, max_rows: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=limit_hours)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT r.id,
                   r.mail_log_id,
                   r.message_id,
                   r.recipient,
                   r.recipient_type,
                   r.submit_status,
                   r.delivery_status,
                   r.delivery_checked_at,
                   l.submit_at
              FROM mfu_mail_delivery_recipients AS r
              JOIN mfu_mail_delivery_log AS l
                ON l.id = r.mail_log_id
             WHERE r.submit_status IN ('queued', 'sent')
               AND r.delivery_status NOT IN ('sent', 'bounced', 'failed')
               AND l.submit_at >= %s
             ORDER BY l.submit_at ASC, r.id ASC
             LIMIT %s
            """,
            (cutoff, max_rows),
        )
        return cur.fetchall() or []
    finally:
        db.close()


def _fetch_poll_targets(limit_hours: int, max_rows: int) -> list[dict]:
    recipients = _fetch_recipient_poll_targets(limit_hours=limit_hours, max_rows=max_rows)
    grouped: dict[int, dict[str, Any]] = {}
    for row in recipients:
        mail_log_id = int(row["mail_log_id"])
        if mail_log_id not in grouped:
            grouped[mail_log_id] = {
                "id": mail_log_id,
                "message_id": row.get("message_id"),
                "last_delivery_status": row.get("delivery_status"),
                "submit_at": row.get("submit_at"),
            }
    return list(grouped.values())


def _update_delivery_recipient_row(
    *,
    row_id: int,
    status: str,
    detail: str | None,
    queue_id: str | None,
    checked_at: datetime,
    submit_status: str | None = None,
) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        if submit_status is None:
            cur.execute(
                """
                UPDATE mfu_mail_delivery_recipients
                   SET delivery_status = %s,
                       delivery_detail = %s,
                       delivery_queue_id = %s,
                       delivery_checked_at = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (
                    _normalize_delivery_status_value(status),
                    _clamp(detail, 1000),
                    _clamp(queue_id, 64),
                    checked_at,
                    checked_at,
                    row_id,
                ),
            )
        else:
            cur.execute(
                """
                UPDATE mfu_mail_delivery_recipients
                   SET submit_status = %s,
                       delivery_status = %s,
                       delivery_detail = %s,
                       delivery_queue_id = %s,
                       delivery_checked_at = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (
                    _normalize_submit_status(submit_status),
                    _normalize_delivery_status_value(status),
                    _clamp(detail, 1000),
                    _clamp(queue_id, 64),
                    checked_at,
                    checked_at,
                    row_id,
                ),
            )
        db.commit()
    finally:
        db.close()


def _touch_delivery_recipient_row(
    *,
    row_id: int,
    detail: str | None,
    checked_at: datetime,
) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_mail_delivery_recipients
               SET delivery_detail = %s,
                   delivery_checked_at = %s,
                   updated_at = %s
             WHERE id = %s
            """,
            (
                _clamp(detail, 1000),
                checked_at,
                checked_at,
                row_id,
            ),
        )
        db.commit()
    finally:
        db.close()


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
    try:
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
                _normalize_delivery_status_value(status),
                _clamp(detail, 1000),
                _clamp(queue_id, 64),
                checked_at,
                row_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def _touch_delivery_row(
    *,
    row_id: int,
    detail: str | None,
    checked_at: datetime,
) -> None:
    db = get_db()
    cur = db.cursor()
    try:
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
    finally:
        db.close()


def _extract_recipient_status_payloads(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidate = None
    for key in ("recipients", "results", "statuses"):
        if isinstance(payload.get(key), (list, dict)):
            candidate = payload.get(key)
            break

    if candidate is None:
        return {}

    status_map: dict[str, dict[str, Any]] = {}
    if isinstance(candidate, dict):
        iterable = candidate.items()
    else:
        iterable = []
        for item in candidate:
            if not isinstance(item, dict):
                continue
            recipient = item.get("recipient") or item.get("email") or item.get("address")
            iterable.append((recipient, item))

    for key, value in iterable:
        recipient = _normalize_email_address(key)
        if not recipient:
            continue
        if isinstance(value, dict):
            status_map[recipient] = {
                "status": value.get("status"),
                "detail": value.get("detail"),
                "queue_id": value.get("queue_id"),
            }
        else:
            status_map[recipient] = {"status": value, "detail": None, "queue_id": None}

    return status_map


def _build_derived_detail(detail: str | None, recipient: str) -> str:
    pieces = [DERIVED_FROM_PARENT_MARKER, f"recipient={recipient}"]
    if detail:
        pieces.append(str(detail))
    return _clamp("; ".join(pieces), 1000) or DERIVED_FROM_PARENT_MARKER


def backfill_mail_delivery_recipients_from_logs(max_rows: int | None = None) -> dict[str, int]:
    ensure_mail_delivery_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    params: list[Any] = []
    limit_sql = ""
    if max_rows is not None:
        limit_sql = " LIMIT %s"
        params.append(int(max_rows))

    try:
        cur.execute(
            f"""
            SELECT l.id,
                   l.mfu_mail_uuid,
                   l.message_id,
                   l.to_addresses,
                   l.submit_status,
                   l.last_delivery_status,
                   l.last_delivery_detail,
                   l.last_delivery_queue_id,
                   l.last_delivery_checked_at
              FROM mfu_mail_delivery_log AS l
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM mfu_mail_delivery_recipients AS r
                     WHERE r.mail_log_id = l.id
                  )
             ORDER BY l.id ASC
             {limit_sql}
            """,
            params,
        )
        rows = cur.fetchall() or []
    finally:
        db.close()

    summary = {"checked": len(rows), "backfilled_logs": 0, "inserted_recipients": 0, "skipped": 0}
    for row in rows:
        recipients = _split_recipients(row.get("to_addresses"))
        if not recipients:
            summary["skipped"] += 1
            continue
        summary["inserted_recipients"] += _insert_or_update_delivery_recipients(
            mail_log_id=int(row["id"]),
            mfu_mail_uuid=row.get("mfu_mail_uuid") or "",
            message_id=row.get("message_id") or "",
            recipients=recipients,
            submit_status=row.get("submit_status") or "queued",
            delivery_status=row.get("last_delivery_status") or "unknown",
            delivery_detail=_build_derived_detail(row.get("last_delivery_detail") or "backfill_from_parent", "backfill"),
            delivery_queue_id=row.get("last_delivery_queue_id"),
            delivery_checked_at=row.get("last_delivery_checked_at"),
        )
        _recompute_mail_log_summary(int(row["id"]))
        summary["backfilled_logs"] += 1

    return summary


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

    rows = _fetch_recipient_poll_targets(limit_hours=limit_hours, max_rows=max_rows)
    now = datetime.now()
    summary = {"checked": 0, "updated": 0, "errors": 0, "messages": 0}

    rows_by_message_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("message_id"):
            rows_by_message_id[str(row["message_id"])].append(row)

    for message_id, recipient_rows in rows_by_message_id.items():
        summary["messages"] += 1
        summary["checked"] += len(recipient_rows)
        url = f"{base_url}/api/mail/status"
        mail_log_ids = {int(row["mail_log_id"]) for row in recipient_rows if row.get("mail_log_id")}

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
                summary["errors"] += len(recipient_rows)
                for row in recipient_rows:
                    _touch_delivery_recipient_row(
                        row_id=int(row["id"]),
                        detail=f"poll_error: {exc.__class__.__name__}",
                        checked_at=now,
                    )
                for mail_log_id in mail_log_ids:
                    _recompute_mail_log_summary(mail_log_id)
                continue
        except requests.RequestException as exc:
            summary["errors"] += len(recipient_rows)
            for row in recipient_rows:
                _touch_delivery_recipient_row(
                    row_id=int(row["id"]),
                    detail=f"poll_error: {exc.__class__.__name__}",
                    checked_at=now,
                )
            for mail_log_id in mail_log_ids:
                _recompute_mail_log_summary(mail_log_id)
            continue

        if resp.status_code != 200:
            summary["errors"] += len(recipient_rows)
            for row in recipient_rows:
                _touch_delivery_recipient_row(
                    row_id=int(row["id"]),
                    detail=f"poll_http_status: {resp.status_code}",
                    checked_at=now,
                )
            for mail_log_id in mail_log_ids:
                _recompute_mail_log_summary(mail_log_id)
            continue

        try:
            payload = resp.json()
        except ValueError:
            summary["errors"] += len(recipient_rows)
            for row in recipient_rows:
                _touch_delivery_recipient_row(
                    row_id=int(row["id"]),
                    detail="poll_error: invalid_json",
                    checked_at=now,
                )
            for mail_log_id in mail_log_ids:
                _recompute_mail_log_summary(mail_log_id)
            continue

        recipient_payloads = _extract_recipient_status_payloads(payload if isinstance(payload, dict) else {})
        if recipient_payloads:
            for row in recipient_rows:
                recipient = _normalize_email_address(row.get("recipient"))
                status_payload = recipient_payloads.get(recipient)
                if not status_payload:
                    _touch_delivery_recipient_row(
                        row_id=int(row["id"]),
                        detail=f"poll_pending: recipient_not_returned; recipient={recipient}",
                        checked_at=now,
                    )
                    continue
                status, detail, queue_id = _normalize_delivery_status(
                    status_payload.get("status"),
                    status_payload.get("detail"),
                    status_payload.get("queue_id"),
                )
                _update_delivery_recipient_row(
                    row_id=int(row["id"]),
                    submit_status="sent",
                    status=status,
                    detail=detail,
                    queue_id=queue_id,
                    checked_at=now,
                )
                summary["updated"] += 1
        else:
            status, detail, queue_id = _normalize_delivery_status(
                payload.get("status") if isinstance(payload, dict) else None,
                payload.get("detail") if isinstance(payload, dict) else None,
                payload.get("queue_id") if isinstance(payload, dict) else None,
            )
            for row in recipient_rows:
                _update_delivery_recipient_row(
                    row_id=int(row["id"]),
                    submit_status="sent",
                    status=status,
                    detail=_build_derived_detail(detail, _normalize_email_address(row.get("recipient"))),
                    queue_id=queue_id,
                    checked_at=now,
                )
                summary["updated"] += 1

        for mail_log_id in mail_log_ids:
            _recompute_mail_log_summary(mail_log_id)

    return summary
