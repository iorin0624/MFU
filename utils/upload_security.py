"""アップロード時のセキュリティ検証ユーティリティ。"""

from __future__ import annotations

import bcrypt
import base64
import hashlib
import hmac
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
    ".heic": "image/heif-bmff",
    ".heif": "image/heif-bmff",
    ".cr2": "image/x-canon-cr2",
    ".cr3": "image/x-canon-cr3",
    ".nef": "image/tiff-raw",
    ".nrw": "image/tiff-raw",
    ".arw": "image/tiff-raw",
    ".dng": "image/tiff-raw",
    ".zip": "application/zip",
}

AUTH_NONE = "none"
AUTH_PASSWORD = "password"
AUTH_ACCESS_TOKEN = "access_token"
AUTH_EMAIL_OTP = "email_otp"
UPLOAD_AUTH_METHODS = {AUTH_NONE, AUTH_PASSWORD, AUTH_ACCESS_TOKEN, AUTH_EMAIL_OTP}
UPLOAD_ACCESS_TOKEN_PREFIX = "mfu_view_"

VIEW_AUTH_SESSION_KEY = "view_auth_uuids"
VIEW_AUTH_VERSION_SESSION_KEY = "view_auth_versions"
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
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        compatible = head[8:64].lower()
        if brand in {b"crx ", b"cr3 "} or b"crx " in compatible:
            return "image/x-canon-cr3"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heif-bmff"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        if len(head) >= 12 and head[8:12] == b"CR\x02\x00":
            return "image/x-canon-cr2"
        return "image/tiff-raw"
    return "application/octet-stream"


def normalize_upload_auth_method(value: object, *, require_password: object = False) -> str:
    method = str(value or "").strip().lower()
    if method in UPLOAD_AUTH_METHODS:
        return method
    password_enabled = str(require_password or "").strip().lower() in {
        "1", "true", "t", "yes", "y", "on"
    }
    return AUTH_PASSWORD if password_enabled else AUTH_NONE


