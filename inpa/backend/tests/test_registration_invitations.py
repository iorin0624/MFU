import hashlib
import hmac
import json
import time

from sqlalchemy import text

from inpa_app import create_app
from inpa_app.admin_auth import canonical_request
from inpa_app.db import get_engine


def _headers(secret: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = f"invite-test-{time.time_ns()}"
    admin = "test-admin"
    signature = hmac.new(
        secret.encode(), canonical_request(method, path, timestamp, nonce, body, admin), hashlib.sha256
    ).hexdigest()
    return {
        "X-INPA-Timestamp": timestamp,
        "X-INPA-Nonce": nonce,
        "X-INPA-Admin": admin,
        "X-INPA-Signature": signature,
    }


def _app():
    secret = "test-admin-secret"
    app = create_app("admin", {
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "TOKEN_PEPPER": "test-pepper",
        "INTERNAL_ADMIN_HMAC_SECRET": secret,
        "PUBLIC_ORIGIN": "https://inpa.example",
    })
    with app.app_context(), get_engine().begin() as connection:
        connection.execute(text(
            "CREATE TABLE admin_api_nonces (nonce_hash VARCHAR(64) PRIMARY KEY, "
            "created_at DATETIME, expires_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE admin_audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "admin_username VARCHAR(128), action VARCHAR(64), target_type VARCHAR(64), "
            "target_id VARCHAR(128), idempotency_key VARCHAR(128), before_json TEXT, "
            "after_json TEXT, result VARCHAR(32), correlation_id VARCHAR(64), created_at DATETIME)"
        ))
        connection.execute(text(
            "CREATE TABLE registration_invitations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "public_id VARCHAR(26), token_hash VARCHAR(64), token_last4 VARCHAR(4), memo VARCHAR(255), "
            "status VARCHAR(16), created_by VARCHAR(128), created_at DATETIME, updated_at DATETIME, "
            "expires_at DATETIME, claimed_email_normalized VARCHAR(254), claimed_at DATETIME, "
            "used_by_user_id INTEGER, used_at DATETIME, revoked_at DATETIME, revoked_by VARCHAR(128))"
        ))
        connection.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, public_id VARCHAR(26))"
        ))
        connection.execute(text(
            "CREATE TABLE registration_settings (id INTEGER PRIMARY KEY, invite_only BOOLEAN, "
            "updated_by VARCHAR(128), updated_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO registration_settings (id,invite_only) VALUES (1,1)"
        ))
    return app, secret


def test_admin_can_create_and_list_single_use_invitation_without_storing_plaintext():
    app, secret = _app()
    path = "/internal/admin/v1/registration-invitations"
    payload = {"memo": "Guest invitation", "expires_in_days": 7}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _headers(secret, "POST", path, body)
    headers.update({"Content-Type": "application/json", "Idempotency-Key": "invite-create-test-key"})

    response = app.test_client().post(path, data=body, headers=headers)

    assert response.status_code == 201
    created = response.get_json()
    assert created["registration_url"].endswith(created["token"])
    with app.app_context(), get_engine().connect() as connection:
        stored = connection.execute(text(
            "SELECT token_hash,token_last4,memo,status FROM registration_invitations"
        )).mappings().one()
    assert stored["token_hash"] != created["token"]
    assert stored["token_last4"] == created["token"][-4:]
    assert stored["memo"] == "Guest invitation"
    assert stored["status"] == "active"

    list_response = app.test_client().get(path, headers=_headers(secret, "GET", path))
    assert list_response.status_code == 200
    listed = list_response.get_json()["invitations"][0]
    assert "token" not in listed
    assert listed["token_last4"] == created["token"][-4:]


def test_admin_can_toggle_general_registration():
    app, secret = _app()
    path = "/internal/admin/v1/registration-settings"
    payload = {"invite_only": False}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _headers(secret, "PATCH", path, body)
    headers.update({"Content-Type": "application/json", "Idempotency-Key": "settings-update-test-key"})

    response = app.test_client().patch(path, data=body, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["invite_only"] is False
    read_response = app.test_client().get(path, headers=_headers(secret, "GET", path))
    assert read_response.status_code == 200
    assert read_response.get_json()["settings"]["invite_only"] is False
