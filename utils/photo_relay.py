"""iPhone -> MFU -> Windows photo relay.

The iPhone uploads with an existing ``ios_shortcut_upload`` token.  Windows
receivers are authorised in a browser and keep a dedicated, revocable token.
Socket.IO is used as the real-time control plane; file bytes are downloaded
over authenticated HTTPS so interrupted transfers can be retried safely.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import disconnect, emit, join_room
from PIL import Image

from app.chat.socketio_ext import socketio
from app.utils.db import get_db
from app.utils.ios_upload_images import (
    IOSUploadImageError,
    convert_heif_to_jpeg,
    looks_like_heif,
)
from app.utils.socket_connection_metrics import register_connection, unregister_connection
from app.utils.upload_security import detect_mime_from_bytes
from app.utils.uploader_auth import TOKEN_SCOPE_IOS, verify_uploader_token


desktop_photo_relay_bp = Blueprint(
    "desktop_photo_relay", __name__, url_prefix="/desktop/photo-relay"
)
photo_relay_api_bp = Blueprint(
    "photo_relay_api", __name__, url_prefix="/api/photo-relay/v1"
)

TOKEN_PREFIX = "mfu_pr_"
TOKEN_DAYS = 180
SPOOL_ROOT = Path(os.environ.get("MFU_PHOTO_RELAY_DIR", "/mnt/mfu/photo_relay"))
MAX_FILE_BYTES = int(os.environ.get("MFU_PHOTO_RELAY_MAX_FILE_MB", "100")) * 1024 * 1024
MAX_FILES_PER_JOB = int(os.environ.get("MFU_PHOTO_RELAY_MAX_FILES", "100"))
JOB_RETENTION_DAYS = int(os.environ.get("MFU_PHOTO_RELAY_RETENTION_DAYS", "7"))
WINDOWS_DOWNLOAD_PATH = Path(
    os.environ.get("MFU_PHOTO_RELAY_WINDOWS_ZIP", "/mnt/mfu/downloads/MFUPhotoRelay.zip")
)

_schema_ready = False
_schema_lock = threading.Lock()
_sid_devices: dict[str, int] = {}
_sid_lock = threading.Lock()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token() -> str:
    value = request.headers.get("Authorization") or ""
    if not value.lower().startswith("bearer "):
        return ""
    return value.split(" ", 1)[1].strip()


def _valid_callback(value: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    return value if parsed.port else ""


def _valid_device_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, TypeError, AttributeError):
        return ""


def _safe_label(value: object, fallback: str = "Windows PC") -> str:
    label = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return (label or fallback)[:120]


def _safe_original_filename(value: object) -> str:
    name = re.split(r"[\\/]", str(value or ""))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(" .")
    return (name or "photo")[:255]


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_relay_devices (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              device_uuid CHAR(36) NOT NULL,
              username VARCHAR(191) NOT NULL,
              label VARCHAR(120) NOT NULL,
              token_hash CHAR(64) NOT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              last_used_at DATETIME NULL,
              last_connected_at DATETIME NULL,
              revoked_at DATETIME NULL,
              removed_at DATETIME NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_photo_relay_device_uuid (device_uuid),
              UNIQUE KEY uq_photo_relay_token_hash (token_hash),
              KEY ix_photo_relay_device_owner (username, removed_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_relay_jobs (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              job_uuid CHAR(36) NOT NULL,
              device_id BIGINT UNSIGNED NOT NULL,
              username VARCHAR(191) NOT NULL,
              status VARCHAR(24) NOT NULL DEFAULT 'receiving',
              expected_files INT UNSIGNED NULL,
              total_files INT UNSIGNED NOT NULL DEFAULT 0,
              completed_files INT UNSIGNED NOT NULL DEFAULT 0,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              ready_at DATETIME NULL,
              completed_at DATETIME NULL,
              expires_at DATETIME NOT NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_photo_relay_job_uuid (job_uuid),
              KEY ix_photo_relay_job_device (device_id, status, created_at),
              CONSTRAINT fk_photo_relay_job_device FOREIGN KEY (device_id)
                REFERENCES photo_relay_devices(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_relay_files (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              job_id BIGINT UNSIGNED NOT NULL,
              client_file_id VARCHAR(128) NOT NULL,
              original_filename VARCHAR(255) NOT NULL,
              stored_filename VARCHAR(255) NOT NULL,
              mime_type VARCHAR(64) NOT NULL,
              size_bytes BIGINT UNSIGNED NOT NULL,
              sha256 CHAR(64) NOT NULL,
              sequence_index INT UNSIGNED NOT NULL DEFAULT 0,
              capture_at VARCHAR(64) NULL,
              source_modified_at VARCHAR(64) NULL,
              converted_to_jpeg TINYINT(1) NOT NULL DEFAULT 0,
              status VARCHAR(24) NOT NULL DEFAULT 'queued',
              attempts INT UNSIGNED NOT NULL DEFAULT 0,
              last_error VARCHAR(500) NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              completed_at DATETIME NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_photo_relay_client_file (job_id, client_file_id),
              KEY ix_photo_relay_file_status (job_id, status, sequence_index),
              CONSTRAINT fk_photo_relay_file_job FOREIGN KEY (job_id)
                REFERENCES photo_relay_jobs(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM photo_relay_jobs LIKE 'expected_files'")
        if not cur.fetchone():
            cur.execute(
                "ALTER TABLE photo_relay_jobs "
                "ADD COLUMN expected_files INT UNSIGNED NULL AFTER status"
            )
        db.commit()
        db.close()
        SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
        _schema_ready = True


def _issue_device_token(username: str, device_uuid: str, label: str) -> str:
    _ensure_schema()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=TOKEN_DAYS)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO photo_relay_devices
          (device_uuid, username, label, token_hash, created_at, updated_at,
           expires_at, last_used_at, last_connected_at, revoked_at, removed_at)
        VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP(), %s,
                NULL, NULL, NULL, NULL)
        ON DUPLICATE KEY UPDATE
          username=VALUES(username), label=VALUES(label), token_hash=VALUES(token_hash),
          updated_at=UTC_TIMESTAMP(), expires_at=VALUES(expires_at),
          revoked_at=NULL, removed_at=NULL
        """,
        (device_uuid, username, label, _hash_token(token), expires_at),
    )
    db.commit()
    db.close()
    return token


