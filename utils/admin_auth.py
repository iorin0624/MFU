"""Central security controls for MFU administrator authentication.

The Flask session cookie only contains an opaque session id.  The authoritative
administrator session lives in MySQL, which makes revocation and credential
rollovers possible even though Flask uses signed client-side cookies.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import current_app, g, request, session

from app.utils.db import get_db
from app.utils.logs import write_login_log

ADMIN_USERNAME = "admin"
PREAUTH_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=7)
RECENT_MFA_TTL = timedelta(minutes=15)
QR_APPROVER_MFA_TTL = timedelta(hours=24)
ADMIN_SESSION_CACHE_SECONDS = 5.0
_session_validation_cache: dict[str, tuple[float, bool]] = {}
_session_validation_cache_lock = threading.Lock()


def _session_cache_get(key: str) -> bool | None:
    now = time.monotonic()
    with _session_validation_cache_lock:
        cached = _session_validation_cache.get(key)
        if not cached:
            return None
        expires_at, valid = cached
        if expires_at <= now:
            _session_validation_cache.pop(key, None)
            return None
        return valid


def _session_cache_put(key: str, valid: bool) -> None:
    # Invalid sessions are cached for only one second so a just-established
    # session is not held back, while repeated invalid requests remain cheap.
    ttl = ADMIN_SESSION_CACHE_SECONDS if valid else 1.0
    with _session_validation_cache_lock:
        if len(_session_validation_cache) >= 2048:
            _session_validation_cache.clear()
        _session_validation_cache[key] = (time.monotonic() + ttl, valid)


def _session_cache_clear(key: str | None = None) -> None:
    with _session_validation_cache_lock:
        if key:
            _session_validation_cache.pop(key, None)
        else:
            _session_validation_cache.clear()


def _now() -> datetime:
    return datetime.now()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_schema() -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_auth_state (
            username VARCHAR(191) PRIMARY KEY,
            auth_version BIGINT NOT NULL DEFAULT 1,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_auth_sessions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            sid_hash CHAR(64) NOT NULL UNIQUE,
            username VARCHAR(191) NOT NULL,
            auth_version BIGINT NOT NULL,
            auth_method VARCHAR(32) NOT NULL,
            created_at DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            mfa_verified_at DATETIME NOT NULL,
            ip VARCHAR(64) NULL,
            user_agent VARCHAR(255) NULL,
            revoked_at DATETIME NULL,
            revoke_reason VARCHAR(191) NULL,
            INDEX idx_admin_session_user (username, revoked_at, expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_auth_attempts (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(191) NOT NULL,
            ip VARCHAR(64) NOT NULL,
            stage VARCHAR(32) NOT NULL,
            success TINYINT(1) NOT NULL,
            attempted_at DATETIME NOT NULL,
            INDEX idx_admin_attempt_user (username, stage, attempted_at),
            INDEX idx_admin_attempt_ip (ip, stage, attempted_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_qr_login_challenges (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            token_hash CHAR(64) NOT NULL UNIQUE,
            desktop_nonce_hash CHAR(64) NOT NULL,
            username VARCHAR(191) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            desktop_ip VARCHAR(64) NULL,
            desktop_user_agent VARCHAR(255) NULL,
            approved_at DATETIME NULL,
            approved_by_sid_hash CHAR(64) NULL,
            consumed_at DATETIME NULL,
            INDEX idx_admin_qr_status (status, expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        "INSERT IGNORE INTO admin_auth_state (username, auth_version) VALUES (%s, 1)",
        (ADMIN_USERNAME,),
    )
    db.commit()
    db.close()


def audit(event: str, *, username: str = ADMIN_USERNAME, details: dict | None = None) -> None:
    safe = dict(details or {})
    safe.setdefault("username", username)
    safe.setdefault("ip", request.remote_addr if request else "")
    current_app.logger.warning("[ADMIN_AUTH_%s] %s", event.upper(), json.dumps(safe, ensure_ascii=False, sort_keys=True))
    try:
        g.mfu_access_log_marker = f"[ADMIN_AUTH_{event.upper()}]"
    except Exception:
        pass


def record_attempt(username: str, stage: str, success: bool) -> None:
    ip = (request.remote_addr or "")[:64]
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO admin_auth_attempts (username, ip, stage, success, attempted_at) VALUES (%s,%s,%s,%s,%s)",
        ((username or "")[:191], ip, stage[:32], 1 if success else 0, _now()),
    )
    # Authentication telemetry is intentionally short-lived.
    cur.execute("DELETE FROM admin_auth_attempts WHERE attempted_at < %s", (_now() - timedelta(days=30),))
    db.commit()
    db.close()


def rate_limited(username: str, stage: str, *, window_minutes: int, max_failures: int) -> bool:
    db = get_db()
    cur = db.cursor(dictionary=True)
    since = _now() - timedelta(minutes=window_minutes)
    cur.execute(
        """
        SELECT
          SUM(CASE WHEN username=%s AND success=0 THEN 1 ELSE 0 END) AS user_failures,
          SUM(CASE WHEN ip=%s AND success=0 THEN 1 ELSE 0 END) AS ip_failures
        FROM admin_auth_attempts
        WHERE stage=%s AND attempted_at >= %s
        """,
        ((username or "")[:191], (request.remote_addr or "")[:64], stage[:32], since),
    )
    row = cur.fetchone() or {}
    db.close()
    return int(row.get("user_failures") or 0) >= max_failures or int(row.get("ip_failures") or 0) >= max_failures


def begin_password_preauth(username: str) -> None:
    preserved_next = session.get("post_login_next")
    csrf = session.get("csrf_token")
    session.clear()
    if preserved_next:
        session["post_login_next"] = preserved_next
    if csrf:
        session["csrf_token"] = csrf
    session["preauth_user"] = username
    session["preauth_expires_at"] = int(time.time() + PREAUTH_TTL.total_seconds())
    session["preauth_password_verified_at"] = int(time.time())
    session["preauth_nonce"] = secrets.token_urlsafe(32)


def password_preauth_valid(username: str = ADMIN_USERNAME) -> bool:
    if session.get("preauth_user") != username:
        return False
    try:
        expires = int(session.get("preauth_expires_at") or 0)
        verified = int(session.get("preauth_password_verified_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(verified and time.time() <= expires)


def clear_preauth() -> None:
    for key in tuple(session.keys()):
        if key.startswith("preauth_") or key.startswith("webauthn_auth_"):
            session.pop(key, None)


def _auth_version(username: str = ADMIN_USERNAME) -> int:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT auth_version FROM admin_auth_state WHERE username=%s", (username,))
    row = cur.fetchone()
    db.close()
    return int((row or {}).get("auth_version") or 1)


def establish_admin_session(*, method: str, nickname: str | None = None) -> None:
    next_url = session.get("post_login_next")
    csrf = session.get("csrf_token")
    clear_preauth()
    raw_sid = secrets.token_urlsafe(48)
    now = _now()
    version = _auth_version()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO admin_auth_sessions
          (sid_hash, username, auth_version, auth_method, created_at, last_seen_at,
           expires_at, mfa_verified_at, ip, user_agent)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            _hash(raw_sid), ADMIN_USERNAME, version, method[:32], now, now,
            now + SESSION_TTL, now, (request.remote_addr or "")[:64],
            (request.user_agent.string or "")[:255],
        ),
    )
    db.commit()
    db.close()
    session.clear()
    if csrf:
        session["csrf_token"] = csrf
    session["user"] = ADMIN_USERNAME
    session["nickname"] = nickname or ADMIN_USERNAME
    session["admin_sid"] = raw_sid
    session["admin_auth_version"] = version
    session["admin_auth_method"] = method
    session["admin_mfa_verified_at"] = int(time.time())
    # Keep the authenticated admin cookie across browser restarts.  Both the
    # cookie and authoritative DB session use a rolling seven-day lifetime.
    session.permanent = True
    if next_url:
        session["post_login_next"] = next_url
    write_login_log(ADMIN_USERNAME, request.remote_addr, tag=f"LOGIN_{method.upper()}")
    audit("SUCCESS", details={"method": method, "user_agent": (request.user_agent.string or "")[:160]})
    _send_login_notification(method)


def _send_login_notification(method: str) -> None:
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT webhook_url FROM users WHERE username=%s", (ADMIN_USERNAME,))
        row = cur.fetchone() or {}
        db.close()
        from app.discord_notifications.repository import get_discord_webhook
        webhook = get_discord_webhook("admin_login", (row.get("webhook_url") or "").strip())
        if not webhook:
            return
        import requests as http
        body = (
            "🔐 **管理者ログイン**\n"
            f"認証方法: {method}\n"
            f"日時: {_now().strftime('%Y/%m/%d %H:%M:%S')}\n"
            f"IP: {request.remote_addr}\n"
            f"端末: {(request.user_agent.string or '')[:180]}"
        )
        http.post(webhook, json={"content": body}, timeout=10).raise_for_status()
    except Exception as exc:
        # Discord webhook URLs contain secrets; never include the request URL
        # or exception text in logs.
        current_app.logger.warning(
            "admin login notification failed error_type=%s", type(exc).__name__
        )


def validate_admin_session(*, touch: bool = True) -> bool:
    if session.get("user") != ADMIN_USERNAME:
        return True
    raw_sid = session.get("admin_sid")
    if not raw_sid:
        return False
    sid_hash = _hash(str(raw_sid))
    cache_key = f"{sid_hash}:{session.get('admin_auth_version', '')}"
    cached = _session_cache_get(cache_key)
    if cached is not None:
        if cached:
            session.permanent = True
        return cached
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT s.*, st.auth_version AS current_auth_version
        FROM admin_auth_sessions s
        JOIN admin_auth_state st ON st.username=s.username
        WHERE s.sid_hash=%s AND s.username=%s
        """,
        (sid_hash, ADMIN_USERNAME),
    )
    row = cur.fetchone()
    now = _now()
    valid = bool(
        row and not row.get("revoked_at") and row.get("expires_at") >= now
        and int(row.get("auth_version") or 0) == int(row.get("current_auth_version") or -1)
    )
    # Upgrade already-issued browser-session cookies on their next valid
    # request as well as making newly-issued admin sessions persistent.
    if valid:
        session.permanent = True
    if valid and touch and row.get("last_seen_at") < now - timedelta(minutes=5):
        cur.execute(
            "UPDATE admin_auth_sessions SET last_seen_at=%s, expires_at=%s WHERE sid_hash=%s",
            (now, now + SESSION_TTL, sid_hash),
        )
        db.commit()
    db.close()
    _session_cache_put(cache_key, valid)
    return valid


