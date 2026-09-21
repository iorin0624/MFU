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
from functools import wraps
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

inpa_admin_bp = Blueprint("inpa_admin", __name__)


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
    "visits": ("/internal/admin/v1/visits", "visits"),
    "reports": ("/internal/admin/v1/reports", "reports"),
    "seasons": ("/internal/admin/v1/seasons", "seasons"),
    "security": ("/internal/admin/v1/security-events", "events"),
    "mail": ("/internal/admin/v1/mail-logs", "logs"),
    "audit": ("/internal/admin/v1/audit-logs", "logs"),
}


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
    query = {name: value for name in ("q", "status", "season_id", "user_id", "page") if (value := request.args.get(name))}
    try:
        if section == "seasons":
            seasons = call_inpa("GET", "/internal/admin/v1/seasons").get("seasons", [])
            restrictions = call_inpa("GET", "/internal/admin/v1/restriction-periods").get("restrictions", [])
            data = {"seasons": seasons, "restrictions": restrictions}
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
        else:
            data = []
        error = str(exc)
    return render_template("admin_inpa.html", section=section, data=data, error=error)


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