def _verify_device_token(token: str | None = None, *, touch: bool = True) -> dict | None:
    raw = (token or _bearer_token()).strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, device_uuid, username, label, expires_at, revoked_at, removed_at
          FROM photo_relay_devices WHERE token_hash=%s LIMIT 1
        """,
        (_hash_token(raw),),
    )
    row = cur.fetchone()
    if (
        not row
        or row.get("revoked_at")
        or row.get("removed_at")
        or (row.get("expires_at") and row["expires_at"] < datetime.utcnow())
    ):
        db.close()
        return None
    if touch:
        cur.execute(
            "UPDATE photo_relay_devices SET last_used_at=UTC_TIMESTAMP() WHERE id=%s",
            (row["id"],),
        )
        db.commit()
    db.close()
    return row


def _shortcut_user() -> str:
    row = verify_uploader_token(allowed_scopes={TOKEN_SCOPE_IOS})
    if not row:
        abort(401)
    username = str(row.get("username") or "").strip()
    if not username:
        abort(403)
    return username


def _job_row(job_uuid: str, username: str) -> dict | None:
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT j.*, d.device_uuid, d.label AS device_label
          FROM photo_relay_jobs j
          JOIN photo_relay_devices d ON d.id=j.device_id
         WHERE j.job_uuid=%s AND j.username=%s LIMIT 1
        """,
        (job_uuid, username),
    )
    row = cur.fetchone()
    db.close()
    return row


