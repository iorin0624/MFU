from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..db import get_engine
from ..privacy import create_default_matrix

_PASSWORDS = PasswordHasher()
_PUBLIC_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CONNECTION_ID_LENGTH = 8
_SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class AuthenticationError(Exception):
    pass


class RegistrationError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_public_id() -> str:
    return "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(26))


def new_connection_id() -> str:
    """Return the short, human-entered identifier used for connections."""
    return "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(_CONNECTION_ID_LENGTH))


def normalize_connection_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[-\s]", "", value).upper()
    if len(normalized) != _CONNECTION_ID_LENGTH:
        return ""
    if any(char not in _PUBLIC_ID_ALPHABET for char in normalized):
        return ""
    return normalized


def _available_connection_id(connection) -> str:
    for _ in range(10):
        candidate = new_connection_id()
        exists = connection.execute(
            text("SELECT 1 FROM users WHERE connection_id=:id LIMIT 1"), {"id": candidate}
        ).first()
        if not exists:
            return candidate
    raise RegistrationError("Could not issue a connection ID. Please try again.")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    pepper = current_app.config["TOKEN_PEPPER"].encode("utf-8")
    return hmac.new(pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _registration_invite_only(connection, *, lock: bool = False) -> bool:
    lock_clause = " FOR UPDATE" if lock and connection.dialect.name != "sqlite" else ""
    return bool(connection.execute(text(
        f"SELECT invite_only FROM registration_settings WHERE id=1{lock_clause}"
    )).scalar_one())


def registration_is_invite_only() -> bool:
    with get_engine().connect() as connection:
        return _registration_invite_only(connection)


def normalize_email(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise RegistrationError("Enter a valid email address.")
    email = value.strip()
    normalized = email.casefold()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise RegistrationError("Enter a valid email address.")
    return email, normalized


def validate_password(value: object) -> str:
    if not isinstance(value, str) or len(value) < 12 or len(value) > 256:
        raise RegistrationError("Use a password between 12 and 256 characters.")
    return value


def validate_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise RegistrationError("Enter a display name.")
    name = value.strip()
    if not (1 <= len(name) <= 40) or any(ord(char) < 32 for char in name):
        raise RegistrationError("Enter a display name between 1 and 40 characters.")
    return name


def normalize_social_handle(value: object, service: str) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        raise RegistrationError("SNS IDを確認してください。")
    handle = value.strip().lstrip("@")
    pattern = _X_HANDLE_RE if service == "x" else _INSTAGRAM_HANDLE_RE
    if not pattern.fullmatch(handle):
        raise RegistrationError(f"{'X' if service == 'x' else 'Instagram'} IDを確認してください。")
    return handle, handle.casefold()


def create_registration_request(
    invitation_value: object,
    email_value: object,
    requested_ip: bytes | None,
    user_agent: str | None,
) -> tuple[str, str] | None:
    """Claim an administrator invitation and create an email confirmation request."""
    invitation_token = invitation_value.strip() if isinstance(invitation_value, str) else ""
    email, normalized = normalize_email(email_value)
    token = new_secret()
    token_hash = hash_secret(token)
    public_id = new_public_id()
    now = _now()
    expires_at = now + timedelta(seconds=current_app.config["REGISTRATION_TTL_SECONDS"])
    engine = get_engine()
    with engine.begin() as connection:
        invite_only = _registration_invite_only(connection, lock=True)
        invitation = None
        if invitation_token:
            invitation = connection.execute(
                text(
                    "SELECT id,status,claimed_email_normalized FROM registration_invitations "
                    "WHERE token_hash=:token_hash AND revoked_at IS NULL AND used_at IS NULL "
                    "AND expires_at>:now FOR UPDATE"
                ),
                {"token_hash": hash_secret(invitation_token), "now": now},
            ).mappings().first()
            if not invitation or invitation["status"] not in {"active", "claimed"}:
                raise RegistrationError("This invitation token is invalid, expired, or unavailable.")
            if invitation["status"] == "claimed" and invitation["claimed_email_normalized"] != normalized:
                raise RegistrationError("This invitation token is invalid, expired, or unavailable.")
        elif invite_only:
            raise RegistrationError("An invitation token is required for registration.")
        existing = connection.execute(
            text("SELECT id FROM users WHERE email_normalized = :email LIMIT 1"), {"email": normalized}
        ).first()
        if existing:
            return None
        if invitation and invitation["status"] == "active":
            connection.execute(
                text(
                    "UPDATE registration_invitations SET status='claimed',"
                    "claimed_email_normalized=:email,claimed_at=:now,updated_at=:now WHERE id=:id"
                ),
                {"email": normalized, "now": now, "id": invitation["id"]},
            )
        connection.execute(
            text(
                "UPDATE registration_requests SET consumed_at = :now "
                "WHERE email_normalized = :email AND consumed_at IS NULL"
            ),
            {"now": now, "email": normalized},
        )
        connection.execute(
            text(
                "INSERT INTO registration_requests "
                "(public_id, invitation_id, email, email_normalized, token_hash, requested_ip, "
                "requested_user_agent, expires_at) VALUES (:public_id, :invitation_id, :email, "
                ":normalized, :token_hash, :ip, :agent, :expires_at)"
            ),
            {
                "public_id": public_id,
                "invitation_id": invitation["id"] if invitation else None,
                "email": email,
                "normalized": normalized,
                "token_hash": token_hash,
                "ip": requested_ip,
                "agent": (user_agent or "")[:512] or None,
                "expires_at": expires_at,
            },
        )
    return email, token


def complete_registration(
    payload: dict[str, object], requested_ip: bytes | None = None, user_agent: str | None = None
) -> tuple[int, str, str]:
    token_value = payload.get("token")
    if not isinstance(token_value, str) or not token_value:
        raise RegistrationError("This registration link is invalid or expired.")
    password = validate_password(payload.get("password"))
    if payload.get("terms_accepted") is not True or payload.get("privacy_accepted") is not True:
        raise RegistrationError("利用規約とプライバシーポリシーへの同意が必要です。")
    display_name = validate_display_name(payload.get("display_name"))
    x_handle, x_normalized = normalize_social_handle(payload.get("x_handle"), "x")
    instagram_handle, instagram_normalized = normalize_social_handle(
        payload.get("instagram_handle"), "instagram"
    )
    if not x_handle and not instagram_handle:
        raise RegistrationError("X IDまたはInstagram IDのどちらかを入力してください。")
    visibility = payload.get("default_visibility", "link")
    detail = payload.get("default_detail_level", "park")
    if visibility not in {"link", "logged_in", "following", "mutual", "private"}:
        raise RegistrationError("Choose a valid visibility setting.")
    if detail not in {"date", "park", "memo", "full"}:
        raise RegistrationError("Choose a valid detail setting.")

    now = _now()
    token_hash = hash_secret(token_value)
    engine = get_engine()
    with engine.begin() as connection:
        request_row = connection.execute(
            text(
                "SELECT r.id,r.invitation_id,r.email,r.email_normalized,i.status AS invitation_status,"
                "i.claimed_email_normalized,i.expires_at AS invitation_expires_at,i.revoked_at,i.used_at "
                "FROM registration_requests r LEFT JOIN registration_invitations i ON i.id=r.invitation_id "
                "WHERE r.token_hash=:token_hash AND r.consumed_at IS NULL AND r.expires_at>:now FOR UPDATE"
            ),
            {"token_hash": token_hash, "now": now},
        ).mappings().first()
        if not request_row:
            raise RegistrationError("This registration link is invalid or expired.")
        if request_row["invitation_id"] is None:
            invite_only = _registration_invite_only(connection, lock=True)
            if invite_only:
                raise RegistrationError("This registration link is invalid or expired.")
        elif (
            request_row["invitation_status"] != "claimed"
            or request_row["claimed_email_normalized"] != request_row["email_normalized"]
            or request_row["invitation_expires_at"] <= now
            or request_row["revoked_at"] is not None
            or request_row["used_at"] is not None
        ):
            raise RegistrationError("This registration link is invalid or expired.")
        duplicate = connection.execute(
            text("SELECT id FROM users WHERE email_normalized = :email LIMIT 1 FOR UPDATE"),
            {"email": request_row["email_normalized"]},
        ).first()
        if duplicate:
            raise RegistrationError("This registration link is invalid or expired.")
        documents = connection.execute(text(
            "SELECT id,public_id,document_type FROM legal_documents "
            "WHERE status='published' AND effective_at<=:now AND document_type IN ('terms','privacy') "
            "ORDER BY effective_at DESC,id DESC FOR UPDATE"
        ), {"now": now}).mappings().all()
        current_documents = {}
        for document in documents:
            current_documents.setdefault(document["document_type"], document)
        if (
            set(current_documents) != {"terms", "privacy"}
            or payload.get("terms_document_id") != current_documents["terms"]["public_id"]
            or payload.get("privacy_document_id") != current_documents["privacy"]["public_id"]
        ):
            raise RegistrationError("法務文書が更新されました。内容を再確認してください。")
        for column, value, label in (
            ("x_handle_normalized", x_normalized, "X ID"),
            ("instagram_handle_normalized", instagram_normalized, "Instagram ID"),
        ):
            if value and connection.execute(
                text(f"SELECT 1 FROM users WHERE {column}=:value LIMIT 1 FOR UPDATE"),
                {"value": value},
            ).first():
                raise RegistrationError(f"この{label}はすでに登録されています。")
        connection.execute(
            text(
                "INSERT INTO users "
                "(public_id, connection_id, email, email_normalized, password_hash, display_name, "
                "x_handle, x_handle_normalized, instagram_handle, instagram_handle_normalized, "
                "x_handle_visible, instagram_handle_visible, default_visibility, default_detail_level, email_verified_at) "
                "VALUES (:public_id, :connection_id, :email, :normalized, :password_hash, :display_name, "
                ":x_handle, :x_normalized, :instagram_handle, :instagram_normalized, :x_visible, "
                ":instagram_visible, :visibility, :detail, :now)"
            ),
            {
                "public_id": new_public_id(),
                "connection_id": _available_connection_id(connection),
                "email": request_row["email"],
                "normalized": request_row["email_normalized"],
                "password_hash": _PASSWORDS.hash(password),
                "display_name": display_name,
                "x_handle": x_handle,
                "x_normalized": x_normalized,
                "instagram_handle": instagram_handle,
                "instagram_normalized": instagram_normalized,
                "x_visible": int(bool(payload.get("x_handle_visible", False))),
                "instagram_visible": int(bool(payload.get("instagram_handle_visible", False))),
                "visibility": visibility,
                "detail": detail,
                "now": now,
            },
        )
        user_id = connection.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()
        create_default_matrix(connection, int(user_id))
        for document in current_documents.values():
            connection.execute(text(
                "INSERT INTO user_legal_consents "
                "(user_id,legal_document_id,consented_at,consent_method,ip_address,user_agent) "
                "VALUES (:user_id,:document_id,:now,'registration',:ip,:agent)"
            ), {
                "user_id": user_id, "document_id": document["id"], "now": now,
                "ip": requested_ip, "agent": (user_agent or "")[:512] or None,
            })
        connection.execute(
            text("UPDATE registration_requests SET consumed_at = :now WHERE id = :id"),
            {"now": now, "id": request_row["id"]},
        )
        if request_row["invitation_id"] is not None:
            connection.execute(
                text(
                    "UPDATE registration_invitations SET status='used',used_by_user_id=:user_id,"
                    "used_at=:now,updated_at=:now WHERE id=:id AND status='claimed'"
                ),
                {"user_id": user_id, "now": now, "id": request_row["invitation_id"]},
            )
    return create_session(int(user_id), remember=True)


def authenticate(email_value: object, password_value: object, remember: bool) -> tuple[int, str, str]:
    try:
        _, normalized = normalize_email(email_value)
    except RegistrationError as exc:
        raise AuthenticationError("Invalid email address or password.") from exc
    if not isinstance(password_value, str):
        raise AuthenticationError("Invalid email address or password.")
    engine = get_engine()
    with engine.connect() as connection:
        user = connection.execute(
            text("SELECT id, password_hash, status, email_verified_at FROM users WHERE email_normalized = :email LIMIT 1"),
            {"email": normalized},
        ).mappings().first()
    if not user or user["status"] != "active" or user["email_verified_at"] is None:
        raise AuthenticationError("Invalid email address or password.")
    try:
        valid = _PASSWORDS.verify(user["password_hash"], password_value)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid:
        raise AuthenticationError("Invalid email address or password.")
    return create_session(int(user["id"]), remember=remember)


def create_session(user_id: int, remember: bool) -> tuple[int, str, str]:
    now = _now()
    token = new_secret()
    csrf_secret = new_secret()
    expires_at = now + timedelta(days=current_app.config["SESSION_IDLE_DAYS"])
    absolute_expires_at = now + timedelta(days=current_app.config["SESSION_ABSOLUTE_DAYS"])
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_sessions "
                "(public_id, user_id, token_hash, csrf_secret_hash, expires_at, absolute_expires_at) "
                "VALUES (:public_id, :user_id, :token_hash, :csrf_hash, :expires_at, :absolute_expires_at)"
            ),
            {
                "public_id": new_public_id(),
                "user_id": user_id,
                "token_hash": hash_secret(token),
                "csrf_hash": hash_secret(csrf_secret),
                "expires_at": expires_at,
                "absolute_expires_at": absolute_expires_at,
            },
        )
    return user_id, token, csrf_secret


