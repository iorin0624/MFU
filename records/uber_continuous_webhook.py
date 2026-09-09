from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from app.utils.db import get_db


TOKEN_PREFIX = "uws_"
RATE_LIMIT_ATTEMPTS = 10
RATE_LIMIT_WINDOW_MINUTES = 15


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(created_by: str) -> str:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE uber_continuous_webhook_tokens SET revoked_at=%s WHERE revoked_at IS NULL",
            (now,),
        )
        cur.execute(
            """
            INSERT INTO uber_continuous_webhook_tokens
                (token_hash, created_by, created_at)
            VALUES (%s, %s, %s)
            """,
            (_hash_token(raw), str(created_by or "")[:128], now),
        )
        db.commit()
        return raw
    finally:
        db.close()


def revoke_active_token() -> bool:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE uber_continuous_webhook_tokens SET revoked_at=%s WHERE revoked_at IS NULL",
            (datetime.now(),),
        )
        changed = int(cur.rowcount or 0) > 0
        db.commit()
        return changed
    finally:
        db.close()


def active_token_metadata() -> dict | None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, created_by, created_at, last_used_at, last_used_ip
            FROM uber_continuous_webhook_tokens
            WHERE revoked_at IS NULL
            ORDER BY id DESC LIMIT 1
            """
        )
        return cur.fetchone()
    finally:
        db.close()


def authenticate_token(token: str, ip_address: str) -> bool:
    if not token.startswith(TOKEN_PREFIX) or len(token) > 128:
        return False
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id FROM uber_continuous_webhook_tokens
            WHERE token_hash=%s AND revoked_at IS NULL
            LIMIT 1
            """,
            (_hash_token(token),),
        )
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            """
            UPDATE uber_continuous_webhook_tokens
            SET last_used_at=%s, last_used_ip=%s WHERE id=%s
            """,
            (datetime.now(), str(ip_address or "")[:64], row["id"]),
        )
        db.commit()
        return True
    finally:
        db.close()


def is_rate_limited(ip_address: str) -> bool:
    since = datetime.now() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM uber_continuous_webhook_audit
            WHERE ip_address=%s AND created_at >= %s
              AND action NOT IN ('issued', 'revoked')
            """,
            (str(ip_address or "")[:64], since),
        )
        row = cur.fetchone()
        count = int(row[0] if row else 0)
        return count >= RATE_LIMIT_ATTEMPTS
    finally:
        db.close()


def audit_request(
    action: str,
    *,
    success: bool,
    ip_address: str = "",
    user_agent: str = "",
    detail: str = "",
) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO uber_continuous_webhook_audit
                (action, success, ip_address, user_agent, detail, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                str(action or "")[:32], 1 if success else 0,
                str(ip_address or "")[:64], str(user_agent or "")[:255],
                str(detail or "")[:500], datetime.now(),
            ),
        )
        db.commit()
    finally:
        db.close()
