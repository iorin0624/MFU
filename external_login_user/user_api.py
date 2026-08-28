"""Mobile-first Vue API for the external event participant portal.

The server-rendered pages stay available during migration.  These endpoints
only expose fields already shown to the current participant and re-check event
membership/ACL on every request.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Any

from flask import current_app, jsonify, render_template, request, session, url_for

from app.utils.db import get_db

from . import bp
from .utils import (
    _event_acl_role,
    _event_by_uuid_str,
    _get_current_privacy_policy_config,
    _get_ext_user_by_social,
    _needs_privacy_policy_agreement,
    _uuid_bytes_to_str,
    avatar_url_for,
    is_withdrawn_ext_user,
)


API_PAGE_SIZE_MAX = 200


@bp.get("/vue-preview", defaults={"vue_path": ""})
@bp.get("/vue-preview/", defaults={"vue_path": ""})
@bp.get("/vue-preview/<path:vue_path>")
def user_vue_preview(vue_path: str):
    """Serve the participant Vue client without replacing legacy pages."""
    return render_template(
        "external_login_vue.html",
        event_vue_config={
            "basePath": url_for("external_login_user.user_vue_preview"),
            "bootstrapUrl": url_for("external_login_user.user_api_bootstrap"),
            "eventsUrl": url_for("external_login_user.user_api_events"),
            "albumApiBase": "/album/api",
            "loginUrl": url_for("external_login_user.index"),
        },
    )


def _value(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return _uuid_bytes_to_str(value) or value.hex()
    return value


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _value(value) for key, value in row.items()}


def _ok(**payload):
    response = jsonify({"ok": True, **payload})
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(error: str, status: int, message: str | None = None, **payload):
    body = {"ok": False, "error": error, **payload}
    if message:
        body["message"] = message
    response = jsonify(body)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _csrf_token() -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _external_user() -> dict[str, Any] | None:
    social_id = session.get("ext_user_social_id")
    user = _get_ext_user_by_social(str(social_id)) if social_id else None
    if not user or is_withdrawn_ext_user(user):
        return None
    try:
        if int(user.get("is_deleted") or 0) == 1:
            return None
    except (TypeError, ValueError):
        return None
    return user


def _actor() -> dict[str, Any] | None:
    external = _external_user()
    if external:
        return {"kind": "external", "external": external, "username": None}
    username = str(session.get("user") or "").strip()
    if username:
        return {"kind": "mfu", "external": None, "username": username}
    return None


def _profile(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(user.get("id") or 0),
        "nickname": str(user.get("nickname") or ""),
        "xId": user.get("x_id"),
        "instagramId": user.get("instagram_id"),
        "email": user.get("email"),
        "emailVerified": bool(user.get("email_verified_at")),
        "avatarUrl": avatar_url_for(user),
    }


def _navigation() -> list[dict[str, Any]]:
    return [
        {"id": "home", "label": "ホーム", "url": url_for("external_login_user.index")},
        {"id": "events", "label": "イベント", "url": url_for("external_login_user.index")},
        {"id": "chat", "label": "チャット", "url": "/chat/", "badge": "chat"},
        {
            "id": "notifications",
            "label": "通知",
            "url": url_for("external_login_user.notifications_page"),
            "badge": "notifications",
        },
        {"id": "account", "label": "アカウント", "url": url_for("external_login_user.profile")},
    ]


def _notification_unread_count(external_user_id: int | None) -> int:
    if not external_user_id:
        return 0
    try:
        from .notifications import _compute_unread_count_external

        return int(_compute_unread_count_external(int(external_user_id)))
    except Exception:
        current_app.logger.warning("Vue event API unread count failed", exc_info=True)
        return 0


def _session_payload() -> dict[str, Any]:
    actor = _actor()
    external = (actor or {}).get("external")
    privacy_required = False
    if external:
        try:
            privacy_required = bool(
                _needs_privacy_policy_agreement(
                    external,
                    _get_current_privacy_policy_config(),
                )
            )
        except Exception:
            privacy_required = False
    return {
        "authenticated": bool(actor),
        "actorKind": (actor or {}).get("kind"),
        "profile": _profile(external) if external else None,
        "mfuUsername": (actor or {}).get("username"),
        "csrfToken": _csrf_token(),
        "navigation": _navigation(),
        "prerequisites": {
            "emailVerificationRequired": bool(
                external and external.get("email") and not external.get("email_verified_at")
            ),
            "privacyAgreementRequired": privacy_required,
        },
        "unread": {
            "notifications": _notification_unread_count(
                int(external.get("id") or 0) if external else None
            )
        },
    }


def _latest_membership(event_id: int, user_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT m.*
              FROM mfu_event_member AS m
             WHERE m.event_id=%s AND m.user_id=%s
             ORDER BY m.id DESC
             LIMIT 1
            """,
            (int(event_id), int(user_id)),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def _event_access(event: dict[str, Any], actor: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
    event_id = int(event.get("id") or 0)
    if actor["kind"] == "external":
        membership = _latest_membership(event_id, int(actor["external"]["id"]))
        return bool(membership), membership, "member" if membership else None
    username = str(actor.get("username") or "")
    if username == "admin":
        return True, None, "admin"
    acl_role = _event_acl_role(event_id, username)
    return bool(acl_role), None, f"acl_{acl_role}" if acl_role else None


def _event_permissions(event: dict[str, Any], membership: dict[str, Any] | None, role: str | None) -> dict[str, bool]:
    elevated = role == "admin" or str(role or "").startswith("acl_")
    approved = bool(
        membership
        and str(membership.get("status") or "").lower() == "approved"
        and int(membership.get("is_canceled") or 0) == 0
    )
    active = bool(membership and int(membership.get("is_canceled") or 0) == 0)
    return {
        "canView": True,
        "canOpenChat": bool(elevated or approved),
        "canOpenAlbum": bool((elevated or approved) and event.get("album_id")),
        "canViewMembers": bool(elevated or approved),
        "canOpenPass": bool(role == "member" and approved),
        "canEditOwnRole": bool(not elevated and active),
        "canManageEvent": bool(elevated),
    }


def _membership_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": int(row.get("id") or 0),
        "status": str(row.get("status") or "pending"),
        "isCanceled": bool(row.get("is_canceled")),
        "paymentStatus": str(row.get("payment_status") or "unpaid"),
        "requirePayment": bool(int(row.get("require_payment") if row.get("require_payment") is not None else 1)),
        "paidAmountYen": row.get("paid_amount_yen"),
        "paidAt": _value(row.get("paid_at")),
        "participantRole": str(row.get("participant_role") or "none"),
        "costumeLabel": str(row.get("costume_label") or ""),
        "isHost": bool(row.get("is_host")),
        "isSubhost": bool(row.get("is_subhost")),
        "process": bool(row.get("process")),
        "checkinAt": _value(row.get("checkin_at")),
    }


