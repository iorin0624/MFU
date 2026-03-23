"""アップロード時のセキュリティ検証ユーティリティ。"""

from __future__ import annotations

import bcrypt
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from flask import current_app, session
from werkzeug.utils import safe_join

from app.utils.db import get_db


# 必要に応じて拡張して利用する。
DEFAULT_ALLOWED_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

VIEW_AUTH_SESSION_KEY = "view_auth_uuids"
VIEW_AUTH_MAX_ITEMS = 50
_UUID32_RE = re.compile(r"^[0-9a-f]{32}$")

# 代表的な「危険な実行系拡張子」
DENY_EXTENSION_SEGMENTS = {
    "php",
    "phtml",
    "php3",
    "php4",
    "php5",
    "phar",
    "cgi",
    "pl",
    "py",
    "rb",
    "sh",
    "bash",
    "exe",
    "dll",
    "so",
    "js",
    "jsp",
    "asp",
    "aspx",
}


def sanitize_filename(name: str, used_names: set[str]) -> str:
    """ファイル名を安全化し、重複時は suffix を付与してユニーク化する。"""
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", name or "")
    cleaned = os.path.basename(cleaned).strip() or "unnamed"
    root, ext = os.path.splitext(cleaned)
    ext = ext.lower()
    candidate = f"{root}{ext}"

    seq = 2
    while candidate in used_names:
        candidate = f"{root}_{seq}{ext}"
        seq += 1

    used_names.add(candidate)
    return candidate


def has_double_extension(filename: str, denied_segments: Iterable[str] | None = None) -> bool:
    """shell.php.jpg のような二重拡張子を検出する。"""
    segments = [seg.lower() for seg in Path(filename).name.split(".") if seg]
    if len(segments) <= 2:
        return False

    denied = set(denied_segments or DENY_EXTENSION_SEGMENTS)
    return any(seg in denied for seg in segments[1:-1])


def detect_mime_from_bytes(head: bytes) -> str:
    """先頭バイトから簡易 MIME 判定する。"""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def validate_upload_file(
    *,
    filename: str,
    header_mime: str,
    detected_mime: str,
    allowed_extensions: dict[str, str],
) -> tuple[bool, str]:
    """拡張子・二重拡張子・MIME整合をチェックする。"""
    lowered = (filename or "").lower()
    ext = os.path.splitext(lowered)[1]

    if not ext or ext not in allowed_extensions:
        return False, f"許可されていない拡張子です: {ext or '(なし)'}"

    if has_double_extension(lowered):
        return False, "二重拡張子ファイルは拒否されました"

    expected_mime = allowed_extensions[ext]
    normalized_header = (header_mime or "").split(";")[0].strip().lower()

    # Header は参考程度。実データ判定を最優先する。
    if detected_mime != expected_mime:
        return False, (
            "MIME 不一致: "
            f"expected={expected_mime}, detected={detected_mime}, header={normalized_header or 'N/A'}"
        )

    return True, "ok"


def cleanup_legacy_view_auth_keys(current_uuid: str | None = None) -> None:
    """旧形式の session['view_auth_<uuid>'] を削除する。"""
    for key in list(session.keys()):
        if not key.startswith("view_auth_"):
            continue
        if current_uuid and key == f"view_auth_{current_uuid}":
            continue
        session.pop(key, None)


def grant_view_auth(uuid: str) -> None:
    cleanup_legacy_view_auth_keys(current_uuid=uuid)
    allowed = session.get(VIEW_AUTH_SESSION_KEY) or []
    if uuid in allowed:
        return
    session[VIEW_AUTH_SESSION_KEY] = (allowed + [uuid])[-VIEW_AUTH_MAX_ITEMS:]


def has_view_auth(uuid: str) -> bool:
    allowed = session.get(VIEW_AUTH_SESSION_KEY) or []
    if uuid in allowed:
        return True

    legacy_key = f"view_auth_{uuid}"
    if session.get(legacy_key):
        grant_view_auth(uuid)
        session.pop(legacy_key, None)
        return True
    return False


