from __future__ import annotations

from email.utils import parseaddr
from typing import Iterable

from app.utils.db import get_db


RATE_LIMIT_MINUTES = 5


def normalize_email(value: str | None) -> str:
    raw = str(value or "").strip()
    _display, address = parseaddr(raw)
    normalized = (address or "").strip().lower()
    if (
        not normalized
        or normalized != raw.lower()
        or len(normalized) > 320
        or normalized.count("@") != 1
        or any(char.isspace() for char in normalized)
    ):
        raise ValueError("正しいメールアドレスを入力してください")
    local, domain = normalized.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("正しいメールアドレスを入力してください")
    return normalized


def ensure_schema() -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_ticket_price_mail_recipients (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                email VARCHAR(320) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                last_received_at DATETIME NULL,
                last_sent_at DATETIME NULL,
                send_count BIGINT NOT NULL DEFAULT 0,
                last_result VARCHAR(32) NOT NULL DEFAULT '',
                last_error VARCHAR(1000) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_ticket_price_mail_recipient_email (email),
                INDEX idx_ticket_price_mail_recipient_active (is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_ticket_price_mail_requests (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                message_id VARCHAR(500) NOT NULL,
                envelope_sender VARCHAR(320) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'processing',
                item_count INT NOT NULL DEFAULT 0,
                fetched_at VARCHAR(32) NOT NULL DEFAULT '',
                error VARCHAR(1000) NOT NULL DEFAULT '',
                received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME NULL,
                UNIQUE KEY uq_ticket_price_mail_request_message_id (message_id),
                INDEX idx_ticket_price_mail_request_received (received_at),
                INDEX idx_ticket_price_mail_request_sender (envelope_sender, received_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_ticket_price_shop_locations (
                shop_code VARCHAR(64) NOT NULL PRIMARY KEY,
                shop_name VARCHAR(255) NOT NULL DEFAULT '',
                detail_url VARCHAR(1000) NOT NULL DEFAULT '',
                full_address VARCHAR(1000) NOT NULL DEFAULT '',
                area VARCHAR(255) NOT NULL DEFAULT '',
                fetch_status VARCHAR(32) NOT NULL DEFAULT 'ok',
                last_error VARCHAR(1000) NOT NULL DEFAULT '',
                checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_ticket_price_shop_location_checked (checked_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    finally:
        db.close()


def get_shop_locations(shop_codes: Iterable[str]) -> dict[str, dict]:
    codes = sorted({str(code or "").strip()[:64] for code in shop_codes if code})
    if not codes:
        return {}
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(codes))
        cur.execute(
            f"""
            SELECT shop_code, shop_name, detail_url, full_address, area,
                   fetch_status, last_error, checked_at
              FROM mfu_ticket_price_shop_locations
             WHERE shop_code IN ({placeholders})
            """,
            tuple(codes),
        )
        return {str(row["shop_code"]): row for row in (cur.fetchall() or [])}
    finally:
        db.close()


def save_shop_location(
    shop_code: str,
    *,
    shop_name: str,
    detail_url: str,
    full_address: str = "",
    area: str = "",
    fetch_status: str = "ok",
    last_error: str = "",
) -> None:
    save_shop_locations(
        [
            {
                "shop_code": shop_code,
                "shop_name": shop_name,
                "detail_url": detail_url,
                "full_address": full_address,
                "area": area,
                "fetch_status": fetch_status,
                "last_error": last_error,
            }
        ]
    )


def save_shop_locations(locations: Iterable[dict]) -> None:
    rows = []
    for location in locations:
        code = str(location.get("shop_code") or "").strip()[:64]
        if not code:
            continue
        rows.append(
            (
                code,
                str(location.get("shop_name") or "")[:255],
                str(location.get("detail_url") or "")[:1000],
                str(location.get("full_address") or "")[:1000],
                str(location.get("area") or "")[:255],
                str(location.get("fetch_status") or "error")[:32],
                str(location.get("last_error") or "")[:1000],
            )
        )
    if not rows:
        return
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.executemany(
            """
            INSERT INTO mfu_ticket_price_shop_locations
                (shop_code, shop_name, detail_url, full_address, area,
                 fetch_status, last_error, checked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                shop_name=VALUES(shop_name),
                detail_url=VALUES(detail_url),
                full_address=VALUES(full_address),
                area=VALUES(area),
                fetch_status=VALUES(fetch_status),
                last_error=VALUES(last_error),
                checked_at=NOW()
            """,
            rows,
        )
        db.commit()
    finally:
        db.close()


def list_recipients() -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, email, is_active, last_received_at, last_sent_at,
                   send_count, last_result, last_error, created_at, updated_at
              FROM mfu_ticket_price_mail_recipients
             ORDER BY email
            """
        )
        return cur.fetchall() or []
    finally:
        db.close()


def add_recipient(email: str) -> dict:
    normalized = normalize_email(email)
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO mfu_ticket_price_mail_recipients (email, is_active)
            VALUES (%s, 1)
            ON DUPLICATE KEY UPDATE is_active=1, last_error=''
            """,
            (normalized,),
        )
        db.commit()
    finally:
        db.close()
    return get_recipient_by_email(normalized) or {"email": normalized, "is_active": 1}


def set_recipient_active(recipient_id: int, is_active: bool) -> bool:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE mfu_ticket_price_mail_recipients SET is_active=%s WHERE id=%s",
            (1 if is_active else 0, int(recipient_id)),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def delete_recipient(recipient_id: int) -> bool:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM mfu_ticket_price_mail_recipients WHERE id=%s",
            (int(recipient_id),),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def get_recipient_by_email(email: str) -> dict | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, email, is_active, last_received_at, last_sent_at,
                   send_count, last_result, last_error, created_at, updated_at,
                   (
                     last_sent_at IS NULL
                     OR last_sent_at <= DATE_SUB(NOW(), INTERVAL %s MINUTE)
                   ) AS rate_limit_ok
              FROM mfu_ticket_price_mail_recipients
             WHERE email=%s
             LIMIT 1
            """,
            (RATE_LIMIT_MINUTES, normalized),
        )
        return cur.fetchone()
    finally:
        db.close()


def start_request(message_id: str, sender: str) -> int | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT IGNORE INTO mfu_ticket_price_mail_requests
                (message_id, envelope_sender, status)
            VALUES (%s, %s, 'processing')
            """,
            (str(message_id)[:500], str(sender)[:320]),
        )
        db.commit()
        return int(cur.lastrowid) if cur.rowcount else None
    finally:
        db.close()


def finish_request(
    request_id: int,
    *,
    status: str,
    item_count: int = 0,
    fetched_at: str = "",
    error: str = "",
) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE mfu_ticket_price_mail_requests
               SET status=%s, item_count=%s, fetched_at=%s, error=%s,
                   processed_at=NOW()
             WHERE id=%s
            """,
            (
                str(status)[:32],
                max(0, int(item_count or 0)),
                str(fetched_at or "")[:32],
                str(error or "")[:1000],
                int(request_id),
            ),
        )
        db.commit()
    finally:
        db.close()


def mark_recipient_received(recipient_id: int, *, result: str, error: str = "") -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE mfu_ticket_price_mail_recipients
               SET last_received_at=NOW(), last_result=%s, last_error=%s
             WHERE id=%s
            """,
            (str(result)[:32], str(error or "")[:1000], int(recipient_id)),
        )
        db.commit()
    finally:
        db.close()


def mark_recipient_sent(recipient_id: int) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE mfu_ticket_price_mail_recipients
               SET last_sent_at=NOW(), send_count=send_count+1,
                   last_result='sent', last_error=''
             WHERE id=%s
            """,
            (int(recipient_id),),
        )
        db.commit()
    finally:
        db.close()