def _cleanup_expired_jobs() -> int:
    """Remove expired queue bytes and their database rows.

    This is intentionally opportunistic: every device listing, job creation and
    receiver connection performs a pass, so no separate cron dependency is
    required for the seven-day retention guarantee.
    """
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, job_uuid FROM photo_relay_jobs WHERE expires_at<=UTC_TIMESTAMP()")
    rows = cur.fetchall() or []
    root = SPOOL_ROOT.resolve()
    for row in rows:
        path = (SPOOL_ROOT / str(row["job_uuid"])).resolve()
        if root in path.parents and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        cur.execute("DELETE FROM photo_relay_jobs WHERE id=%s", (row["id"],))
    db.commit()
    db.close()
    return len(rows)


def _file_payload(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "job_uuid": str(row["job_uuid"]),
        "original_filename": str(row["original_filename"]),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]),
        "sequence_index": int(row.get("sequence_index") or 0),
        "capture_at": row.get("capture_at") or "",
        "source_modified_at": row.get("source_modified_at") or "",
        "converted_to_jpeg": bool(row.get("converted_to_jpeg")),
        "download_path": f"/desktop/photo-relay/api/files/{int(row['id'])}/download",
    }


def _queued_files(device_id: int) -> list[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT f.*, j.job_uuid
          FROM photo_relay_files f
          JOIN photo_relay_jobs j ON j.id=f.job_id
         WHERE j.device_id=%s AND j.expires_at>UTC_TIMESTAMP()
           AND f.status IN ('queued','delivering')
         ORDER BY j.created_at, f.sequence_index, f.id
        """,
        (device_id,),
    )
    rows = cur.fetchall() or []
    db.close()
    return [_file_payload(row) for row in rows]


def _emit_file_ready(device_uuid: str, payload: dict) -> None:
    socketio.emit(
        "photo_relay_file_ready",
        payload,
        namespace="/photo-relay",
        to=f"photo-relay:{device_uuid}",
    )


def _extract_capture_at(path: Path) -> str:
    try:
        if path.suffix.lower() in {".heic", ".heif"}:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        with Image.open(path) as image:
            exif = image.getexif()
            value = exif.get(36867) or exif.get(306)
            if value:
                parsed = datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""
    return ""


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _file_record_for_device(file_id: int, device_id: int) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT f.*, j.job_uuid, j.device_id
          FROM photo_relay_files f
          JOIN photo_relay_jobs j ON j.id=f.job_id
         WHERE f.id=%s AND j.device_id=%s AND j.expires_at>UTC_TIMESTAMP()
         LIMIT 1
        """,
        (file_id, device_id),
    )
    row = cur.fetchone()
    db.close()
    return row


@desktop_photo_relay_bp.get("/login/start")
def login_start():
    callback = _valid_callback(request.args.get("callback") or "")
    state = (request.args.get("state") or "").strip()
    device_uuid = _valid_device_uuid(request.args.get("device_uuid") or "")
    device_name = _safe_label(request.args.get("device_name"), "Windows PC")
    if not callback or not state or not device_uuid:
        return "Invalid callback or device", 400
    if not session.get("user"):
        next_url = url_for(
            "desktop_photo_relay.login_start",
            callback=callback,
            state=state,
            device_uuid=device_uuid,
            device_name=device_name,
        )
        return redirect(url_for("login", next=next_url))
    return render_template_string(
        """
        <!doctype html><html lang="ja"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>MFU写真転送 認可</title><style>
        body{font-family:system-ui,"Yu Gothic",sans-serif;background:#f6f7fb;color:#1f2937;margin:0;min-height:100vh;display:grid;place-items:center}
        main{width:min(520px,calc(100vw - 32px));background:#fff;border:1px solid #d1d5db;border-radius:12px;box-shadow:0 18px 45px #0f172a24;padding:28px}
        h1{font-size:22px;margin:0 0 14px}p{line-height:1.7}.device{font-weight:700}.actions{display:flex;gap:10px;justify-content:flex-end;margin-top:24px}
        button,a{font:inherit}button{background:#0b63dd;color:#fff;border:0;border-radius:7px;padding:10px 16px;cursor:pointer}a{color:#4b5563;text-decoration:none;padding:10px 12px}
        </style></head><body><main><h1>MFU写真転送を許可しますか？</h1>
        <p><strong>{{ username }}</strong>として、Windows端末 <span class="device">{{ device_name }}</span> に写真を受信・保存する権限を与えます。</p>
        <p>ChromeのCookieやパスワードはアプリへ渡されません。専用トークンは後から無効化できます。</p>
        <form method="post" action="{{ url_for('desktop_photo_relay.login_approve') }}" class="actions">
        <input type="hidden" name="callback" value="{{ callback }}"><input type="hidden" name="state" value="{{ state }}">
        <input type="hidden" name="device_uuid" value="{{ device_uuid }}"><input type="hidden" name="device_name" value="{{ device_name }}">
        <a href="/">キャンセル</a><button type="submit">許可する</button></form></main></body></html>
        """,
        username=session.get("user"),
        callback=callback,
        state=state,
        device_uuid=device_uuid,
        device_name=device_name,
    )