def hash_upload_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def ensure_upload_password_schema() -> None:
    """
    uploads.password_hash を追加し、legacy の平文 password を安全にハッシュ移行する。
    再実行しても壊れないようにする。
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SHOW COLUMNS FROM uploads")
        columns = {row["Field"] for row in cur.fetchall()}
        if "password_hash" not in columns:
            cur.execute("ALTER TABLE uploads ADD COLUMN password_hash VARCHAR(255) NULL AFTER password")
            db.commit()

        if "password" not in columns:
            return

        cur.execute(
            """
            SELECT id, password
              FROM uploads
             WHERE COALESCE(password_hash, '') = ''
               AND COALESCE(password, '') <> ''
            """
        )
        legacy_rows = cur.fetchall()
        for row in legacy_rows:
            cur.execute(
                "UPDATE uploads SET password_hash=%s, password='' WHERE id=%s",
                (hash_upload_password(row["password"]), row["id"]),
            )
        if legacy_rows:
            db.commit()
    finally:
        db.close()


def migrate_upload_password_if_needed(upload: dict) -> Optional[str]:
    legacy_password = (upload.get("password") or "").strip()
    if upload.get("password_hash") or not legacy_password or not upload.get("id"):
        return upload.get("password_hash")

    password_hash = hash_upload_password(legacy_password)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE uploads SET password_hash=%s, password='' WHERE id=%s",
            (password_hash, upload["id"]),
        )
        db.commit()
    finally:
        db.close()
    upload["password_hash"] = password_hash
    upload["password"] = ""
    return password_hash


def upload_password_required(upload: dict | None) -> bool:
    if not upload:
        return False
    require_password = str(upload.get("require_password") or "").lower() in ("1", "true", "t", "yes", "y")
    has_secret = bool((upload.get("password_hash") or "").strip() or (upload.get("password") or "").strip())
    return require_password or has_secret


def verify_upload_password(upload: dict, input_password: str) -> bool:
    if not upload_password_required(upload):
        return True

    candidate = (input_password or "").encode("utf-8")
    password_hash = (upload.get("password_hash") or "").strip()
    if password_hash:
        try:
            return bcrypt.checkpw(candidate, password_hash.encode("utf-8"))
        except ValueError:
            return False

    legacy_password = (upload.get("password") or "").strip()
    if not legacy_password:
        return False
    if input_password != legacy_password:
        return False
    migrate_upload_password_if_needed(upload)
    return True


def can_access_upload_record(upload: dict | None, *, has_view_auth_func=None) -> bool:
    if not upload:
        return False

    username = session.get("user")
    if username == "admin":
        return True
    if username and username == upload.get("username"):
        return True
    if has_view_auth_func and has_view_auth_func(upload.get("uuid")):
        return True
    return not upload_password_required(upload)


def fetch_upload_access_record(uuid: str) -> Optional[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM uploads WHERE uuid=%s", (uuid,))
        upload = cur.fetchone()
        if not upload:
            return None

        cur.execute(
            """
            SELECT require_password, generate_thumbnails
              FROM upload_modes
             WHERE username=%s AND mode=%s
             LIMIT 1
            """,
            (upload["username"], upload["mode"]),
        )
        mode_row = cur.fetchone() or {}
        upload["require_password"] = mode_row.get("require_password")
        upload["generate_thumbnails"] = mode_row.get("generate_thumbnails")
        return upload
    finally:
        db.close()


def resolve_upload_subpath(subpath: str, *, allow_zip: bool = True) -> Optional[dict]:
    normalized = (subpath or "").strip().lstrip("/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/"):]

    parts = normalized.split("/", 2)
    allowed_kinds = {"original", "thumb"}
    if allow_zip:
        allowed_kinds.add("zip")
    if len(parts) != 3:
        return None

    uuid, kind, filename = parts
    if not (_UUID32_RE.fullmatch(uuid or "") and kind in allowed_kinds and filename):
        return None

    storage_root = current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")
    full = safe_join(storage_root, uuid, kind, filename)
    if not full:
        return None

    target = Path(full).resolve()
    base_dir = Path(storage_root).resolve()
    try:
        if not target.is_relative_to(base_dir):
            return None
    except AttributeError:
        base_prefix = str(base_dir) + os.sep
        if str(target) != str(base_dir) and not str(target).startswith(base_prefix):
            return None

    return {
        "uuid": uuid,
        "kind": kind,
        "filename": filename,
        "target": target,
    }
