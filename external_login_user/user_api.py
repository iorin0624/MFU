"""Mobile-first Vue API for the external event participant portal.

The server-rendered pages stay available during migration.  These endpoints
only expose fields already shown to the current participant and re-check event
membership/ACL on every request.
"""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import date, datetime
from typing import Any

from flask import current_app, jsonify, render_template, request, session, url_for

from app.utils.db import get_db

from . import bp
from .utils import (
    _event_acl_role,
    _agree_current_privacy_policy,
    _event_by_uuid_str,
    _get_current_commerce_law_config,
    _get_current_participant_terms_config,
    _get_current_privacy_policy_config,
    _is_privacy_policy_effective,
    _get_ext_user_by_social,
    _needs_privacy_policy_agreement,
    _uuid_bytes_to_str,
    avatar_url_for,
    is_withdrawn_ext_user,
    normalize_event_theme_color,
)


API_PAGE_SIZE_MAX = 200


def _profile_incomplete(user: dict[str, Any] | None, *, force: bool = False) -> bool:
    if force:
        return True
    nickname = str((user or {}).get("nickname") or "").strip()
    return not nickname or nickname == "（未設定）"


def _render_user_vue_portal(*, base_endpoint: str):
    return render_template(
        "external_login_vue.html",
        event_vue_config={
            "basePath": url_for(base_endpoint),
            "bootstrapUrl": url_for("external_login_user.user_api_bootstrap"),
            "eventsUrl": url_for("external_login_user.user_api_events"),
            "albumApiBase": "/album/api",
            "loginUrl": url_for("external_login_user.index"),
        },
    )


@bp.get("/app", defaults={"vue_path": ""})
@bp.get("/app/", defaults={"vue_path": ""})
@bp.get("/app/<path:vue_path>")
def user_vue_portal(vue_path: str):
    """Serve the production participant Vue portal."""
    session["ext_portal_ui"] = "vue"
    return _render_user_vue_portal(base_endpoint="external_login_user.user_vue_portal")


@bp.get("/vue-preview", defaults={"vue_path": ""})
@bp.get("/vue-preview/", defaults={"vue_path": ""})
@bp.get("/vue-preview/<path:vue_path>")
def user_vue_preview(vue_path: str):
    """Backward-compatible preview URL kept for existing bookmarks."""
    return _render_user_vue_portal(base_endpoint="external_login_user.user_vue_preview")


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
    # The participant-facing Vue portal intentionally accepts only an
    # external-user session (LINE or the email-PIN login that resolves to the
    # same external_login_user row).  An MFU administrator may be signed in in
    # the same browser, but that session must never become the portal actor
    # after the participant logs out.
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
    vue_base = url_for("external_login_user.user_vue_portal").rstrip("/")
    return [
        {"id": "home", "label": "ホーム", "url": f"{vue_base}/"},
        {"id": "events", "label": "イベント", "url": f"{vue_base}/"},
        {"id": "chat", "label": "チャット", "url": f"{vue_base}/chat", "badge": "chat"},
        {
            "id": "notifications",
            "label": "通知",
            "url": f"{vue_base}/notifications",
            "badge": "notifications",
        },
        {"id": "account", "label": "アカウント", "url": f"{vue_base}/profile"},
    ]


def _notification_unread_counts(external_user_id: int | None) -> dict[str, int]:
    if not external_user_id:
        return {"total": 0, "notifications": 0, "chat": 0}
    try:
        from .notifications import (
            _compute_unread_counts_external,
            _compute_unread_counts_mfu,
            _get_chat_admin_alias_ext_user_row,
        )

        if _get_chat_admin_alias_ext_user_row(int(external_user_id)):
            return _compute_unread_counts_mfu("admin")
        return _compute_unread_counts_external(int(external_user_id))
    except Exception:
        current_app.logger.warning("Vue event API unread count failed", exc_info=True)
        return {"total": 0, "notifications": 0, "chat": 0}


