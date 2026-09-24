"""Thin MFU administration bridge for INPA's Unix-socket API."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import time
from datetime import UTC, date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

inpa_admin_bp = Blueprint("inpa_admin", __name__)
_JST = timezone(timedelta(hours=9), "JST")


@inpa_admin_bp.app_template_filter("inpa_jst")
def inpa_jst(value):
    """Render API UTC timestamps consistently in JST; leave other values unchanged."""
    if value in (None, ""):
        return ""
    raw = str(value)
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return date.fromisoformat(raw).strftime("%Y年%m月%d日")
        except ValueError:
            return raw
    if len(raw) < 19 or raw[4] != "-" or raw[7] != "-" or raw[10] not in {"T", " "}:
        return value
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(_JST).strftime("%Y年%m月%d日 %H:%M:%S")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def _admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user") != "admin":
            return "管理者のみアクセス可能", 403
        return view(*args, **kwargs)
    return wrapped


def _canonical(method: str, path: str, timestamp: str, nonce: str, body: bytes, admin: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{nonce}\n{digest}\n{admin}".encode()


def _valid_inpa_notification_signature() -> bool:
    timestamp = request.headers.get("X-INPA-Timestamp", "")
    nonce = request.headers.get("X-INPA-Nonce", "")
    sender = request.headers.get("X-INPA-Admin", "")
    signature = request.headers.get("X-INPA-Signature", "")
    try:
        fresh = abs(int(time.time()) - int(timestamp)) <= 60
    except ValueError:
        return False
    if not fresh or sender != "inpa-feedback" or not 16 <= len(nonce) <= 128 or len(signature) != 64:
        return False
    secret = os.environ.get("INPA_INTERNAL_ADMIN_HMAC_SECRET", "")
    if not secret:
        return False
    expected = hmac.new(
        secret.encode(),
        _canonical(request.method, request.path, timestamp, nonce, request.get_data(cache=True), sender),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@inpa_admin_bp.post("/api/internal/inpa/feedback-notification")
def receive_feedback_notification():
    if not _valid_inpa_notification_signature():
        return {"error": "authentication_failed"}, 401
    data = request.get_json(silent=True)
    required = {
        "event_id", "sender_public_id", "sender_connection_id", "sender_display_name",
        "sender_email", "category", "message", "created_at",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        return {"error": "invalid_payload"}, 400
    category_labels = {
        "bug": "不具合・エラー", "feature": "機能の要望",
        "usability": "操作性・使いやすさ", "wording": "文言・表示", "other": "その他",
    }
    category = category_labels.get(str(data["category"]))
    message = str(data["message"]).strip()
    if not category or not 1 <= len(message) <= 2000:
        return {"error": "invalid_payload"}, 400
    connection_id = str(data["sender_connection_id"])
    connection_display = (
        f"{connection_id[:4]}-{connection_id[4:]}" if len(connection_id) == 8 else connection_id
    )
    payload = {
        "embeds": [{
            "title": "📣 INPAフィードバック",
            "url": "https://mfu.iori0624.jp/admin/inpa/feedback",
            "description": message,
            "color": 0xE26D3F,
            "fields": [
                {"name": "項目", "value": category, "inline": True},
                {"name": "表示名", "value": str(data["sender_display_name"])[:40] or "—", "inline": True},
                {"name": "つながりID", "value": connection_display[:16] or "—", "inline": True},
                {"name": "メールアドレス", "value": str(data["sender_email"])[:254] or "—", "inline": False},
                {"name": "送信日時", "value": str(inpa_jst(data["created_at"])), "inline": True},
                {"name": "送信元", "value": str(data.get("source_path") or "—")[:500], "inline": True},
            ],
            "footer": {"text": f"INPA feedback {str(data['event_id'])[:26]}"},
        }],
        "allowed_mentions": {"parse": []},
    }
    try:
        from app.discord_notifications.service import post_discord_notification

        delivered = post_discord_notification("inpa_feedback", payload)
    except Exception:  # noqa: BLE001 - notification integrations have heterogeneous failures.
        return {"error": "delivery_failed"}, 502
    return {"delivered": delivered}, 200


def call_inpa(method: str, path: str, *, payload: object = None, idempotency_key: str | None = None):
    secret = os.environ.get("INPA_INTERNAL_ADMIN_HMAC_SECRET", "")
    if not secret:
        raise RuntimeError("INPA admin bridge secret is not configured")
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    parsed = urlsplit(path); timestamp = str(int(time.time())); nonce = secrets.token_urlsafe(24)
    admin = str(session.get("user") or "admin")[:128]
    signature = hmac.new(secret.encode(), _canonical(method.upper(), parsed.path, timestamp, nonce, body, admin), hashlib.sha256).hexdigest()
    headers = {
        "X-INPA-Timestamp": timestamp, "X-INPA-Nonce": nonce,
        "X-INPA-Admin": admin, "X-INPA-Signature": signature,
    }
    if body:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    connection = _UnixConnection(os.environ.get("INPA_INTERNAL_ADMIN_SOCKET", "/run/inpa/admin.sock"))
    try:
        connection.request(method.upper(), path, body=body, headers=headers)
        response = connection.getresponse(); raw = response.read()
    finally:
        connection.close()
    data = json.loads(raw) if raw else {}
    if response.status >= 400:
        message = data.get("error", {}).get("message", f"INPA API error ({response.status})")
        raise RuntimeError(message)
    return data


_SECTIONS = {
    "dashboard": ("/internal/admin/v1/summary", "summary"),
    "users": ("/internal/admin/v1/users", "users"),
    "invitations": ("/internal/admin/v1/registration-invitations", "invitations"),
    "visits": ("/internal/admin/v1/visits", "visits"),
    "reports": ("/internal/admin/v1/reports", "reports"),
    "feedback": ("/internal/admin/v1/feedbacks", "feedbacks"),
    "updates": ("/internal/admin/v1/releases", "releases"),
    "seasons": ("/internal/admin/v1/seasons", "seasons"),
    "security": ("/internal/admin/v1/security-events", "events"),
    "mail": ("/internal/admin/v1/mail-logs", "logs"),
    "legal": ("/internal/admin/v1/legal-documents", "documents"),
    "audit": ("/internal/admin/v1/audit-logs", "logs"),
}


def _open_feedback_count(summary: dict | None = None) -> int:
    try:
        values = summary if isinstance(summary, dict) else call_inpa(
            "GET", "/internal/admin/v1/summary"
        ).get("summary", {})
        return max(0, int(values.get("open_feedback", 0)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return 0


@inpa_admin_bp.get("/admin/inpa")
@inpa_admin_bp.get("/admin/inpa/<section>")
@_admin_required
def index(section: str = "dashboard"):
    # The former standalone restriction page is kept as a compatibility URL,
    # but season and restriction management now share one screen.
    if section == "restrictions":
        return redirect(url_for("inpa_admin.index", section="seasons"))
    if section not in _SECTIONS:
        section = "dashboard"
    query = {name: value for name in ("q", "status", "category", "season_id", "user_id", "page") if (value := request.args.get(name))}
    try:
        if section == "seasons":
            seasons = call_inpa("GET", "/internal/admin/v1/seasons").get("seasons", [])
            restrictions = call_inpa("GET", "/internal/admin/v1/restriction-periods").get("restrictions", [])
            data = {"seasons": seasons, "restrictions": restrictions}
        elif section == "invitations":
            invitations = call_inpa(
                "GET", "/internal/admin/v1/registration-invitations"
            ).get("invitations", [])
            settings = call_inpa(
                "GET", "/internal/admin/v1/registration-settings"
            ).get("settings", {"invite_only": True})
            data = {"invitations": invitations, "settings": settings}
        elif section == "updates":
            result = call_inpa("GET", "/internal/admin/v1/releases")
            data = {
                "releases": result.get("releases", []),
                "current_version": result.get("current_version"),
                "suggestions": result.get("suggestions", {}),
            }
        else:
            path, key = _SECTIONS[section]
            if query:
                path = f"{path}?{urlencode(query)}"
            result = call_inpa("GET", path)
            data = result.get(key, result)
        error = None
    except (OSError, RuntimeError, ValueError) as exc:
        if section == "dashboard":
            data = {}
        elif section == "seasons":
            data = {"seasons": [], "restrictions": []}
        elif section == "invitations":
            data = {"invitations": [], "settings": {"invite_only": True}}
        elif section == "updates":
            data = {"releases": [], "current_version": None, "suggestions": {}}
        else:
            data = []
        error = str(exc)
    feedback_badge_count = _open_feedback_count(data if section == "dashboard" else None)
    return render_template(
        "admin_inpa.html", section=section, data=data, error=error, invite_result=None,
        feedback_badge_count=feedback_badge_count,
    )


@inpa_admin_bp.post("/admin/inpa/invitations")
@_admin_required
def create_invitation():
    try:
        days = int(request.form.get("expires_in_days", "7"))
        result = call_inpa(
            "POST", "/internal/admin/v1/registration-invitations",
            payload={"memo": request.form.get("memo", ""), "expires_in_days": days},
            idempotency_key=secrets.token_urlsafe(24),
        )
        invitations = call_inpa(
            "GET", "/internal/admin/v1/registration-invitations"
        ).get("invitations", [])
        settings = call_inpa(
            "GET", "/internal/admin/v1/registration-settings"
        ).get("settings", {"invite_only": True})
        return render_template(
            "admin_inpa.html", section="invitations",
            data={"invitations": invitations, "settings": settings}, error=None,
            invite_result=result, feedback_badge_count=_open_feedback_count(),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("inpa_admin.index", section="invitations"))


@inpa_admin_bp.post("/admin/inpa/invitations/<public_id>")
@_admin_required
def update_invitation(public_id: str):
    try:
        call_inpa(
            "PATCH", f"/internal/admin/v1/registration-invitations/{public_id}",
            payload={"memo": request.form.get("memo", "")},
            idempotency_key=secrets.token_urlsafe(24),
        )
        flash("招待トークンのメモを更新しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="invitations"))


@inpa_admin_bp.post("/admin/inpa/registration-settings")
@_admin_required
def update_registration_settings():
    try:
        invite_only = request.form.get("invite_only") == "1"
        call_inpa(
            "PATCH", "/internal/admin/v1/registration-settings",
            payload={"invite_only": invite_only}, idempotency_key=secrets.token_urlsafe(24),
        )
        if invite_only:
            flash("一般の新規登録を無効にし、招待トークンを必須にしました。", "success")
        else:
            flash("一般の新規登録を有効にしました。", "warning")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="invitations"))


@inpa_admin_bp.post("/admin/inpa/invitations/<public_id>/revoke")
@_admin_required
def revoke_invitation(public_id: str):
    try:
        call_inpa(
            "POST", f"/internal/admin/v1/registration-invitations/{public_id}/revoke",
            idempotency_key=secrets.token_urlsafe(24),
        )
        flash("招待トークンを無効化しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="invitations"))


@inpa_admin_bp.post("/admin/inpa/action")
@_admin_required
def action():
    action_name = request.form.get("action", "")
    target = request.form.get("target", "")
    routes = {
        "suspend": ("POST", f"/internal/admin/v1/users/{target}/suspend", None),
        "unsuspend": ("POST", f"/internal/admin/v1/users/{target}/unsuspend", None),
        "logout_all": ("POST", f"/internal/admin/v1/users/{target}/logout-all", None),
        "delete_visit": ("DELETE", f"/internal/admin/v1/visits/{target}", None),
        "revoke_share": ("POST", f"/internal/admin/v1/share-tokens/{target}/revoke", None),
        "retry_mail": ("POST", f"/internal/admin/v1/mail-logs/{target}/retry", None),
        "report_status": ("PATCH", f"/internal/admin/v1/reports/{target}", {"status": request.form.get("status")}),
    }
    if action_name not in routes:
        flash("不明な操作です。", "danger")
    else:
        method, path, payload = routes[action_name]
        try:
            call_inpa(method, path, payload=payload, idempotency_key=secrets.token_urlsafe(24))
            flash("INPA管理操作を実行しました。", "success")
        except (OSError, RuntimeError, ValueError) as exc:
            flash(str(exc), "danger")
    return redirect(request.referrer or url_for("inpa_admin.index"))


@inpa_admin_bp.post("/admin/inpa/feedbacks/<public_id>")
@_admin_required
def update_feedback(public_id: str):
    try:
        call_inpa(
            "PATCH", f"/internal/admin/v1/feedbacks/{public_id}",
            payload={
                "status": request.form.get("status", "new"),
                "admin_memo": request.form.get("admin_memo", ""),
            },
            idempotency_key=secrets.token_urlsafe(24),
        )
        flash("フィードバックの対応状況を更新しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="feedback"))


def _release_payload():
    return {
        "version": request.form.get("version", ""),
        "change_type": request.form.get("change_type", "feature"),
        "title": request.form.get("title", ""),
        "content_markdown": request.form.get("content_markdown", ""),
    }


@inpa_admin_bp.post("/admin/inpa/releases")
@_admin_required
def create_release():
    try:
        call_inpa("POST", "/internal/admin/v1/releases", payload=_release_payload(),
                  idempotency_key=secrets.token_urlsafe(24))
        flash("アップデート情報の下書きを作成しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="updates"))


@inpa_admin_bp.post("/admin/inpa/releases/<public_id>")
@_admin_required
def update_release(public_id: str):
    try:
        call_inpa("PATCH", f"/internal/admin/v1/releases/{public_id}", payload=_release_payload(),
                  idempotency_key=secrets.token_urlsafe(24))
        flash("アップデート情報の下書きを更新しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="updates"))


@inpa_admin_bp.post("/admin/inpa/releases/<public_id>/publish")
@_admin_required
def publish_release(public_id: str):
    try:
        call_inpa("POST", f"/internal/admin/v1/releases/{public_id}/publish",
                  idempotency_key=secrets.token_urlsafe(24))
        flash("アップデート情報を公開しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="updates"))


@inpa_admin_bp.post("/admin/inpa/seasons")
@_admin_required
def create_season():
    payload = {
        "name": request.form.get("name", ""), "slug": request.form.get("slug", ""),
        "start_date": request.form.get("start_date", ""), "end_date": request.form.get("end_date", ""),
        "is_active": request.form.get("is_active") == "1",
    }
    try:
        call_inpa("POST", "/internal/admin/v1/seasons", payload=payload, idempotency_key=secrets.token_urlsafe(24))
        flash("シーズンを作成しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="seasons"))


@inpa_admin_bp.post("/admin/inpa/seasons/<public_id>")
@_admin_required
def update_season(public_id: str):
    payload = {
        "name": request.form.get("name", ""), "slug": request.form.get("slug", ""),
        "start_date": request.form.get("start_date", ""), "end_date": request.form.get("end_date", ""),
        "is_active": request.form.get("is_active") == "1",
        "confirm_impacted": request.form.get("confirm_impacted") == "1",
        "expected_updated_at": request.form.get("expected_updated_at", ""),
    }
    try:
        result = call_inpa("PATCH", f"/internal/admin/v1/seasons/{public_id}", payload=payload,
                           idempotency_key=secrets.token_urlsafe(24))
        flash(f"シーズンを更新しました。影響予定: {result.get('impacted_count', 0)}件", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="seasons"))


@inpa_admin_bp.post("/admin/inpa/restrictions")
@_admin_required
def create_restriction():
    payload = {
        "season_public_id": request.form.get("season_public_id", ""),
        "name": request.form.get("name", ""),
        "restriction_type": request.form.get("restriction_type", "costume_prohibited"),
        "start_date": request.form.get("start_date", ""), "end_date": request.form.get("end_date", ""),
        "park_scope": request.form.get("park_scope", "all"),
        "description": request.form.get("description", ""), "is_active": True,
    }
    try:
        result = call_inpa("POST", "/internal/admin/v1/restriction-periods", payload=payload,
                           idempotency_key=secrets.token_urlsafe(24))
        flash(f"禁止期間を作成しました。該当予定: {result.get('impacted_count', 0)}件", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="seasons"))


@inpa_admin_bp.post("/admin/inpa/restrictions/<public_id>")
@_admin_required
def update_restriction(public_id: str):
    payload = {
        "season_public_id": request.form.get("season_public_id", ""),
        "name": request.form.get("name", ""),
        "restriction_type": request.form.get("restriction_type", "costume_prohibited"),
        "start_date": request.form.get("start_date", ""), "end_date": request.form.get("end_date", ""),
        "park_scope": request.form.get("park_scope", "all"),
        "description": request.form.get("description", ""),
        "is_active": request.form.get("is_active") == "1",
    }
    try:
        result = call_inpa("PATCH", f"/internal/admin/v1/restriction-periods/{public_id}", payload=payload,
                           idempotency_key=secrets.token_urlsafe(24))
        flash(f"禁止期間を更新しました。該当予定: {result.get('impacted_count', 0)}件", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="seasons"))


@inpa_admin_bp.post("/admin/inpa/restrictions/<public_id>/deactivate")
@_admin_required
def deactivate_restriction(public_id: str):
    try:
        call_inpa("DELETE", f"/internal/admin/v1/restriction-periods/{public_id}",
                  idempotency_key=secrets.token_urlsafe(24))
        flash("禁止期間を無効化しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="seasons"))


def _legal_payload():
    effective_date = request.form.get("effective_at", "")
    return {
        "document_type": request.form.get("document_type", ""),
        "version": request.form.get("version", ""),
        "title": request.form.get("title", ""),
        "content_markdown": request.form.get("content_markdown", ""),
        "requires_reconsent": request.form.get("requires_reconsent") == "1",
        "effective_at": f"{effective_date}T00:00:00" if effective_date else None,
    }


@inpa_admin_bp.post("/admin/inpa/legal-documents")
@_admin_required
def create_legal_document():
    try:
        call_inpa("POST", "/internal/admin/v1/legal-documents", payload=_legal_payload(),
                  idempotency_key=secrets.token_urlsafe(24))
        flash("法務文書の下書きを作成しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="legal"))


@inpa_admin_bp.post("/admin/inpa/legal-documents/<public_id>")
@_admin_required
def update_legal_document(public_id: str):
    try:
        call_inpa("PATCH", f"/internal/admin/v1/legal-documents/{public_id}",
                  payload=_legal_payload(), idempotency_key=secrets.token_urlsafe(24))
        flash("法務文書の下書きを更新しました。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="legal"))


@inpa_admin_bp.post("/admin/inpa/legal-documents/<public_id>/publish")
@_admin_required
def publish_legal_document(public_id: str):
    try:
        call_inpa("POST", f"/internal/admin/v1/legal-documents/{public_id}/publish",
                  idempotency_key=secrets.token_urlsafe(24))
        flash("法務文書を公開しました。公開済みの本文は変更できません。", "success")
    except (OSError, RuntimeError, ValueError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("inpa_admin.index", section="legal"))