@desktop_photo_relay_bp.post("/login/approve")
def login_approve():
    callback = _valid_callback(request.form.get("callback") or "")
    state = (request.form.get("state") or "").strip()
    device_uuid = _valid_device_uuid(request.form.get("device_uuid") or "")
    device_name = _safe_label(request.form.get("device_name"), "Windows PC")
    username = str(session.get("user") or "").strip()
    if not callback or not state or not device_uuid:
        return "Invalid callback or device", 400
    if not username:
        next_url = url_for(
            "desktop_photo_relay.login_start",
            callback=callback,
            state=state,
            device_uuid=device_uuid,
            device_name=device_name,
        )
        return redirect(url_for("login", next=next_url))
    token = _issue_device_token(username, device_uuid, device_name)
    separator = "&" if "?" in callback else "?"
    return redirect(callback + separator + urlencode({"token": token, "state": state}))


@desktop_photo_relay_bp.get("/api/session")
def receiver_session():
    row = _verify_device_token()
    if not row:
        return jsonify({"ok": False, "authenticated": False, "error": "invalid_token"}), 401
    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "username": row["username"],
            "device_uuid": row["device_uuid"],
            "device_name": row["label"],
        }
    )


@desktop_photo_relay_bp.post("/api/device")
def update_receiver_device():
    row = _verify_device_token()
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    payload = request.get_json(silent=True) or {}
    label = _safe_label(payload.get("device_name"), row["label"])
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE photo_relay_devices SET label=%s, updated_at=UTC_TIMESTAMP() WHERE id=%s",
        (label, row["id"]),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "device_name": label})


@desktop_photo_relay_bp.post("/api/revoke")
def revoke_receiver():
    row = _verify_device_token(touch=False)
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    payload = request.get_json(silent=True) or {}
    unregister = bool(payload.get("unregister"))
    db = get_db()
    cur = db.cursor()
    if unregister:
        cur.execute(
            "UPDATE photo_relay_devices SET revoked_at=UTC_TIMESTAMP(), removed_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE id=%s",
            (row["id"],),
        )
    else:
        cur.execute(
            "UPDATE photo_relay_devices SET revoked_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE id=%s",
            (row["id"],),
        )
    db.commit()
    db.close()
    return jsonify({"ok": True, "unregistered": unregister})


@desktop_photo_relay_bp.get("/api/queue")
def receiver_queue():
    """Return the durable queue so the Windows client can repair missed events."""
    device = _verify_device_token()
    if not device:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    files = _queued_files(int(device["id"]))
    return jsonify({"ok": True, "files": files, "count": len(files)})


