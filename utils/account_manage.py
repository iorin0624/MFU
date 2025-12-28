from flask import Blueprint, render_template, session, redirect, url_for, request, flash
import bcrypt
from app.utils.db import get_db

account_bp = Blueprint("account", __name__)

@account_bp.route("/account", methods=["GET", "POST"])
def manage_account():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]

    db = get_db()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        nickname = request.form.get("nickname", "").strip()
        email = request.form.get("email", "").strip()
        webhook_url = request.form.get("webhook_url", "").strip()
        notify_method = request.form.get("notify_method", "discord")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if password or confirm:
            if password != confirm:
                flash("パスワードが一致しません", "danger")
                return redirect(url_for("account.manage_account"))
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cursor.execute("""
                UPDATE users SET nickname = %s, password_hash = %s, email = %s, webhook_url = %s, notify_method = %s
                WHERE username = %s
            """, (nickname, hashed, email, webhook_url, notify_method, username))
        else:
            cursor.execute("""
                UPDATE users SET nickname = %s, email = %s, webhook_url = %s, notify_method = %s
                WHERE username = %s
            """, (nickname, email, webhook_url, notify_method, username))

        db.commit()
        db.close()
        flash("アカウント情報を更新しました", "success")
        return redirect(url_for("account.manage_account"))

    cursor.execute("SELECT nickname, email, webhook_url, notify_method FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    db.close()

    return render_template("account.html", user=user, username=username)
