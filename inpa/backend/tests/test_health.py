import hashlib
import hmac
import time

from sqlalchemy import text

from inpa_app import create_app
from inpa_app.admin_auth import canonical_request
from inpa_app.db import get_engine


def test_live_check_does_not_require_database() -> None:
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    response = app.test_client().get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_internal_routes_are_not_registered_in_public_app() -> None:
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    response = app.test_client().get("/internal/admin/v1/bootstrap-status")

    assert response.status_code == 404


def test_internal_bootstrap_route_is_available_only_in_admin_app() -> None:
    secret = "internal-test-secret"
    app = create_app("admin", {
        "TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "INTERNAL_ADMIN_HMAC_SECRET": secret,
    })
    with app.app_context(), get_engine().begin() as connection:
        connection.execute(text(
            "CREATE TABLE admin_api_nonces (nonce_hash VARCHAR(64) PRIMARY KEY, created_at DATETIME, expires_at DATETIME NOT NULL)"
        ))
    timestamp = str(int(time.time())); nonce = "0123456789abcdef"; admin = "test-admin"
    signature = hmac.new(
        secret.encode(),
        canonical_request("GET", "/internal/admin/v1/bootstrap-status", timestamp, nonce, b"", admin),
        hashlib.sha256,
    ).hexdigest()
    response = app.test_client().get("/internal/admin/v1/bootstrap-status", headers={
        "X-INPA-Timestamp": timestamp, "X-INPA-Nonce": nonce,
        "X-INPA-Admin": admin, "X-INPA-Signature": signature,
    })

    assert response.status_code == 200


def test_internal_bootstrap_rejects_unsigned_request() -> None:
    app = create_app("admin", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    assert app.test_client().get("/internal/admin/v1/bootstrap-status").status_code == 401
