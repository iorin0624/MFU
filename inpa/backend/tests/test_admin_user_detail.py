import hashlib
import hmac
import time

from sqlalchemy import text

from inpa_app import create_app
from inpa_app.admin_auth import canonical_request
from inpa_app.db import get_engine


def _headers(secret: str, path: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = f"user-detail-test-{time.time_ns()}"
    admin = "test-admin"
    signature = hmac.new(
        secret.encode(),
        canonical_request("GET", path, timestamp, nonce, b"", admin),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-INPA-Timestamp": timestamp,
        "X-INPA-Nonce": nonce,
        "X-INPA-Admin": admin,
        "X-INPA-Signature": signature,
    }


def _app():
    secret = "test-admin-secret"
    app = create_app(
        "admin",
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TOKEN_PEPPER": "test-pepper",
            "INTERNAL_ADMIN_HMAC_SECRET": secret,
        },
    )
    with app.app_context(), get_engine().begin() as connection:
        for statement in (
            "CREATE TABLE admin_api_nonces (nonce_hash TEXT PRIMARY KEY, created_at DATETIME, expires_at DATETIME NOT NULL)",
            "CREATE TABLE admin_audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_username TEXT, action TEXT, target_type TEXT, target_id TEXT, idempotency_key TEXT, before_json TEXT, after_json TEXT, result TEXT, correlation_id TEXT, created_at DATETIME)",
            "CREATE TABLE users (id INTEGER PRIMARY KEY, public_id TEXT, connection_id TEXT, email TEXT, display_name TEXT, x_handle TEXT, instagram_handle TEXT, status TEXT, email_verified_at DATETIME, created_at DATETIME, updated_at DATETIME, deletion_scheduled_at DATETIME)",
            "CREATE TABLE seasons (id INTEGER PRIMARY KEY, public_id TEXT, name TEXT, start_date DATE, end_date DATE, is_active BOOLEAN)",
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, public_id TEXT, user_id INTEGER, season_id INTEGER, visit_date DATE, park TEXT, arrival_time TIME, costume TEXT, memo TEXT, created_at DATETIME, updated_at DATETIME)",
            "CREATE TABLE user_sessions (id INTEGER PRIMARY KEY, public_id TEXT, user_id INTEGER, created_at DATETIME, last_seen_at DATETIME, expires_at DATETIME, revoked_at DATETIME, revoke_reason TEXT)",
            "CREATE TABLE user_privacy_settings (user_id INTEGER, audience TEXT, show_date BOOLEAN, show_park BOOLEAN, show_costume BOOLEAN, show_memo BOOLEAN)",
            "CREATE TABLE user_season_privacy_settings (user_id INTEGER, season_id INTEGER, audience TEXT, show_date BOOLEAN, show_park BOOLEAN, show_costume BOOLEAN, show_memo BOOLEAN)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO users VALUES (1,'USER0000000000000000000001','ABCD1234','user@example.com','テスト利用者','x_user','insta_user','active','2026-09-01 01:00:00','2026-09-01 00:00:00','2026-09-02 00:00:00',NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO seasons VALUES (10,'SEASON00000000000000000001','秋2026','2026-09-15','2026-10-31',1),"
                "(11,'SEASON00000000000000000002','春2026','2026-04-01','2026-06-30',0)"
            )
        )
        for audience, values in {
            "link": (1, 0, 0, 0),
            "logged_in": (0, 1, 0, 0),
            "mutual": (0, 0, 1, 0),
            "private": (1, 1, 1, 1),
        }.items():
            connection.execute(
                text(
                    "INSERT INTO user_privacy_settings VALUES (1,:audience,:date,:park,:costume,:memo)"
                ),
                {
                    "audience": audience,
                    "date": values[0],
                    "park": values[1],
                    "costume": values[2],
                    "memo": values[3],
                },
            )
        for audience, values in {
            "link": (1, 1, 0, 0),
            "logged_in": (0, 0, 1, 0),
            "mutual": (0, 0, 0, 1),
            "private": (1, 1, 1, 1),
        }.items():
            connection.execute(
                text(
                    "INSERT INTO user_season_privacy_settings VALUES (1,10,:audience,:date,:park,:costume,:memo)"
                ),
                {
                    "audience": audience,
                    "date": values[0],
                    "park": values[1],
                    "costume": values[2],
                    "memo": values[3],
                },
            )
        connection.execute(
            text(
                "INSERT INTO visits VALUES (20,'VISIT000000000000000000001',1,10,'2026-10-01','land','08:30:00','赤い仮装','管理確認用メモ','2026-09-20 01:00:00','2026-09-21 02:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_sessions VALUES (30,'SESSION0000000000000000001',1,'2026-09-20 00:00:00','2026-09-21 00:00:00','2026-10-20 00:00:00',NULL,NULL)"
            )
        )
    return app, secret


def test_admin_user_detail_returns_all_visit_fields_and_season_privacy():
    app, secret = _app()
    path = "/internal/admin/v1/users/USER0000000000000000000001"

    response = app.test_client().get(path, headers=_headers(secret, path))

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user"]["connection_id"] == "ABCD1234"
    assert "id" not in payload["user"]
    assert len(payload["seasons"]) == 2
    autumn = payload["seasons"][0]
    assert "id" not in autumn
    assert autumn["customized"] is True
    assert autumn["privacy_matrix"]["link"] == {
        "date": True,
        "park": True,
        "costume": False,
        "memo": False,
    }
    assert set(autumn["effective_fields"]["logged_in"]) == {"date", "park", "costume"}
    assert autumn["visits"][0]["costume"] == "赤い仮装"
    assert autumn["visits"][0]["memo"] == "管理確認用メモ"
    assert "season_id" not in autumn["visits"][0]
    assert payload["seasons"][1]["customized"] is False

    with app.app_context(), get_engine().connect() as connection:
        audit = (
            connection.execute(
                text(
                    "SELECT action,target_id,result FROM admin_audit_logs WHERE action='user_detail_view'"
                )
            )
            .mappings()
            .one()
        )
    assert audit == {
        "action": "user_detail_view",
        "target_id": "USER0000000000000000000001",
        "result": "success",
    }
