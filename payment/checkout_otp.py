from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta

from flask import current_app, request, session

from app.utils.db import get_db
from app.utils.mail import send_mail


OTP_TTL_MINUTES = 5
OTP_GRANT_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_SEND_COOLDOWN_SECONDS = 60
OTP_SCOPE_LIMIT_PER_HOUR = 5
OTP_IP_LIMIT_PER_HOUR = 10
OTP_SESSION_KEY = "checkout_otp_grants"

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


class CheckoutOtpError(Exception):
    def __init__(self, message: str, status_code: int = 400, error_code: str = "otp_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


def ensure_checkout_otp_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS checkout_email_otps (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    checkout_type VARCHAR(32) NOT NULL,
                    checkout_key_hash CHAR(64) NOT NULL,
                    email_hash CHAR(64) NOT NULL,
                    code_hash CHAR(64) NOT NULL,
                    code_salt CHAR(32) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    attempts INT NOT NULL DEFAULT 0,
                    sent_at DATETIME NOT NULL,
                    verified_at DATETIME NULL,
                    used_at DATETIME NULL,
                    ip VARCHAR(64) NULL,
                    ua VARCHAR(255) NULL,
                    cooldown_until DATETIME NULL,
                    PRIMARY KEY (id),
                    KEY idx_checkout_otp_scope
                        (checkout_type, checkout_key_hash, email_hash, sent_at),
                    KEY idx_checkout_otp_ip (ip, sent_at),
                    KEY idx_checkout_otp_expiry (expires_at, used_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
            _SCHEMA_READY = True
        finally:
            db.close()


def mask_email(email: str | None) -> str:
    value = (email or "").strip()
    if "@" not in value:
        return ""
    local, domain = value.rsplit("@", 1)
    if not local or not domain:
        return ""
    visible = local[0]
    return f"{visible}{'*' * max(3, min(len(local) - 1, 8))}@{domain}"


def _now() -> datetime:
    return datetime.now()


def _pepper() -> str:
    return str(
        current_app.config.get("CHECKOUT_OTP_PEPPER")
        or current_app.config.get("OTP_PEPPER")
        or current_app.config.get("SECRET_KEY")
        or ""
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope_parts(checkout_type: str, checkout_key: str, email: str) -> tuple[str, str, str, str]:
    normalized_type = (checkout_type or "").strip().lower()
    normalized_key = (checkout_key or "").strip()
    normalized_email = (email or "").strip().lower()
    if normalized_type not in {"event", "invoice"} or not normalized_key or not normalized_email:
        raise CheckoutOtpError("決済情報またはメールアドレスを確認できません。", 400, "invalid_scope")
    key_hash = _sha256(f"{_pepper()}:{normalized_type}:{normalized_key}")
    email_hash = _sha256(f"{_pepper()}:{normalized_email}")
    scope_id = f"{normalized_type}:{key_hash}:{email_hash}"
    return normalized_type, key_hash, email_hash, scope_id


def _code_hash(scope_id: str, code: str, salt: str) -> str:
    return _sha256(f"{_pepper()}:{scope_id}:{code}:{salt}")


def _grant_map() -> dict:
    value = session.get(OTP_SESSION_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _save_grant(scope_id: str, otp_id: int, verified_until: datetime) -> None:
    grants = _grant_map()
    grants[scope_id] = {"otp_id": int(otp_id), "until": verified_until.isoformat()}
    if len(grants) > 5:
        grants = dict(list(grants.items())[-5:])
    session[OTP_SESSION_KEY] = grants
    session.modified = True


def _drop_grant(scope_id: str) -> None:
    grants = _grant_map()
    if scope_id in grants:
        grants.pop(scope_id, None)
        session[OTP_SESSION_KEY] = grants
        session.modified = True


def send_checkout_otp(*, checkout_type: str, checkout_key: str, email: str) -> dict:
    ensure_checkout_otp_schema()
    normalized_type, key_hash, email_hash, scope_id = _scope_parts(checkout_type, checkout_key, email)
    now = _now()
    db = get_db()
    otp_id = None
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT sent_at
              FROM checkout_email_otps
             WHERE checkout_type=%s AND checkout_key_hash=%s AND email_hash=%s
             ORDER BY id DESC LIMIT 1
            """,
            (normalized_type, key_hash, email_hash),
        )
        latest = cur.fetchone() or {}
        sent_at = latest.get("sent_at")
        if sent_at and (now - sent_at).total_seconds() < OTP_SEND_COOLDOWN_SECONDS:
            wait_seconds = max(1, OTP_SEND_COOLDOWN_SECONDS - int((now - sent_at).total_seconds()))
            raise CheckoutOtpError(
                f"再送は{wait_seconds}秒後に利用できます。", 429, "send_cooldown"
            )

        hour_ago = now - timedelta(hours=1)
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM checkout_email_otps
             WHERE checkout_type=%s AND checkout_key_hash=%s AND email_hash=%s
               AND sent_at >= %s
            """,
            (normalized_type, key_hash, email_hash, hour_ago),
        )
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_SCOPE_LIMIT_PER_HOUR:
            raise CheckoutOtpError(
                "認証コードの送信回数が多すぎます。時間をおいてください。",
                429,
                "scope_rate_limit",
            )

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM checkout_email_otps WHERE ip=%s AND sent_at >= %s",
            (request.remote_addr, hour_ago),
        )
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_IP_LIMIT_PER_HOUR:
            raise CheckoutOtpError(
                "認証コードの送信回数が多すぎます。時間をおいてください。",
                429,
                "ip_rate_limit",
            )

        code = f"{secrets.randbelow(10 ** 6):06d}"
        salt = secrets.token_hex(16)
        expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
        cur.execute(
            """
            INSERT INTO checkout_email_otps
                (checkout_type, checkout_key_hash, email_hash, code_hash, code_salt,
                 expires_at, attempts, sent_at, ip, ua)
            VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s)
            """,
            (
                normalized_type,
                key_hash,
                email_hash,
                _code_hash(scope_id, code, salt),
                salt,
                expires_at,
                now,
                request.remote_addr,
                (request.user_agent.string or "")[:255],
            ),
        )
        otp_id = int(cur.lastrowid)
        db.commit()
    finally:
        db.close()

    try:
        send_mail(
            to=email,
            subject="【MFU】お支払い用ワンタイムコード",
            body="\n".join(
                [
                    "MFUのお支払い画面で、以下の認証コードを入力してください。",
                    "",
                    f"認証コード: {code}",
                    f"有効期限: {OTP_TTL_MINUTES}分",
                    "",
                    "このコードに心当たりがない場合は、入力せずにメールを破棄してください。",
                ]
            ),
            event_uuid=None,
            mail_kind="checkout_otp",
            append_signature=True,
        )
    except Exception as exc:
        current_app.logger.exception("checkout otp mail send failed type=%s otp_id=%s", normalized_type, otp_id)
        cleanup_db = get_db()
        try:
            cleanup_cur = cleanup_db.cursor()
            cleanup_cur.execute("DELETE FROM checkout_email_otps WHERE id=%s AND verified_at IS NULL", (otp_id,))
            cleanup_db.commit()
        finally:
            cleanup_db.close()
        raise CheckoutOtpError("認証コードのメール送信に失敗しました。", 500, "mail_failed") from exc

    _drop_grant(scope_id)
    return {"masked_email": mask_email(email), "expires_in": OTP_TTL_MINUTES * 60}


def verify_checkout_otp(*, checkout_type: str, checkout_key: str, email: str, code: str) -> dict:
    ensure_checkout_otp_schema()
    normalized_type, key_hash, email_hash, scope_id = _scope_parts(checkout_type, checkout_key, email)
    normalized_code = (code or "").strip()
    if len(normalized_code) != 6 or not normalized_code.isdigit():
        raise CheckoutOtpError("6桁の認証コードを入力してください。", 400, "invalid_code_format")

    now = _now()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT *
              FROM checkout_email_otps
             WHERE checkout_type=%s AND checkout_key_hash=%s AND email_hash=%s
             ORDER BY id DESC LIMIT 1
            """,
            (normalized_type, key_hash, email_hash),
        )
        row = cur.fetchone()
        if not row:
            raise CheckoutOtpError("認証コードが発行されていません。", 400, "not_issued")
        if row.get("used_at"):
            raise CheckoutOtpError("この認証コードは既に使用されています。", 400, "already_used")
        if row.get("cooldown_until") and row["cooldown_until"] > now:
            raise CheckoutOtpError("試行回数が多すぎます。時間をおいてください。", 429, "verify_locked")
        if row.get("expires_at") and row["expires_at"] < now:
            raise CheckoutOtpError("認証コードの有効期限が切れています。", 400, "expired")

        expected = _code_hash(scope_id, normalized_code, row["code_salt"])
        if not hmac.compare_digest(expected, row["code_hash"]):
            attempts = int(row.get("attempts") or 0) + 1
            cooldown = now + timedelta(minutes=OTP_TTL_MINUTES) if attempts >= OTP_MAX_ATTEMPTS else None
            cur.execute(
                "UPDATE checkout_email_otps SET attempts=%s, cooldown_until=%s WHERE id=%s",
                (attempts, cooldown, row["id"]),
            )
            db.commit()
            if attempts >= OTP_MAX_ATTEMPTS:
                raise CheckoutOtpError("試行回数が多すぎます。時間をおいてください。", 429, "verify_locked")
            raise CheckoutOtpError("認証コードが一致しません。", 400, "code_mismatch")

        verified_until = now + timedelta(minutes=OTP_GRANT_MINUTES)
        cur.execute(
            "UPDATE checkout_email_otps SET verified_at=%s, attempts=0, cooldown_until=NULL WHERE id=%s",
            (now, row["id"]),
        )
        db.commit()
        _save_grant(scope_id, int(row["id"]), verified_until)
        return {"verified": True, "expires_in": OTP_GRANT_MINUTES * 60}
    finally:
        db.close()


def is_checkout_otp_verified(*, checkout_type: str, checkout_key: str, email: str) -> bool:
    try:
        _, _, _, scope_id = _scope_parts(checkout_type, checkout_key, email)
    except CheckoutOtpError:
        return False
    grant = _grant_map().get(scope_id)
    if not isinstance(grant, dict):
        return False
    try:
        until = datetime.fromisoformat(str(grant.get("until") or ""))
        otp_id = int(grant.get("otp_id") or 0)
    except Exception:
        _drop_grant(scope_id)
        return False
    if otp_id <= 0 or until < _now():
        _drop_grant(scope_id)
        return False

    ensure_checkout_otp_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT verified_at, used_at FROM checkout_email_otps WHERE id=%s LIMIT 1",
            (otp_id,),
        )
        row = cur.fetchone() or {}
        ok = bool(row.get("verified_at") and not row.get("used_at"))
    finally:
        db.close()
    if not ok:
        _drop_grant(scope_id)
    return ok


def require_checkout_otp(*, checkout_type: str, checkout_key: str, email: str) -> None:
    if not is_checkout_otp_verified(checkout_type=checkout_type, checkout_key=checkout_key, email=email):
        raise CheckoutOtpError(
            "メール認証が完了していないか、有効期限が切れています。",
            403,
            "otp_required",
        )


def consume_checkout_otp(*, checkout_type: str, checkout_key: str, email: str) -> None:
    try:
        _, _, _, scope_id = _scope_parts(checkout_type, checkout_key, email)
    except CheckoutOtpError:
        return
    grant = _grant_map().get(scope_id)
    if not isinstance(grant, dict):
        return
    try:
        otp_id = int(grant.get("otp_id") or 0)
    except Exception:
        otp_id = 0
    if otp_id > 0:
        ensure_checkout_otp_schema()
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "UPDATE checkout_email_otps SET used_at=%s WHERE id=%s AND used_at IS NULL",
                (_now(), otp_id),
            )
            db.commit()
        finally:
            db.close()
    _drop_grant(scope_id)
