from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, request, session
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.structs import (
    AuthenticationCredential,
    RegistrationCredential,
    UserVerificationRequirement,
)

from app.utils.db import get_db
from app.utils.logs import write_login_log

webauthn_bp = Blueprint("webauthn", __name__, url_prefix="/webauthn")

RP_ID = "mfu.iori0624.jp"
ORIGIN = "https://mfu.iori0624.jp"
RP_NAME = "MFU"
CHALLENGE_TTL_SECONDS = 120
LABEL_MAX_LENGTH = 128


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


def _fetch_user(username: str) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT username, nickname, webhook_url FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    db.close()
    return row


def _derive_label(explicit_label: str | None) -> str:
    label = (explicit_label or "").strip()
    if not label:
        ua = (request.user_agent.string or "").strip()
        label = ua[:LABEL_MAX_LENGTH]
    return label[:LABEL_MAX_LENGTH]


@webauthn_bp.post("/register/options")
def register_options():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    if not username:
        return jsonify(error="ユーザー名が必要です"), 400

    user = _fetch_user(username)
    if not user:
        return jsonify(error="ユーザーが見つかりません"), 404

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT credential_id, transports FROM user_passkeys WHERE username = %s", (username,)
    )
    rows = cur.fetchall()
    db.close()

    exclude_credentials = []
    for row in rows:
        if row.get("credential_id"):
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
        "authenticatorSelection": {"userVerification": _user_verification_string(username)},
        "pubKeyCredParams": [
            {"type": "public-key", "alg": -7},
            {"type": "public-key", "alg": -257},
        ],
    }
    if exclude_credentials:
        options["excludeCredentials"] = exclude_credentials

    _store_challenge("register", username, challenge)
    return jsonify(options)


@webauthn_bp.post("/register/verify")
def register_verify():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    credential = payload.get("credential")
    if not username or not credential:
        return jsonify(error="リクエストが不正です"), 400

    challenge = _load_challenge("register", username)
    if not challenge:
        return jsonify(error="登録チャレンジの有効期限が切れました"), 400

    user = _fetch_user(username)
    if not user:
        return jsonify(error="ユーザーが見つかりません"), 404

    try:
        verification = verify_registration_response(
            credential=RegistrationCredential.parse_raw(json.dumps(credential)),
            expected_challenge=_b64url_decode(challenge),
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            require_user_verification=(
                _user_verification(username) == UserVerificationRequirement.REQUIRED
            ),
        )
    except Exception:
        return jsonify(error="パスキー登録に失敗しました"), 400
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
    if not username:
        return jsonify(error="ユーザー名が必要です"), 400

    user = _fetch_user(username)
    if not user:
        return jsonify(error="ユーザーが見つかりません"), 404

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT credential_id, transports FROM user_passkeys WHERE username = %s", (username,)
    )
    rows = cur.fetchall()
    db.close()

    if not rows:
        return jsonify(error="このユーザーはパスキー未登録です"), 404

    allow_credentials = []
    for row in rows:
        if row.get("credential_id"):
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

    _store_challenge("auth", username, challenge)
    return jsonify(options)


@webauthn_bp.post("/auth/verify")
def auth_verify():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    credential = payload.get("credential")
    if not username or not credential:
        return jsonify(error="リクエストが不正です"), 400

    challenge = _load_challenge("auth", username)
    if not challenge:
        return jsonify(error="ログインチャレンジの有効期限が切れました"), 400

    credential_id = credential.get("id") if isinstance(credential, dict) else None
    if not credential_id:
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

    try:
        verification = verify_authentication_response(
            credential=AuthenticationCredential.parse_raw(json.dumps(credential)),
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
        db.close()
        return jsonify(error="パスキー認証に失敗しました"), 400
    finally:
        _clear_challenge("auth")

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

    session["user"] = username
    session["nickname"] = user.get("nickname")
    session["login_expires_at"] = datetime.now() + timedelta(hours=24)
    session["login_extension_count"] = 0
    write_login_log(username, request.remote_addr, tag="LOGIN_PASSKEY")

    if username == "admin" and user.get("webhook_url"):
        try:
            login_time = datetime.now().strftime("%Y/%m/%d %H:%M")
            login_ip = request.remote_addr
            message = (
                "👤 **管理者ログイン**\n"
                f"📅 ログイン日時: {login_time}\n"
                f"🌐 ログインIP: {login_ip}"
            )
            requests.post(user["webhook_url"], json={"content": message})
        except Exception as e:
            print(f"Discord通知エラー: {e}")

    return jsonify(ok=True, redirect="/upload")