@desktop_photo_relay_bp.get("/download/windows")
def download_windows_receiver():
    """Provide the signed-in MFU user with the packaged Windows receiver."""
    if not session.get("user"):
        return redirect(url_for("login", next=request.url))
    if not WINDOWS_DOWNLOAD_PATH.is_file():
        abort(404)
    response = send_file(
        WINDOWS_DOWNLOAD_PATH,
        mimetype="application/zip",
        as_attachment=True,
        download_name="MFUPhotoRelay.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@desktop_photo_relay_bp.get("/api/files/<int:file_id>/download")
def download_relay_file(file_id: int):
    device = _verify_device_token()
    if not device:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    row = _file_record_for_device(file_id, int(device["id"]))
    if not row or row.get("status") == "completed":
        abort(404)
    path = (SPOOL_ROOT / str(row["job_uuid"]) / str(row["stored_filename"])).resolve()
    expected_root = SPOOL_ROOT.resolve()
    if expected_root not in path.parents or not path.is_file():
        abort(404)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE photo_relay_files SET status='delivering', attempts=attempts+1 WHERE id=%s",
        (file_id,),
    )
    db.commit()
    db.close()
    response = send_file(path, mimetype=row["mime_type"], as_attachment=True, download_name=row["original_filename"])
    response.headers["X-Content-SHA256"] = row["sha256"]
    return response


@desktop_photo_relay_bp.post("/api/files/<int:file_id>/ack")
def acknowledge_relay_file(file_id: int):
    device = _verify_device_token()
    if not device:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    row = _file_record_for_device(file_id, int(device["id"]))
    if not row:
        abort(404)
    payload = request.get_json(silent=True) or {}
    received_hash = str(payload.get("sha256") or "").lower()
    if received_hash and received_hash != str(row["sha256"]).lower():
        return jsonify({"ok": False, "error": "sha256_mismatch"}), 409
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE photo_relay_files SET status='completed', completed_at=UTC_TIMESTAMP(), last_error=NULL WHERE id=%s",
        (file_id,),
    )
    cur.execute(
        "UPDATE photo_relay_jobs SET completed_files=(SELECT COUNT(*) FROM photo_relay_files WHERE job_id=%s AND status='completed') WHERE id=%s",
        (row["job_id"], row["job_id"]),
    )
    cur.execute(
        "SELECT total_files, completed_files, status, ready_at "
        "FROM photo_relay_jobs WHERE id=%s",
        (row["job_id"],),
    )
    progress = cur.fetchone()
    if (
        progress
        and progress[3] is not None
        and str(progress[2]) != "receiving"
        and int(progress[1] or 0) >= int(progress[0] or 0)
    ):
        cur.execute(
            "UPDATE photo_relay_jobs SET status='completed', completed_at=UTC_TIMESTAMP() WHERE id=%s",
            (row["job_id"],),
        )
    db.commit()
    db.close()
    path = SPOOL_ROOT / str(row["job_uuid"]) / str(row["stored_filename"])
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass
    return jsonify({"ok": True})


@desktop_photo_relay_bp.post("/api/files/<int:file_id>/fail")
def fail_relay_file(file_id: int):
    device = _verify_device_token()
    if not device:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    row = _file_record_for_device(file_id, int(device["id"]))
    if not row:
        abort(404)
    error = str((request.get_json(silent=True) or {}).get("error") or "受信に失敗しました。")[:500]
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE photo_relay_files SET status='queued', last_error=%s WHERE id=%s",
        (error, file_id),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@photo_relay_api_bp.get("/devices")
