"""Password changes and WebAuthn passkey ceremonies."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from argon2.exceptions import InvalidHashError, VerificationError
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..db import get_engine
from ..mail_queue import queue_security_email
from .service import _PASSWORDS, create_session, hash_secret, new_public_id, validate_password


class SecuritySettingsError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _lock(connection) -> str:
    return " FOR UPDATE" if connection.dialect.name != "sqlite" else ""


def _event(connection, user_id: int, event_type: str, target: str | None, ip, user_agent) -> str:
    public_id = new_public_id()
    connection.execute(
        text(
            "INSERT INTO user_security_events "
            "(public_id,user_id,event_type,target_public_id,ip_address,user_agent) "
            "VALUES (:public_id,:user_id,:event_type,:target,:ip,:agent)"
        ),
        {
            "public_id": public_id,
            "user_id": user_id,
            "event_type": event_type,
            "target": target,
            "ip": ip,
            "agent": (user_agent or "")[:512] or None,
        },
    )
    return public_id


def _queue_notification(recipient: str, user_id: int, event: str, event_id: str) -> None:
    try:
        queue_security_email(recipient, user_id, event, event_id)
    except Exception:  # Notification failure must not roll back a completed security change.
        current_app.logger.exception("Could not queue security notification")


def _consume_attempt(user_id: int, action: str, *, limit: int = 5) -> None:
    now = _now()
    window_seconds = 900
    epoch = int(now.replace(tzinfo=UTC).timestamp())
    window = datetime.fromtimestamp(epoch - epoch % window_seconds, UTC).replace(tzinfo=None)
    params = {
        "bucket": hash_secret(f"security:{user_id}"),
        "action": action,
        "window": window,
        "seconds": window_seconds,
    }
    with get_engine().begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.execute(
                text(
                    "INSERT INTO rate_limit_counters "
                    "(bucket_key,action_name,window_started_at,window_seconds,request_count) "
                    "VALUES (:bucket,:action,:window,:seconds,1) ON CONFLICT "
                    "(bucket_key,action_name,window_started_at) DO UPDATE SET request_count=request_count+1"
                ),
                params,
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO rate_limit_counters "
                    "(bucket_key,action_name,window_started_at,window_seconds,request_count) "
                    "VALUES (:bucket,:action,:window,:seconds,1) "
                    "ON DUPLICATE KEY UPDATE request_count=request_count+1"
                ),
                params,
            )
        count = connection.execute(
            text(
                "SELECT request_count FROM rate_limit_counters WHERE bucket_key=:bucket "
                "AND action_name=:action AND window_started_at=:window"
            ),
            params,
        ).scalar_one()
    if int(count) > limit:
        raise SecuritySettingsError("試行回数が多すぎます。15分ほど待ってからお試しください。")


def _verify_password(encoded: str, supplied: object) -> None:
    if not isinstance(supplied, str):
        raise SecuritySettingsError("現在のパスワードが正しくありません。")
    try:
        valid = _PASSWORDS.verify(encoded, supplied)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid:
        raise SecuritySettingsError("現在のパスワードが正しくありません。")


def change_password(
    user_id: int, current_password: object, new_password: object, ip, user_agent
) -> None:
    password = validate_password(new_password)
    _consume_attempt(user_id, "password_change")
    with get_engine().begin() as connection:
        user = (
            connection.execute(
                text("SELECT email,password_hash FROM users WHERE id=:id" + _lock(connection)),
                {"id": user_id},
            )
            .mappings()
            .one()
        )
        _verify_password(user["password_hash"], current_password)
        try:
            same = _PASSWORDS.verify(user["password_hash"], password)
        except (VerificationError, InvalidHashError):
            same = False
        if same:
            raise SecuritySettingsError("現在と異なる新しいパスワードを設定してください。")
        connection.execute(
            text("UPDATE users SET password_hash=:password WHERE id=:id"),
            {"password": _PASSWORDS.hash(password), "id": user_id},
        )
        connection.execute(
            text(
                "UPDATE user_sessions SET revoked_at=:now,revoke_reason='password_changed' "
                "WHERE user_id=:user_id AND revoked_at IS NULL"
            ),
            {"now": _now(), "user_id": user_id},
        )
        event_id = _event(connection, user_id, "password_changed", None, ip, user_agent)
        email = user["email"]
    _queue_notification(email, user_id, "password_changed", event_id)


def list_passkeys(user_id: int) -> list[dict[str, object]]:
    with get_engine().connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT public_id,name,created_at,last_used_at,device_type,backed_up "
                    "FROM user_passkeys WHERE user_id=:user_id AND revoked_at IS NULL ORDER BY created_at"
                ),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def begin_passkey_registration(user_id: int, password: object) -> dict[str, object]:
    _consume_attempt(user_id, "passkey_reauth")
    with get_engine().begin() as connection:
        user = (
            connection.execute(
                text(
                    "SELECT public_id,email,display_name,password_hash FROM users WHERE id=:id"
                    + _lock(connection)
                ),
                {"id": user_id},
            )
            .mappings()
            .one()
        )
        _verify_password(user["password_hash"], password)
        credentials = (
            connection.execute(
                text(
                    "SELECT credential_id,transports FROM user_passkeys "
                    "WHERE user_id=:user_id AND revoked_at IS NULL"
                ),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
        options = generate_registration_options(
            rp_id=current_app.config["WEBAUTHN_RP_ID"],
            rp_name=current_app.config["WEBAUTHN_RP_NAME"],
            user_name=user["email"],
            user_id=user["public_id"].encode("ascii"),
            user_display_name=user["display_name"],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=row["credential_id"]) for row in credentials
            ],
        )
        challenge_id = new_public_id()
        connection.execute(
            text(
                "INSERT INTO webauthn_challenges "
                "(public_id,user_id,purpose,challenge,expires_at) "
                "VALUES (:public_id,:user_id,'registration',:challenge,:expires_at)"
            ),
            {
                "public_id": challenge_id,
                "user_id": user_id,
                "challenge": options.challenge,
                "expires_at": _now()
                + timedelta(seconds=current_app.config["WEBAUTHN_CHALLENGE_TTL_SECONDS"]),
            },
        )
    return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}


def finish_passkey_registration(
    user_id: int,
    challenge_id: object,
    credential: object,
    name: object,
    ip,
    user_agent,
) -> dict[str, object]:
    if not isinstance(challenge_id, str) or not isinstance(credential, dict):
        raise SecuritySettingsError("パスキーの登録情報が正しくありません。")
    passkey_name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(passkey_name) <= 80:
        raise SecuritySettingsError("パスキー名は1〜80文字で入力してください。")
    now = _now()
    try:
        with get_engine().begin() as connection:
            challenge = (
                connection.execute(
                    text(
                        "SELECT id,challenge FROM webauthn_challenges WHERE public_id=:public_id "
                        "AND user_id=:user_id AND purpose='registration' AND used_at IS NULL "
                        "AND expires_at>:now" + _lock(connection)
                    ),
                    {"public_id": challenge_id, "user_id": user_id, "now": now},
                )
                .mappings()
                .first()
            )
            if not challenge:
                raise SecuritySettingsError(
                    "パスキー登録の有効期限が切れました。もう一度お試しください。"
                )
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge["challenge"],
                expected_rp_id=current_app.config["WEBAUTHN_RP_ID"],
                expected_origin=current_app.config["WEBAUTHN_ORIGIN"],
                require_user_verification=True,
            )
            public_id = new_public_id()
            transports = credential.get("response", {}).get("transports", [])
            allowed = {"usb", "nfc", "ble", "smart-card", "internal", "cable", "hybrid"}
            transport_text = ",".join(item for item in transports if item in allowed) or None
            connection.execute(
                text(
                    "INSERT INTO user_passkeys "
                    "(public_id,user_id,credential_id,credential_public_key,name,sign_count,transports,"
                    "device_type,backed_up) VALUES "
                    "(:public_id,:user_id,:credential_id,:public_key,:name,:sign_count,:transports,"
                    ":device_type,:backed_up)"
                ),
                {
                    "public_id": public_id,
                    "user_id": user_id,
                    "credential_id": verified.credential_id,
                    "public_key": verified.credential_public_key,
                    "name": passkey_name,
                    "sign_count": verified.sign_count,
                    "transports": transport_text,
                    "device_type": verified.credential_device_type.value,
                    "backed_up": int(verified.credential_backed_up),
                },
            )
            connection.execute(
                text("UPDATE webauthn_challenges SET used_at=:now WHERE id=:id"),
                {"now": now, "id": challenge["id"]},
            )
            user = (
                connection.execute(text("SELECT email FROM users WHERE id=:id"), {"id": user_id})
                .mappings()
                .one()
            )
            event_id = _event(connection, user_id, "passkey_added", public_id, ip, user_agent)
        _queue_notification(user["email"], user_id, "passkey_added", event_id)
        return {"public_id": public_id, "name": passkey_name}
    except (InvalidRegistrationResponse, IntegrityError) as exc:
        raise SecuritySettingsError("パスキーを登録できませんでした。") from exc


def rename_passkey(user_id: int, public_id: str, name: object) -> None:
    value = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(value) <= 80:
        raise SecuritySettingsError("パスキー名は1〜80文字で入力してください。")
    with get_engine().begin() as connection:
        result = connection.execute(
            text(
                "UPDATE user_passkeys SET name=:name WHERE public_id=:public_id "
                "AND user_id=:user_id AND revoked_at IS NULL"
            ),
            {"name": value, "public_id": public_id, "user_id": user_id},
        )
        if result.rowcount != 1:
            raise SecuritySettingsError("パスキーが見つかりません。")


def remove_passkey(user_id: int, public_id: str, password: object, ip, user_agent) -> None:
    now = _now()
    _consume_attempt(user_id, "passkey_reauth")
    with get_engine().begin() as connection:
        user = (
            connection.execute(
                text("SELECT email,password_hash FROM users WHERE id=:id" + _lock(connection)),
                {"id": user_id},
            )
            .mappings()
            .one()
        )
        _verify_password(user["password_hash"], password)
        result = connection.execute(
            text(
                "UPDATE user_passkeys SET revoked_at=:now WHERE public_id=:public_id "
                "AND user_id=:user_id AND revoked_at IS NULL"
            ),
            {"now": now, "public_id": public_id, "user_id": user_id},
        )
        if result.rowcount != 1:
            raise SecuritySettingsError("パスキーが見つかりません。")
        event_id = _event(connection, user_id, "passkey_removed", public_id, ip, user_agent)
    _queue_notification(user["email"], user_id, "passkey_removed", event_id)


def begin_passkey_authentication() -> dict[str, object]:
    options = generate_authentication_options(
        rp_id=current_app.config["WEBAUTHN_RP_ID"],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = new_public_id()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO webauthn_challenges (public_id,purpose,challenge,expires_at) "
                "VALUES (:public_id,'authentication',:challenge,:expires_at)"
            ),
            {
                "public_id": challenge_id,
                "challenge": options.challenge,
                "expires_at": _now()
                + timedelta(seconds=current_app.config["WEBAUTHN_CHALLENGE_TTL_SECONDS"]),
            },
        )
    return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}


def finish_passkey_authentication(
    challenge_id: object,
    credential: object,
    remember: bool,
    ip,
    user_agent,
) -> tuple[int, str, str]:
    if not isinstance(challenge_id, str) or not isinstance(credential, dict):
        raise SecuritySettingsError("パスキーでログインできませんでした。")
    credential_value = credential.get("id")
    if not isinstance(credential_value, str):
        raise SecuritySettingsError("パスキーでログインできませんでした。")
    try:
        credential_id = base64url_to_bytes(credential_value)
    except Exception as exc:
        raise SecuritySettingsError("パスキーでログインできませんでした。") from exc
    now = _now()
    try:
        with get_engine().begin() as connection:
            challenge = (
                connection.execute(
                    text(
                        "SELECT id,challenge FROM webauthn_challenges WHERE public_id=:public_id "
                        "AND purpose='authentication' AND used_at IS NULL AND expires_at>:now"
                        + _lock(connection)
                    ),
                    {"public_id": challenge_id, "now": now},
                )
                .mappings()
                .first()
            )
            passkey = (
                connection.execute(
                    text(
                        "SELECT p.id,p.public_id,p.user_id,p.credential_public_key,p.sign_count,"
                        "u.public_id AS user_public_id FROM user_passkeys p JOIN users u ON u.id=p.user_id "
                        "WHERE p.credential_id=:credential_id AND p.revoked_at IS NULL "
                        "AND u.status='active' AND u.email_verified_at IS NOT NULL"
                        + _lock(connection)
                    ),
                    {"credential_id": credential_id},
                )
                .mappings()
                .first()
            )
            if not challenge or not passkey:
                raise SecuritySettingsError("パスキーでログインできませんでした。")
            user_handle = credential.get("response", {}).get("userHandle")
            if user_handle and base64url_to_bytes(user_handle) != passkey["user_public_id"].encode(
                "ascii"
            ):
                raise SecuritySettingsError("パスキーでログインできませんでした。")
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge["challenge"],
                expected_rp_id=current_app.config["WEBAUTHN_RP_ID"],
                expected_origin=current_app.config["WEBAUTHN_ORIGIN"],
                credential_public_key=passkey["credential_public_key"],
                credential_current_sign_count=int(passkey["sign_count"]),
                require_user_verification=True,
            )
            connection.execute(
                text(
                    "UPDATE user_passkeys SET sign_count=:sign_count,last_used_at=:now,"
                    "device_type=:device_type,backed_up=:backed_up WHERE id=:id"
                ),
                {
                    "sign_count": verified.new_sign_count,
                    "now": now,
                    "device_type": verified.credential_device_type.value,
                    "backed_up": int(verified.credential_backed_up),
                    "id": passkey["id"],
                },
            )
            connection.execute(
                text("UPDATE webauthn_challenges SET used_at=:now WHERE id=:id"),
                {
                    "now": now,
                    "id": challenge["id"],
                },
            )
            _event(
                connection,
                int(passkey["user_id"]),
                "passkey_login",
                passkey["public_id"],
                ip,
                user_agent,
            )
            user_id = int(passkey["user_id"])
    except InvalidAuthenticationResponse as exc:
        raise SecuritySettingsError("パスキーでログインできませんでした。") from exc
    return create_session(user_id, remember=remember)