def _session_payload() -> dict[str, Any]:
    actor = _actor()
    external = (actor or {}).get("external")
    chat_admin_alias = False
    if external:
        try:
            from .notifications import _get_chat_admin_alias_ext_user_row

            chat_admin_alias = bool(
                _get_chat_admin_alias_ext_user_row(int(external.get("id") or 0))
            )
        except Exception:
            current_app.logger.warning("Vue event API admin alias lookup failed", exc_info=True)
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
    privacy_config = _get_current_privacy_policy_config()
    commerce_config = _get_current_commerce_law_config()
    participant_terms_config = _get_current_participant_terms_config()
    return {
        "authenticated": bool(actor),
        "actorKind": (actor or {}).get("kind"),
        "profile": _profile(external) if external else None,
        "chatAdminAlias": chat_admin_alias,
        "notificationScope": "mfu" if chat_admin_alias else "external",
        "csrfToken": _csrf_token(),
        "navigation": _navigation(),
        "prerequisites": {
            "profileCompletionRequired": bool(
                external and _profile_incomplete(external, force=bool(session.get("ext_user_onboarding")))
            ),
            "emailVerificationRequired": bool(
                external and external.get("email") and not external.get("email_verified_at")
            ),
            "privacyAgreementRequired": privacy_required,
        },
        "unread": _notification_unread_counts(
            int(external.get("id") or 0) if external else None
        ),
        "documents": {
            "privacyPolicyUrl": str(privacy_config.get("privacy_policy_url") or ""),
            "commerceLawUrl": str(commerce_config.get("commerce_law_url") or ""),
            "participantTermsUrl": str(participant_terms_config.get("participant_terms_url") or ""),
        },
    }


@bp.post("/api/vue/privacy-policy/agree")
def user_api_privacy_policy_agree():
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    supplied = str(request.headers.get("X-CSRF-Token") or "")
    expected = _csrf_token()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return _error("csrf_failed", 403, "画面の有効期限が切れました。再読み込みしてください。")
    config = _get_current_privacy_policy_config()
    if _is_privacy_policy_effective(config) and not _agree_current_privacy_policy(
        int(actor["external"]["id"]), source="vue"
    ):
        return _error("save_failed", 500, "同意内容を保存できませんでした。")
    session.pop("ext_after_privacy_policy_next", None)
    return _ok(agreed=True)


@bp.get("/api/vue/events/<event_uuid>/payment-options")
def user_api_payment_options(event_uuid: str):
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    allowed, membership, role = _event_access(event, actor)
    if not allowed or not membership:
        return _error("forbidden", 403)
    from .payments import _enabled_methods, _fetch_event_banks_active, _paypay_p2p_url, _resolve_member_fee
    methods = _enabled_methods(event)
    bank_rows = _fetch_event_banks_active(int(event["id"])) if methods.get("bank") else []
    banks = [
        {
            "id": row[0], "label": row[1], "bankName": row[2], "branchName": row[3],
            "accountKind": row[4], "accountNumber": row[5], "accountHolder": row[6], "memo": row[7],
        }
        for row in bank_rows
    ]
    return _ok(payment={
        "methods": methods,
        "feeYen": _resolve_member_fee(int(event["id"]), int(actor["external"]["id"]), int(event.get("fee_yen") or 0)),
        "paypayUrl": _paypay_p2p_url(event) if methods.get("paypay") else None,
        "paypayDisplay": str(event.get("paypay_display") or ""),
        "banks": banks,
        "squareUrl": url_for("external_login_user.pay_start", event_uuid=event_uuid, force="1", portal="vue"),
    })