def _event_payload(event: dict[str, Any], membership: dict[str, Any] | None, role: str | None) -> dict[str, Any]:
    event_uuid = str(event.get("event_uuid_str") or _uuid_bytes_to_str(event.get("event_uuid")) or "")
    album_id = str(event.get("album_id") or "") or None
    permissions = _event_permissions(event, membership, role)
    return {
        "id": int(event.get("id") or 0),
        "uuid": event_uuid,
        "title": str(event.get("title") or ""),
        "startsAt": _value(event.get("starts_at")),
        "placeName": event.get("place_name"),
        "address": event.get("address"),
        "mapsUrl": event.get("maps_url"),
        "snsHashtag": event.get("sns_hashtag"),
        "googleFormUrl": event.get("google_form_url"),
        "lineOpenchatUrl": event.get("line_openchat_url"),
        "feeYen": event.get("fee_yen"),
        "tipEnabled": bool(event.get("tip_enabled")),
        "payFrom": _value(event.get("pay_from")),
        "payUntil": _value(event.get("pay_until")),
        "albumId": album_id,
        "membership": _membership_payload(membership),
        "accessRole": role,
        "permissions": permissions,
        "urls": {
            "detail": url_for("external_login_user.view_event", event_uuid=event_uuid),
            "chat": f"/chat/events/{int(event.get('id') or 0)}",
            "album": (
                url_for("external_login_user.event_album_direct", event_uuid=event_uuid)
                if album_id
                else None
            ),
            "members": url_for("external_login_user.member_list", event_uuid=event_uuid),
            "payment": url_for("external_login_user.pay_start", event_uuid=event_uuid),
            "pass": (
                url_for("external_login_user.user_vue_preview", vue_path=f"events/{event_uuid}/pass")
                if permissions.get("canOpenPass")
                else None
            ),
        },
    }