def shortcut_devices():
    username = _shortcut_user()
    _ensure_schema()
    _cleanup_expired_jobs()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT device_uuid, label, last_connected_at
          FROM photo_relay_devices
         WHERE username=%s AND revoked_at IS NULL AND removed_at IS NULL
           AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP())
         ORDER BY label, id
        """,
        (username,),
    )
    devices = cur.fetchall() or []
    db.close()
    for item in devices:
        if item.get("last_connected_at"):
            item["last_connected_at"] = item["last_connected_at"].isoformat() + "Z"
    return jsonify({"ok": True, "devices": devices})


@photo_relay_api_bp.post("/jobs")
def create_relay_job():
    username = _shortcut_user()
    _cleanup_expired_jobs()
    payload = request.get_json(silent=True) or {}
    device_uuid = _valid_device_uuid(payload.get("device_uuid") or "")
    if not device_uuid:
        return jsonify({"ok": False, "error": "device_uuid_required"}), 400
    try:
        expected_files = int(payload.get("expected_file_count") or 0)
    except (TypeError, ValueError):
        expected_files = 0
    if expected_files < 0 or expected_files > MAX_FILES_PER_JOB:
        return jsonify({"ok": False, "error": "invalid_expected_file_count"}), 400
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, device_uuid, label FROM photo_relay_devices
         WHERE device_uuid=%s AND username=%s AND revoked_at IS NULL AND removed_at IS NULL
           AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP()) LIMIT 1
        """,
        (device_uuid, username),
    )
    device = cur.fetchone()
    if not device:
        db.close()
        return jsonify({"ok": False, "error": "device_not_found"}), 404
    job_uuid = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=JOB_RETENTION_DAYS)
    cur.execute(
        """
        INSERT INTO photo_relay_jobs
          (job_uuid, device_id, username, status, expected_files,
           total_files, completed_files, created_at, expires_at)
        VALUES (%s, %s, %s, 'receiving', %s, 0, 0, UTC_TIMESTAMP(), %s)
        """,
        (job_uuid, device["id"], username, expected_files or None, expires_at),
    )
    db.commit()
    db.close()
    (SPOOL_ROOT / job_uuid).mkdir(parents=True, exist_ok=True)
    return jsonify(
        {
            "ok": True,
            "job_uuid": job_uuid,
            "device_uuid": device_uuid,
            "device_name": device["label"],
            "expected_file_count": expected_files or None,
            "expires_at": expires_at.isoformat() + "Z",
        }
    )