@bp.post("/api/vue/events/<event_uuid>/payment-paypay")
def user_api_payment_paypay(event_uuid: str):
    from .payments import pay_paypay
    if not str(request.form.get("remitter_name") or "").strip():
        return _error("validation_error", 400, "送金名を入力してください。")
    response = pay_paypay(event_uuid)
    status = getattr(response, "status_code", 200)
    if 300 <= status < 400:
        return _ok(submitted=True)
    return _error("submit_failed", status if status >= 400 else 400, "送金申告を保存できませんでした。")


@bp.post("/api/vue/events/<event_uuid>/payment-bank")
def user_api_payment_bank(event_uuid: str):
    from .payments import pay_bank
    for field, message in (("bank_id", "振込先を選択してください。"), ("remitter_name", "振込元名を入力してください。"), ("deposit_date", "着金日を入力してください。")):
        if not str(request.form.get(field) or "").strip():
            return _error("validation_error", 400, message)
    response = pay_bank(event_uuid)
    status = getattr(response, "status_code", 200)
    if 300 <= status < 400:
        return _ok(submitted=True)
    return _error("submit_failed", status if status >= 400 else 400, "振込申告を保存できませんでした。")


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
        "canRequestParticipantsPngEmail": bool(
            role == "member"
            and active
            and membership
            and (bool(membership.get("is_host")) or bool(membership.get("is_subhost")))
        ),
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
        "checkinMethod": "venue_qr" if row.get("checkin_at") else None,
        "checkinMethodLabel": "会場掲示QRコード" if row.get("checkin_at") else None,
        "receiptUrl": row.get("receipt_url"),
    }


