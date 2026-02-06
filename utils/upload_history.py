from flask import Blueprint, render_template, session, redirect, url_for, abort, request
import os
import shutil
from app.utils.db import get_db

upload_history_bp = Blueprint("upload_history", __name__)

UPLOAD_BASE_DIR = "/mnt/mfu/uploads"

@upload_history_bp.route("/upload_list")
def upload_list():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    if username == "admin":
        cursor.execute("SELECT * FROM uploads ORDER BY created_at DESC")
    else:
        cursor.execute("SELECT * FROM uploads WHERE username = %s ORDER BY created_at DESC", (username,))
    uploads = cursor.fetchall()
    db.close()

    return render_template("upload_list.html", uploads=uploads, is_admin=(username == "admin"))


@upload_history_bp.route("/upload_delete/<uuid>", methods=["POST"])
def upload_delete(uuid):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    # admin以外は本人のアップロードのみ削除可
    cursor.execute("SELECT * FROM uploads WHERE uuid = %s", (uuid,))
    upload = cursor.fetchone()

    if not upload:
        db.close()
        return abort(404)

    if username != "admin" and upload["username"] != username:
        db.close()
        return abort(403)

    # files, messages, uploads テーブルから削除
    cursor.execute("DELETE FROM files WHERE upload_id = %s", (upload["id"],))
    cursor.execute("DELETE FROM messages WHERE uuid = %s", (uuid,))
    cursor.execute("DELETE FROM uploads WHERE id = %s", (upload["id"],))
    db.commit()
    db.close()

    # 実フォルダ削除
    target_dir = os.path.join(UPLOAD_BASE_DIR, uuid)
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)


    # layer_uploads 側も削除
    layer_dir = os.path.join(UPLOAD_BASE_DIR, "layer_uploads", uuid)
    if os.path.exists(layer_dir):
        shutil.rmtree(layer_dir)

    return redirect(url_for("upload_history.upload_list"))