def recent_admin_mfa(max_age: timedelta = RECENT_MFA_TTL) -> bool:
    if session.get("user") != ADMIN_USERNAME or not validate_admin_session(touch=False):
        return False
    try:
        verified = int(session.get("admin_mfa_verified_at") or 0)
    except (TypeError, ValueError):
        return False
    return verified >= int(time.time() - max_age.total_seconds())


def invalidate_all_admin_sessions(reason: str) -> None:
    now = _now()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE admin_auth_state SET auth_version=auth_version+1, updated_at=%s WHERE username=%s",
        (now, ADMIN_USERNAME),
    )
    cur.execute(
        "UPDATE admin_auth_sessions SET revoked_at=%s, revoke_reason=%s WHERE username=%s AND revoked_at IS NULL",
        (now, reason[:191], ADMIN_USERNAME),
    )
    db.commit()
    db.close()
    _session_cache_clear()


def revoke_current_admin_session(reason: str = "logout") -> None:
    raw_sid = session.get("admin_sid")
    if not raw_sid:
        return
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE admin_auth_sessions SET revoked_at=%s, revoke_reason=%s WHERE sid_hash=%s AND revoked_at IS NULL",
        (_now(), reason[:191], _hash(str(raw_sid))),
    )
    db.commit()
    db.close()
    _session_cache_clear(f"{_hash(str(raw_sid))}:{session.get('admin_auth_version', '')}")