def _event_payload(event: dict[str, Any], membership: dict[str, Any] | None, role: str | None) -> dict[str, Any]:
    event_uuid = str(event.get("event_uuid_str") or _uuid_bytes_to_str(event.get("event_uuid")) or "")
    album_id = str(event.get("album_id") or "") or None
    permissions = _event_permissions(event, membership, role)
    custom_fee = membership.get("custom_fee_yen") if membership else None
    effective_fee = custom_fee if custom_fee is not None else event.get("fee_yen")
    receipt_pdf_url = None
    if (
        membership
        and str(membership.get("payment_status") or "") == "paid"
        and membership.get("id")
        and (
            int(membership.get("bank_transfer") or 0) == 1
            or int(membership.get("paypay_transfer") or 0) == 1
            or bool(str(membership.get("receipt_url") or "").strip())
        )
    ):
        receipt_pdf_url = url_for(
            "external_login_user.member_receipt_pdf",
            event_uuid=event_uuid,
            member_id=int(membership["id"]),
        )
    active_features = bool(permissions.get("canViewMembers"))
    return {
        "id": int(event.get("id") or 0),
        "uuid": event_uuid,
        "title": str(event.get("title") or ""),
        "themeColor": normalize_event_theme_color(event.get("theme_color")),
        "startsAt": _value(event.get("starts_at")),
        "placeName": event.get("place_name"),
        "address": event.get("address"),
        "mapsUrl": event.get("maps_url"),
        "snsHashtag": event.get("sns_hashtag"),
        "participantMemo": event.get("memo_all") if active_features else None,
        "googleFormUrl": event.get("google_form_url"),
        "lineOpenchatUrl": event.get("line_openchat_url") if active_features else None,
        "lineOpenchatPass": event.get("line_openchat_pass") if active_features else None,
        "feeYen": effective_fee,
        "tipEnabled": bool(event.get("tip_enabled")),
        "payFrom": _value(event.get("pay_from")),
        "payUntil": _value(event.get("pay_until")),
        "albumId": album_id,
        "membership": _membership_payload(membership),
        "accessRole": role,
        "permissions": permissions,
        "urls": {
            "detail": url_for("external_login_user.view_event", event_uuid=event_uuid),
            "chat": (
                None
                if active_features and str(event.get("line_openchat_url") or "").strip()
                else url_for("external_login_user.user_vue_portal", vue_path=f"events/{event_uuid}/chat")
            ),
            "album": (
                url_for("external_login_user.event_album_direct", event_uuid=event_uuid)
                if album_id
                else None
            ),
            "members": url_for("external_login_user.member_list", event_uuid=event_uuid),
            "social": url_for("external_login_user.user_vue_portal", vue_path=f"events/{event_uuid}/social"),
            # Vueのイベント一覧から直接支払う場合も、Square完了後はVue詳細へ戻す。
            "payment": url_for("external_login_user.pay_start", event_uuid=event_uuid, portal="vue"),
            "receipt": receipt_pdf_url,
            "tip": url_for("external_login_user.tip_start"),
            "participantsEmail": url_for("external_login_user.user_api_participants_email", event_uuid=event_uuid),
            "pass": (
                url_for("external_login_user.user_vue_portal", vue_path=f"events/{event_uuid}/pass")
                if permissions.get("canOpenPass")
                else None
            ),
            "admin": (
                url_for("external_login_user.admin_event_view", event_id=int(event.get("id") or 0))
                if permissions.get("canManageEvent")
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
                "themeColor": normalize_event_theme_color(event.get("theme_color")),
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


@bp.post("/api/vue/events/<event_uuid>/participants-email")
def user_api_participants_email(event_uuid: str):
    """Queue the existing participant PNG mail job for an authorized host."""
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    user = actor["external"]
    membership = _latest_membership(int(event.get("id") or 0), int(user.get("id") or 0))
    from .users import _can_send_participants_png_mail, _start_participants_png_email_job

    if not _can_send_participants_png_mail(membership):
        return _error("forbidden", 403, "主催者または副主催者のみ利用できます。")
    if not str(user.get("email") or "").strip() or not user.get("email_verified_at"):
        return _error("verified_email_required", 400, "確認済みメールアドレスが必要です。")
    _start_participants_png_email_job(event_uuid=event_uuid, ext_user_id=int(user["id"]))
    return _ok(accepted=True, message="参加者一覧PNGを確認済みメールアドレスへ送信します。")


@bp.route("/api/vue/events/<event_uuid>/my-role", methods=["PATCH", "POST"])
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


@bp.route("/api/vue/events/<event_uuid>/my-process", methods=["PATCH", "POST"])
def user_api_update_my_process(event_uuid: str):
    """Allow an active external participant to update their own processing preference."""
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    membership = _latest_membership(int(event["id"]), int(actor["external"]["id"]))
    if not membership or int(membership.get("is_canceled") or 0) == 1:
        return _error("forbidden", 403, "キャンセル済みの参加では変更できません。")

    payload = request.get_json(silent=True) or {}
    process_flag = 1 if bool(payload.get("process")) else 0
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_event_member
               SET process=%s
             WHERE id=%s AND event_id=%s AND user_id=%s
             LIMIT 1
            """,
            (
                process_flag,
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
        "EVENT_USER_PROCESS_UPDATE event_id=%s ext_user_id=%s process=%s",
        int(event["id"]),
        int(actor["external"]["id"]),
        process_flag,
    )
    return _ok(process=bool(process_flag))


@bp.get("/api/vue/events/<event_uuid>/join")
def user_api_join_info(event_uuid: str):
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    event = _event_by_uuid_str(event_uuid)
    if not event:
        return _error("event_not_found", 404)
    user = actor["external"]
    membership = _latest_membership(int(event.get("id") or 0), int(user.get("id") or 0))
    status = str((membership or {}).get("status") or "") or None
    if membership and int(membership.get("is_canceled") or 0) == 1:
        status = "canceled"
    terms = _get_current_participant_terms_config()
    effective = bool(terms.get("participant_terms_url") and terms.get("participant_terms_revised_date"))
    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)
    iv = str(request.args.get("iv") or request.args.get("vi") or "").strip()
    submit_url = url_for("external_login_user.join_event", event_uuid=event_uuid)
    if iv:
        from urllib.parse import quote
        submit_url = f"{submit_url}?iv={quote(iv, safe='')}"
    return _ok(join={
        "event": {
            "uuid": event_uuid,
            "title": str(event.get("title") or ""),
            "themeColor": normalize_event_theme_color(event.get("theme_color")),
            "startsAt": _value(event.get("starts_at")),
            "feeYen": event.get("fee_yen"),
            "placeName": event.get("place_name"),
            "address": event.get("address"),
        },
        "status": status,
        "participantRole": str((membership or {}).get("participant_role") or "cosplayer"),
        "costumeLabel": str((membership or {}).get("costume_label") or ""),
        "process": bool((membership or {}).get("process")),
        "termsRequired": effective,
        "termsUrl": str(terms.get("participant_terms_url") or ""),
        "csrfToken": str(session["ext_csrf"]),
        "submitUrl": submit_url,
    })


@bp.get("/api/vue/profile")
def user_api_profile():
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    user_id = int(actor["external"].get("id") or 0)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, nickname, x_id, instagram_id, email, email_verified_at,
                   avatar_file, avatar_url, updated_at,
                   COALESCE(payment_mode,'manual') AS payment_mode,
                   COALESCE(notify_album_upload,1) AS notify_album_upload,
                   COALESCE(notify_album_process,1) AS notify_album_process
              FROM external_login_user WHERE id=%s LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        cur.execute(
            """
            SELECT card_brand, last4, exp_month, exp_year
              FROM external_login_user_card_data
             WHERE user_id=%s AND deleted_at IS NULL
             ORDER BY is_default DESC, id DESC LIMIT 1
            """,
            (user_id,),
        )
        card = cur.fetchone()
    finally:
        cur.close()
        db.close()
    if not row:
        return _error("profile_not_found", 404)
    return _ok(profile={
        **_profile(row),
        "paymentMode": str(row.get("payment_mode") or "manual"),
        "notifyAlbumUpload": bool(row.get("notify_album_upload")),
        "notifyAlbumProcess": bool(row.get("notify_album_process")),
        "hasCard": bool(card),
        "cardSummary": (
            f"{str(card.get('card_brand') or '').upper()} ****{card.get('last4') or '****'}"
            if card else None
        ),
    })


@bp.route("/api/vue/profile", methods=["PATCH", "POST"])
def user_api_update_profile():
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    user = actor["external"]
    payload = request.form if request.form else (request.get_json(silent=True) or {})
    nickname = str(payload.get("nickname") or "").strip()
    x_id = str(payload.get("xId") or payload.get("x_id") or "").strip().lstrip("@") or None
    instagram_id = str(payload.get("instagramId") or payload.get("instagram_id") or "").strip().lstrip("@") or None
    email = str(payload.get("email") or "").strip().lower() or None
    payment_mode = str(payload.get("paymentMode") or payload.get("payment_mode") or "manual").strip().lower()
    notify_upload = str(payload.get("notifyAlbumUpload") or payload.get("notify_album_upload") or "0").lower() in {"1", "true", "on", "yes"}
    notify_process = str(payload.get("notifyAlbumProcess") or payload.get("notify_album_process") or "0").lower() in {"1", "true", "on", "yes"}
    errors: dict[str, str] = {}
    if not nickname:
        errors["nickname"] = "ニックネームは必須です。"
    if x_id and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", x_id):
        errors["xId"] = "X IDは半角英数と_で1〜15文字で入力してください。"
    if instagram_id and not (
        re.fullmatch(r"[A-Za-z0-9._]{1,30}", instagram_id)
        and not instagram_id.startswith(".") and not instagram_id.endswith(".") and ".." not in instagram_id
    ):
        errors["instagramId"] = "Instagram IDの形式が正しくありません。"
    if not email or "@" not in email or len(email) > 255:
        errors["email"] = "正しいメールアドレスを入力してください。"
    if payment_mode not in {"manual", "auto"}:
        errors["paymentMode"] = "決済方法が正しくありません。"
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT email, email_verified_at FROM external_login_user WHERE id=%s LIMIT 1", (int(user["id"]),))
        before = cur.fetchone() or {}
        cur.execute("SELECT 1 AS ok FROM external_login_user_card_data WHERE user_id=%s AND deleted_at IS NULL LIMIT 1", (int(user["id"]),))
        has_card = bool(cur.fetchone())
        if payment_mode == "auto" and not has_card:
            errors["paymentMode"] = "自動決済にはカード登録が必要です。"
        if email:
            cur.execute(
                """SELECT id FROM external_login_user
                     WHERE LOWER(TRIM(email))=%s AND id<>%s LIMIT 1""",
                (email, int(user["id"])),
            )
            if cur.fetchone():
                errors["email"] = "このメールアドレスは既に登録されています。"
        if errors:
            return _error("validation_error", 400, "入力内容を確認してください。", errors=errors)
        email_changed = str(before.get("email") or "").lower() != str(email or "").lower()
        cur.execute(
            """
            UPDATE external_login_user
               SET nickname=%s, x_id=%s, instagram_id=%s, email=%s,
                   email_verified_at=IF(%s, NULL, email_verified_at),
                   payment_mode=%s, notify_album_upload=%s, notify_album_process=%s,
                   updated_at=NOW()
             WHERE id=%s LIMIT 1
            """,
            (nickname, x_id, instagram_id, email, 1 if email_changed else 0,
             payment_mode, int(notify_upload), int(notify_process), int(user["id"])),
        )
        avatar = request.files.get("avatar")
        if avatar and getattr(avatar, "filename", ""):
            from .users import _save_avatar
            saved = _save_avatar(avatar)
            if not saved:
                db.rollback()
                return _error("invalid_avatar", 400, "画像形式はPNG・JPEG・WEBP・GIFのみ対応しています。")
            cur.execute("UPDATE external_login_user SET avatar_file=%s, updated_at=NOW() WHERE id=%s", (saved, int(user["id"])))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()
    session["ext_user_nickname"] = nickname
    session.pop("ext_user_onboarding", None)
    verification_sent = False
    if email_changed and email:
        try:
            from .users import _issue_verify_pin, _send_verify_pin_mail
            ok, _reason, pin = _issue_verify_pin(int(user["id"]), email)
            if ok and pin:
                _send_verify_pin_mail(email, pin)
                verification_sent = True
        except Exception:
            current_app.logger.exception("Vue profile verification mail failed")
    return _ok(saved=True, emailVerificationRequired=bool(email_changed), verificationSent=verification_sent)


@bp.post("/api/vue/email-verification/send")
def user_api_send_email_verification():
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    user = actor["external"]
    email = str(user.get("email") or "").strip()
    if not email:
        return _error("email_required", 400, "プロフィールにメールアドレスを登録してください。")
    from .users import _issue_verify_pin, _send_verify_pin_mail
    ok, reason, pin = _issue_verify_pin(int(user["id"]), email)
    if not ok or not pin:
        return _error(reason or "send_failed", 429 if reason in {"cooldown", "rate_limited"} else 500)
    _send_verify_pin_mail(email, pin)
    return _ok(sent=True, email=email)


@bp.post("/api/vue/email-verification/verify")
def user_api_verify_email():
    actor = _actor()
    if not actor or actor.get("kind") != "external":
        return _error("unauthorized", 401)
    user = actor["external"]
    email = str(user.get("email") or "").strip()
    pin = str((request.get_json(silent=True) or {}).get("pin") or "").strip()
    from .users import _consume_verify_pin
    ok, reason = _consume_verify_pin(int(user["id"]), email, pin)
    if not ok:
        return _error(reason or "invalid_pin", 400, "PINコードが一致しないか、有効期限が切れています。")
    return _ok(verified=True, nextUrl=session.pop("ext_after_verify_next", None))


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