def _event_rows(actor: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any] | None, str | None]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if actor["kind"] == "external":
            cur.execute(
                """
                SELECT e.*
                  FROM mfu_event AS e
                  JOIN (
                        SELECT event_id, MAX(id) AS member_id
                          FROM mfu_event_member
                         WHERE user_id=%s
                         GROUP BY event_id
                  ) AS latest ON latest.event_id=e.id
                 WHERE e.deleted_at IS NULL
                 ORDER BY e.starts_at IS NULL, e.starts_at ASC
                 LIMIT 500
                """,
                (int(actor["external"]["id"]),),
            )
        elif actor.get("username") == "admin":
            cur.execute(
                "SELECT e.* FROM mfu_event AS e WHERE e.deleted_at IS NULL "
                "ORDER BY e.starts_at IS NULL, e.starts_at ASC LIMIT 500"
            )
        else:
            cur.execute(
                """
                SELECT e.*
                  FROM mfu_event AS e
                  JOIN mfu_event_admin_acl AS acl ON acl.event_id=e.id
                 WHERE acl.username=%s AND e.deleted_at IS NULL
                 ORDER BY e.starts_at IS NULL, e.starts_at ASC
                 LIMIT 500
                """,
                (str(actor.get("username") or ""),),
            )
        events = cur.fetchall() or []
    finally:
        cur.close()
        db.close()

    result = []
    for event in events:
        event["event_uuid_str"] = _uuid_bytes_to_str(event.get("event_uuid"))
        allowed, membership, role = _event_access(event, actor)
        if allowed:
            result.append((event, membership, role))
    return result


@bp.get("/api/vue/session")
def user_api_session():
    return _ok(session=_session_payload())


@bp.get("/api/vue/bootstrap")
def user_api_bootstrap():
    actor = _actor()
    session_payload = _session_payload()
    if not actor:
        return _ok(session=session_payload, events=[])
    rows = _event_rows(actor)
    events = [_event_payload(event, membership, role) for event, membership, role in rows]
    return _ok(session=session_payload, events=events)


@bp.get("/api/vue/events")
def user_api_events():
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    scope = str(request.args.get("scope") or "all").lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
        per_page = min(API_PAGE_SIZE_MAX, max(1, int(request.args.get("perPage") or 50)))
    except (TypeError, ValueError):
        return _error("invalid_pagination", 400)
    now = datetime.now()
    items = []
    for event, membership, role in _event_rows(actor):
        starts_at = event.get("starts_at")
        is_upcoming = not starts_at or starts_at.date() >= now.date()
        if scope == "upcoming" and not is_upcoming:
            continue
        if scope == "past" and is_upcoming:
            continue
        items.append(_event_payload(event, membership, role))
    if scope == "past":
        items.reverse()
    total = len(items)
    start = (page - 1) * per_page
    return _ok(
        events=items[start:start + per_page],
        pagination={
            "page": page,
            "perPage": per_page,
            "total": total,
            "hasNext": start + per_page < total,
            "hasPrevious": page > 1,
        },
    )


@bp.get("/api/vue/events/<event_uuid>")
def user_api_event(event_uuid: str):
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    allowed, membership, role = _event_access(event, actor)
    if not allowed:
        return _error("forbidden", 403)
    return _ok(event=_event_payload(event, membership, role))


@bp.get("/api/vue/events/<event_uuid>/pass")
def user_api_event_pass(event_uuid: str):
    """Return the participant's own pass without weakening legacy access rules."""
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401, "参加者としてのログインが必要です。")
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    membership = _latest_membership(int(event.get("id") or 0), int(actor["external"].get("id") or 0))
    approved = bool(
        membership
        and str(membership.get("status") or "").strip().lower() == "approved"
        and int(membership.get("is_canceled") or 0) == 0
    )
    if not approved:
        return _error("forbidden", 403, "承認済みの参加者のみ参加証を利用できます。")

    fee_yen = int(event.get("fee_yen") or 0)
    require_payment = bool(
        int(membership.get("require_payment") if membership.get("require_payment") is not None else 1)
    )
    payment_status = str(membership.get("payment_status") or "unpaid")
    if not require_payment or fee_yen <= 0:
        payment_key = "free"
        payment_label = "支払不要"
    elif payment_status == "paid":
        payment_key = "paid"
        payment_label = "支払済み"
    else:
        payment_key = "unpaid"
        payment_label = "未支払"

    checked_in = bool(membership.get("checkin_at"))
    user = actor["external"]
    return _ok(
        participantPass={
            "event": {
                "uuid": event_uuid,
                "title": str(event.get("title") or ""),
                "startsAt": _value(event.get("starts_at")),
                "placeName": event.get("place_name"),
            },
            "participant": {
                "id": int(user.get("id") or 0),
                "nickname": str(user.get("nickname") or ""),
                "avatarUrl": avatar_url_for(user),
            },
            "payment": {
                "status": payment_status,
                "key": payment_key,
                "label": payment_label,
                "amountYen": membership.get("paid_amount_yen"),
                "paidAt": _value(membership.get("paid_at")),
                "receiptUrl": membership.get("receipt_url"),
            },
            "checkin": {
                "checkedIn": checked_in,
                "at": _value(membership.get("checkin_at")),
                "method": "venue_qr" if checked_in else None,
                "methodLabel": "会場掲示QRコード" if checked_in else None,
            },
        }
    )


