from flask import Blueprint, render_template, request, abort, redirect, url_for, current_app
import os
import json
import uuid as uuidlib
import urllib.parse
from datetime import datetime

from app.utils.db import get_db
from app.utils.image import save_as_jpeg
from app.utils.file_ops import create_zip
from app.utils.mail import send_mail  # ← 追加：送信はmail.pyに統一
from app.utils.upload_notifications import build_processed_upload_message, send_discord_upload_notification

layer_reply_bp = Blueprint("layer_reply", __name__)
UPLOAD_BASE_DIR = "/mnt/mfu/uploads"
LAYER_ROOT = os.path.join(UPLOAD_BASE_DIR, "layer_uploads")

@layer_reply_bp.route("/layer_upload/<uuid>", methods=["GET", "POST"])
def layer_upload(uuid):
    db = get_db()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT title, date, username FROM uploads WHERE uuid = %s AND mode = 'layer'", (uuid,))
    upload = cursor.fetchone()

    if not upload:
        return "対象のアップロードが見つかりません", 404

    cursor.execute("SELECT webhook_url, email, notify_method FROM users WHERE username = %s", (upload["username"],))
    user = cursor.fetchone()
    db.close()

    if request.method == "POST":
        comment = request.form.get("comment", "")
        files = request.files.getlist("photos")
        if not files:
            return "ファイルが選択されていません", 400

        reply_uuid = uuidlib.uuid4().hex
        base_dir = os.path.join(LAYER_ROOT, uuid, reply_uuid)
        original_dir = os.path.join(base_dir, "original")
        zip_dir = os.path.join(base_dir, "zip")
        os.makedirs(original_dir, exist_ok=True)
        os.makedirs(zip_dir, exist_ok=True)

        now_str = datetime.now().strftime("%Y年%m月%d日_%H時%M分")
        prefix = f"{now_str}_{upload['title']}"
        saved_files = []

        for idx, file in enumerate(files, 1):
            filename = f"{prefix}_{idx:04}.jpg"
            save_path = os.path.join(original_dir, filename)
            success = save_as_jpeg(file.stream, save_path)
            if not success:
                continue
            saved_files.append(filename)

        zip_path = os.path.join(zip_dir, f"{prefix}.zip")
        create_zip(zip_path, [os.path.join(original_dir, f) for f in saved_files])

        info = {
            "reply_uuid": reply_uuid,
            "comment": comment,
            "title": upload["title"],
            "filenames": saved_files,
            "created": datetime.now().isoformat()
        }
        with open(os.path.join(base_dir, "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

        # 通知URL
        zip_filename_encoded = urllib.parse.quote(os.path.basename(zip_path))
        zip_url = f"https://mfu.iori0624.jp/uploads/layer_uploads/{uuid}/{reply_uuid}/zip/{zip_filename_encoded}"

        msg = build_processed_upload_message(
            title=upload["title"],
            comment=comment,
            download_url=zip_url,
        )

        if user:
            notify_method = user.get("notify_method", "discord")
            webhook_url = user.get("webhook_url")
            email = user.get("email")

            send_discord_upload_notification(
                logger=current_app.logger,
                username=upload["username"],
                notify_method=notify_method,
                webhook_url=webhook_url,
                upload_id=reply_uuid,
                message=msg,
                context_label="layer upload",
            )

            if (notify_method or "").strip().lower() in ("email", "both") and (email or "").strip():
                try:
                    # mail.py 統一呼び出し
                    send_mail(
                        to=email,
                        subject="加工済み写真アップロード通知",
                        body=msg,
                        event_uuid="notify",               # From: notify@mail.iori0624.jp
                        smtp_host="192.168.103.15",
                        smtp_port=25,
                        timeout=10,
                    )
                except Exception as e:
                    current_app.logger.exception("layer upload メール通知に失敗: user=%s upload_id=%s err=%r", upload["username"], reply_uuid, e)

        # レイヤーさん用の履歴ページへリダイレクト
        return redirect(url_for('layer_reply.view_reply', reply_uuid=reply_uuid))

    try:
        formatted_date = datetime.strptime(str(upload['date']), "%Y-%m-%d").strftime("%Y年%m月%d日")
    except Exception:
        formatted_date = upload['date']

    return render_template("layer_upload.html", title=upload['title'], uuid=uuid, date=formatted_date)


@layer_reply_bp.route("/layer_reply/<reply_uuid>")
def view_reply(reply_uuid):
    # reply_uuid を含むパスを探索
    for parent_uuid in os.listdir(LAYER_ROOT):
        candidate = os.path.join(LAYER_ROOT, parent_uuid, reply_uuid)
        if os.path.isdir(candidate):
            info_path = os.path.join(candidate, "info.json")
            if os.path.exists(info_path):
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                return render_template("layer_reply.html", info=info, uuid=parent_uuid, reply_uuid=reply_uuid)
    return abort(404)
