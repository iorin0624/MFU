from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import disconnect, emit

from app.chat.socketio_ext import socketio
from app.utils.media_clipboard_auth import (
    issue_media_clipboard_token,
    verify_media_clipboard_token,
)
from app.utils.photo_relay import _issue_device_token, _valid_device_uuid
from app.utils.notification_service import issue_notification_token


media_hub_bp = Blueprint("media_hub", __name__, url_prefix="/desktop/media-hub")
WINDOWS_DOWNLOAD_PATH = Path(os.environ.get("MFU_MEDIA_HUB_WINDOWS_ZIP", "/mnt/mfu/downloads/MFUMediaHub.zip"))

_sid_tokens: dict[str, str] = {}


def _valid_callback(value: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    return value if parsed.port else ""


@media_hub_bp.get("/login/start")
def login_start():
    callback = _valid_callback(request.args.get("callback") or "")
    state = (request.args.get("state") or "").strip()
    device_uuid = _valid_device_uuid(request.args.get("device_uuid") or "")
    device_name = (request.args.get("device_name") or "Windows PC").strip()[:120]
    if not callback or not state or not device_uuid:
        return "Invalid callback or device", 400
    if not session.get("user"):
        next_url = url_for(
            "media_hub.login_start",
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
        <title>MFU Media Hub 認可</title><style>
        body{font-family:system-ui,"Yu Gothic",sans-serif;background:#f6f7fb;color:#1f2937;margin:0;min-height:100vh;display:grid;place-items:center}
        main{width:min(540px,calc(100vw - 32px));background:#fff;border:1px solid #d1d5db;border-radius:12px;box-shadow:0 18px 45px #0f172a24;padding:28px}
        h1{font-size:22px;margin:0 0 14px}p{line-height:1.7}.actions{display:flex;gap:10px;justify-content:flex-end;margin-top:24px}
        button,a{font:inherit}button{background:#0b63dd;color:#fff;border:0;border-radius:7px;padding:10px 16px;cursor:pointer}a{color:#4b5563;text-decoration:none;padding:10px 12px}
        </style></head><body><main><h1>MFU Media Hub を許可しますか？</h1>
        <p><strong>{{ username }}</strong> として、Windows端末 <strong>{{ device_name }}</strong> に写真転送、Media Clipboard、MFU通知の受信を許可します。</p>
        <p>ChromeのCookieやパスワードはアプリへ渡されません。3機能の専用トークンを一度に発行します。</p>
        <form method="post" action="{{ url_for('media_hub.login_approve') }}" class="actions">
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


@media_hub_bp.post("/login/approve")
def login_approve():
    callback = _valid_callback(request.form.get("callback") or "")
    state = (request.form.get("state") or "").strip()
    device_uuid = _valid_device_uuid(request.form.get("device_uuid") or "")
    device_name = (request.form.get("device_name") or "Windows PC").strip()[:120]
    username = str(session.get("user") or "").strip()
    if not callback or not state or not device_uuid:
        return "Invalid callback or device", 400
    if not username:
        return redirect(url_for("media_hub.login_start", callback=callback, state=state,
                                device_uuid=device_uuid, device_name=device_name))
    photo_token = _issue_device_token(username, device_uuid, device_name)
    media_token = issue_media_clipboard_token(username, "MFU Media Hub")
    notification_token = issue_notification_token(username, device_uuid, device_name)
    separator = "&" if "?" in callback else "?"
    return redirect(callback + separator + urlencode({
        "photo_token": photo_token,
        "media_token": media_token,
        "notification_token": notification_token,
        "state": state,
    }))


@media_hub_bp.post("/api/notification-token")
def notification_token_upgrade():
    """Upgrade an existing Media Hub install without another browser login."""
    media_row = verify_media_clipboard_token()
    if not media_row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    body = request.get_json(silent=True) or {}
    device_uuid = _valid_device_uuid(str(body.get("device_uuid") or ""))
    device_name = str(body.get("device_name") or "Windows PC").strip()[:120]
    if not device_uuid:
        return jsonify({"ok": False, "error": "invalid_device_uuid"}), 400
    token = issue_notification_token(str(media_row["username"]), device_uuid, device_name)
    return jsonify({"ok": True, "notification_token": token})


def _internal_request(token: str, method: str, path: str, payload: dict | None = None):
    app = current_app._get_current_object()
    headers = {"Authorization": f"Bearer {token}"}
    with app.test_client() as client:
        response = client.open(path, method=method, json=payload, headers=headers)
        data = response.get_json(silent=True)
    if not isinstance(data, dict):
        data = {"ok": False, "error": f"HTTP {response.status_code}: JSON以外の応答です。"}
    if response.status_code >= 400:
        data.setdefault("ok", False)
        data.setdefault("error", f"HTTP {response.status_code}")
    return data


_OPERATIONS = {
    "session": ("GET", "/desktop/media-clipboard/api/session"),
    "folders": ("GET", "/image_viewer/api/images"),
    "next_number": ("GET", "/image_viewer/api/instagram/next-number"),
    "instagram_browser_start": ("POST", "/image_viewer/api/instagram/browser/start"),
    "fetch_images": ("POST", "/image_viewer/api/instagram/fetch"),
    "fetch_videos": ("POST", "/image_viewer/api/video/fetch"),
    "fetch_video_frames": ("POST", "/image_viewer/api/video/frames/fetch"),
    "save_images": ("POST", "/image_viewer/api/instagram/save"),
    "save_videos": ("POST", "/image_viewer/api/video/save-async"),
}


def _direct_job_status(operation: str, data: dict) -> dict:
    """Read the in-process worker state without issuing nested Flask requests.

    The job starter and this Socket.IO handler run in the same Gunicorn worker,
    so reading the worker's synchronized job store is both reliable and avoids
    consuming a database connection on every progress tick.
    """
    from app.image_viewer.routes import (
        _read_instagram_job,
        _read_video_job,
        _read_video_save_job,
    )

    if operation in {"fetch_images", "fetch_video_frames"}:
        job_id = str(data.get("jobId") or "")
        job = _read_instagram_job(job_id) if job_id else {}
        if not job:
            return {"ok": False, "status": "error", "error": "取得ジョブが見つかりません。"}
        images = []
        for item in job.get("images") or []:
            if not isinstance(item, dict):
                continue
            next_item = dict(item)
            next_item.pop("previewCache", None)
            index = int(next_item.get("index") or 0)
            next_item["previewUrl"] = f"/image_viewer/api/instagram/jobs/{job_id}/preview/{index}"
            images.append(next_item)
        status = str(job.get("status") or "pending")
        return {
            "ok": status not in {"error"},
            "status": status,
            "source": job.get("source") or "instagram",
            "shortcode": job.get("shortcode"),
            "loginRequired": status == "login_required",
            "error": job.get("error"),
            "total": int(job.get("total") or len(images)),
            "processed": int(job.get("processed") or 0),
            "downloaded": int(job.get("downloaded") or 0),
            "failed": int(job.get("failed") or 0),
            "images": images,
        }
    if operation == "fetch_videos":
        job_id = str(data.get("jobId") or "")
        job = _read_video_job(job_id) if job_id else {}
        if not job:
            return {"ok": False, "status": "error", "error": "取得ジョブが見つかりません。"}
        status = str(job.get("status") or "pending")
        videos = job.get("videos") or []
        return {
            "ok": status not in {"error"},
            "status": status,
            "source": job.get("source") or "instagram",
            "identifier": job.get("identifier"),
            "loginRequired": status == "login_required",
            "error": job.get("error"),
            "total": int(job.get("total") or len(videos)),
            "processed": int(job.get("processed") or len(videos)),
            "downloaded": int(job.get("downloaded") or len(videos)),
            "failed": int(job.get("failed") or 0),
            "videos": videos,
        }
    if operation == "save_videos":
        job_id = str(data.get("saveJobId") or "")
        job = _read_video_save_job(job_id) if job_id else {}
        if not job:
            return {"ok": False, "status": "error", "error": "動画保存ジョブが見つかりません。"}
        status = str(job.get("status") or "pending")
        return {
            "ok": status != "error",
            "status": status,
            "error": job.get("error"),
            "processed": int(job.get("processed") or 0),
            "total": int(job.get("total") or 0),
            "saved": job.get("saved") or [],
            "duplicates": job.get("duplicates") or [],
            "errors": job.get("errors") or [],
        }
    return data


@socketio.on("connect", namespace="/media-clipboard")
def media_clipboard_connect(auth=None):
    auth = auth if isinstance(auth, dict) else {}
    token = str(auth.get("media_token") or auth.get("token") or "")
    if not verify_media_clipboard_token(token):
        return False
    _sid_tokens[request.sid] = token
    emit("media_clipboard_connected", {"ok": True})


@socketio.on("media_clipboard_request", namespace="/media-clipboard")
def media_clipboard_request(payload=None):
    body = payload if isinstance(payload, dict) else {}
    operation = str(body.get("operation") or "")
    request_id = str(body.get("request_id") or secrets.token_urlsafe(12))[:80]
    token = _sid_tokens.get(request.sid, "")
    if not token:
        disconnect()
        return {"ok": False, "error": "invalid_token", "request_id": request_id}
    route = _OPERATIONS.get(operation)
    if not route:
        return {"ok": False, "error": "unsupported_operation", "request_id": request_id}
    method, path = route
    params = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if method == "GET" and params:
        from urllib.parse import urlencode as encode_query
        path += "?" + encode_query(params)
    data = _internal_request(token, method, path, params if method == "POST" else None)
    data["request_id"] = request_id
    is_background_job = operation in {"fetch_images", "fetch_video_frames", "fetch_videos", "save_videos"}
    if not data.get("ok") or not is_background_job:
        return data

    attempts = 1800 if operation == "save_videos" else 180
    for _ in range(attempts):
        socketio.sleep(1)
        current = _direct_job_status(operation, data)
        current.update({"request_id": request_id, "operation": operation})
        emit("media_clipboard_progress", current)
        if str(current.get("status") or "") in {"done", "error", "cancelled", "login_required"}:
            return current
    return {"ok": False, "error": "取得がタイムアウトしました。", "request_id": request_id}


@socketio.on("disconnect", namespace="/media-clipboard")
def media_clipboard_disconnect():
    _sid_tokens.pop(request.sid, None)


@media_hub_bp.get("/api/health")
def health():
    return jsonify({"ok": True, "websocket_namespace": "/media-clipboard"})


@media_hub_bp.get("/download/windows")
def download_windows():
    if not session.get("user"):
        return redirect(url_for("login", next=request.url))
    if not WINDOWS_DOWNLOAD_PATH.is_file():
        return "Windows版は準備中です。", 404
    response = send_file(
        WINDOWS_DOWNLOAD_PATH,
        mimetype="application/zip",
        as_attachment=True,
        download_name="MFUMediaHub.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response