@bp.get("/api/vue/events/<event_uuid>/members")
def user_api_event_members(event_uuid: str):
    actor = _actor()
    if not actor:
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    allowed, membership, role = _event_access(event, actor)
    permissions = _event_permissions(event, membership, role) if allowed else {}
    if not allowed or not permissions.get("canViewMembers"):
        return _error("forbidden", 403)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT u.id, u.nickname, u.x_id, u.instagram_id, u.avatar_file,
                   u.avatar_url, u.updated_at, COALESCE(u.is_deleted,0) AS is_deleted,
                   m.participant_role, m.costume_label, m.is_host, m.is_subhost,
                   m.checkin_at
              FROM mfu_event_member AS m
              JOIN external_login_user AS u ON u.id=m.user_id
             WHERE m.event_id=%s AND m.status='approved'
               AND COALESCE(m.is_canceled,0)=0
             ORDER BY m.is_host DESC, m.is_subhost DESC, u.nickname ASC
            """,
            (int(event["id"]),),
        )
        rows = cur.fetchall() or []
    finally:
        cur.close()
        db.close()
    members = []
    for row in rows:
        if is_withdrawn_ext_user(row):
            continue
        members.append(
            {
                "id": int(row.get("id") or 0),
                "nickname": str(row.get("nickname") or ""),
                "xId": row.get("x_id"),
                "instagramId": row.get("instagram_id"),
                "avatarUrl": avatar_url_for(row),
                "participantRole": str(row.get("participant_role") or "none"),
                "costumeLabel": str(row.get("costume_label") or ""),
                "isHost": bool(row.get("is_host")),
                "isSubhost": bool(row.get("is_subhost")),
                "checkinAt": _value(row.get("checkin_at")),
            }
        )
    return _ok(members=members)


@bp.patch("/api/vue/events/<event_uuid>/my-role")
def user_api_update_my_role(event_uuid: str):
    actor = _actor()
    if not actor or actor["kind"] != "external":
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    membership = _latest_membership(int(event["id"]), int(actor["external"]["id"]))
    if not membership or int(membership.get("is_canceled") or 0) == 1:
        return _error("forbidden", 403)
    payload = request.get_json(silent=True) or {}
    role = str(payload.get("participantRole") or "none").strip().lower()
    if role not in {"none", "camera", "assistant", "cosplayer", "other"}:
        return _error("invalid_participant_role", 400)
    costume = str(payload.get("costumeLabel") or "").strip() or None
    if role not in {"cosplayer", "other"}:
        costume = None
    if costume and len(costume) > 120:
        return _error("costume_label_too_long", 400)
    from .users import _normalize_role_for_db

    db = get_db()
    cur = db.cursor()
    try:
        saved_role, degraded = _normalize_role_for_db(db, role)
        cur.execute(
            """
            UPDATE mfu_event_member
               SET participant_role=%s, costume_label=%s
             WHERE id=%s AND event_id=%s AND user_id=%s
             LIMIT 1
            """,
            (
                saved_role,
                costume,
                int(membership["id"]),
                int(event["id"]),
                int(actor["external"]["id"]),
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()
    current_app.logger.info(
        "EVENT_USER_ROLE_UPDATE event_id=%s ext_user_id=%s role=%s",
        int(event["id"]),
        int(actor["external"]["id"]),
        saved_role,
    )
    return _ok(participantRole=saved_role, costumeLabel=costume, degraded=bool(degraded))


@bp.post("/api/vue/logout")
def user_api_logout():
    try:
        from app.albums.routes import clear_event_album_auth

        clear_event_album_auth()
    except Exception:
        current_app.logger.warning("Vue event API album auth cleanup failed", exc_info=True)
    for key in (
        "ext_user_id",
        "ext_user_social_id",
        "ext_user_nickname",
        "ext_after_login_next",
        "ext_user_onboarding",
        "ext_user_need_email",
        "ext_user_email_unverified",
        "ext_login_mode",
        "ext_pwa_client_id",
    ):
        session.pop(key, None)
    return _ok(loggedOut=True)
