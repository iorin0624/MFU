from flask import Blueprint, request, session, redirect, url_for, render_template, send_file, jsonify, flash
from app.utils.totp_util import (
    assign_otp_secret_to_user,
    disable_totp_for_user,
    get_user_otp_secret,
    reset_totp_secret,
)
import pyotp
import qrcode
import io

# Blueprintの定義（これを最初に！）
otp_bp = Blueprint("otp", __name__, url_prefix="/otp")


def _generate_qr_response(username: str, otp_secret: str):
    uri = pyotp.totp.TOTP(otp_secret).provisioning_uri(name=username, issuer_name="MFU")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@otp_bp.route("/setup")
def setup_otp():
    # 互換維持: /otp/setup は /otp/qr と同じ挙動
    username = session.get("user")
    if not username:
        return "ログインしてください", 401

    otp_secret = assign_otp_secret_to_user(username)
    return _generate_qr_response(username, otp_secret)


@otp_bp.route("/qr")
def qr_otp():
    username = session.get("user")
    if not username:
        return "ログインしてください", 401

    otp_secret = assign_otp_secret_to_user(username)
    return _generate_qr_response(username, otp_secret)

@otp_bp.route("/input", methods=["GET"])
def otp_input_form():
    return render_template("otp_input.html")

@otp_bp.route("/verify", methods=["POST"])
def verify_otp():
    username = session.get("user")
    if not username:
        return "ログインしてください", 401

    user_input = request.form.get("otp_code")
    otp_secret = get_user_otp_secret(username)
    if not otp_secret:
        return "TOTP未設定", 400

    totp = pyotp.TOTP(otp_secret)
    if totp.verify(user_input, valid_window=1):
        session["authenticated"] = True
        return redirect(url_for("account.manage_account"))
    return "認証失敗", 403


@otp_bp.post("/reset")
def reset_otp():
    username = session.get("user")
    if not username:
        return jsonify(ok=False, error="ログインしてください"), 401

    payload = request.get_json(silent=True) or {}
    confirm = request.form.get("confirm") or payload.get("confirm")
    if str(confirm) != "1":
        if request.is_json:
            return jsonify(ok=False, error="確認が必要です"), 400
        flash("確認が必要です。", "danger")
        return redirect(url_for("account.manage_account"))

    current_code = request.form.get("current_code") or payload.get("current_code")
    if current_code:
        otp_secret = get_user_otp_secret(username)
        if otp_secret:
            totp = pyotp.TOTP(otp_secret)
            if not totp.verify(str(current_code).strip(), valid_window=1):
                if request.is_json:
                    return jsonify(ok=False, error="現在のコードが一致しません"), 400
                flash("現在のコードが一致しません。", "danger")
                return redirect(url_for("account.manage_account"))

    reset_totp_secret(username)
    if request.is_json:
        return jsonify(ok=True)
    flash("TOTPを再発行しました。QRを再表示してください。", "success")
    return redirect(url_for("account.manage_account"))


@otp_bp.post("/disable")
def disable_otp():
    username = session.get("user")
    if not username:
        return jsonify(ok=False, error="ログインしてください"), 401

    payload = request.get_json(silent=True) or {}
    confirm = request.form.get("confirm") or payload.get("confirm")
    if str(confirm) != "1":
        if request.is_json:
            return jsonify(ok=False, error="確認が必要です"), 400
        flash("確認が必要です。", "danger")
        return redirect(url_for("account.manage_account"))

    disable_totp_for_user(username)
    if request.is_json:
        return jsonify(ok=True)
    flash("TOTPを無効化しました。", "success")
    return redirect(url_for("account.manage_account"))