@photo_relay_api_bp.post("/files")
def upload_relay_file():
    username = _shortcut_user()
    job_uuid = _valid_device_uuid(request.form.get("job_uuid") or "")
    incoming = request.files.get("file")
    if not job_uuid or not incoming or not incoming.filename:
        return jsonify({"ok": False, "error": "job_uuid_and_file_required"}), 400
    job = _job_row(job_uuid, username)
    if not job:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    # Older servers could close a job when Windows acknowledged its first file.
    # Without ready_at, the iPhone has not explicitly completed the upload yet.
    if job.get("status") == "completed" and not job.get("ready_at"):
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "UPDATE photo_relay_jobs SET status='receiving', completed_at=NULL WHERE id=%s",
            (job["id"],),
        )
        db.commit()
        db.close()
        job["status"] = "receiving"
    if job.get("status") not in {"receiving", "queued"}:
        return jsonify({"ok": False, "error": "job_closed"})
    client_file_id = str(request.form.get("client_file_id") or uuid.uuid4().hex)[:128]
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT f.*, %s AS job_uuid FROM photo_relay_files f WHERE f.job_id=%s AND f.client_file_id=%s LIMIT 1",
        (job_uuid, job["id"], client_file_id),
    )
    existing = cur.fetchone()
    if existing:
        db.close()
        payload = _file_payload(existing)
        _emit_file_ready(job["device_uuid"], payload)
        return jsonify({"ok": True, "already_uploaded": True, "file": payload})
    cur.execute("SELECT COUNT(*) AS count FROM photo_relay_files WHERE job_id=%s", (job["id"],))
    count = int((cur.fetchone() or {}).get("count") or 0)
    db.close()
    if count >= MAX_FILES_PER_JOB:
        return jsonify({"ok": False, "error": "file_count_limit"}), 413

    job_dir = SPOOL_ROOT / job_uuid
    job_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".incoming-", suffix=".part", dir=job_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    total = 0
    try:
        with temp_path.open("wb") as target:
            while True:
                chunk = incoming.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ValueError("file_too_large")
                target.write(chunk)
        with temp_path.open("rb") as source:
            header = source.read(128)
        mime_type = detect_mime_from_bytes(header)
        heif_input = looks_like_heif(header)
        if mime_type not in {"image/jpeg", "image/png", "image/heif-bmff"}:
            return jsonify({"ok": False, "error": "unsupported_image"}), 400
        original_name = _safe_original_filename(incoming.filename)
        capture_at = str(request.form.get("capture_at") or "").strip()[:64]
        source_modified_at = str(request.form.get("source_modified_at") or "").strip()[:64]
        file_uuid = uuid.uuid4().hex
        converted = False
        if heif_input:
            source_heic = job_dir / f"{file_uuid}.heic"
            os.replace(temp_path, source_heic)
            temp_path = source_heic
            if not capture_at:
                capture_at = _extract_capture_at(source_heic)
            try:
                final_path_str, _ = convert_heif_to_jpeg(
                    str(source_heic), str(job_dir), f"{file_uuid}.heic"
                )
            except IOSUploadImageError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            final_path = Path(final_path_str)
            source_heic.unlink(missing_ok=True)
            temp_path = Path("")
            converted = True
            mime_type = "image/jpeg"
        else:
            extension = ".jpg" if mime_type == "image/jpeg" else ".png"
            final_path = job_dir / f"{file_uuid}{extension}"
            os.replace(temp_path, final_path)
            temp_path = Path("")
            if not capture_at:
                capture_at = _extract_capture_at(final_path)
        digest, size = _hash_file(final_path)
        sequence_index = max(0, int(request.form.get("sequence_index") or count))
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO photo_relay_files
              (job_id, client_file_id, original_filename, stored_filename, mime_type,
               size_bytes, sha256, sequence_index, capture_at, source_modified_at,
               converted_to_jpeg, status, attempts, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',0,UTC_TIMESTAMP())
            """,
            (
                job["id"], client_file_id, original_name, final_path.name, mime_type,
                size, digest, sequence_index, capture_at or None,
                source_modified_at or None, 1 if converted else 0,
            ),
        )
        file_id = int(cur.lastrowid)
        cur.execute(
            "UPDATE photo_relay_jobs SET total_files=(SELECT COUNT(*) FROM photo_relay_files WHERE job_id=%s) WHERE id=%s",
            (job["id"], job["id"]),
        )
        db.commit()
        db.close()
        payload = {
            "id": file_id,
            "job_uuid": job_uuid,
            "original_filename": original_name,
            "mime_type": mime_type,
            "size_bytes": size,
            "sha256": digest,
            "sequence_index": sequence_index,
            "capture_at": capture_at,
            "source_modified_at": source_modified_at,
            "converted_to_jpeg": converted,
            "download_path": f"/desktop/photo-relay/api/files/{file_id}/download",
        }
        _emit_file_ready(job["device_uuid"], payload)
        return jsonify({"ok": True, "already_uploaded": False, "file": payload})
    except ValueError as exc:
        if str(exc) == "file_too_large":
            return jsonify({"ok": False, "error": "file_too_large"}), 413
        raise
    finally:
        if temp_path and str(temp_path) not in {"", "."}:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


@photo_relay_api_bp.post("/jobs/<job_uuid>/done")
def finish_relay_job(job_uuid: str):
    username = _shortcut_user()
    normalized = _valid_device_uuid(job_uuid)
    job = _job_row(normalized, username) if normalized else None
    if not job:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    total_files = int(job.get("total_files") or 0)
    expected_files = int(job.get("expected_files") or 0)
    if total_files == 0:
        return jsonify(
            {
                "ok": False,
                "error": "no_files_uploaded",
                "message": "写真を1枚も受信できなかったため、転送完了にしていません。",
                "uploaded_count": 0,
                "expected_count": expected_files,
            }
        )
    if expected_files and total_files < expected_files:
        return jsonify(
            {
                "ok": False,
                "error": "incomplete_upload",
                "message": "選択枚数より受信枚数が少ないため、転送完了にしていません。",
                "uploaded_count": total_files,
                "expected_count": expected_files,
            }
        )
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE photo_relay_jobs
           SET ready_at=COALESCE(ready_at, UTC_TIMESTAMP()),
               status=CASE
                 WHEN total_files>0 AND completed_files>=total_files THEN 'completed'
                 ELSE 'queued'
               END,
               completed_at=CASE
                 WHEN total_files>0 AND completed_files>=total_files THEN UTC_TIMESTAMP()
                 ELSE NULL
               END
         WHERE id=%s
        """,
        (job["id"],),
    )
    db.commit()
    db.close()
    files = _queued_files(int(job["device_id"]))
    for payload in files:
        if payload["job_uuid"] == normalized:
            _emit_file_ready(job["device_uuid"], payload)
    socketio.emit(
        "photo_relay_job_ready",
        {"job_uuid": normalized, "file_count": total_files},
        namespace="/photo-relay",
        to=f"photo-relay:{job['device_uuid']}",
    )
    return jsonify(
        {
            "ok": True,
            "job_uuid": normalized,
            "queued_count": len([f for f in files if f["job_uuid"] == normalized]),
            "uploaded_count": total_files,
            "expected_count": expected_files or total_files,
            "message": "Windowsへ転送しました。オフラインの場合は再接続後に自動転送します。",
        }
    )


