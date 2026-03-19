from __future__ import annotations

from datetime import datetime

from flask import current_app, jsonify, request

from app.utils.db import get_db

from . import bank_account_bp
from .token_service import (
    create_payout_access_token,
    touch_payout_token_api_client_usage,
    verify_payout_token_api_key,
)


DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"



def _parse_expires_at(raw_value: str | None) -> datetime | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, DATETIME_FORMAT)



def _client_ip() -> str | None:
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded_for or request.remote_addr


@bank_account_bp.route("/admin/api/payout/access-token", methods=["POST"])
def create_payout_access_token_api():
    api_key = request.headers.get("X-MFU-PAYOUT-API-KEY") or ""
    db = get_db()
    client = None
    ip_address = _client_ip()

    try:
        client = verify_payout_token_api_key(db, api_key)
        if not client:
            current_app.logger.warning(
                "bank_account payout token api auth failed ip=%s",
                ip_address,
            )
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        memo = (payload.get("memo") or "").strip()
        issued_by_app = (payload.get("issued_by_app") or "").strip() or None
        expires_at_raw = payload.get("expires_at")

        if not memo:
            return jsonify({"ok": False, "error": "memo is required"}), 400
        if len(memo) > 255:
            return jsonify({"ok": False, "error": "memo must be 255 characters or fewer"}), 400
        if issued_by_app and len(issued_by_app) > 64:
            return jsonify({"ok": False, "error": "issued_by_app must be 64 characters or fewer"}), 400

        try:
            expires_at = _parse_expires_at(expires_at_raw)
        except ValueError:
            return jsonify({"ok": False, "error": f"expires_at must be in {DATETIME_FORMAT} format"}), 400

        created = create_payout_access_token(
            db,
            memo=memo,
            issued_via="api",
            issued_by_app=issued_by_app or client.get("app_name"),
            created_by_admin=None,
            expires_at=expires_at,
        )
        touch_payout_token_api_client_usage(db, int(client["id"]), ip_address)

        current_app.logger.info(
            "bank_account payout token api issued token_id=%s client=%s issued_by_app=%s ip=%s",
            created.get("id"),
            client.get("app_name"),
            created.get("issued_by_app"),
            ip_address,
        )
        return jsonify(
            {
                "ok": True,
                "token": created.get("token"),
                "token_preview": created.get("token_preview"),
                "token_id": created.get("id"),
                "memo": created.get("memo"),
                "issued_by_app": created.get("issued_by_app"),
                "created_at": created.get("created_at").strftime(DATETIME_FORMAT) if created.get("created_at") else None,
            }
        )
    except Exception:
        current_app.logger.exception(
            "bank_account payout token api unexpected error client=%s ip=%s",
            client.get("app_name") if client else None,
            ip_address,
        )
        return jsonify({"ok": False, "error": "internal_server_error"}), 500
    finally:
        db.close()
