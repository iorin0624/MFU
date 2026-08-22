"""Login-free mobile download jobs for saving selected JPEGs to Photos."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import (
    Blueprint,
    current_app,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

from app.utils.db import get_db
from app.utils.upload_security import (
    can_access_upload_record,
    fetch_upload_access_record,
    fetch_upload_file_record,
    has_view_auth,
    resolve_upload_subpath,
    upload_file_is_hidden,
)
from app.utils.upload_download_history import (
    mark_upload_download_status,
    record_upload_download,
    request_ip as download_request_ip,
)


mobile_download_bp = Blueprint("mobile_download", __name__)

APP_NAME = "MFU Download"
SHORTCUT_NAME = "MFU写真保存"
ALBUM_NAME = "MFU"
ANDROID_PACKAGE = "jp.iori0624.mfudownload"
IOS_BUNDLE_ID = "jp.iori0624.mfudownload"
LAUNCH_TOKEN_MINUTES = 10
SESSION_TOKEN_HOURS = 1
MAX_JOB_FILES = 1000
LAUNCH_TOKEN_PREFIX = "mfu_launch_"
ACCESS_TOKEN_PREFIX = "mfu_dl_"

_schema_ready = False

DEFAULT_SHORTCUT_SETTINGS = {
    "is_enabled": True,
    "shortcut_name": SHORTCUT_NAME,
    "popup_title": "MFU写真保存ショートカットが必要です",
    "popup_body": "選択した写真や動画を保存するには、MFU写真保存ショートカットを追加してください。",
    "install_steps": "「ショートカットを入手」を押して追加したあと、この画面へ戻って「もう一度起動」を押してください。",
    "download_button_label": "ショートカットを入手",
    "download_url": "",
    "detection_timeout_seconds": 10,
}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _image_id(upload_uuid: str, filename: str) -> str:
    value = f"{upload_uuid}\0{filename}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def _shortcut_url(launch_url: str, shortcut_name: str = SHORTCUT_NAME) -> str:
    return "shortcuts://run-shortcut?" + urlencode(
        {"name": shortcut_name or SHORTCUT_NAME, "input": "text", "text": launch_url}
    )


def _bearer_token() -> str:
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_download_jobs (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              upload_uuid CHAR(32) NOT NULL,
              upload_title VARCHAR(255) NOT NULL DEFAULT '',
              files_json LONGTEXT NOT NULL,
              launch_token_hash CHAR(64) NOT NULL,
              access_token_hash CHAR(64) NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              launch_expires_at DATETIME NOT NULL,
              exchanged_at DATETIME NULL,
              session_expires_at DATETIME NULL,
              last_accessed_at DATETIME NULL,
              completed_at DATETIME NULL,
              revoked_at DATETIME NULL,
              client_platform VARCHAR(32) NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_mobile_download_launch (launch_token_hash),
              UNIQUE KEY uq_mobile_download_access (access_token_hash),
              KEY idx_mobile_download_upload (upload_uuid),
              KEY idx_mobile_download_launch_expiry (launch_expires_at),
              KEY idx_mobile_download_session_expiry (session_expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM mobile_download_jobs")
        column_rows = cur.fetchall() or []
        columns = {
            str(row[0] if isinstance(row, tuple) else row.get("Field")): row
            for row in column_rows
        }
        if "source_type" not in columns:
            cur.execute(
                "ALTER TABLE mobile_download_jobs "
                "ADD COLUMN source_type VARCHAR(32) NOT NULL DEFAULT 'upload' AFTER id"
            )
        if "album_id" not in columns:
            cur.execute(
                "ALTER TABLE mobile_download_jobs "
                "ADD COLUMN album_id CHAR(36) NULL AFTER upload_title"
            )
        if "child_id" not in columns:
            cur.execute(
                "ALTER TABLE mobile_download_jobs "
                "ADD COLUMN child_id CHAR(36) NULL AFTER album_id"
            )
        if "authorization_json" not in columns:
            cur.execute(
                "ALTER TABLE mobile_download_jobs "
                "ADD COLUMN authorization_json TEXT NULL AFTER child_id"
            )
        upload_column = columns.get("upload_uuid")
        upload_nullable = (
            str(upload_column[2] if isinstance(upload_column, tuple) else upload_column.get("Null"))
            .strip()
            .upper()
        )
        if upload_nullable != "YES":
            cur.execute(
                "ALTER TABLE mobile_download_jobs MODIFY upload_uuid CHAR(32) NULL"
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mobile_download_shortcut_settings (
              id TINYINT UNSIGNED NOT NULL,
              is_enabled TINYINT(1) NOT NULL DEFAULT 1,
              shortcut_name VARCHAR(128) NOT NULL DEFAULT 'MFU写真保存',
              popup_title VARCHAR(255) NOT NULL DEFAULT 'MFU写真保存ショートカットが必要です',
              popup_body TEXT NOT NULL,
              install_steps TEXT NOT NULL,
              download_button_label VARCHAR(128) NOT NULL DEFAULT 'ショートカットを入手',
              download_url VARCHAR(2048) NOT NULL DEFAULT '',
              detection_timeout_seconds SMALLINT UNSIGNED NOT NULL DEFAULT 10,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              updated_by VARCHAR(191) NOT NULL DEFAULT '',
              PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            INSERT IGNORE INTO mobile_download_shortcut_settings
              (id, is_enabled, shortcut_name, popup_title, popup_body,
               install_steps, download_button_label, download_url,
               detection_timeout_seconds, updated_by)
            VALUES (1, 1, %s, %s, %s, %s, %s, '', 10, 'system')
            """,
            (
                DEFAULT_SHORTCUT_SETTINGS["shortcut_name"],
                DEFAULT_SHORTCUT_SETTINGS["popup_title"],
                DEFAULT_SHORTCUT_SETTINGS["popup_body"],
                DEFAULT_SHORTCUT_SETTINGS["install_steps"],
                DEFAULT_SHORTCUT_SETTINGS["download_button_label"],
            ),
        )
        db.commit()
    finally:
        db.close()
    _schema_ready = True