@photo_relay_api_bp.get("/jobs/<job_uuid>")
def relay_job_status(job_uuid: str):
    username = _shortcut_user()
    normalized = _valid_device_uuid(job_uuid)
    job = _job_row(normalized, username) if normalized else None
    if not job:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT status, COUNT(*) AS count
          FROM photo_relay_files
         WHERE job_id=%s
         GROUP BY status
        """,
        (job["id"],),
    )
    counts = {str(row["status"]): int(row["count"] or 0) for row in (cur.fetchall() or [])}
    db.close()
    total = int(job.get("total_files") or 0)
    completed = counts.get("completed", 0)
    return jsonify(
        {
            "ok": True,
            "job_uuid": normalized,
            "status": "completed" if total > 0 and completed >= total else job.get("status"),
            "total_files": total,
            "expected_files": int(job.get("expected_files") or total),
            "completed_files": completed,
            "pending_files": max(0, total - completed),
            "counts": counts,
        }
    )


@socketio.on("connect", namespace="/photo-relay")
def photo_relay_socket_connect(auth=None):
    auth = auth if isinstance(auth, dict) else {}
    device = _verify_device_token(str(auth.get("token") or ""))
    if not device:
        return False
    _cleanup_expired_jobs()
    join_room(f"photo-relay:{device['device_uuid']}")
    with _sid_lock:
        _sid_devices[request.sid] = int(device["id"])
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE photo_relay_devices SET last_connected_at=UTC_TIMESTAMP() WHERE id=%s",
        (device["id"],),
    )
    db.commit()
    db.close()
    register_connection(socketio, "/photo-relay")
    emit(
        "photo_relay_connected",
        {
            "device_uuid": device["device_uuid"],
            "device_name": device["label"],
            "queued_files": _queued_files(int(device["id"])),
        },
    )


@socketio.on("photo_relay_resync", namespace="/photo-relay")
def photo_relay_resync(_payload=None):
    with _sid_lock:
        device_id = _sid_devices.get(request.sid)
    if not device_id:
        disconnect()
        return
    emit("photo_relay_queue", {"files": _queued_files(device_id)})


@socketio.on("disconnect", namespace="/photo-relay")
def photo_relay_socket_disconnect():
    with _sid_lock:
        _sid_devices.pop(request.sid, None)
    unregister_connection(socketio, "/photo-relay")
