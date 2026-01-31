from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request, session

from app.utils.db import get_db
from app.utils.logs import write_login_log
from app.utils.mail import send_mail
from app.utils.totp_util import get_user_otp_secret, get_totp_status

mfa_bp = Blueprint("mfa", __name__)

PREAUTH_TTL_MINUTES = 5
TOTP_MAX_ATTEMPTS = 5
TOTP_LOCK_MINUTES = 5
EMAIL_OTP_TTL_MINUTES = 5
EMAIL_OTP_MAX_ATTEMPTS = 5
EMAIL_OTP_SEND_COOLDOWN_SECONDS = 60
EMAIL_OTP_IP_LIMIT_PER_HOUR = 10


def _now() -> datetime:
    return datetime.now()


def _ensure_email_otp_schema() -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_email_otps (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(191) NOT NULL,
            code_hash VARCHAR(255) NOT NULL,
            code_salt VARCHAR(64) NOT NULL,
            expires_at DATETIME NOT NULL,
            attempts INT NOT NULL DEFAULT 0,
            sent_at DATETIME NOT NULL,
            used_at DATETIME NULL,
            ip VARCHAR(64) NULL,
            ua VARCHAR(255) NULL,
            cooldown_until DATETIME NULL,
            INDEX idx_username (username),
            INDEX idx_username_expires (username, expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    db.close()


def _pepper() -> str:
    return str(current_app.config.get("OTP_PEPPER") or current_app.config.get("SECRET_KEY") or "")


def _hash_code(username: str, code: str, salt: str) -> str:
    payload = f"{_pepper()}:{username}:{code}:{salt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_user_row(username: str) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT username, nickname, webhook_url, email FROM users WHERE username = %s",
        (username,),
    )
    row = cur.fetchone()
    db.close()
    return row


def _preauth_valid(username: str) -> bool:
    if session.get("preauth_user") != username:
        return False
    expires_at = session.get("preauth_expires_at")
    if not expires_at:
        return False
    return _now() <= expires_at


def _clear_preauth() -> None:
    session.pop("preauth_user", None)
    session.pop("preauth_expires_at", None)
    session.pop("preauth_totp_attempts", None)
    session.pop("preauth_totp_locked_until", None)


def _establish_login(username: str, *, tag: str) -> None:
    user = _get_user_row(username)
    if not user:
        return
    session["user"] = username
    session["nickname"] = user.get("nickname")
    session["login_expires_at"] = _now() + timedelta(hours=24)
    session["login_extension_count"] = 0
    write_login_log(username, request.remote_addr, tag=tag)
    if username == "admin" and user.get("webhook_url"):
        try:
            login_time = _now().strftime("%Y/%m/%d %H:%M")
            login_ip = request.remote_addr
            message = (
                "👤 **管理者ログイン**\n"
                f"📅 ログイン日時: {login_time}\n"
                f"🌐 ログインIP: {login_ip}"
            )
            send_payload = {"content": message}
            import requests

            requests.post(user["webhook_url"], json=send_payload)
        except Exception:
            pass


@mfa_bp.post("/auth/preauth")
def create_preauth():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    if not username:
        return jsonify(ok=False, error="ユーザー名が必要です"), 400

    user = _get_user_row(username)
    if not user:
        return jsonify(ok=False, error="ユーザーが見つかりません"), 404

    session["preauth_user"] = username
    session["preauth_expires_at"] = _now() + timedelta(minutes=PREAUTH_TTL_MINUTES)
    session.pop("preauth_totp_attempts", None)
    session.pop("preauth_totp_locked_until", None)

    totp_status = get_totp_status(username)
    return jsonify(
        ok=True,
        totp_enabled=bool(totp_status.get("enabled") and totp_status.get("has_secret")),
        email_available=bool((user.get("email") or "").strip()),
    )


@mfa_bp.post("/mfa/totp/verify")
def verify_totp():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    code = (payload.get("code") or "").strip()
    if not username or not code:
        return jsonify(ok=False, error="入力が不足しています"), 400

    if not _preauth_valid(username):
        return jsonify(ok=False, error="認証の有効期限が切れました"), 400

    locked_until = session.get("preauth_totp_locked_until")
    if locked_until and _now() < locked_until:
        return jsonify(ok=False, error="試行回数が多すぎます。時間をおいてください。"), 429

    otp_secret = get_user_otp_secret(username)
    if not otp_secret:
        return jsonify(ok=False, error="TOTPが未設定です"), 400

    import pyotp

    totp = pyotp.TOTP(otp_secret)
    if not totp.verify(code, valid_window=1):
        attempts = int(session.get("preauth_totp_attempts") or 0) + 1
        session["preauth_totp_attempts"] = attempts
        if attempts >= TOTP_MAX_ATTEMPTS:
            session["preauth_totp_locked_until"] = _now() + timedelta(minutes=TOTP_LOCK_MINUTES)
            return jsonify(ok=False, error="試行回数が多すぎます。時間をおいてください。"), 429
        return jsonify(ok=False, error="コードが一致しません"), 400

    _clear_preauth()
    _establish_login(username, tag="LOGIN_TOTP")
    return jsonify(ok=True, redirect="/upload")


@mfa_bp.post("/mfa/email/send")
def send_email_otp():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    if not username:
        return jsonify(ok=False, error="ユーザー名が必要です"), 400

    if not _preauth_valid(username):
        return jsonify(ok=False, error="認証の有効期限が切れました"), 400

    _ensure_email_otp_schema()
    user = _get_user_row(username)
    if not user or not (user.get("email") or "").strip():
        return jsonify(ok=False, error="メールアドレスが登録されていません"), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    now = _now()

    cur.execute(
        "SELECT sent_at FROM user_email_otps WHERE username = %s ORDER BY sent_at DESC LIMIT 1",
        (username,),
    )
    last = cur.fetchone()
    if last and last.get("sent_at") and (now - last["sent_at"]).total_seconds() < EMAIL_OTP_SEND_COOLDOWN_SECONDS:
        db.close()
        return jsonify(ok=False, error="送信間隔が短すぎます。少し時間をおいてください。"), 429

    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM user_email_otps
        WHERE ip = %s AND sent_at >= %s
        """,
        (request.remote_addr, now - timedelta(hours=1)),
    )
    row = cur.fetchone()
    if row and int(row.get("cnt") or 0) >= EMAIL_OTP_IP_LIMIT_PER_HOUR:
        db.close()
        return jsonify(ok=False, error="送信回数が多すぎます。時間をおいてください。"), 429

    code = f"{secrets.randbelow(10 ** 6):06d}"
    salt = secrets.token_hex(16)
    code_hash = _hash_code(username, code, salt)
    expires_at = now + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)

    cur.execute(
        """
        INSERT INTO user_email_otps
        (username, code_hash, code_salt, expires_at, attempts, sent_at, ip, ua)
        VALUES (%s, %s, %s, %s, 0, %s, %s, %s)
        """,
        (
            username,
            code_hash,
            salt,
            expires_at,
            now,
            request.remote_addr,
            (request.user_agent.string or "")[:255],
        ),
    )
    db.commit()
    db.close()

    subject = "【MFU】ログイン用ワンタイムコード"
    body = "\n".join(
        [
            "以下のワンタイムコードを入力してください。",
            f"OTP: {code}",
            f"有効期限: {EMAIL_OTP_TTL_MINUTES}分",
        ]
    )
    try:
        send_mail(to=user["email"], subject=subject, body=body, event_uuid=None)
    except Exception:
        current_app.logger.exception("email otp send failed for %s", username)
        return jsonify(ok=False, error="メール送信に失敗しました"), 500
    return jsonify(ok=True)


@mfa_bp.post("/mfa/email/verify")
def verify_email_otp():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    code = (payload.get("code") or "").strip()
    if not username or not code:
        return jsonify(ok=False, error="入力が不足しています"), 400

    if not _preauth_valid(username):
        return jsonify(ok=False, error="認証の有効期限が切れました"), 400

    _ensure_email_otp_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    now = _now()
    cur.execute(
        """
        SELECT *
        FROM user_email_otps
        WHERE username = %s
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        (username,),
    )
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify(ok=False, error="OTPが未発行です"), 400

    if row.get("used_at"):
        db.close()
        return jsonify(ok=False, error="OTPは既に使用されています"), 400

    if row.get("cooldown_until") and row["cooldown_until"] > now:
        db.close()
        return jsonify(ok=False, error="試行回数が多すぎます。時間をおいてください。"), 429

    if row.get("expires_at") and row["expires_at"] < now:
        db.close()
        return jsonify(ok=False, error="OTPの有効期限が切れています"), 400

    expected = _hash_code(username, code, row["code_salt"])
    if expected != row["code_hash"]:
        attempts = int(row.get("attempts") or 0) + 1
        cooldown_until = row.get("cooldown_until")
        if attempts >= EMAIL_OTP_MAX_ATTEMPTS:
            cooldown_until = now + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)
        cur.execute(
            """
            UPDATE user_email_otps
            SET attempts = %s, cooldown_until = %s
            WHERE id = %s
            """,
            (attempts, cooldown_until, row["id"]),
        )
        db.commit()
        db.close()
        if attempts >= EMAIL_OTP_MAX_ATTEMPTS:
            return jsonify(ok=False, error="試行回数が多すぎます。時間をおいてください。"), 429
        return jsonify(ok=False, error="コードが一致しません"), 400

    cur.execute(
        "UPDATE user_email_otps SET used_at = %s WHERE id = %s",
        (now, row["id"]),
    )
    db.commit()
    db.close()

    _clear_preauth()
    _establish_login(username, tag="LOGIN_EMAIL_OTP")
    return jsonify(ok=True, redirect="/upload")
