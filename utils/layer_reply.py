from __future__ import annotations

import shutil
import uuid as uuidlib
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, redirect, request, send_file, url_for
from werkzeug.utils import secure_filename

from app.utils.db import get_db
from app.utils.image import save_as_jpeg
from app.utils.layer_reply_store import (
    create_layer_reply,
    get_layer_reply,
    layer_reply_file_exists,
)
from app.utils.mail import send_mail
from app.utils.upload_notifications import (
    build_processed_upload_discord_embed,
    build_processed_upload_message,
    send_discord_upload_notification,
)
from app.utils.upload_security import (
    can_access_upload_record_from_session,
    grant_layer_reply_upload_auth,
    grant_layer_reply_view_auth,
    has_layer_reply_upload_auth,
    has_layer_reply_view_auth,
)


layer_reply_bp = Blueprint("layer_reply", __name__)
UPLOAD_BASE_DIR = "/mnt/mfu/uploads"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _layer_root() -> Path:
    return (Path(current_app.config.get("STORAGE_ROOT", UPLOAD_BASE_DIR)) / "layer_uploads").resolve()


def _fetch_layer_upload(uuid: str) -> tuple[dict | None, dict | None]:
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT * FROM uploads
             WHERE uuid=%s AND layer_deleted_at IS NULL
             LIMIT 1
            """,
            (uuid,),
        )
        upload = cursor.fetchone()
        if not upload:
            return None, None
        cursor.execute(
            """
            SELECT mode.enable_layer_upload_url, mode.require_password,
                   users.webhook_url, users.email, users.notify_method
              FROM upload_modes AS mode
              LEFT JOIN users ON users.username=mode.username
             WHERE mode.username=%s AND mode.mode=%s
             LIMIT 1
            """,
            (upload["username"], upload["mode"]),
        )
        settings = cursor.fetchone() or {}
        upload["require_password"] = settings.get("require_password")
        return upload, settings
    finally:
        cursor.close()
        db.close()


def _reply_access(upload: dict) -> tuple[bool, bool]:
    full_access = can_access_upload_record_from_session(upload)
    upload_only = has_layer_reply_upload_auth(str(upload.get("uuid") or ""))
    return full_access, upload_only


def _save_reply(upload: dict, user: dict, files: list, comment: str) -> dict:
    if not files or not any(file and file.filename for file in files):
        raise ValueError("ファイルが選択されていません。")

    upload_uuid = str(upload["uuid"])
    reply_uuid = uuidlib.uuid4().hex
    base_dir = _layer_root() / secure_filename(upload_uuid) / secure_filename(reply_uuid)
    original_dir = base_dir / "original"
    original_dir.mkdir(parents=True, exist_ok=True)

    posted_at = datetime.now()
    prefix = f"{posted_at.strftime('%Y年%m月%d日_%H時%M分')}_{upload['title']}"
    saved_files: list[str] = []
    for index, file in enumerate(files, start=1):
        if not file or not file.filename:
            continue
        filename = f"{prefix}_{index:04}.jpg"
        if save_as_jpeg(file.stream, str(original_dir / filename)):
            saved_files.append(filename)

    if not saved_files:
        shutil.rmtree(base_dir, ignore_errors=True)
        raise ValueError("保存できる画像がありませんでした。")

    try:
        create_layer_reply(
            upload_id=int(upload["id"]),
            reply_uuid=reply_uuid,
            title_snapshot=str(upload.get("title") or ""),
            comment=comment,
            posted_at=posted_at,
            image_filenames=saved_files,
        )
    except Exception:
        shutil.rmtree(base_dir, ignore_errors=True)
        current_app.logger.exception(
            "レイヤーアップロードのDB登録に失敗: upload_uuid=%s reply_uuid=%s",
            upload_uuid,
            reply_uuid,
        )
        raise

    detail_url = f"https://mfu.iori0624.jp/layer_upload_list/{upload_uuid}"
    message = build_processed_upload_message(
        title=str(upload.get("title") or ""),
        comment=comment,
        detail_url=detail_url,
        image_count=len(saved_files),
        posted_at=posted_at,
    )
    embed = build_processed_upload_discord_embed(
        title=str(upload.get("title") or ""),
        comment=comment,
        detail_url=detail_url,
        image_count=len(saved_files),
        posted_at=posted_at,
    )

    notify_method = str(user.get("notify_method") or "discord")
    send_discord_upload_notification(
        logger=current_app.logger,
        username=str(upload.get("username") or ""),
        notify_method=notify_method,
        webhook_url=user.get("webhook_url"),
        upload_id=reply_uuid,
        message=message,
        context_label="layer upload",
        embed=embed,
        feature_key="layer_reply",
    )
    if notify_method.strip().lower() in {"email", "both"} and str(user.get("email") or "").strip():
        try:
            send_mail(
                to=user["email"], subject="加工済み写真アップロード通知",
                body=message, event_uuid="notify", timeout=10,
            )
        except Exception as exc:
            current_app.logger.exception(
                "layer upload メール通知に失敗: user=%s upload_id=%s err=%r",
                upload.get("username"), reply_uuid, exc,
            )

    return {"reply_uuid": reply_uuid, "posted_at": posted_at, "count": len(saved_files)}


@layer_reply_bp.route("/layer_upload/<uuid>", methods=["GET", "POST"])
def layer_upload(uuid):
    upload, user = _fetch_layer_upload(uuid)
    if not upload or not user or not user.get("enable_layer_upload_url"):
        return "対象のアップロードが見つかりません", 404
    grant_layer_reply_upload_auth(uuid)
    if request.method == "POST":
        try:
            _save_reply(upload, user, request.files.getlist("photos"), str(request.form.get("comment") or ""))
        except ValueError as exc:
            return str(exc), 400
    return redirect(url_for("view_upload", uuid=uuid, section="reply") + "#reply")


@layer_reply_bp.post("/view/<uuid>/replies")
def public_replies(uuid):
    upload, user = _fetch_layer_upload(uuid)
    if not upload or not user or not user.get("enable_layer_upload_url"):
        return jsonify(ok=False, message="対象のアップロードが見つかりません。"), 404
    full_access, upload_only = _reply_access(upload)
    if not full_access and not upload_only:
        return jsonify(ok=False, message="アップロード権限がありません。"), 403
    try:
        result = _save_reply(upload, user, request.files.getlist("photos"), str(request.form.get("comment") or ""))
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    return jsonify(
        ok=True, replyUuid=result["reply_uuid"],
        postedAt=result["posted_at"].isoformat(), count=result["count"],
    ), 201


def _safe_reply_image(upload_uuid: str, reply_uuid: str, filename: str) -> Path | None:
    if any(Path(value).name != value for value in (upload_uuid, reply_uuid, filename)):
        return None
    base = (_layer_root() / secure_filename(upload_uuid) / secure_filename(reply_uuid) / "original").resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file() or target.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return target


@layer_reply_bp.get("/view/<uuid>/replies/<reply_uuid>/images/<filename>")
def public_reply_image(uuid, reply_uuid, filename):
    upload, _ = _fetch_layer_upload(uuid)
    if not upload:
        abort(404)
    if not (
        can_access_upload_record_from_session(upload)
        or has_layer_reply_view_auth(uuid, reply_uuid)
    ):
        abort(403)
    if not layer_reply_file_exists(upload_id=int(upload["id"]), reply_uuid=reply_uuid, filename=filename):
        abort(404)
    target = _safe_reply_image(uuid, reply_uuid, filename)
    if not target:
        abort(404)
    return send_file(target, conditional=True)


@layer_reply_bp.get("/layer_reply/<reply_uuid>")
def view_reply(reply_uuid):
    info = get_layer_reply(reply_uuid)
    if not info:
        abort(404)
    grant_layer_reply_upload_auth(str(info["upload_uuid"]))
    grant_layer_reply_view_auth(str(info["upload_uuid"]), reply_uuid)
    return redirect(url_for("view_upload", uuid=info["upload_uuid"], section="replies") + "#replies")
