from flask import Blueprint, request, session, redirect, url_for, render_template, send_file
from app.utils.totp_util import assign_otp_secret_to_user, get_user_otp_secret
import pyotp
import qrcode
import io

# Blueprintの定義（これを最初に！）
otp_bp = Blueprint("otp", __name__, url_prefix="/otp")

@otp_bp.route("/setup")
def setup_otp():
    username = session.get("user")
    if not username:
        return "ログインしてください", 401

    otp_secret = assign_otp_secret_to_user(username)
    uri = pyotp.totp.TOTP(otp_secret).provisioning_uri(name=username, issuer_name="MFU")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

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
    if totp.verify(user_input):
        session["authenticated"] = True
        return redirect(url_for("dashboard"))  # 必要に応じて "dashboard" を実在する関数名に変更
    return "認証失敗", 403
