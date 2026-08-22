from __future__ import annotations

import hashlib


def social_identity_hash(provider: str, social_id: str) -> str:
    normalized_provider = (provider or "").strip().lower()
    normalized_social_id = (social_id or "").strip()
    if not normalized_provider or not normalized_social_id:
        return ""
    value = f"{normalized_provider}\0{normalized_social_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def lock_deleted_identity(
    cur,
    *,
    provider: str,
    social_id: str,
    user_id: int,
    deleted_by: str,
    reason: str | None = None,
) -> str:
    normalized_provider = (provider or "").strip().lower()
    identity_hash = social_identity_hash(normalized_provider, social_id)
    if not identity_hash:
        raise ValueError("provider and social_id are required")
    cur.execute(
        """
        INSERT INTO external_login_deleted_identity
          (provider, identity_hash, original_user_id, deleted_at, deleted_by, deletion_reason)
        VALUES (%s, %s, %s, NOW(), %s, %s)
        ON DUPLICATE KEY UPDATE
          original_user_id=VALUES(original_user_id),
          deleted_at=NOW(),
          deleted_by=VALUES(deleted_by),
          deletion_reason=VALUES(deletion_reason)
        """,
        (
            normalized_provider,
            identity_hash,
            int(user_id),
            (deleted_by or "admin")[:80],
            (reason or None),
        ),
    )
    return identity_hash


def get_deleted_identity_lock(
    cur,
    *,
    provider: str,
    social_id: str,
) -> tuple[bool, int | None]:
    normalized_provider = (provider or "").strip().lower()
    identity_hash = social_identity_hash(normalized_provider, social_id)
    if not identity_hash:
        return False, None
    cur.execute(
        """
        SELECT original_user_id
          FROM external_login_deleted_identity
         WHERE provider=%s
           AND identity_hash=%s
         LIMIT 1
        """,
        (normalized_provider, identity_hash),
    )
    row = cur.fetchone()
    if row is None:
        return False, None

    if isinstance(row, dict):
        original_user_id = row.get("original_user_id")
    else:
        original_user_id = row[0]
    return True, int(original_user_id) if original_user_id is not None else None


def is_deleted_identity_locked(cur, *, provider: str, social_id: str) -> bool:
    locked, _original_user_id = get_deleted_identity_lock(
        cur,
        provider=provider,
        social_id=social_id,
    )
    return locked
