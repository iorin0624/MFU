"""HMAC authentication, replay protection, idempotency, and admin audit helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from functools import wraps

from flask import current_app, g, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import get_engine


def canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes, admin: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}\n{admin}".encode()


def _denied(reason: str, status: int = 401):
    admin = (request.headers.get("X-INPA-Admin") or "unknown")[:128]
    try:
        with get_engine().begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO admin_audit_logs "
                    "(admin_username,action,target_type,target_id,result,after_json) "
                    "VALUES (:admin,'internal_api_auth','request',:path,'denied',:details)"
                ),
                {"admin": admin, "path": request.path[:128], "details": json.dumps({"reason": reason})},
            )
    except Exception:  # Authentication response must not expose audit storage failures.
        current_app.logger.exception("Could not write denied internal API audit")
    return jsonify(error={"code": "admin_auth_failed", "message": "Authentication failed."}), status


def require_admin_hmac(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        timestamp = request.headers.get("X-INPA-Timestamp", "")
        nonce = request.headers.get("X-INPA-Nonce", "")
        admin = request.headers.get("X-INPA-Admin", "")
        signature = request.headers.get("X-INPA-Signature", "")
        try:
            request_time = int(timestamp)
        except ValueError:
            return _denied("invalid_timestamp")
        if abs(int(time.time()) - request_time) > 60:
            return _denied("expired_timestamp")
        if not 16 <= len(nonce) <= 128 or not 1 <= len(admin) <= 128 or len(signature) != 64:
            return _denied("invalid_headers")
        secret = current_app.config["INTERNAL_ADMIN_HMAC_SECRET"].encode("utf-8")
        expected = hmac.new(
            secret,
            canonical_request(request.method, request.path, timestamp, nonce, request.get_data(cache=True), admin),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return _denied("invalid_signature")
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            with get_engine().begin() as connection:
                connection.execute(text("DELETE FROM admin_api_nonces WHERE expires_at<:now"), {"now": now})
                connection.execute(
                    text("INSERT INTO admin_api_nonces (nonce_hash,expires_at) VALUES (:hash,:expires)"),
                    {"hash": nonce_hash, "expires": now + timedelta(minutes=5)},
                )
        except IntegrityError:
            return _denied("replayed_nonce")
        g.inpa_admin = admin
        return view(*args, **kwargs)
    return wrapped


def require_idempotency(connection) -> str:
    key = request.headers.get("Idempotency-Key", "")
    if not 16 <= len(key) <= 128:
        raise ValueError("Idempotency-Key is required for this operation.")
    if connection.execute(
        text("SELECT 1 FROM admin_audit_logs WHERE idempotency_key=:key"), {"key": key}
    ).first():
        raise LookupError("This operation was already processed.")
    return key


def write_audit(
    connection, *, action: str, target_type: str, target_id: str | None,
    result: str = "success", before: object = None, after: object = None,
    idempotency_key: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO admin_audit_logs "
            "(admin_username,action,target_type,target_id,idempotency_key,before_json,after_json,result) "
            "VALUES (:admin,:action,:target_type,:target_id,:key,:before,:after,:result)"
        ),
        {
            "admin": g.inpa_admin, "action": action, "target_type": target_type,
            "target_id": target_id, "key": idempotency_key,
            "before": json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
            "after": json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
            "result": result,
        },
    )