def _upload_access_token_secret() -> bytes:
    value = (
        current_app.config.get("UPLOAD_ACCESS_TOKEN_SECRET")
        or os.environ.get("UPLOAD_ACCESS_TOKEN_SECRET")
        or current_app.secret_key
    )
    if not value:
        raise RuntimeError("UPLOAD_ACCESS_TOKEN_SECRET or Flask SECRET_KEY is required")
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def generate_upload_access_token(uuid: str) -> str:
    """Derive a stable, unguessable per-upload bearer token without storing it raw."""
    digest = hmac.new(
        _upload_access_token_secret(),
        f"mfu-upload-access-v1:{str(uuid or '').strip()}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return UPLOAD_ACCESS_TOKEN_PREFIX + encoded


def hash_upload_access_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def create_upload_access_token_hash(uuid: str, auth_method: object) -> str | None:
    if normalize_upload_auth_method(auth_method) != AUTH_ACCESS_TOKEN:
        return None
    return hash_upload_access_token(generate_upload_access_token(uuid))


def verify_upload_access_token(upload: dict | None, token: str) -> bool:
    if upload_auth_method(upload) != AUTH_ACCESS_TOKEN:
        return False
    stored_hash = str((upload or {}).get("access_token_hash") or "").strip().lower()
    if not stored_hash or not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return False
    return hmac.compare_digest(stored_hash, hash_upload_access_token(token))


def build_upload_view_url(public_base: str, upload: dict) -> str:
    base = str(public_base or "").rstrip("/")
    uuid = str((upload or {}).get("uuid") or "").strip()
    if upload_auth_method(upload) == AUTH_ACCESS_TOKEN:
        token = generate_upload_access_token(uuid)
        return f"{base}/view/{uuid}/access#{token}"
    return f"{base}/view/{uuid}"


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
        if key in {VIEW_AUTH_SESSION_KEY, VIEW_AUTH_VERSION_SESSION_KEY}:
            continue
        if current_uuid and key == f"view_auth_{current_uuid}":
            continue
        session.pop(key, None)


def grant_view_auth(uuid: str, auth_version: int = 0) -> None:
    cleanup_legacy_view_auth_keys(current_uuid=uuid)
    allowed = session.get(VIEW_AUTH_SESSION_KEY) or []
    if uuid in allowed:
        session[VIEW_AUTH_SESSION_KEY] = allowed
    else:
        session[VIEW_AUTH_SESSION_KEY] = (allowed + [uuid])[-VIEW_AUTH_MAX_ITEMS:]
    versions = dict(session.get(VIEW_AUTH_VERSION_SESSION_KEY) or {})
    versions[uuid] = int(auth_version or 0)
    allowed_now = set(session.get(VIEW_AUTH_SESSION_KEY) or [])
    session[VIEW_AUTH_VERSION_SESSION_KEY] = {
        key: value for key, value in versions.items() if key in allowed_now
    }


def has_view_auth(uuid: str, auth_version: int | None = None) -> bool:
    allowed = session.get(VIEW_AUTH_SESSION_KEY) or []
    if uuid in allowed:
        if auth_version is None:
            return True
        versions = session.get(VIEW_AUTH_VERSION_SESSION_KEY) or {}
        return int(versions.get(uuid, -1)) == int(auth_version or 0)

    legacy_key = f"view_auth_{uuid}"
    if session.get(legacy_key):
        grant_view_auth(uuid, auth_version or 0)
        session.pop(legacy_key, None)
        return True
    return False


def hash_upload_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def ensure_upload_password_schema() -> None:
    """
    uploads の認証列と files の公開状態列を追加し、
    legacy の平文 password を安全にハッシュ移行する。
    再実行しても壊れないようにする。
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SHOW COLUMNS FROM uploads")
        columns = {row["Field"] for row in cur.fetchall()}
        if "password_hash" not in columns:
            cur.execute("ALTER TABLE uploads ADD COLUMN password_hash VARCHAR(255) NULL AFTER password")
        if "auth_method" not in columns:
            cur.execute(
                "ALTER TABLE uploads ADD COLUMN auth_method VARCHAR(20) NOT NULL DEFAULT 'none' AFTER password_hash"
            )
        if "otp_email" not in columns:
            cur.execute("ALTER TABLE uploads ADD COLUMN otp_email VARCHAR(320) NULL AFTER auth_method")
        if "auth_version" not in columns:
            cur.execute(
                "ALTER TABLE uploads ADD COLUMN auth_version BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER otp_email"
            )
        if "access_token_hash" not in columns:
            cur.execute(
                "ALTER TABLE uploads ADD COLUMN access_token_hash CHAR(64) NULL AFTER auth_version"
            )
        if "upload_deleted_at" not in columns:
            cur.execute("ALTER TABLE uploads ADD COLUMN upload_deleted_at DATETIME NULL AFTER created_at")
        if "layer_deleted_at" not in columns:
            cur.execute("ALTER TABLE uploads ADD COLUMN layer_deleted_at DATETIME NULL AFTER upload_deleted_at")
        if "visibility_version" not in columns:
            cur.execute(
                "ALTER TABLE uploads "
                "ADD COLUMN visibility_version BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER layer_deleted_at"
            )

        cur.execute("SHOW COLUMNS FROM upload_modes")
        mode_columns = {row["Field"] for row in cur.fetchall()}
        if "auth_method" not in mode_columns:
            cur.execute(
                "ALTER TABLE upload_modes ADD COLUMN auth_method VARCHAR(20) NULL AFTER require_password"
            )
        cur.execute(
            """
            UPDATE upload_modes
               SET auth_method=CASE WHEN require_password=1 THEN 'password' ELSE 'none' END
             WHERE auth_method IS NULL OR auth_method NOT IN ('none','password','access_token','email_otp')
            """
        )
        cur.execute(
            """
            UPDATE uploads
               SET auth_method='password'
             WHERE auth_method='none'
               AND (COALESCE(password_hash, '') <> '' OR COALESCE(password, '') <> '')
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_email_otps (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                upload_id BIGINT NOT NULL,
                email VARCHAR(320) NOT NULL,
                code_hash CHAR(64) NOT NULL,
                request_ip VARCHAR(64) NULL,
                attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
                expires_at DATETIME NOT NULL,
                used_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_upload_email_otps_upload_created (upload_id, created_at),
                INDEX idx_upload_email_otps_ip_created (request_ip, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

        cur.execute("SHOW COLUMNS FROM files")
        file_columns = {row["Field"] for row in cur.fetchall()}
        if "is_hidden" not in file_columns:
            cur.execute(
                "ALTER TABLE files "
                "ADD COLUMN is_hidden TINYINT(1) NOT NULL DEFAULT 0 AFTER filename"
            )
        if "hidden_at" not in file_columns:
            cur.execute(
                "ALTER TABLE files "
                "ADD COLUMN hidden_at DATETIME NULL AFTER is_hidden"
            )
        if "hidden_by" not in file_columns:
            cur.execute(
                "ALTER TABLE files "
                "ADD COLUMN hidden_by VARCHAR(255) NULL AFTER hidden_at"
            )
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


def upload_auth_method(upload: dict | None) -> str:
    if not upload:
        return AUTH_NONE
    return normalize_upload_auth_method(
        upload.get("auth_method"),
        require_password=upload_password_required(upload),
    )


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
    if has_view_auth_func and has_view_auth_func(upload):
        return True
    return upload_auth_method(upload) == AUTH_NONE


def is_upload_owner(upload: dict | None) -> bool:
    """The uploader is the signed-in MFU account recorded on the upload."""
    if not upload:
        return False
    username = str(session.get("user") or "")
    owner = str(upload.get("username") or "")
    return bool(username and owner and username == owner)


def upload_file_is_hidden(file_row: dict | None) -> bool:
    if not file_row:
        return False
    value = file_row.get("is_hidden")
    return str(value or "0").strip().lower() in {"1", "true", "t", "yes", "y"}


def can_preview_upload_file(upload: dict | None, file_row: dict | None) -> bool:
    """Hidden originals remain previewable only by their uploader."""
    if not upload or not file_row:
        return False
    return not upload_file_is_hidden(file_row) or is_upload_owner(upload)


def fetch_upload_file_record(upload_id: int, filename: str) -> Optional[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, upload_id, filename, is_hidden, hidden_at, hidden_by
              FROM files
             WHERE upload_id=%s AND filename=%s
             LIMIT 1
            """,
            (upload_id, filename),
        )
        return cur.fetchone()
    finally:
        db.close()


def fetch_upload_thumbnail_source(upload_id: int, thumbnail_name: str) -> Optional[dict]:
    """
    Resolve a thumbnail back to its original DB row.

    WebP thumbnails use the original stem. Existing uploads may theoretically
    contain the same stem with different extensions, so a public candidate wins;
    otherwise the first stable file id is returned for uploader-only preview.
    """
    thumb_path = Path(thumbnail_name or "")
    if not thumb_path.name:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if thumb_path.suffix.lower() != ".webp":
            cur.execute(
                """
                SELECT id, upload_id, filename, is_hidden, hidden_at, hidden_by
                  FROM files
                 WHERE upload_id=%s AND filename=%s
                 LIMIT 1
                """,
                (upload_id, thumb_path.name),
            )
        else:
            cur.execute(
                """
                SELECT id, upload_id, filename, is_hidden, hidden_at, hidden_by
                  FROM files
                 WHERE upload_id=%s
                   AND LEFT(
                         filename,
                         CHAR_LENGTH(filename) - CHAR_LENGTH(SUBSTRING_INDEX(filename, '.', -1)) - 1
                       )=%s
                 ORDER BY is_hidden ASC, id ASC
                 LIMIT 1
                """,
                (upload_id, thumb_path.stem),
            )
        return cur.fetchone()
    finally:
        db.close()


def current_upload_visibility_version(upload: dict | None) -> int:
    try:
        return max(0, int((upload or {}).get("visibility_version") or 0))
    except (TypeError, ValueError):
        return 0


def fetch_upload_access_record(uuid: str) -> Optional[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM uploads WHERE uuid=%s AND upload_deleted_at IS NULL",
            (uuid,),
        )
        upload = cur.fetchone()
        if not upload:
            return None

        cur.execute(
            """
            SELECT require_password, auth_method, generate_thumbnails
              FROM upload_modes
             WHERE username=%s AND mode=%s
             LIMIT 1
            """,
            (upload["username"], upload["mode"]),
        )
        mode_row = cur.fetchone() or {}
        upload["require_password"] = mode_row.get("require_password")
        if not upload.get("auth_method"):
            upload["auth_method"] = normalize_upload_auth_method(
                mode_row.get("auth_method"),
                require_password=mode_row.get("require_password"),
            )
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
