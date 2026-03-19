from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any

TOKEN_PREFIX = "mfu_pat_"
TOKEN_CORE_BYTES = 32
TOKEN_PREVIEW_HEAD = 4
TOKEN_PREVIEW_TAIL = 4


def _hash_secret(raw_value: str) -> str:
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _build_token_preview(token_prefix: str, token_suffix: str) -> str:
    return f"{token_prefix}...{token_suffix}"


def generate_payout_access_token() -> tuple[str, str, str, str]:
    raw_body = secrets.token_urlsafe(TOKEN_CORE_BYTES)
    token = f"{TOKEN_PREFIX}{raw_body}"
    token_hash = _hash_secret(token)
    token_prefix = token[: len(TOKEN_PREFIX) + TOKEN_PREVIEW_HEAD]
    token_suffix = token[-TOKEN_PREVIEW_TAIL:]
    return token, token_hash, token_prefix, token_suffix


def create_payout_access_token(
    db,
    memo: str,
    issued_via: str = "admin_ui",
    issued_by_app: str | None = None,
    created_by_admin: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    token, token_hash, token_prefix, token_suffix = generate_payout_access_token()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            INSERT INTO mfu_payout_access_token (
                token_hash,
                token_prefix,
                token_suffix,
                memo,
                is_active,
                access_count,
                last_accessed_at,
                last_access_ip,
                issued_via,
                issued_by_app,
                created_by_admin,
                expires_at,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, 1, 0, NULL, NULL, %s, %s, %s, %s, NOW(), NOW())
            """,
            (
                token_hash,
                token_prefix,
                token_suffix,
                memo,
                issued_via,
                issued_by_app,
                created_by_admin,
                expires_at,
            ),
        )
        token_id = cursor.lastrowid
        db.commit()
        cursor.execute(
            """
            SELECT id, memo, issued_via, issued_by_app, created_by_admin, expires_at, created_at, updated_at
            FROM mfu_payout_access_token
            WHERE id = %s
            LIMIT 1
            """,
            (token_id,),
        )
        created = cursor.fetchone() or {}
    finally:
        cursor.close()

    created["token"] = token
    created["token_hash"] = token_hash
    created["token_prefix"] = token_prefix
    created["token_suffix"] = token_suffix
    created["token_preview"] = _build_token_preview(token_prefix, token_suffix)
    return created


def verify_payout_access_token(db, raw_token: str) -> dict[str, Any] | None:
    normalized = (raw_token or "").strip()
    if not normalized or not normalized.startswith(TOKEN_PREFIX):
        return None

    token_prefix = normalized[: len(TOKEN_PREFIX) + TOKEN_PREVIEW_HEAD]
    token_suffix = normalized[-TOKEN_PREVIEW_TAIL:]
    token_hash = _hash_secret(normalized)

    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, token_hash, token_prefix, token_suffix, memo, is_active, access_count,
                   last_accessed_at, last_access_ip, issued_via, issued_by_app,
                   created_by_admin, expires_at, created_at, updated_at
            FROM mfu_payout_access_token
            WHERE token_prefix = %s
              AND token_suffix = %s
              AND is_active = 1
              AND (expires_at IS NULL OR expires_at >= NOW())
            ORDER BY id DESC
            """,
            (token_prefix, token_suffix),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    for row in rows:
        if row.get("token_hash") == token_hash:
            return row
    return None


def get_payout_access_token_by_id(db, token_id: int) -> dict[str, Any] | None:
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, token_hash, token_prefix, token_suffix, memo, is_active, access_count,
                   last_accessed_at, last_access_ip, issued_via, issued_by_app,
                   created_by_admin, expires_at, created_at, updated_at
            FROM mfu_payout_access_token
            WHERE id = %s
            LIMIT 1
            """,
            (token_id,),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def is_payout_access_token_usable(token_row: dict[str, Any] | None) -> bool:
    if not token_row or not token_row.get("is_active"):
        return False
    expires_at = token_row.get("expires_at")
    if expires_at and expires_at < datetime.now():
        return False
    return True


def touch_payout_access_token_usage(db, token_id: int, ip_address: str | None) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            UPDATE mfu_payout_access_token
            SET access_count = access_count + 1,
                last_accessed_at = NOW(),
                last_access_ip = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (ip_address, token_id),
        )
        db.commit()
    finally:
        cursor.close()


def toggle_payout_access_token_active(db, token_id: int, is_active: bool) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            UPDATE mfu_payout_access_token
            SET is_active = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (1 if is_active else 0, token_id),
        )
        db.commit()
    finally:
        cursor.close()


def verify_payout_token_api_key(db, raw_api_key: str) -> dict[str, Any] | None:
    normalized = (raw_api_key or "").strip()
    if not normalized:
        return None

    api_key_hash = _hash_secret(normalized)
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, app_name, api_key_hash, is_active, last_used_at, last_used_ip, created_at, updated_at
            FROM mfu_payout_token_api_client
            WHERE is_active = 1
            """
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    for row in rows:
        if row.get("api_key_hash") == api_key_hash:
            return row
    return None


def touch_payout_token_api_client_usage(db, client_id: int, ip_address: str | None) -> None:
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            UPDATE mfu_payout_token_api_client
            SET last_used_at = NOW(),
                last_used_ip = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (ip_address, client_id),
        )
        db.commit()
    finally:
        cursor.close()
