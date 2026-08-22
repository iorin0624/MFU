from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta

import requests
from flask import Blueprint, current_app, jsonify, request, session
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.structs import (
    AuthenticationCredential,
    RegistrationCredential,
    UserVerificationRequirement,
)

from app.utils.db import get_db
from app.utils.logs import write_login_log
from app.utils.admin_auth import (
    ADMIN_USERNAME,
    audit,
    establish_admin_session,
    password_preauth_valid,
    recent_admin_mfa,
    validate_admin_session,
)
from app.utils.admin_passkey_stepup import (
    STEPUP_PURPOSE,
    consume_admin_passkey_grant,
    issue_admin_passkey_grant,
    normalize_admin_action,
)

webauthn_bp = Blueprint("webauthn", __name__, url_prefix="/webauthn")

RP_ID = "mfu.iori0624.jp"
ORIGIN = "https://mfu.iori0624.jp"
RP_NAME = "MFU"
CHALLENGE_TTL_SECONDS = 120
LABEL_MAX_LENGTH = 128
QR_APPROVAL_PURPOSE = "admin_qr_approval"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _user_verification(username: str) -> UserVerificationRequirement:
    return (
        UserVerificationRequirement.REQUIRED
        if username == "admin"
        else UserVerificationRequirement.PREFERRED
    )


def _user_verification_string(username: str) -> str:
    return "required" if username == "admin" else "preferred"


def _store_challenge(kind: str, username: str, challenge: str) -> None:
    session[f"webauthn_{kind}_challenge"] = challenge
    session[f"webauthn_{kind}_username"] = username
    session[f"webauthn_{kind}_expires_at"] = int(time.time()) + CHALLENGE_TTL_SECONDS


def _load_challenge(kind: str, username: str) -> str | None:
    if session.get(f"webauthn_{kind}_username") != username:
        return None
    expires_at = session.get(f"webauthn_{kind}_expires_at")
    if not expires_at or int(time.time()) > int(expires_at):
        return None
    return session.get(f"webauthn_{kind}_challenge")