def _get_shortcut_settings() -> dict:
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT is_enabled, shortcut_name, popup_title, popup_body,
                   install_steps, download_button_label, download_url,
                   detection_timeout_seconds, updated_at, updated_by
              FROM mobile_download_shortcut_settings
             WHERE id=1
             LIMIT 1
            """
        )
        row = cur.fetchone() or {}
    finally:
        db.close()
    settings = dict(DEFAULT_SHORTCUT_SETTINGS)
    settings.update(row)
    settings["is_enabled"] = bool(settings.get("is_enabled"))
    settings["detection_timeout_seconds"] = max(
        3, min(30, int(settings.get("detection_timeout_seconds") or 10))
    )
    return settings


def _public_shortcut_settings(settings: dict | None = None) -> dict:
    settings = settings or _get_shortcut_settings()
    return {
        "enabled": bool(settings.get("is_enabled")),
        "shortcut_name": str(settings.get("shortcut_name") or SHORTCUT_NAME),
        "popup_title": str(settings.get("popup_title") or ""),
        "popup_body": str(settings.get("popup_body") or ""),
        "install_steps": str(settings.get("install_steps") or ""),
        "download_button_label": str(
            settings.get("download_button_label") or "ショートカットを入手"
        ),
        "download_url": str(settings.get("download_url") or ""),
        "detection_timeout_seconds": int(
            settings.get("detection_timeout_seconds") or 10
        ),
    }


def _record_shortcut_download(job: dict, *, status: str) -> int | None:
    """Record an upload SC job once; album jobs have separate ownership."""
    if str(job.get("source_type") or "upload") != "upload":
        return None
    upload_uuid = str(job.get("upload_uuid") or "").strip()
    if not upload_uuid:
        return None
    upload = fetch_upload_access_record(upload_uuid)
    try:
        files = json.loads(job.get("files_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        files = []
    if not upload or not files:
        return None
    return record_upload_download(
        upload_id=int(upload["id"]),
        event_key=f"ios-shortcut:{int(job['id'])}",
        download_kind="ios_shortcut",
        ip_address=download_request_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
        files=files,
        status=status,
    )


def _cleanup_expired_jobs() -> None:
    _ensure_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            DELETE FROM mobile_download_jobs
             WHERE (completed_at IS NOT NULL AND completed_at < UTC_TIMESTAMP() - INTERVAL 7 DAY)
                OR (revoked_at IS NOT NULL AND revoked_at < UTC_TIMESTAMP() - INTERVAL 7 DAY)
                OR (
                    exchanged_at IS NULL
                    AND launch_expires_at < UTC_TIMESTAMP() - INTERVAL 1 DAY
                )
                OR (
                    session_expires_at IS NOT NULL
                    AND session_expires_at < UTC_TIMESTAMP() - INTERVAL 7 DAY
                )
            """
        )
        db.commit()
    finally:
        db.close()


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
MOVIE_EXTENSIONS = {".mp4", ".mov", ".m4v"}


