from __future__ import annotations

import ipaddress

import httpx
from flask import Blueprint, current_app, g, jsonify, request

from ..mail_queue import queue_registration_email
from .service import (
    AuthenticationError,
    RegistrationError,
    authenticate,
    complete_registration,
    create_registration_request,
    get_session,
    registration_is_invite_only,
    revoke_session,
    verify_csrf,
)

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")


def _error(code: str, message: str, status: int):
    return jsonify(error={"code": code, "message": message}), status


def _payload() -> dict[str, object]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise RegistrationError("Send a JSON object.")
    return value


def _client_ip() -> bytes | None:
    try:
        return ipaddress.ip_address(request.remote_addr or "").packed
    except ValueError:
        return None


def _verify_turnstile(token: object) -> bool:
    if current_app.config["TURNSTILE_BYPASS"]:
        return True
    if not isinstance(token, str) or not token:
        return False
    try:
        response = httpx.post(
            current_app.config["TURNSTILE_VERIFY_URL"],
            data={
                "secret": current_app.config["TURNSTILE_SECRET_KEY"],
                "response": token,
                "remoteip": request.remote_addr,
            },
            timeout=5,
        )
        return bool(response.raise_for_status().json().get("success"))
    except httpx.HTTPError:
        return False


def _set_session_cookies(response, token: str, csrf_secret: str, remember: bool) -> None:
    secure = current_app.config["ENVIRONMENT"] == "production"
    kwargs = {"secure": secure, "httponly": True, "samesite": "Lax", "path": "/"}
    if remember:
        kwargs["max_age"] = current_app.config["SESSION_IDLE_DAYS"] * 24 * 60 * 60
    response.set_cookie(current_app.config["SESSION_COOKIE_NAME"], token, **kwargs)
    csrf_kwargs = {**kwargs, "httponly": False}
    response.set_cookie(current_app.config["CSRF_COOKIE_NAME"], csrf_secret, **csrf_kwargs)


def _clear_session_cookies(response) -> None:
    secure = current_app.config["ENVIRONMENT"] == "production"
    response.delete_cookie(current_app.config["SESSION_COOKIE_NAME"], path="/", secure=secure, httponly=True, samesite="Lax")
    response.delete_cookie(current_app.config["CSRF_COOKIE_NAME"], path="/", secure=secure, httponly=False, samesite="Lax")


def _require_session():
    session = get_session(request.cookies.get(current_app.config["SESSION_COOKIE_NAME"]))
    if not session:
        return None, _error("authentication_required", "Sign in to continue.", 401)
    g.inpa_session = session
    return session, None


@bp.get("/registration-settings")
def registration_settings():
    return jsonify(invite_only=registration_is_invite_only())


@bp.post("/register/request")
def register_request():
    try:
        payload = _payload()
        if not _verify_turnstile(payload.get("turnstile_token")):
            return _error("verification_failed", "We could not verify your request. Please try again.", 400)
        created = create_registration_request(
            payload.get("invitation_token"), payload.get("email"), _client_ip(), request.user_agent.string
        )
        if created:
            queue_registration_email(*created)
    except RegistrationError as exc:
        return _error("invalid_request", str(exc), 400)
    return jsonify(message="If the address can be registered, we sent a confirmation email."), 202


@bp.post("/register/complete")
def register_complete():
    try:
        _, token, csrf_secret = complete_registration(_payload())
    except RegistrationError as exc:
        return _error("registration_invalid", str(exc), 400)
    response = jsonify(message="Registration complete.")
    _set_session_cookies(response, token, csrf_secret, remember=True)
    return response, 201


@bp.post("/email/resend")
def resend_registration_email():
    try:
        payload = _payload()
        if not _verify_turnstile(payload.get("turnstile_token")):
            return _error("verification_failed", "We could not verify your request. Please try again.", 400)
        created = create_registration_request(
            payload.get("invitation_token"), payload.get("email"), _client_ip(), request.user_agent.string
        )
        if created:
            queue_registration_email(*created)
    except RegistrationError as exc:
        return _error("invalid_request", str(exc), 400)
    return jsonify(message="If the address can be registered, we sent a confirmation email."), 202


@bp.post("/login")
def login():
    try:
        payload = _payload()
        _, token, csrf_secret = authenticate(payload.get("email"), payload.get("password"), bool(payload.get("remember", False)))
    except (RegistrationError, AuthenticationError):
        return _error("login_failed", "Invalid email address or password.", 401)
    response = jsonify(message="Signed in.")
    _set_session_cookies(response, token, csrf_secret, remember=bool(payload.get("remember", False)))
    return response


@bp.post("/logout")
def logout():
    session, error = _require_session()
    if error:
        return error
    csrf_secret = request.headers.get("X-CSRF-Token")
    if not verify_csrf(session, csrf_secret):
        return _error("csrf_failed", "Your request could not be verified.", 403)
    revoke_session(int(session["id"]))
    response = jsonify(message="Signed out.")
    _clear_session_cookies(response)
    return response


@bp.get("/me")
def me():
    session, error = _require_session()
    if error:
        return error
    return jsonify(user={
        "public_id": session["public_id"],
        "connection_id": session["connection_id"],
        "display_name": session["display_name"],
    })
