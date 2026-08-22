"""Shared, scope-safe deletion for normal uploads.

The parent ``uploads`` row and every layer-upload artifact are intentionally
preserved. Both manual deletion and expiry deletion call this module.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from app.utils.db import get_db
from app.utils.upload_download_history import purge_upload_download_history


UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LAYER_DIRECTORY_NAME = "layer_uploads"


class UnsafeUploadPath(ValueError):
    """Raised when a UUID cannot resolve to a direct normal-upload child."""


def resolve_normal_upload_directory(storage_root: str | Path, uuid: str) -> Path:
    root = Path(storage_root).resolve()
    candidate = (uuid or "").strip()
    if (
        not UPLOAD_ID_PATTERN.fullmatch(candidate)
        or candidate == LAYER_DIRECTORY_NAME
        or Path(candidate).name != candidate
    ):
        raise UnsafeUploadPath(f"unsafe upload UUID: {candidate!r}")

    unresolved = root / candidate
    if unresolved.is_symlink():
        raise UnsafeUploadPath(f"upload path must not be a symbolic link: {unresolved}")
    target = unresolved.resolve()
    if target.parent != root or target.name != candidate:
        raise UnsafeUploadPath(f"upload path escaped storage root: {target}")
    return target


def delete_normal_upload(
    *,
    upload_id: int,
    uuid: str,
    storage_root: str | Path,
    db_factory: Callable = get_db,
) -> dict:
    """Delete only the normal-upload scope and soft-delete its list entry.

    Filesystem removal is attempted before the database marker is committed.
    The operation is idempotent, so a retry after partial completion is safe.
    """

    target = resolve_normal_upload_directory(storage_root, uuid)
    removed_directory = False
    if target.exists():
        if not target.is_dir():
            raise UnsafeUploadPath(f"normal upload target is not a directory: {target}")
        shutil.rmtree(target)
        removed_directory = True

    db = db_factory()
    cursor = db.cursor()
    try:
        deleted_download_history = purge_upload_download_history(
            upload_id,
            db=db,
            cursor=cursor,
        )
        cursor.execute("DELETE FROM files WHERE upload_id = %s", (upload_id,))
        deleted_files = max(0, int(cursor.rowcount or 0))
        cursor.execute("DELETE FROM messages WHERE uuid = %s", (uuid,))
        deleted_messages = max(0, int(cursor.rowcount or 0))
        cursor.execute("DELETE FROM upload_email_otps WHERE upload_id = %s", (upload_id,))
        deleted_email_otps = max(0, int(cursor.rowcount or 0))
        cursor.execute(
            """
            UPDATE uploads
               SET upload_deleted_at = COALESCE(upload_deleted_at, NOW())
             WHERE id = %s AND uuid = %s
            """,
            (upload_id, uuid),
        )
        updated_uploads = max(0, int(cursor.rowcount or 0))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "uuid": uuid,
        "removed_directory": removed_directory,
        "deleted_download_history": deleted_download_history,
        "deleted_files": deleted_files,
        "deleted_messages": deleted_messages,
        "deleted_email_otps": deleted_email_otps,
        "updated_uploads": updated_uploads,
    }
