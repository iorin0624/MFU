import base64
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from webauthn.helpers.structs import CredentialDeviceType

from inpa_app import create_app
from inpa_app.auth import security
from inpa_app.auth.security import SecuritySettingsError


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@pytest.fixture
def app(monkeypatch):
    app = create_app(
        "public",
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TOKEN_PEPPER": "test-pepper",
            "WEBAUTHN_RP_ID": "inpa.example",
            "WEBAUTHN_ORIGIN": "https://inpa.example",
        },
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    app.extensions["inpa_db_engine"] = engine
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY,public_id VARCHAR(26),email VARCHAR(254),"
                "display_name VARCHAR(40),password_hash VARCHAR(255),status VARCHAR(16),"
                "email_verified_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users VALUES (1,'USERPUBLICID00000000000001','user@example.com','User',"
                ":password,'active',CURRENT_TIMESTAMP)"
            ),
            {"password": security._PASSWORDS.hash("current-password")},
        )
        connection.execute(
            text(
                "CREATE TABLE user_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,public_id VARCHAR(26),"
                "user_id INTEGER,token_hash VARCHAR(64),csrf_secret_hash VARCHAR(64),"
                "last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,expires_at DATETIME,"
                "absolute_expires_at DATETIME,revoked_at DATETIME,revoke_reason VARCHAR(64))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_sessions (public_id,user_id,token_hash,csrf_secret_hash,expires_at,absolute_expires_at) "
                "VALUES ('SESSION0000000000000000001',1,'a','b',datetime('now','+1 day'),datetime('now','+2 day'))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE rate_limit_counters (bucket_key VARCHAR(64),action_name VARCHAR(64),"
                "window_started_at DATETIME,window_seconds INTEGER,request_count INTEGER DEFAULT 0,"
                "PRIMARY KEY (bucket_key,action_name,window_started_at))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE security_events (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,"
                "event_type VARCHAR(64),severity VARCHAR(16),result VARCHAR(16),ip_address BLOB,"
                "user_agent VARCHAR(512),correlation_id VARCHAR(36),metadata_json TEXT,"
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_passkeys (id INTEGER PRIMARY KEY AUTOINCREMENT,public_id VARCHAR(26),"
                "user_id INTEGER,credential_id BLOB UNIQUE,credential_public_key BLOB,name VARCHAR(80),"
                "sign_count INTEGER DEFAULT 0,transports VARCHAR(255),device_type VARCHAR(32),"
                "backed_up INTEGER DEFAULT 0,created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "last_used_at DATETIME,updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,revoked_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE webauthn_challenges (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "public_id VARCHAR(26),user_id INTEGER,purpose VARCHAR(24),challenge BLOB,"
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,expires_at DATETIME,used_at DATETIME)"
            )
        )
    monkeypatch.setattr(security, "queue_security_email", lambda *_args: None)
    return app


def test_password_change_rehashes_password_and_revokes_every_session(app):
    with app.app_context():
        security.change_password(1, "current-password", "new-secure-password", None, "pytest")
        with app.extensions["inpa_db_engine"].connect() as connection:
            user = (
                connection.execute(text("SELECT password_hash FROM users WHERE id=1"))
                .mappings()
                .one()
            )
            session = (
                connection.execute(
                    text("SELECT revoked_at,revoke_reason FROM user_sessions WHERE user_id=1")
                )
                .mappings()
                .one()
            )
            event = connection.execute(
                text("SELECT event_type FROM security_events WHERE user_id=1")
            ).scalar_one()
        assert security._PASSWORDS.verify(user["password_hash"], "new-secure-password")
        assert session["revoked_at"] is not None
        assert session["revoke_reason"] == "password_changed"
        assert event == "password_changed"


def test_failed_password_attempts_are_committed_for_rate_limiting(app):
    with app.app_context():
        for _ in range(5):
            with pytest.raises(SecuritySettingsError, match="正しくありません"):
                security.change_password(1, "wrong-password", "new-secure-password", None, "pytest")
        with pytest.raises(SecuritySettingsError, match="15分"):
            security.change_password(1, "wrong-password", "new-secure-password", None, "pytest")


def test_passkey_registration_and_authentication_are_single_use(app, monkeypatch):
    registration = SimpleNamespace(
        credential_id=b"credential-id",
        credential_public_key=b"public-key",
        sign_count=0,
        credential_device_type=CredentialDeviceType.MULTI_DEVICE,
        credential_backed_up=True,
    )
    authentication = SimpleNamespace(
        new_sign_count=1,
        credential_device_type=CredentialDeviceType.MULTI_DEVICE,
        credential_backed_up=True,
    )
    monkeypatch.setattr(security, "verify_registration_response", lambda **_kwargs: registration)
    monkeypatch.setattr(
        security, "verify_authentication_response", lambda **_kwargs: authentication
    )

    with app.app_context():
        started = security.begin_passkey_registration(1, "current-password")
        credential = {"id": _b64(b"credential-id"), "response": {"transports": ["internal"]}}
        created = security.finish_passkey_registration(
            1, started["challenge_id"], credential, "iPhone", None, "pytest"
        )
        assert created["name"] == "iPhone"
        assert security.list_passkeys(1)[0]["backed_up"] == 1

        login = security.begin_passkey_authentication()
        credential["response"]["userHandle"] = _b64(b"USERPUBLICID00000000000001")
        user_id, token, csrf = security.finish_passkey_authentication(
            login["challenge_id"], credential, True, None, "pytest"
        )
        assert user_id == 1 and token and csrf
        with pytest.raises(SecuritySettingsError):
            security.finish_passkey_authentication(
                login["challenge_id"], credential, True, None, "pytest"
            )