def _output_name(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return filename
    return str(Path(filename).with_suffix(".jpg"))


def _selected_upload_photos(upload: dict, paths: list) -> list[dict]:
    if not isinstance(paths, list) or not paths:
        return []
    if len(paths) > MAX_JOB_FILES:
        raise ValueError("too_many_files")

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, filename FROM files WHERE upload_id=%s AND is_hidden=0",
            (upload["id"],),
        )
        allowed_names = {
            str(row["filename"]): int(row["id"])
            for row in cur.fetchall()
        }
    finally:
        db.close()

    selected = []
    seen = set()
    upload_uuid = str(upload["uuid"])
    for raw_path in paths:
        ref = resolve_upload_subpath(str(raw_path), allow_zip=False)
        if not ref or ref["uuid"] != upload_uuid or ref["kind"] != "original":
            continue
        filename = str(ref["filename"])
        if filename not in allowed_names or Path(filename).suffix.lower() not in PHOTO_EXTENSIONS:
            continue
        target = ref["target"]
        if filename in seen or not target.is_file():
            continue
        seen.add(filename)
        selected.append(
            {
                "id": _image_id(upload_uuid, filename),
                "file_id": allowed_names[filename],
                "name": filename,
                "output_name": _output_name(filename),
                "size": target.stat().st_size,
            }
        )
    return selected


def _album_access_allowed(album_id: str) -> tuple[bool, dict | None, dict]:
    # 遅延 import にして、アプリ初期化時の Blueprint 循環参照を避ける。
    from app.albums.routes import (
        _fetch_album_meta,
        _has_album_auth,
        _is_event_member_approved,
        load_meta,
    )

    meta = load_meta(album_id)
    if not meta:
        return False, None, {}
    user = session.get("user")
    if user == "admin" or (user and user == meta.get("owner")):
        return True, meta, {"kind": "internal_user", "user": str(user)}
    album_meta = _fetch_album_meta(album_id) or {}
    if album_meta.get("access_mode") == "event":
        event_id = album_meta.get("event_id")
        approved = bool(event_id and _is_event_member_approved(int(event_id)))
        ext_user_id = session.get("ext_user_id")
        try:
            ext_user_id = int(ext_user_id) if ext_user_id is not None else None
        except (TypeError, ValueError):
            ext_user_id = None
        return approved, meta, {
            "kind": "event_member",
            "event_id": int(event_id) if event_id else None,
            "external_user_id": ext_user_id,
        }
    return bool(_has_album_auth(album_id)), meta, {"kind": "album_session"}


