"""One-time, action-bound passkey grants for critical administrator actions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from flask import jsonify, request, session

from app.utils.admin_auth import ADMIN_USERNAME, audit, validate_admin_session


STEPUP_PURPOSE = "admin_action"
STEPUP_TTL_SECONDS = 120
TOKEN_FIELD = "admin_passkey_token"
_ACTION_RE = re.compile(r"[\w@+.-][\w@+.:=-]{0,190}", re.UNICODE)


def normalize_admin_action(value: str | None) -> str:
    action = str(value or "").strip().lower()
    if not _ACTION_RE.fullmatch(action):
        raise ValueError("invalid admin passkey action")
    return action


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_admin_passkey_grant(action: str) -> str:
    action = normalize_admin_action(action)
    token = secrets.token_urlsafe(48)
    session["admin_passkey_grant"] = {
        "action": action,
        "token_hash": _hash(token),
        "expires_at": int(time.time()) + STEPUP_TTL_SECONDS,
    }
    audit("ACTION_PASSKEY_VERIFIED", details={"action": action})
    return token


def _request_token() -> str:
    token = str(request.headers.get("X-MFU-Admin-Passkey") or "").strip()
    if token:
        return token
    token = str(request.form.get(TOKEN_FIELD) or "").strip()
    if token:
        return token
    payload = request.get_json(silent=True) if request.is_json else None
    return str((payload or {}).get(TOKEN_FIELD) or "").strip()


def consume_admin_passkey_grant(action: str) -> bool:
    action = normalize_admin_action(action)
    grant = session.get("admin_passkey_grant")
    token = _request_token()
    if not isinstance(grant, dict) or not token:
        return False
    try:
        expires_at = int(grant.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    valid = bool(
        expires_at >= int(time.time())
        and hmac.compare_digest(str(grant.get("action") or ""), action)
        and hmac.compare_digest(str(grant.get("token_hash") or ""), _hash(token))
    )
    if valid:
        session.pop("admin_passkey_grant", None)
        audit("ACTION_PASSKEY_CONSUMED", details={"action": action})
    return valid


def require_admin_passkey(action: str):
    """Return None when authorized, otherwise a 428 response.

    This intentionally leaves non-admin owner operations unchanged.
    """
    if session.get("user") != ADMIN_USERNAME:
        return None
    action = normalize_admin_action(action)
    if not validate_admin_session(touch=False):
        audit("ACTION_PASSKEY_REJECTED", details={"action": action, "reason": "invalid_session"})
        return jsonify(ok=False, error="admin_session_invalid"), 401
    if consume_admin_passkey_grant(action):
        return None
    audit("ACTION_PASSKEY_REQUIRED", details={"action": action})
    return jsonify(
        ok=False,
        error="passkey_required",
        message="この操作にはパスキー認証が必要です。",
        action=action,
    ), 428