def get_session(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    now = _now()
    engine = get_engine()
    with engine.connect() as connection:
        session = connection.execute(
            text(
                "SELECT s.id, s.user_id, s.csrf_secret_hash, s.last_seen_at, "
                "u.public_id, u.connection_id, "
                "u.display_name, u.status "
                "FROM user_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = :token_hash AND s.revoked_at IS NULL "
                "AND s.expires_at > :now AND s.absolute_expires_at > :now LIMIT 1"
            ),
            {"token_hash": hash_secret(token), "now": now},
        ).mappings().first()
        if not session or session["status"] != "active":
            return None
    _touch_session(engine, int(session["id"]), session["last_seen_at"], now)
    return dict(session)


def _touch_session(engine, session_id: int, last_seen_at: datetime | None, now: datetime) -> None:
    """Refresh session activity at most once per interval without failing the request."""
    threshold = now - _SESSION_TOUCH_INTERVAL
    if last_seen_at is not None and last_seen_at >= threshold:
        return
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE user_sessions SET last_seen_at=:now "
                    "WHERE id=:id AND last_seen_at<:threshold"
                ),
                {"now": now, "id": session_id, "threshold": threshold},
            )
    except SQLAlchemyError:
        # Session validity was already established by the read above. Activity
        # tracking is best-effort and must not turn a valid API request into 500.
        current_app.logger.warning(
            "Session activity timestamp update failed; continuing the request.",
            exc_info=True,
        )


def verify_csrf(session: dict[str, object], csrf_secret: str | None) -> bool:
    return bool(csrf_secret) and hmac.compare_digest(str(session["csrf_secret_hash"]), hash_secret(csrf_secret))


def revoke_session(session_id: int) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE user_sessions SET revoked_at = :now, revoke_reason = 'logout' WHERE id = :id AND revoked_at IS NULL"),
            {"now": _now(), "id": session_id},
        )