def _album_job_access_allowed(job: dict) -> bool:
    from app.albums.routes import _fetch_album_meta, load_meta

    album_id = str(job.get("album_id") or "")
    meta = load_meta(album_id)
    if not meta:
        return False
    try:
        authorization = json.loads(job.get("authorization_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    kind = str(authorization.get("kind") or "")
    if kind == "internal_user":
        user = str(authorization.get("user") or "")
        return user == "admin" or (bool(user) and user == str(meta.get("owner") or ""))
    if kind == "album_session":
        album_meta = _fetch_album_meta(album_id) or {}
        return album_meta.get("access_mode") != "event"
    if kind != "event_member":
        return False

    try:
        event_id = int(authorization.get("event_id"))
        external_user_id = int(authorization.get("external_user_id"))
    except (TypeError, ValueError):
        return False
    album_meta = _fetch_album_meta(album_id) or {}
    if album_meta.get("access_mode") != "event" or int(album_meta.get("event_id") or 0) != event_id:
        return False
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT m.status, COALESCE(m.is_canceled,0) AS is_canceled,
                   COALESCE(u.is_deleted,0) AS is_deleted
              FROM mfu_event_member AS m
              JOIN external_login_user AS u ON u.id=m.user_id
             WHERE m.event_id=%s AND m.user_id=%s
             LIMIT 1
            """,
            (event_id, external_user_id),
        )
        row = cur.fetchone()
    finally:
        db.close()
    return bool(
        row
        and row.get("status") == "approved"
        and int(row.get("is_canceled") or 0) == 0
        and int(row.get("is_deleted") or 0) == 0
    )


def _selected_album_media(album_id: str, child_id: str, filenames: list) -> tuple[list[dict], dict, dict]:
    from app.albums.routes import _movie_find_abs, _open_media_path

    allowed, meta, authorization = _album_access_allowed(album_id)
    if not allowed or not meta:
        raise PermissionError("album_forbidden")
    child = next(
        (row for row in (meta.get("children") or []) if str(row.get("folder")) == child_id),
        None,
    )
    if not child:
        raise FileNotFoundError("child_not_found")
    mode = str(child.get("mode") or "normal").lower()
    if mode not in {"normal", "movie"}:
        raise ValueError("unsupported_album_mode")
    if not isinstance(filenames, list) or not filenames:
        return [], meta, authorization
    if len(filenames) > MAX_JOB_FILES:
        raise ValueError("too_many_files")

    selected = []
    seen = set()
    source_key = f"album:{album_id}:{child_id}"
    for raw_name in filenames:
        filename = str(raw_name or "").strip()
        if not filename or filename != os.path.basename(filename) or filename in seen:
            continue
        suffix = Path(filename).suffix.lower()
        storage_name = filename
        media_type = "image"
        if mode == "normal":
            if suffix not in PHOTO_EXTENSIONS:
                continue
            target = _open_media_path(album_id, child_id, filename, mode="normal")
            output_name = _output_name(filename)
            content_type = "image/jpeg"
        else:
            if suffix not in MOVIE_EXTENSIONS:
                continue
            media_type = "video"
            base_name = str(Path(filename).with_suffix(""))
            web_name = base_name + ".web.mp4"
            converted = _movie_find_abs(album_id, child_id, web_name)
            if converted:
                target = converted
                storage_name = web_name
                output_name = base_name + ".mp4"
                content_type = "video/mp4"
            else:
                target = _movie_find_abs(album_id, child_id, filename)
                output_name = filename
                content_type = {
                    ".mov": "video/quicktime",
                    ".m4v": "video/mp4",
                    ".mp4": "video/mp4",
                }.get(suffix, mimetypes.guess_type(filename)[0] or "application/octet-stream")
        if not target or not os.path.isfile(target):
            continue
        seen.add(filename)
        selected.append(
            {
                "id": _image_id(source_key, filename),
                "name": filename,
                "storage_name": storage_name,
                "output_name": output_name,
                "media_type": media_type,
                "content_type": content_type,
                "size": os.path.getsize(target),
            }
        )
    return selected, meta, authorization


def _manifest(job: dict) -> dict:
    files = json.loads(job.get("files_json") or "[]")
    job_id = int(job["id"])
    for item in files:
        item["download_url"] = url_for(
            "mobile_download.download_file",
            job_id=job_id,
            image_id=item["id"],
            _external=True,
        )
    return {
        "job_id": job_id,
        "source_type": job.get("source_type") or "upload",
        "title": job.get("upload_title") or "MFU Photos",
        "album": ALBUM_NAME,
        "expires_at": job.get("session_expires_at").isoformat() + "Z",
        "files": files,
    }


def _authorized_job(job_id: int, *, allow_completed: bool = False) -> dict | None:
    token = _bearer_token()
    if not token.startswith(ACCESS_TOKEN_PREFIX):
        return None
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        sql = """
            SELECT *
              FROM mobile_download_jobs
             WHERE id=%s
               AND access_token_hash=%s
               AND revoked_at IS NULL
               AND session_expires_at > UTC_TIMESTAMP()
        """
        if not allow_completed:
            sql += " AND completed_at IS NULL"
        cur.execute(sql + " LIMIT 1", (job_id, _hash_token(token)))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE mobile_download_jobs SET last_accessed_at=UTC_TIMESTAMP() WHERE id=%s",
                (job_id,),
            )
            db.commit()
        return row
    finally:
        db.close()


def _authorized_job_id_from_token() -> int | None:
    """Resolve the current shortcut job for clients that omit the job ID."""
    token = _bearer_token()
    if not token.startswith(ACCESS_TOKEN_PREFIX):
        return None
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id
              FROM mobile_download_jobs
             WHERE access_token_hash=%s
               AND revoked_at IS NULL
               AND completed_at IS NULL
               AND session_expires_at > UTC_TIMESTAMP()
             LIMIT 1
            """,
            (_hash_token(token),),
        )
        row = cur.fetchone()
        return int(row["id"]) if row else None
    finally:
        db.close()


@mobile_download_bp.post("/mobile-download/api/jobs")
def create_job():
    _cleanup_expired_jobs()
    data = request.get_json(silent=True) or {}
    source_type = str(data.get("source_type") or "upload").strip().lower()
    upload_uuid = None
    album_id = None
    child_id = None
    title = ""
    authorization = {}
    try:
        if source_type == "album":
            album_id = str(data.get("album_id") or "").strip()
            child_id = str(data.get("child_id") or "").strip()
            files, album_meta, authorization = _selected_album_media(
                album_id,
                child_id,
                data.get("filenames") or [],
            )
            title = str(album_meta.get("album_name") or "")[:255]
        else:
            source_type = "upload"
            upload_uuid = str(data.get("upload_uuid") or "").strip().lower()
            upload = fetch_upload_access_record(upload_uuid)
            if not upload:
                return jsonify({"ok": False, "error": "upload_not_found"}), 404
            if not can_access_upload_record(upload, has_view_auth_func=has_view_auth):
                return jsonify({"ok": False, "error": "upload_forbidden"}), 403
            files = _selected_upload_photos(upload, data.get("paths") or [])
            title = str(upload.get("title") or "")[:255]
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not files:
        return jsonify({"ok": False, "error": "no_photo_selected"}), 400

    launch_token = LAUNCH_TOKEN_PREFIX + secrets.token_urlsafe(32)
    launch_expires_at = datetime.utcnow() + timedelta(minutes=LAUNCH_TOKEN_MINUTES)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mobile_download_jobs
                (source_type, upload_uuid, upload_title, album_id, child_id,
                 authorization_json, files_json, launch_token_hash,
                 created_at, launch_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s)
            """,
            (
                source_type,
                upload_uuid,
                title,
                album_id,
                child_id,
                json.dumps(authorization, ensure_ascii=False, separators=(",", ":")),
                json.dumps(files, ensure_ascii=False, separators=(",", ":")),
                _hash_token(launch_token),
                launch_expires_at,
            ),
        )
        job_id = int(cur.lastrowid)
        db.commit()
    finally:
        db.close()

    shortcut_settings = _get_shortcut_settings()
    universal_link = url_for(
        "mobile_download.open_job",
        launch_token=launch_token,
        _external=True,
    )
    custom_scheme = "mfudownload://job?" + urlencode({"token": launch_token})
    shortcut_url = _shortcut_url(
        universal_link,
        str(shortcut_settings.get("shortcut_name") or SHORTCUT_NAME),
    )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "count": len(files),
            "expires_in": LAUNCH_TOKEN_MINUTES * 60,
            "app_url": universal_link,
            "custom_scheme_url": custom_scheme,
            "shortcut_url": shortcut_url,
            "shortcut_status_url": url_for(
                "mobile_download.shortcut_launch_status",
                launch_token=launch_token,
            ),
        }
    )


@mobile_download_bp.get("/mobile-download/api/shortcut-config")
def shortcut_config():
    response = jsonify({"ok": True, **_public_shortcut_settings()})
    response.headers["Cache-Control"] = "no-store"
    return response


@mobile_download_bp.get(
    "/mobile-download/api/shortcut-status/<launch_token>"
)
def shortcut_launch_status(launch_token: str):
    if not launch_token.startswith(LAUNCH_TOKEN_PREFIX):
        return jsonify({"ok": False, "error": "invalid_launch_token"}), 400
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT exchanged_at, completed_at, revoked_at, launch_expires_at
              FROM mobile_download_jobs
             WHERE launch_token_hash=%s
             LIMIT 1
            """,
            (_hash_token(launch_token),),
        )
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    now = datetime.utcnow()
    expired = bool(
        row.get("revoked_at")
        or not row.get("launch_expires_at")
        or row.get("launch_expires_at") <= now
    )
    response = jsonify(
        {
            "ok": True,
            "started": bool(row.get("exchanged_at")),
            "completed": bool(row.get("completed_at")),
            "expired": expired,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _admin_username() -> str:
    return str(session.get("user") or "")


@mobile_download_bp.route(
    "/admin/mobile-download/shortcut", methods=["GET", "POST"]
)
def admin_shortcut_settings():
    username = _admin_username()
    if username != "admin":
        abort(403)
    settings = _get_shortcut_settings()
    if request.method == "POST":
        shortcut_name = str(request.form.get("shortcut_name") or "").strip()
        popup_title = str(request.form.get("popup_title") or "").strip()
        popup_body = str(request.form.get("popup_body") or "").strip()
        install_steps = str(request.form.get("install_steps") or "").strip()
        download_button_label = str(
            request.form.get("download_button_label") or ""
        ).strip()
        download_url = str(request.form.get("download_url") or "").strip()
        try:
            timeout_seconds = int(
                request.form.get("detection_timeout_seconds") or 10
            )
        except (TypeError, ValueError):
            timeout_seconds = 0
        errors = []
        if not shortcut_name or len(shortcut_name) > 128:
            errors.append("ショートカット名は1～128文字で入力してください。")
        if not popup_title or len(popup_title) > 255:
            errors.append("ポップアップタイトルは1～255文字で入力してください。")
        if not popup_body:
            errors.append("説明文を入力してください。")
        if not install_steps:
            errors.append("導入手順を入力してください。")
        if not download_button_label or len(download_button_label) > 128:
            errors.append("ダウンロードボタン名は1～128文字で入力してください。")
        if download_url:
            parsed = urlparse(download_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append("配布URLはhttp://またはhttps://で始まるURLを入力してください。")
        if not 3 <= timeout_seconds <= 30:
            errors.append("判定待ち時間は3～30秒で入力してください。")
        submitted = {
            "is_enabled": request.form.get("is_enabled") == "1",
            "shortcut_name": shortcut_name,
            "popup_title": popup_title,
            "popup_body": popup_body,
            "install_steps": install_steps,
            "download_button_label": download_button_label,
            "download_url": download_url,
            "detection_timeout_seconds": timeout_seconds or 10,
            "updated_at": settings.get("updated_at"),
            "updated_by": username,
        }
        settings.update(submitted)
        if errors:
            for message in errors:
                flash(message, "danger")
        else:
            db = get_db()
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE mobile_download_shortcut_settings
                       SET is_enabled=%s,
                           shortcut_name=%s,
                           popup_title=%s,
                           popup_body=%s,
                           install_steps=%s,
                           download_button_label=%s,
                           download_url=%s,
                           detection_timeout_seconds=%s,
                           updated_by=%s
                     WHERE id=1
                    """,
                    (
                        1 if submitted["is_enabled"] else 0,
                        shortcut_name,
                        popup_title,
                        popup_body,
                        install_steps,
                        download_button_label,
                        download_url,
                        timeout_seconds,
                        username,
                    ),
                )
                db.commit()
            finally:
                db.close()
            flash("ショートカット案内設定を保存しました。", "success")
            return redirect(url_for("mobile_download.admin_shortcut_settings"))
    return render_template(
        "admin_mobile_download_shortcut.html",
        settings=settings,
    )


@mobile_download_bp.get("/mobile-download/open/<launch_token>")
def open_job(launch_token: str):
    shortcut_settings = _get_shortcut_settings()
    custom_scheme = "mfudownload://job?" + urlencode({"token": launch_token})
    launch_url = url_for(
        "mobile_download.open_job",
        launch_token=launch_token,
        _external=True,
    )
    shortcut_url = _shortcut_url(
        launch_url,
        str(shortcut_settings.get("shortcut_name") or SHORTCUT_NAME),
    )
    response = make_response(
        render_template_string(
            """
            <!doctype html>
            <html lang="ja">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1">
              <title>MFU Download</title>
              <style>
                body{font-family:system-ui,"Yu Gothic",sans-serif;margin:0;background:#f4f6f8;color:#172033;min-height:100vh;display:grid;place-items:center}
                main{width:min(420px,calc(100vw - 32px));text-align:center}
                h1{font-size:24px;margin:0 0 12px} p{line-height:1.7;color:#526071}
                a{display:block;margin-top:22px;padding:14px 18px;background:#1267d6;color:#fff;text-decoration:none;border-radius:8px;font-weight:700}
              </style>
            </head>
            <body>
              <main>
                <h1>MFU Download</h1>
                <p>ショートカットを開いて、選択した写真を「MFU」アルバムへ保存します。</p>
                <a href="{{ shortcut_url }}">ショートカットを開く</a>
                <a class="legacy" href="{{ custom_scheme }}">MFU Downloadアプリを開く</a>
              </main>
            </body>
            </html>
            """,
            custom_scheme=custom_scheme,
            shortcut_url=shortcut_url,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@mobile_download_bp.post("/mobile-download/api/exchange")
def exchange_token():
    _cleanup_expired_jobs()
    data = request.get_json(silent=True) or {}
    launch_token = str(data.get("launch_token") or "").strip()
    if not launch_token:
        launch_url = str(data.get("launch_url") or "").strip().rstrip("/")
        launch_token = launch_url.rsplit("/", 1)[-1] if launch_url else ""
    if not launch_token.startswith(LAUNCH_TOKEN_PREFIX):
        return jsonify({"ok": False, "error": "invalid_launch_token"}), 400

    access_token = ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
    session_expires_at = datetime.utcnow() + timedelta(hours=SESSION_TOKEN_HOURS)
    platform = str(data.get("platform") or "unknown")[:32]

    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        db.start_transaction()
        cur.execute(
            """
            SELECT *
              FROM mobile_download_jobs
             WHERE launch_token_hash=%s
             LIMIT 1
             FOR UPDATE
            """,
            (_hash_token(launch_token),),
        )
        job = cur.fetchone()
        if not job or job.get("revoked_at") or job.get("launch_expires_at") <= datetime.utcnow():
            db.rollback()
            return jsonify({"ok": False, "error": "launch_token_expired"}), 410
        if job.get("exchanged_at") or job.get("access_token_hash"):
            db.rollback()
            return jsonify({"ok": False, "error": "launch_token_used"}), 410

        cur.execute(
            """
            UPDATE mobile_download_jobs
               SET access_token_hash=%s,
                   exchanged_at=UTC_TIMESTAMP(),
                   session_expires_at=%s,
                   client_platform=%s,
                   last_accessed_at=UTC_TIMESTAMP()
             WHERE id=%s
            """,
            (_hash_token(access_token), session_expires_at, platform, job["id"]),
        )
        db.commit()
        job["session_expires_at"] = session_expires_at
        manifest = _manifest(job)
    finally:
        db.close()

    try:
        _record_shortcut_download(job, status="started")
    except Exception:
        current_app.logger.exception(
            "mobile download history start insert failed job_id=%s",
            job.get("id"),
        )
    return jsonify({"ok": True, "access_token": access_token, "manifest": manifest})


@mobile_download_bp.get("/mobile-download/api/jobs/<int:job_id>")
def get_job(job_id: int):
    job = _authorized_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "invalid_or_expired_session"}), 401
    return jsonify({"ok": True, "manifest": _manifest(job)})


@mobile_download_bp.get("/mobile-download/api/files/<int:job_id>/<image_id>")
def download_file(job_id: int, image_id: str):
    job = _authorized_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "invalid_or_expired_session"}), 401

    files = json.loads(job.get("files_json") or "[]")
    item = next((row for row in files if row.get("id") == image_id), None)
    if not item:
        return jsonify({"ok": False, "error": "file_not_found"}), 404

    source_type = str(job.get("source_type") or "upload")
    target = None
    if source_type == "album":
        if _album_job_access_allowed(job):
            if str(item.get("media_type") or "image") == "video":
                from app.albums.routes import _movie_find_abs
                target = _movie_find_abs(
                    str(job.get("album_id") or ""),
                    str(job.get("child_id") or ""),
                    str(item.get("storage_name") or item["name"]),
                )
            else:
                from app.albums.routes import _open_media_path
                target = _open_media_path(
                    str(job.get("album_id") or ""),
                    str(job.get("child_id") or ""),
                    str(item["name"]),
                    mode="normal",
                )
    else:
        ref = resolve_upload_subpath(
            f"{job['upload_uuid']}/original/{item['name']}",
            allow_zip=False,
        )
        upload = fetch_upload_access_record(str(job["upload_uuid"]))
        file_row = (
            fetch_upload_file_record(upload["id"], str(item["name"]))
            if upload
            else None
        )
        if ref and ref["target"].is_file() and file_row and not upload_file_is_hidden(file_row):
            target = str(ref["target"])
    if not target or not os.path.isfile(target):
        return jsonify({"ok": False, "error": "file_not_found"}), 404

    output_name = str(item.get("output_name") or item["name"])
    suffix = Path(target).suffix.lower()
    if str(item.get("media_type") or "image") == "video":
        response = send_file(
            target,
            mimetype=str(item.get("content_type") or mimetypes.guess_type(output_name)[0] or "application/octet-stream"),
            as_attachment=False,
            download_name=output_name,
            conditional=True,
            max_age=0,
        )
    elif suffix in {".jpg", ".jpeg"}:
        response = send_file(
            target,
            mimetype="image/jpeg",
            as_attachment=False,
            download_name=output_name,
            conditional=True,
            max_age=0,
        )
    else:
        magick = shutil.which("magick") or shutil.which("convert")
        if not magick:
            return jsonify({"ok": False, "error": "jpeg_converter_unavailable"}), 503
        try:
            converted = subprocess.run(
                [magick, target, "-auto-orient", "-background", "white", "-alpha", "remove", "jpg:-"],
                check=True,
                capture_output=True,
                timeout=120,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            current_app.logger.exception("shortcut JPEG conversion failed: %s", target)
            return jsonify({"ok": False, "error": "jpeg_conversion_failed"}), 500
        response = send_file(
            io.BytesIO(converted),
            mimetype="image/jpeg",
            as_attachment=False,
            download_name=output_name,
            max_age=0,
        )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-MFU-Image-ID"] = image_id
    response.headers["X-MFU-Media-Type"] = str(item.get("media_type") or "image")
    return response


@mobile_download_bp.post("/mobile-download/api/jobs/<int:job_id>/complete")
def complete_job(job_id: int):
    job = _authorized_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "invalid_or_expired_session"}), 401
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE mobile_download_jobs SET completed_at=UTC_TIMESTAMP() WHERE id=%s",
            (job_id,),
        )
        db.commit()
    finally:
        db.close()

    try:
        event_id = _record_shortcut_download(job, status="completed")
        mark_upload_download_status(event_id, "completed")
    except Exception:
        current_app.logger.exception(
            "mobile download history insert failed job_id=%s",
            job_id,
        )
    return jsonify({"ok": True})


@mobile_download_bp.post("/mobile-download/api/jobs/complete")
def complete_current_job():
    job_id = _authorized_job_id_from_token()
    if job_id is None:
        return jsonify({"ok": False, "error": "invalid_or_expired_session"}), 401
    return complete_job(job_id)


@mobile_download_bp.get("/.well-known/apple-app-site-association")
def apple_app_site_association():
    team_id = os.environ.get("MFU_IOS_TEAM_ID", "").strip()
    app_ids = [f"{team_id}.{IOS_BUNDLE_ID}"] if team_id else []
    response = jsonify(
        {
            "applinks": {
                "apps": [],
                "details": [
                    {
                        "appIDs": app_ids,
                        "components": [{"/": "/mobile-download/open/*"}],
                    }
                ],
            }
        }
    )
    response.mimetype = "application/json"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@mobile_download_bp.get("/.well-known/assetlinks.json")
def android_asset_links():
    fingerprints = [
        value.strip()
        for value in os.environ.get("MFU_ANDROID_SHA256_FINGERPRINTS", "").split(",")
        if value.strip()
    ]
    payload = []
    if fingerprints:
        payload.append(
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": ANDROID_PACKAGE,
                    "sha256_cert_fingerprints": fingerprints,
                },
            }
        )
    response = jsonify(payload)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