def _clear_challenge(kind: str) -> None:
    session.pop(f"webauthn_{kind}_challenge", None)
    session.pop(f"webauthn_{kind}_username", None)
    session.pop(f"webauthn_{kind}_expires_at", None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_qr_challenge(token: str) -> dict | None:
    if len(token) < 40:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, status, expires_at
        FROM admin_qr_login_challenges
        WHERE token_hash=%s
        """,
        (_hash(token),),
    )
    row = cur.fetchone()
    db.close()
    if not row or row.get("status") != "pending" or row.get("expires_at") < datetime.now():
        return None
    return row


def _fetch_user(username: str) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT username, nickname, webhook_url FROM users WHERE username = %s",
        (username,),
    )
    row = cur.fetchone()
    db.close()
    return row


def _derive_label(explicit_label: str | None) -> str:
    label = (explicit_label or "").strip()
    if not label:
        ua = (request.user_agent.string or "").strip()
        label = ua[:LABEL_MAX_LENGTH]
    return label[:LABEL_MAX_LENGTH]


def _collect_request_metadata() -> dict:
    headers_to_log = [
        "Host",
        "Origin",
        "Referer",
        "User-Agent",
        "Content-Type",
        "Content-Length",
        "X-Forwarded-Proto",
        "X-Forwarded-For",
    ]
    headers = {
        header: request.headers.get(header)
        for header in headers_to_log
        if request.headers.get(header) is not None
    }
    json_data = None
    if request.is_json:
        try:
            json_data = request.get_json(silent=True)
        except Exception:
            json_data = None
    return {
        "method": request.method,
        "path": request.path,
        "endpoint": request.endpoint,
        "scheme": request.scheme,
        "host": request.host,
        "remote_addr": request.remote_addr,
        "headers": headers,
        "cookie_present": "Cookie" in request.headers,
        "is_json": request.is_json,
        "json_keys": sorted(json_data.keys()) if isinstance(json_data, dict) else [],
        "form_keys": sorted(request.form.keys()),
        "args_keys": sorted(request.args.keys()),
    }


def _log_webauthn_request(
    logger, reason: str | None = None, *, exception: bool = False
) -> None:
    data = _collect_request_metadata()
    if reason:
        data["reason"] = reason
    if exception:
        logger.exception("webauthn_request %s", data)
    else:
        logger.info("webauthn_request %s", data)


def _log_credential_shape(logger, credential, *, kind: str) -> None:
    """
    機密を出さずに credential の構造だけをログに出す。
    値（clientDataJSON/attestationObject/signature 等）は絶対に出さない。
    """
    try:
        cred_keys = sorted(credential.keys()) if isinstance(credential, dict) else []
        resp_keys = []
        if isinstance(credential, dict) and isinstance(credential.get("response"), dict):
            resp_keys = sorted(credential["response"].keys())
        logger.info(
            "webauthn_credential_shape %s",
            {"kind": kind, "cred_keys": cred_keys, "response_keys": resp_keys},
        )
    except Exception:
        logger.exception("webauthn_credential_shape_failed kind=%s", kind)


@webauthn_bp.post("/register/options")
def register_options():
    logger = current_app.logger
    _log_webauthn_request(logger)
    try:
        payload = request.get_json(silent=True) or {}
        username = (session.get("user") or "").strip()
        if not username:
            audit("PASSKEY_REGISTER_REJECTED", username="", details={"reason": "login_required"})
            return jsonify(error="ログインが必要です。"), 401
        if username == ADMIN_USERNAME:
            if not validate_admin_session(touch=False):
                return jsonify(error="管理者としてログインしてください。"), 401
            if not consume_admin_passkey_grant("admin_passkey_add"):
                audit("PASSKEY_REGISTER_REJECTED", details={"reason": "action_passkey_required"})
                return jsonify(error="パスキー追加には既存パスキーでの確認が必要です。"), 428
            session["admin_passkey_registration_until"] = int(time.time()) + CHALLENGE_TTL_SECONDS
        if False and username == ADMIN_USERNAME and not recent_admin_mfa():
            audit("PASSKEY_REGISTER_REJECTED", details={"reason": "recent_mfa_required"})
            return jsonify(error="パスキー登録には管理者の再認証が必要です。"), 403

        user = _fetch_user(username)
        if not user:
            return jsonify(error="ユーザーが見つかりません"), 404

        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT credential_id, transports FROM user_passkeys WHERE username = %s",
            (username,),
        )
        rows = cur.fetchall()
        db.close()

        exclude_credentials = []
        for row in rows:
            if row.get("credential_id"):
                # NOTE:
                # WebAuthn の excludeCredentials[].id は本来 bytes(ArrayBuffer)。
                # 現状は文字列(保存済み credential_id)を返している。
                # フロント側で base64url -> Uint8Array に復元して渡す前提。
                descriptor = {"type": "public-key", "id": row["credential_id"]}
                transports = row.get("transports")
                if transports:
                    try:
                        descriptor["transports"] = json.loads(transports)
                    except Exception:
                        pass
                exclude_credentials.append(descriptor)

        challenge = _b64url_encode(os.urandom(32))
        options = {
            "rp": {"name": RP_NAME, "id": RP_ID},
            "user": {
                "id": _b64url_encode(username.encode("utf-8")),
                "name": username,
                "displayName": user.get("nickname") or username,
            },
            "challenge": challenge,
            "timeout": CHALLENGE_TTL_SECONDS * 1000,
            "attestation": "none",
            "authenticatorSelection": {
                "userVerification": _user_verification_string(username)
            },
            "pubKeyCredParams": [
                {"type": "public-key", "alg": -7},
                {"type": "public-key", "alg": -257},
            ],
        }
        if exclude_credentials:
            options["excludeCredentials"] = exclude_credentials

        _store_challenge("register", username, challenge)
        return jsonify(options)
    except Exception:
        _log_webauthn_request(logger, reason="exception", exception=True)
        return jsonify(error="パスキー登録オプションの生成に失敗しました"), 500


@webauthn_bp.post("/register/verify")
def register_verify():
    logger = current_app.logger
    _log_webauthn_request(logger)
    if not request.is_json:
        _log_webauthn_request(logger, reason="not_json")
        return jsonify(error="リクエストが不正です"), 400

    payload = request.get_json(silent=True) or {}
    username = (session.get("user") or "").strip()
    credential = payload.get("credential")
    if not username or not credential:
        _log_webauthn_request(logger, reason="missing_fields")
        return jsonify(error="リクエストが不正です"), 400

    if username == ADMIN_USERNAME:
        registration_until = int(session.pop("admin_passkey_registration_until", 0) or 0)
        if not validate_admin_session(touch=False) or registration_until < int(time.time()):
            audit("PASSKEY_REGISTER_REJECTED", details={"reason": "registration_authorization_expired"})
            return jsonify(error="パスキー追加の操作許可が失効しました。最初からやり直してください。"), 428
    if False and username == ADMIN_USERNAME and not recent_admin_mfa():
        audit("PASSKEY_REGISTER_REJECTED", details={"reason": "recent_mfa_required"})
        return jsonify(error="パスキー登録には管理者の再認証が必要です。"), 403

    challenge = _load_challenge("register", username)
    if not challenge:
        _log_webauthn_request(logger, reason="challenge_missing")
        return jsonify(error="登録チャレンジの有効期限が切れました"), 400

    user = _fetch_user(username)
    if not user:
        return jsonify(error="ユーザーが見つかりません"), 404

    # credential の構造だけログ（値は出さない）
    _log_credential_shape(logger, credential, kind="register")

    try:
        # RegistrationCredential は parse_* を持たないため、dict のまま渡す
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge),
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            require_user_verification=(
                _user_verification(username) == UserVerificationRequirement.REQUIRED
            ),
        )
    except Exception:
        _log_webauthn_request(logger, reason="exception", exception=True)
        return jsonify(error="パスキー登録に失敗しました", reason="exception"), 500
    finally:
        _clear_challenge("register")

    credential_id = _b64url_encode(verification.credential_id)
    public_key = _b64url_encode(verification.credential_public_key)
    sign_count = int(verification.sign_count or 0)
    aaguid = str(getattr(verification, "aaguid", "") or "") or None
    uv = 1 if getattr(verification, "user_verified", False) else 0
    label = _derive_label(payload.get("label"))

    transports = None
    if isinstance(credential, dict):
        transports = (
            credential.get("response", {}).get("transports") or credential.get("transports")
        )
    transports_json = json.dumps(transports) if transports else None

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO user_passkeys
                (username, label, credential_id, public_key, sign_count, transports, aaguid, uv)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                username,
                label,
                credential_id,
                public_key,
                sign_count,
                transports_json,
                aaguid,
                uv,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        return jsonify(error="パスキーの保存に失敗しました"), 500
    finally:
        db.close()

    return jsonify(ok=True)


@webauthn_bp.post("/auth/options")
def auth_options():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    purpose = (payload.get("purpose") or "").strip()
    qr_token = str(payload.get("qr_token") or "")
    action = str(payload.get("action") or "")
    if not username:
        return jsonify(error="ユーザー名が必要です"), 400

    challenge_kind = "auth"
    if purpose == STEPUP_PURPOSE:
        if (
            username != ADMIN_USERNAME
            or session.get("user") != ADMIN_USERNAME
            or not validate_admin_session(touch=False)
        ):
            audit("ACTION_PASSKEY_REJECTED", details={"reason": "admin_session_required"})
            return jsonify(error="管理者としてログインしてください。"), 401
        try:
            action = normalize_admin_action(action)
        except ValueError:
            return jsonify(error="操作内容が不正です。"), 400
        challenge_kind = "admin_action"
        session["webauthn_admin_action"] = action
    elif purpose == QR_APPROVAL_PURPOSE:
        if (
            username != ADMIN_USERNAME
            or session.get("user") != ADMIN_USERNAME
            or not validate_admin_session(touch=False)
        ):
            audit("QR_PASSKEY_REJECTED", details={"reason": "admin_session_required"})
            return jsonify(error="管理者としてログインしてください。"), 401
        qr_challenge = _active_qr_challenge(qr_token)
        if not qr_challenge:
            audit("QR_PASSKEY_REJECTED", details={"reason": "qr_expired"})
            return jsonify(error="QRコードの有効期限が切れています。"), 410
        challenge_kind = "qr_approval"
        session["webauthn_qr_approval_token_hash"] = _hash(qr_token)
        session["webauthn_qr_approval_challenge_id"] = qr_challenge["id"]
    elif username == ADMIN_USERNAME and not password_preauth_valid(username):
        audit("PASSKEY_AUTH_REJECTED", details={"reason": "password_required"})
        return jsonify(error="IDとパスワードを先に確認してください。"), 401

    user = _fetch_user(username)
    if not user:
        return jsonify(error="ユーザーが見つかりません"), 404

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT credential_id, transports FROM user_passkeys WHERE username = %s",
        (username,),
    )
    rows = cur.fetchall()
    db.close()

    if not rows:
        return jsonify(error="このユーザーはパスキー未登録です"), 404

    allow_credentials = []
    for row in rows:
        if row.get("credential_id"):
            # NOTE:
            # allowCredentials[].id も本来 bytes(ArrayBuffer)。
            # 現状は文字列(保存済み credential_id)を返している。
            # フロント側で base64url -> Uint8Array に復元して渡す前提。
            descriptor = {"type": "public-key", "id": row["credential_id"]}
            transports = row.get("transports")
            if transports:
                try:
                    descriptor["transports"] = json.loads(transports)
                except Exception:
                    pass
            allow_credentials.append(descriptor)

    challenge = _b64url_encode(os.urandom(32))
    options = {
        "challenge": challenge,
        "timeout": CHALLENGE_TTL_SECONDS * 1000,
        "rpId": RP_ID,
        "allowCredentials": allow_credentials,
        "userVerification": _user_verification_string(username),
    }

    _store_challenge(challenge_kind, username, challenge)
    return jsonify(options)


@webauthn_bp.post("/auth/verify")
def auth_verify():
    logger = current_app.logger
    _log_webauthn_request(logger)

    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    credential = payload.get("credential")
    purpose = (payload.get("purpose") or "").strip()
    qr_token = str(payload.get("qr_token") or "")
    action = str(payload.get("action") or "")
    if not username or not credential:
        _log_webauthn_request(logger, reason="missing_fields")
        return jsonify(error="リクエストが不正です"), 400

    challenge_kind = "auth"
    if purpose == STEPUP_PURPOSE:
        try:
            action = normalize_admin_action(action)
        except ValueError:
            return jsonify(error="操作内容が不正です。"), 400
        if (
            username != ADMIN_USERNAME
            or session.get("user") != ADMIN_USERNAME
            or not validate_admin_session(touch=False)
            or session.get("webauthn_admin_action") != action
        ):
            audit("ACTION_PASSKEY_REJECTED", details={"action": action, "reason": "challenge_binding_failed"})
            return jsonify(error="操作時認証の情報が一致しません。"), 401
        challenge_kind = "admin_action"
    elif purpose == QR_APPROVAL_PURPOSE:
        challenge_kind = "qr_approval"
        if (
            username != ADMIN_USERNAME
            or session.get("user") != ADMIN_USERNAME
            or not validate_admin_session(touch=False)
            or session.get("webauthn_qr_approval_token_hash") != _hash(qr_token)
            or not _active_qr_challenge(qr_token)
        ):
            audit("QR_PASSKEY_REJECTED", details={"reason": "challenge_binding_failed"})
            return jsonify(error="QR承認の認証情報が一致しません。"), 401

    challenge = _load_challenge(challenge_kind, username)
    if not challenge:
        _log_webauthn_request(logger, reason="challenge_missing")
        return jsonify(error="ログインチャレンジの有効期限が切れました"), 400

    credential_id = credential.get("id") if isinstance(credential, dict) else None
    if not credential_id:
        _log_webauthn_request(logger, reason="credential_id_missing")
        return jsonify(error="クレデンシャルIDが取得できませんでした"), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, credential_id, public_key, sign_count
        FROM user_passkeys
        WHERE username = %s AND credential_id = %s
        """,
        (username, credential_id),
    )
    passkey = cur.fetchone()
    if not passkey:
        db.close()
        return jsonify(error="このパスキーは登録されていません"), 404

    user = _fetch_user(username)
    if not user:
        db.close()
        return jsonify(error="ユーザーが見つかりません"), 404

    # credential の構造だけログ（値は出さない）
    _log_credential_shape(logger, credential, kind="auth")

    try:
        # AuthenticationCredential は parse_* を持たないため、dict のまま渡す
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge),
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=_b64url_decode(passkey["public_key"]),
            credential_current_sign_count=int(passkey["sign_count"] or 0),
            require_user_verification=(
                _user_verification(username) == UserVerificationRequirement.REQUIRED
            ),
        )
    except Exception:
        logger.exception("webauthn_auth_verify_failed")
        db.close()
        return jsonify(error="パスキー認証に失敗しました", reason="exception"), 400
    finally:
        _clear_challenge(challenge_kind)

    try:
        cur.execute(
            """
            UPDATE user_passkeys
            SET sign_count = %s, last_used_at = NOW()
            WHERE id = %s
            """,
            (int(verification.new_sign_count), passkey["id"]),
        )
        db.commit()
    finally:
        db.close()

    if purpose == QR_APPROVAL_PURPOSE:
        token_hash = _hash(qr_token)
        challenge_id = session.pop("webauthn_qr_approval_challenge_id", None)
        session.pop("webauthn_qr_approval_token_hash", None)
        session["admin_qr_passkey_verified_token_hash"] = token_hash
        session["admin_qr_passkey_verified_until"] = int(time.time()) + CHALLENGE_TTL_SECONDS
        audit("QR_PASSKEY_VERIFIED", details={"challenge_id": challenge_id})
        return jsonify(ok=True, purpose=QR_APPROVAL_PURPOSE)

    if purpose == STEPUP_PURPOSE:
        session.pop("webauthn_admin_action", None)
        token = issue_admin_passkey_grant(action)
        return jsonify(ok=True, purpose=STEPUP_PURPOSE, action=action, token=token)

    if username == ADMIN_USERNAME:
        if not password_preauth_valid(username):
            audit("PASSKEY_AUTH_REJECTED", details={"reason": "password_preauth_expired"})
            return jsonify(error="パスワード認証からやり直してください。"), 401
        establish_admin_session(method="passkey", nickname=user.get("nickname"))
    else:
        session["user"] = username
        session["nickname"] = user.get("nickname")
        session.permanent = True
        write_login_log(username, request.remote_addr, tag="LOGIN_PASSKEY")

    return jsonify(ok=True, redirect=session.get("post_login_next") or "/upload")
