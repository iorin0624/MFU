# /mnt/mfu/app/utils/admin_users.py
# 管理者用: ユーザー編集 / ログ閲覧ルート（Blueprint分離）

from flask import (
    Blueprint, session, request, render_template,
    redirect, url_for, flash
)
from datetime import datetime, timedelta
import bcrypt

# アプリ内ユーティリティ
from app.utils.db import get_db

# --- get_netinfo は任意依存：無ければダミーで空情報を返す ---
try:
    from app.utils.netinfo import get_netinfo  # ある環境
except Exception:
    def get_netinfo(ip: str) -> dict:
        # 互換ダミー: 何も解決できない場合は空を返す
        return {"netname": "", "country": ""}

admin_bp = Blueprint("admin_bp", __name__)

# ------------------------------------------------------------
# 管理者チェック用ヘルパ
def _require_admin():
    return session.get("user") == "admin"

# ------------------------------------------------------------
# ユーザー一覧
@admin_bp.route("/admin/users")
def admin_users():
    if not _require_admin():
        return "管理者のみアクセス可能", 403
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT username, nickname, webhook_url, email FROM users ORDER BY username")
    users = cursor.fetchall()
    db.close()
    return render_template("admin_users.html", users=users)

# ユーザー追加
@admin_bp.route("/admin/users/add", methods=["GET", "POST"])
def admin_users_add():
    if not _require_admin():
        return "管理者のみアクセス可能", 403

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        nickname = request.form.get("nickname", "").strip()
        webhook = request.form.get("webhook", "").strip()
        email = request.form.get("email", "").strip()
        notify_method = request.form.get("notify_method", "").strip()

        if not username or not password:
            return "ユーザー名とパスワードは必須です", 400

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO users (username, password_hash, nickname, webhook_url, email, notify_method)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, hashed, nickname, webhook, email, notify_method),
            )
            db.commit()
        finally:
            db.close()

        return redirect(url_for("admin_bp.admin_users"))

    return render_template("admin_user_form.html", action="add", user=None)

# ユーザー編集
@admin_bp.route("/admin/users/edit/<username>", methods=["GET", "POST"])
def admin_users_edit(username):
    if not _require_admin():
        return "管理者のみアクセス可能", 403

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT username, nickname, webhook_url, email, notify_method FROM users WHERE username = %s",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        db.close()
        return "ユーザーが見つかりません", 404

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        nickname = request.form.get("nickname", "").strip()
        webhook = request.form.get("webhook", "").strip()
        email = request.form.get("email", "").strip()
        notify_method = request.form.get("notify_method", "").strip()

        if password or confirm_password:
            if password != confirm_password:
                db.close()
                return "パスワードが一致しません", 400

        if password:
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cursor.execute(
                """
                UPDATE users SET password_hash=%s, nickname=%s, webhook_url=%s, email=%s, notify_method=%s
                WHERE username=%s
                """,
                (hashed, nickname, webhook, email, notify_method, username),
            )
        else:
            cursor.execute(
                """
                UPDATE users SET nickname=%s, webhook_url=%s, email=%s, notify_method=%s
                WHERE username=%s
                """,
                (nickname, webhook, email, notify_method, username),
            )

        db.commit()
        db.close()
        return redirect(url_for("admin_bp.admin_users"))

    db.close()
    return render_template("admin_user_form.html", action="edit", user=user)

# ユーザー削除
@admin_bp.route("/admin/users/delete/<username>", methods=["POST"])
def admin_users_delete(username):
    if not _require_admin():
        return "管理者のみアクセス可能", 403
    if username == "admin":
        flash("adminアカウントは削除できません。")
        return redirect(url_for("admin_bp.admin_users"))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM users WHERE username = %s", (username,))
    db.commit()
    db.close()
    return redirect(url_for("admin_bp.admin_users"))

# ログ閲覧
@admin_bp.route("/admin/logs")
def admin_logs():
    if not _require_admin():
        return "管理者のみアクセス可能", 403

    selected_date = request.args.get("date")
    page = int(request.args.get("page", 1))
    per_page = 2000
    offset = (page - 1) * per_page

    db = get_db()
    cursor = db.cursor(dictionary=True)

    if selected_date:
        cursor.execute(
            """
            SELECT COUNT(*) AS total FROM logs
            WHERE log_date >= %s AND log_date < DATE_ADD(%s, INTERVAL 1 DAY)
            """,
            (selected_date, selected_date),
        )
        total_logs = cursor.fetchone()["total"]
        cursor.execute(
            """
            SELECT id, log_date, ip, log_text FROM logs
            WHERE log_date >= %s AND log_date < DATE_ADD(%s, INTERVAL 1 DAY)
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (selected_date, selected_date, per_page, offset),
        )
    else:
        cursor.execute("SELECT COUNT(*) AS total FROM logs")
        total_logs = cursor.fetchone()["total"]
        cursor.execute(
            """
            SELECT id, log_date, ip, log_text FROM logs
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (per_page, offset),
        )

    rows = cursor.fetchall()
    db.close()

    logs = []
    for row in rows:
        ip = row.get("ip", "")
        netinfo = get_netinfo(ip) if ip else {"netname": "", "country": ""}
        row["netname"] = netinfo.get("netname", "")
        row["country"] = netinfo.get("country", "")
        logs.append(row)

    total_pages = max((total_logs + per_page - 1) // per_page, 1)

    return render_template(
        "admin_logs.html",
        logs=logs,
        selected_date=selected_date,
        now=datetime.utcnow,
        timedelta=timedelta,
        current_page=page,
        total_pages=total_pages,
    )
