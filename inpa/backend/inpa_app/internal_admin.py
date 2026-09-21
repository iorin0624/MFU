"""HMAC-protected administration API exposed only through the Unix socket."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .admin_auth import require_admin_hmac, require_idempotency, write_audit
from .auth.service import hash_secret, new_public_id, new_secret
from .db import get_engine

bp = Blueprint("internal_admin", __name__, url_prefix="/internal/admin/v1")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_REPORT_STATUSES = {"open", "in_progress", "resolved", "dismissed"}


def _invitation_memo(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) > 255:
        raise ValueError("Invitation memo must be 255 characters or fewer.")
    return value.strip() or None


def _error(message: str, status: int = 400):
    return jsonify(error={"message": message}), status


def _json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row(row) -> dict[str, object]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _paging() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", "1")))
        per_page = min(100, max(1, int(request.args.get("per_page", "50"))))
    except ValueError as exc:
        raise ValueError("Invalid pagination.") from exc
    return per_page, (page - 1) * per_page


def _mutation_error(exc: Exception):
    if isinstance(exc, LookupError):
        return _error(str(exc), 409)
    return _error(str(exc), 400)


@bp.get("/bootstrap-status")
@require_admin_hmac
def bootstrap_status():
    return jsonify(service="inpa", status="internal_api_ready")


@bp.get("/summary")
@require_admin_hmac
def summary():
    with get_engine().connect() as connection:
        counts = connection.execute(text(
            "SELECT (SELECT COUNT(*) FROM users) AS users_total,"
            "(SELECT COUNT(*) FROM users WHERE status='active') AS users_active,"
            "(SELECT COUNT(*) FROM visits) AS visits_total,"
            "(SELECT COUNT(*) FROM share_tokens WHERE status='active') AS active_shares,"
            "(SELECT COUNT(*) FROM reports WHERE status IN ('open','in_progress')) AS open_reports,"
            "(SELECT COUNT(*) FROM mail_logs WHERE status='failed') AS failed_mail,"
            "(SELECT COUNT(*) FROM registration_invitations WHERE status IN ('active','claimed') "
            "AND expires_at>UTC_TIMESTAMP(6)) AS available_invitations,"
            "(SELECT COUNT(*) FROM security_events WHERE severity IN ('warning','critical') "
            "AND created_at>=UTC_TIMESTAMP()-INTERVAL 24 HOUR) AS security_warnings"
        )).mappings().one()
    return jsonify(summary=_row(counts))


@bp.get("/registration-invitations")
@require_admin_hmac
def registration_invitations():
    try:
        limit, offset = _paging()
    except ValueError as exc:
        return _error(str(exc))
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT i.public_id,i.token_last4,i.memo,i.status,i.created_by,i.created_at,i.expires_at,"
            "i.claimed_email_normalized,i.claimed_at,i.used_at,i.revoked_at,i.revoked_by,"
            "u.public_id AS used_by_user_public_id FROM registration_invitations i "
            "LEFT JOIN users u ON u.id=i.used_by_user_id ORDER BY i.created_at DESC "
            "LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset}).mappings().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    result = []
    for row in rows:
        item = _row(row)
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if item["status"] in {"active", "claimed"} and expires_at <= now:
            item["status"] = "expired"
        result.append(item)
    return jsonify(invitations=result)


@bp.post("/registration-invitations")
@require_admin_hmac
def create_registration_invitation():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise TypeError("JSON body is required.")
        memo = _invitation_memo(data.get("memo"))
        days = data.get("expires_in_days", 7)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise ValueError("Invitation expiry must be between 1 and 90 days.")
        token = new_secret()
        public_id = new_public_id()
        now = datetime.now(UTC).replace(tzinfo=None)
        expires_at = now + timedelta(days=days)
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            connection.execute(text(
                "INSERT INTO registration_invitations "
                "(public_id,token_hash,token_last4,memo,status,created_by,created_at,updated_at,expires_at) "
                "VALUES (:public_id,:token_hash,:last4,:memo,'active',:admin,:now,:now,:expires_at)"
            ), {
                "public_id": public_id, "token_hash": hash_secret(token), "last4": token[-4:],
                "memo": memo, "admin": g.inpa_admin, "now": now, "expires_at": expires_at,
            })
            write_audit(
                connection, action="registration_invitation_create",
                target_type="registration_invitation", target_id=public_id,
                after={"memo": memo, "token_last4": token[-4:], "expires_at": expires_at},
                idempotency_key=key,
            )
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    origin = current_app.config["PUBLIC_ORIGIN"].rstrip("/")
    return jsonify(
        public_id=public_id,
        token=token,
        registration_url=f"{origin}/register?invite={token}",
        expires_at=expires_at.isoformat(),
    ), 201


@bp.patch("/registration-invitations/<public_id>")
@require_admin_hmac
def update_registration_invitation(public_id: str):
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise TypeError("JSON body is required.")
        memo = _invitation_memo(data.get("memo"))
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT memo,status FROM registration_invitations WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Invitation not found.", 404)
            connection.execute(text(
                "UPDATE registration_invitations SET memo=:memo,updated_at=:now WHERE public_id=:id"
            ), {"memo": memo, "now": datetime.now(UTC).replace(tzinfo=None), "id": public_id})
            write_audit(
                connection, action="registration_invitation_update",
                target_type="registration_invitation", target_id=public_id,
                before={"memo": before["memo"]}, after={"memo": memo}, idempotency_key=key,
            )
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(public_id=public_id, memo=memo)


@bp.post("/registration-invitations/<public_id>/revoke")
@require_admin_hmac
def revoke_registration_invitation(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT status,token_last4,memo FROM registration_invitations "
                "WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Invitation not found.", 404)
            if before["status"] == "used":
                return _error("A used invitation cannot be revoked.", 409)
            if before["status"] == "revoked":
                return _error("Invitation is already revoked.", 409)
            now = datetime.now(UTC).replace(tzinfo=None)
            connection.execute(text(
                "UPDATE registration_invitations SET status='revoked',revoked_at=:now,"
                "revoked_by=:admin,updated_at=:now WHERE public_id=:id"
            ), {"now": now, "admin": g.inpa_admin, "id": public_id})
            write_audit(
                connection, action="registration_invitation_revoke",
                target_type="registration_invitation", target_id=public_id,
                before=_row(before), after={"status": "revoked"}, idempotency_key=key,
            )
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(status="revoked")


@bp.get("/users")
@require_admin_hmac
def users():
    try:
        limit, offset = _paging()
    except ValueError as exc:
        return _error(str(exc))
    status = request.args.get("status", "")
    query = request.args.get("q", "").strip()
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT public_id,email,display_name,status,email_verified_at,created_at,updated_at "
            "FROM users WHERE (:status='' OR status=:status) AND "
            "(:query='' OR email LIKE :like_query OR display_name LIKE :like_query OR public_id=:query) "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ), {"status": status, "query": query, "like_query": f"%{query}%", "limit": limit, "offset": offset}).mappings().all()
    return jsonify(users=[_row(row) for row in rows])


@bp.get("/users/<public_id>")
@require_admin_hmac
def user_detail(public_id: str):
    with get_engine().connect() as connection:
        user = connection.execute(text(
            "SELECT public_id,email,display_name,x_handle,instagram_handle,status,email_verified_at,"
            "created_at,updated_at,deletion_scheduled_at FROM users WHERE public_id=:id"
        ), {"id": public_id}).mappings().first()
        if not user:
            return _error("User not found.", 404)
        visits = connection.execute(text(
            "SELECT v.public_id,s.name AS season_name,v.visit_date,v.park,v.arrival_time,v.visibility,"
            "v.detail_level FROM visits v JOIN seasons s ON s.id=v.season_id "
            "JOIN users u ON u.id=v.user_id WHERE u.public_id=:id ORDER BY v.visit_date DESC LIMIT 100"
        ), {"id": public_id}).mappings().all()
        sessions = connection.execute(text(
            "SELECT us.public_id,us.created_at,us.last_seen_at,us.expires_at,us.revoked_at,us.revoke_reason "
            "FROM user_sessions us JOIN users u ON u.id=us.user_id WHERE u.public_id=:id "
            "ORDER BY us.created_at DESC LIMIT 100"
        ), {"id": public_id}).mappings().all()
    return jsonify(user=_row(user), visits=[_row(row) for row in visits], sessions=[_row(row) for row in sessions])


def _set_user_status(public_id: str, status: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text("SELECT status FROM users WHERE public_id=:id FOR UPDATE"), {"id": public_id}).mappings().first()
            if not before:
                return _error("User not found.", 404)
            connection.execute(text("UPDATE users SET status=:status WHERE public_id=:id"), {"status": status, "id": public_id})
            if status == "suspended":
                connection.execute(text(
                    "UPDATE user_sessions us JOIN users u ON u.id=us.user_id SET us.revoked_at=UTC_TIMESTAMP(6),"
                    "us.revoke_reason='admin_suspended' WHERE u.public_id=:id AND us.revoked_at IS NULL"
                ), {"id": public_id})
            write_audit(connection, action=f"user_{status}", target_type="user", target_id=public_id,
                        before=_row(before), after={"status": status}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(status=status)


@bp.post("/users/<public_id>/suspend")
@require_admin_hmac
def suspend_user(public_id: str):
    return _set_user_status(public_id, "suspended")


@bp.post("/users/<public_id>/unsuspend")
@require_admin_hmac
def unsuspend_user(public_id: str):
    return _set_user_status(public_id, "active")


@bp.post("/users/<public_id>/logout-all")
@require_admin_hmac
def logout_all(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            result = connection.execute(text(
                "UPDATE user_sessions us JOIN users u ON u.id=us.user_id "
                "SET us.revoked_at=UTC_TIMESTAMP(6),us.revoke_reason='admin_logout_all' "
                "WHERE u.public_id=:id AND us.revoked_at IS NULL"
            ), {"id": public_id})
            write_audit(connection, action="user_logout_all", target_type="user", target_id=public_id,
                        after={"revoked_sessions": result.rowcount}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(revoked_sessions=result.rowcount)


@bp.get("/visits")
@require_admin_hmac
def visits():
    try:
        limit, offset = _paging()
    except ValueError as exc:
        return _error(str(exc))
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT v.public_id,u.public_id AS user_public_id,u.display_name,s.name AS season_name,"
            "v.visit_date,v.park,v.arrival_time,v.costume,v.memo,v.visibility,v.detail_level,v.created_at "
            "FROM visits v JOIN users u ON u.id=v.user_id JOIN seasons s ON s.id=v.season_id "
            "WHERE (:season='' OR s.public_id=:season) AND (:user='' OR u.public_id=:user) "
            "ORDER BY v.visit_date DESC,v.id DESC LIMIT :limit OFFSET :offset"
        ), {"season": request.args.get("season_id", ""), "user": request.args.get("user_id", ""),
            "limit": limit, "offset": offset}).mappings().all()
    return jsonify(visits=[_row(row) for row in rows])


@bp.delete("/visits/<public_id>")
@require_admin_hmac
def delete_visit(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT public_id,user_id,visit_date,park,visibility,detail_level FROM visits WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Visit not found.", 404)
            connection.execute(text("DELETE FROM visits WHERE public_id=:id"), {"id": public_id})
            write_audit(connection, action="visit_delete", target_type="visit", target_id=public_id,
                        before=_row(before), idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return "", 204


@bp.post("/share-tokens/<public_id>/revoke")
@require_admin_hmac
def revoke_share(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT public_id,status,token_last4 FROM share_tokens WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Share token not found.", 404)
            connection.execute(text(
                "UPDATE share_tokens SET status='revoked',revoked_at=COALESCE(revoked_at,UTC_TIMESTAMP(6)),"
                "revoke_reason='admin_revoked' WHERE public_id=:id AND status='active'"
            ), {"id": public_id})
            write_audit(connection, action="share_revoke", target_type="share_token", target_id=public_id,
                        before=_row(before), after={"status": "revoked"}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(status="revoked")


def _season(row):
    return _row(row)


@bp.get("/seasons")
@require_admin_hmac
def list_seasons():
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT public_id,name,slug,start_date,end_date,is_active,created_at,updated_at "
            "FROM seasons ORDER BY start_date DESC"
        )).mappings().all()
    return jsonify(seasons=[_season(row) for row in rows])


def _season_values(data: object, current: dict | None = None):
    if not isinstance(data, dict):
        raise TypeError("JSON body is required.")
    merged = {**(current or {}), **data}
    name, slug = merged.get("name"), merged.get("slug")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Invalid season name.")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("Invalid season slug.")
    try:
        start, end = date.fromisoformat(str(merged["start_date"])), date.fromisoformat(str(merged["end_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid season dates.") from exc
    if start > end:
        raise ValueError("Season start must not be after the end.")
    active = merged.get("is_active", True)
    if not isinstance(active, bool):
        raise TypeError("is_active must be boolean.")
    return {"name": name.strip(), "slug": slug, "start_date": start, "end_date": end, "is_active": active}


@bp.post("/seasons")
@require_admin_hmac
def create_season():
    try:
        values = _season_values(request.get_json(silent=True)); public_id = new_public_id()
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            connection.execute(text(
                "INSERT INTO seasons (public_id,name,slug,start_date,end_date,is_active) "
                "VALUES (:public_id,:name,:slug,:start_date,:end_date,:is_active)"
            ), {**values, "public_id": public_id})
            write_audit(connection, action="season_create", target_type="season", target_id=public_id,
                        after=values, idempotency_key=key)
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    except IntegrityError:
        return _error("Season slug already exists.", 409)
    return jsonify(public_id=public_id), 201


@bp.patch("/seasons/<public_id>")
@require_admin_hmac
def update_season(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT name,slug,start_date,end_date,is_active,updated_at FROM seasons WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Season not found.", 404)
            data = request.get_json(silent=True) or {}
            if data.get("expected_updated_at") and data["expected_updated_at"] != _row(before)["updated_at"]:
                return _error("Season was updated by another administrator. Reload and try again.", 409)
            values = _season_values(data, _season(before))
            impacted = connection.execute(text(
                "SELECT COUNT(*) FROM visits v JOIN seasons s ON s.id=v.season_id "
                "WHERE s.public_id=:id AND (v.visit_date<:start OR v.visit_date>:end)"
            ), {"id": public_id, "start": values["start_date"], "end": values["end_date"]}).scalar()
            if impacted and data.get("confirm_impacted") is not True:
                return jsonify(error={"message": "Season change affects visits.", "impacted_count": impacted}), 409
            connection.execute(text(
                "UPDATE seasons SET name=:name,slug=:slug,start_date=:start_date,end_date=:end_date,"
                "is_active=:is_active WHERE public_id=:public_id"
            ), {**values, "public_id": public_id})
            write_audit(connection, action="season_update", target_type="season", target_id=public_id,
                        before=_season(before), after={**values, "impacted_count": impacted}, idempotency_key=key)
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    except IntegrityError:
        return _error("Season slug already exists.", 409)
    return jsonify(public_id=public_id, impacted_count=impacted)


@bp.get("/restriction-periods")
@require_admin_hmac
def list_restriction_periods():
    season_id = request.args.get("season_id", "")
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT r.public_id,s.public_id AS season_public_id,s.name AS season_name,r.name,"
            "r.restriction_type,r.start_date,r.end_date,r.park_scope,r.description,"
            "r.enforcement,r.is_active,r.created_at,r.updated_at FROM restriction_periods r "
            "JOIN seasons s ON s.id=r.season_id WHERE (:season='' OR s.public_id=:season) "
            "ORDER BY r.start_date DESC,r.id DESC"
        ), {"season": season_id}).mappings().all()
    return jsonify(restrictions=[_row(row) for row in rows])


def _restriction_values(connection, data: object, current: dict | None = None):
    if not isinstance(data, dict):
        raise TypeError("JSON body is required.")
    merged = {**(current or {}), **data}
    name = merged.get("name")
    restriction_type = merged.get("restriction_type", "costume_prohibited")
    season_public_id = merged.get("season_public_id")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Invalid restriction name.")
    if restriction_type not in {"costume_prohibited", "custom_notice"}:
        raise ValueError("Invalid restriction type.")
    if not isinstance(season_public_id, str):
        raise TypeError("Season is required.")
    season = connection.execute(text(
        "SELECT id,start_date,end_date FROM seasons WHERE public_id=:id"
    ), {"id": season_public_id}).mappings().first()
    if not season:
        raise ValueError("Season not found.")
    try:
        start = date.fromisoformat(str(merged["start_date"])); end = date.fromisoformat(str(merged["end_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid restriction dates.") from exc
    if start > end or start < season["start_date"] or end > season["end_date"]:
        raise ValueError("Restriction dates must be inside the season.")
    park_scope = merged.get("park_scope", "all")
    if park_scope not in {"all", "land", "sea"}:
        raise ValueError("Invalid park scope.")
    description = merged.get("description") or None
    if description is not None and (not isinstance(description, str) or len(description) > 500):
        raise ValueError("Restriction description is too long.")
    active = merged.get("is_active", True)
    if not isinstance(active, bool):
        raise TypeError("is_active must be boolean.")
    return {
        "season_id": season["id"], "season_public_id": season_public_id,
        "name": name.strip(), "restriction_type": restriction_type,
        "start_date": start, "end_date": end, "park_scope": park_scope,
        "description": description, "enforcement": "warning", "is_active": active,
    }


def _restriction_overlap(connection, values: dict[str, object], exclude: str = "") -> bool:
    if not values["is_active"]:
        return False
    return bool(connection.execute(text(
        "SELECT 1 FROM restriction_periods WHERE season_id=:season_id AND is_active=1 "
        "AND restriction_type=:restriction_type AND park_scope=:park_scope "
        "AND start_date<=:end_date AND end_date>=:start_date "
        "AND (:exclude='' OR public_id<>:exclude) LIMIT 1"
    ), {**values, "exclude": exclude}).first())


def _restriction_impact(connection, values: dict[str, object]) -> int:
    if values["restriction_type"] != "costume_prohibited":
        return 0
    return int(connection.execute(text(
        "SELECT COUNT(*) FROM visits WHERE season_id=:season_id AND costume IS NOT NULL "
        "AND costume<>'' AND visit_date BETWEEN :start_date AND :end_date "
        "AND (:park_scope='all' OR park=:park_scope OR park='both')"
    ), values).scalar())


@bp.post("/restriction-periods")
@require_admin_hmac
def create_restriction_period():
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            values = _restriction_values(connection, request.get_json(silent=True))
            if _restriction_overlap(connection, values):
                return _error("An overlapping active restriction already exists.", 409)
            public_id = new_public_id(); impact = _restriction_impact(connection, values)
            connection.execute(text(
                "INSERT INTO restriction_periods "
                "(public_id,season_id,name,restriction_type,start_date,end_date,park_scope,description,enforcement,is_active) "
                "VALUES (:public_id,:season_id,:name,:restriction_type,:start_date,:end_date,:park_scope,:description,:enforcement,:is_active)"
            ), {**values, "public_id": public_id})
            write_audit(connection, action="restriction_create", target_type="restriction_period",
                        target_id=public_id, after={**values, "impacted_visits": impact}, idempotency_key=key)
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(public_id=public_id, impacted_count=impact), 201


@bp.patch("/restriction-periods/<public_id>")
@require_admin_hmac
def update_restriction_period(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT r.name,r.restriction_type,r.start_date,r.end_date,r.park_scope,r.description,"
                "r.enforcement,r.is_active,s.public_id AS season_public_id FROM restriction_periods r "
                "JOIN seasons s ON s.id=r.season_id WHERE r.public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Restriction not found.", 404)
            values = _restriction_values(connection, request.get_json(silent=True), _row(before))
            if _restriction_overlap(connection, values, public_id):
                return _error("An overlapping active restriction already exists.", 409)
            impact = _restriction_impact(connection, values)
            connection.execute(text(
                "UPDATE restriction_periods SET season_id=:season_id,name=:name,"
                "restriction_type=:restriction_type,start_date=:start_date,end_date=:end_date,"
                "park_scope=:park_scope,description=:description,enforcement=:enforcement,"
                "is_active=:is_active,updated_at=UTC_TIMESTAMP(6) WHERE public_id=:public_id"
            ), {**values, "public_id": public_id})
            write_audit(connection, action="restriction_update", target_type="restriction_period",
                        target_id=public_id, before=_row(before),
                        after={**values, "impacted_visits": impact}, idempotency_key=key)
    except (LookupError, TypeError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(public_id=public_id, impacted_count=impact)


@bp.delete("/restriction-periods/<public_id>")
@require_admin_hmac
def deactivate_restriction_period(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text(
                "SELECT name,is_active FROM restriction_periods WHERE public_id=:id FOR UPDATE"
            ), {"id": public_id}).mappings().first()
            if not before:
                return _error("Restriction not found.", 404)
            connection.execute(text(
                "UPDATE restriction_periods SET is_active=0,updated_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
            ), {"id": public_id})
            write_audit(connection, action="restriction_deactivate", target_type="restriction_period",
                        target_id=public_id, before=_row(before), after={"is_active": False}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return "", 204


@bp.get("/reports")
@require_admin_hmac
def reports():
    try:
        limit, offset = _paging()
    except ValueError as exc:
        return _error(str(exc))
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            "SELECT r.public_id,r.category,r.detail,r.status,r.handled_by,r.handled_at,r.created_at,"
            "reporter.public_id AS reporter_public_id,target.public_id AS target_public_id,"
            "v.public_id AS target_visit_public_id FROM reports r "
            "LEFT JOIN users reporter ON reporter.id=r.reporter_user_id "
            "LEFT JOIN users target ON target.id=r.target_user_id LEFT JOIN visits v ON v.id=r.target_visit_id "
            "WHERE (:status='' OR r.status=:status) ORDER BY r.created_at DESC LIMIT :limit OFFSET :offset"
        ), {"status": request.args.get("status", ""), "limit": limit, "offset": offset}).mappings().all()
    return jsonify(reports=[_row(row) for row in rows])


@bp.patch("/reports/<public_id>")
@require_admin_hmac
def update_report(public_id: str):
    data = request.get_json(silent=True) or {}; status = data.get("status")
    if status not in _REPORT_STATUSES:
        return _error("Invalid report status.")
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text("SELECT status,handled_by,handled_at FROM reports WHERE public_id=:id FOR UPDATE"), {"id": public_id}).mappings().first()
            if not before:
                return _error("Report not found.", 404)
            connection.execute(text(
                "UPDATE reports SET status=:status,handled_by=:admin,handled_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
            ), {"status": status, "admin": request.headers["X-INPA-Admin"], "id": public_id})
            write_audit(connection, action="report_update", target_type="report", target_id=public_id,
                        before=_row(before), after={"status": status}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(status=status)


def _event_list(table: str, columns: str, order: str = "created_at"):
    limit, offset = _paging()
    with get_engine().connect() as connection:
        rows = connection.execute(text(
            f"SELECT {columns} FROM {table} ORDER BY {order} DESC LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset}).mappings().all()
    return [_row(row) for row in rows]


@bp.get("/security-events")
@require_admin_hmac
def security_events():
    try:
        return jsonify(events=_event_list("security_events", "event_type,severity,result,correlation_id,metadata_json,created_at"))
    except ValueError as exc:
        return _error(str(exc))


@bp.get("/mail-logs")
@require_admin_hmac
def mail_logs():
    try:
        return jsonify(logs=_event_list("mail_logs", "public_id,mail_type,status,attempt_count,next_attempt_at,sent_at,last_error_code,created_at,updated_at"))
    except ValueError as exc:
        return _error(str(exc))


@bp.post("/mail-logs/<public_id>/retry")
@require_admin_hmac
def retry_mail(public_id: str):
    try:
        with get_engine().begin() as connection:
            key = require_idempotency(connection)
            before = connection.execute(text("SELECT status,attempt_count FROM mail_logs WHERE public_id=:id FOR UPDATE"), {"id": public_id}).mappings().first()
            if not before:
                return _error("Mail log not found.", 404)
            connection.execute(text(
                "UPDATE mail_logs SET status='queued',next_attempt_at=NULL,last_error_code=NULL WHERE public_id=:id"
            ), {"id": public_id})
            write_audit(connection, action="mail_retry", target_type="mail_log", target_id=public_id,
                        before=_row(before), after={"status": "queued"}, idempotency_key=key)
    except (LookupError, ValueError) as exc:
        return _mutation_error(exc)
    return jsonify(status="queued")


@bp.get("/audit-logs")
@require_admin_hmac
def audit_logs():
    try:
        return jsonify(logs=_event_list(
            "admin_audit_logs",
            "admin_username,action,target_type,target_id,idempotency_key,before_json,after_json,result,correlation_id,created_at",
        ))
    except ValueError as exc:
        return _error(str(exc))
