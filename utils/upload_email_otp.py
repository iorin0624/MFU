"""Email PIN authentication for public upload viewing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from flask import current_app

from app.utils.db import get_db
from app.utils.mail import send_mail


OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_UPLOAD_HOURLY_LIMIT = 10
OTP_IP_HOURLY_LIMIT = 5


class UploadOtpError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "otp_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def mask_email(value: object) -> str:
    email = normalize_email(value)
    if "@" not in email:
        return "未設定"
    local, domain = email.rsplit("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[0]}{'*' * max(3, len(local) - 1)}@{domain}"


def _code_hash(upload_id: int, email: str, code: str) -> str:
    secret = str(current_app.secret_key or current_app.config.get("SECRET_KEY") or "")
    raw = f"{secret}\0{int(upload_id)}\0{normalize_email(email)}\0{code}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def replace_upload_otp_recipient(upload_id: int, email: str) -> None:
    """Replace the To recipient and invalidate every prior grant/code for the upload."""
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("email is required")
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE uploads
               SET otp_email=%s, auth_version=auth_version+1
             WHERE id=%s AND auth_method='email_otp'
            """,
            (normalized, int(upload_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("email OTP is not enabled for this upload")
        cur.execute(
            "UPDATE upload_email_otps SET used_at=COALESCE(used_at, NOW()) WHERE upload_id=%s",
            (int(upload_id),),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _invalidate_otp(otp_id: int) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE upload_email_otps SET used_at=COALESCE(used_at, NOW()) WHERE id=%s",
            (int(otp_id),),
        )
        db.commit()
    finally:
        db.close()


def send_upload_otp(upload: dict, *, request_ip: str, view_url: str) -> dict:
    upload_id = int(upload["id"])
    email = normalize_email(upload.get("otp_email"))
    if not email:
        raise UploadOtpError(
            "閲覧先メールアドレスがまだ登録されていません。",
            status=409,
            code="recipient_not_configured",
        )

    now = datetime.now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    request_ip = str(request_ip or "")[:64]

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT created_at FROM upload_email_otps WHERE upload_id=%s ORDER BY id DESC LIMIT 1",
            (upload_id,),
        )
        latest = cur.fetchone() or {}
        if latest.get("created_at") and (now - latest["created_at"]).total_seconds() < OTP_RESEND_SECONDS:
            remaining = OTP_RESEND_SECONDS - int((now - latest["created_at"]).total_seconds())
            raise UploadOtpError(
                f"再送信は{max(1, remaining)}秒後にできます。",
                status=429,
                code="resend_limited",
            )

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM upload_email_otps WHERE upload_id=%s AND created_at >= NOW() - INTERVAL 1 HOUR",
            (upload_id,),
        )
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_UPLOAD_HOURLY_LIMIT:
            raise UploadOtpError(
                "認証コードの送信回数が上限に達しました。時間をおいてお試しください。",
                status=429,
                code="upload_rate_limited",
            )

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM upload_email_otps WHERE request_ip=%s AND created_at >= NOW() - INTERVAL 1 HOUR",
            (request_ip,),
        )
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_IP_HOURLY_LIMIT:
            raise UploadOtpError(
                "認証コードの送信回数が上限に達しました。時間をおいてお試しください。",
                status=429,
                code="ip_rate_limited",
            )

        cur.execute(
            "UPDATE upload_email_otps SET used_at=COALESCE(used_at, NOW()) WHERE upload_id=%s AND used_at IS NULL",
            (upload_id,),
        )
        cur.execute(
            """
            INSERT INTO upload_email_otps
                (upload_id, email, code_hash, request_ip, attempts, expires_at)
            VALUES (%s, %s, %s, %s, 0, %s)
            """,
            (upload_id, email, _code_hash(upload_id, email, code), request_ip, expires_at),
        )
        otp_id = int(cur.lastrowid)
        db.commit()
    except UploadOtpError:
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    body = (
        "MFUのファイル閲覧用認証コードです。\n\n"
        f"認証コード: {code}\n"
        f"有効期限: {OTP_TTL_MINUTES}分\n\n"
        f"閲覧ページ: {view_url}\n\n"
        "この操作に心当たりがない場合は、このメールを破棄してください。"
    )
    try:
        send_mail(
            email,
            subject="MFU ファイル閲覧用認証コード",
            body=body,
            event_uuid="upload-otp",
            append_signature=False,
            mail_kind="upload_view_otp",
        )
    except Exception:
        _invalidate_otp(otp_id)
        raise

    return {"masked_email": mask_email(email), "expires_at": expires_at}


def verify_upload_otp(upload: dict, code: str) -> bool:
    upload_id = int(upload["id"])
    email = normalize_email(upload.get("otp_email"))
    candidate = str(code or "").strip()
    if len(candidate) != 6 or not candidate.isdigit() or not email:
        return False

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, code_hash, attempts, expires_at
              FROM upload_email_otps
             WHERE upload_id=%s AND email=%s AND used_at IS NULL
             ORDER BY id DESC LIMIT 1
             FOR UPDATE
            """,
            (upload_id, email),
        )
        row = cur.fetchone()
        if not row or row["expires_at"] < datetime.now() or int(row.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
            if row:
                cur.execute("UPDATE upload_email_otps SET used_at=NOW() WHERE id=%s", (row["id"],))
                db.commit()
            return False

        valid = hmac.compare_digest(row["code_hash"], _code_hash(upload_id, email, candidate))
        if valid:
            cur.execute("UPDATE upload_email_otps SET used_at=NOW() WHERE id=%s", (row["id"],))
        else:
            cur.execute(
                """
                UPDATE upload_email_otps
                   SET attempts=attempts+1,
                       used_at=CASE WHEN attempts+1 >= %s THEN NOW() ELSE used_at END
                 WHERE id=%s
                """,
                (OTP_MAX_ATTEMPTS, row["id"]),
            )
        db.commit()
        return valid
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
