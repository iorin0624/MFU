from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from inpa_app import create_app
from inpa_app.auth.service import (
    RegistrationError,
    _touch_session,
    hash_secret,
    new_connection_id,
    new_public_id,
    normalize_connection_id,
    validate_password,
)
from inpa_app.db import get_engine
from inpa_app.internal_admin import _invitation_memo


@pytest.fixture
def app():
    return create_app(
        "public",
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TOKEN_PEPPER": "test-pepper",
            "MAIL_ENCRYPTION_KEY": "c29tZV9ub3RfcmVhbF9mZXJuZXRfa2V5XzMyX2J5dGVzISE=",
        },
    )


def test_public_ids_use_the_documented_crockford_alphabet():
    public_id = new_public_id()

    assert len(public_id) == 26
    assert set(public_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_connection_ids_are_short_and_normalized_for_human_input():
    connection_id = new_connection_id()

    assert len(connection_id) == 8
    assert set(connection_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert normalize_connection_id("7k3m-p9qx") == "7K3MP9QX"
    assert normalize_connection_id(" 7K3M P9QX ") == "7K3MP9QX"
    assert normalize_connection_id("contains-O") == ""


def test_secret_hash_is_pepper_bound(app):
    with app.app_context():
        assert hash_secret("token") == hash_secret("token")
        assert hash_secret("token") != hash_secret("other-token")


def test_password_policy_requires_twelve_characters():
    with pytest.raises(RegistrationError):
        validate_password("too-short")
    assert validate_password("a-safe-password") == "a-safe-password"


def test_login_does_not_leak_invalid_json_details(app):
    response = app.test_client().post("/api/v1/auth/login", json=[])

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "login_failed"


def test_registration_requires_an_administrator_invitation(app):
    app.config["TURNSTILE_BYPASS"] = True
    with app.app_context(), get_engine().begin() as connection:
        connection.execute(text(
            "CREATE TABLE registration_settings (id INTEGER PRIMARY KEY, invite_only BOOLEAN)"
        ))
        connection.execute(text(
            "INSERT INTO registration_settings (id,invite_only) VALUES (1,1)"
        ))

    response = app.test_client().post(
        "/api/v1/auth/register/request", json={"email": "new@example.com"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"

    settings = app.test_client().get("/api/v1/auth/registration-settings")
    assert settings.status_code == 200
    assert settings.get_json()["invite_only"] is True


def test_invitation_memo_is_trimmed_and_limited():
    assert _invitation_memo("  invited guest  ") == "invited guest"
    assert _invitation_memo("") is None
    with pytest.raises(ValueError):
        _invitation_memo("x" * 256)


def test_session_activity_is_updated_only_after_five_minutes(app):
    now = datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    with app.app_context():
        engine = get_engine()
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE user_sessions (id INTEGER PRIMARY KEY, last_seen_at DATETIME)"
            ))
            connection.execute(
                text("INSERT INTO user_sessions (id,last_seen_at) VALUES (1,:seen)"),
                {"seen": now - timedelta(minutes=10)},
            )
        _touch_session(engine, 1, now - timedelta(minutes=10), now)
        _touch_session(engine, 1, now, now + timedelta(minutes=1))
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT last_seen_at FROM user_sessions WHERE id=1"
            )).scalar_one() == now.isoformat(" ")


def test_session_activity_failure_does_not_fail_the_request(app):
    class BrokenEngine:
        @contextmanager
        def begin(self):
            raise OperationalError("UPDATE user_sessions", {}, Exception("concurrent update"))
            yield

    with app.app_context():
        _touch_session(
            BrokenEngine(),
            session_id=1,
            last_seen_at=datetime(2026, 9, 21, 23, 0, 0, tzinfo=UTC).replace(tzinfo=None),
            now=datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC).replace(tzinfo=None),
        )
