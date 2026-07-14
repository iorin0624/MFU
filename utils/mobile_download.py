"""Login-free mobile download jobs for saving selected JPEGs to Photos."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    render_template_string,
    request,
    send_file,
    url_for,
)

from app.utils.db import get_db
from app.utils.upload_security import (
    can_access_upload_record,
    fetch_upload_access_record,
    has_view_auth,
    resolve_upload_subpath,
)


mobile_download_bp = Blueprint("mobile_download", __name__)

APP_NAME = "MFU Download"
ALBUM_NAME = "iori0624"
ANDROID_PACKAGE = "jp.iori0624.mfudownload"
IOS_BUNDLE_ID = "jp.iori0624.mfudownload"
LAUNCH_TOKEN_MINUTES = 10
SESSION_TOKEN_HOURS = 24
MAX_JOB_FILES = 1000
LAUNCH_TOKEN_PREFIX = "mfu_launch_"
ACCESS_TOKEN_PREFIX = "mfu_dl_"

_schema_ready = False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _image_id(upload_uuid: str, filename: str) -> str:
    value = f"{upload_uuid}\0{filename}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


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
        db.commit()
    finally:
        db.close()
    _schema_ready = True


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


def _selected_jpegs(upload: dict, paths: list) -> list[dict]:
    if not isinstance(paths, list) or not paths:
        return []
    if len(paths) > MAX_JOB_FILES:
        raise ValueError("too_many_files")

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT filename FROM files WHERE upload_id=%s",
            (upload["id"],),
        )
        allowed_names = {str(row["filename"]) for row in cur.fetchall()}
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
        if filename not in allowed_names or Path(filename).suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        target = ref["target"]
        if filename in seen or not target.is_file():
            continue
        seen.add(filename)
        selected.append(
            {
                "id": _image_id(upload_uuid, filename),
                "name": filename,
                "size": target.stat().st_size,
            }
        )
    return selected


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


@mobile_download_bp.post("/mobile-download/api/jobs")
def create_job():
    _cleanup_expired_jobs()
    data = request.get_json(silent=True) or {}
    upload_uuid = str(data.get("upload_uuid") or "").strip().lower()
    upload = fetch_upload_access_record(upload_uuid)
    if not upload:
        return jsonify({"ok": False, "error": "upload_not_found"}), 404
    if not can_access_upload_record(upload, has_view_auth_func=has_view_auth):
        return jsonify({"ok": False, "error": "upload_forbidden"}), 403

    try:
        files = _selected_jpegs(upload, data.get("paths") or [])
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not files:
        return jsonify({"ok": False, "error": "no_jpeg_selected"}), 400

    launch_token = LAUNCH_TOKEN_PREFIX + secrets.token_urlsafe(32)
    launch_expires_at = datetime.utcnow() + timedelta(minutes=LAUNCH_TOKEN_MINUTES)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mobile_download_jobs
                (upload_uuid, upload_title, files_json, launch_token_hash,
                 created_at, launch_expires_at)
            VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), %s)
            """,
            (
                upload_uuid,
                str(upload.get("title") or "")[:255],
                json.dumps(files, ensure_ascii=False, separators=(",", ":")),
                _hash_token(launch_token),
                launch_expires_at,
            ),
        )
        job_id = int(cur.lastrowid)
        db.commit()
    finally:
        db.close()

    universal_link = url_for(
        "mobile_download.open_job",
        launch_token=launch_token,
        _external=True,
    )
    custom_scheme = "mfudownload://job?" + urlencode({"token": launch_token})
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "count": len(files),
            "expires_in": LAUNCH_TOKEN_MINUTES * 60,
            "app_url": universal_link,
            "custom_scheme_url": custom_scheme,
        }
    )


@mobile_download_bp.get("/mobile-download/open/<launch_token>")
def open_job(launch_token: str):
    custom_scheme = "mfudownload://job?" + urlencode({"token": launch_token})
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
                <p>アプリを開いて、選択した写真を「iori0624」アルバムへ保存します。</p>
                <a href="{{ custom_scheme }}">アプリを開く</a>
              </main>
            </body>
            </html>
            """,
            custom_scheme=custom_scheme,
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

    ref = resolve_upload_subpath(
        f"{job['upload_uuid']}/original/{item['name']}",
        allow_zip=False,
    )
    if not ref or not ref["target"].is_file():
        return jsonify({"ok": False, "error": "file_not_found"}), 404

    response = send_file(
        ref["target"],
        mimetype="image/jpeg",
        as_attachment=False,
        download_name=item["name"],
        conditional=True,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-MFU-Image-ID"] = image_id
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
    return jsonify({"ok": True})


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

