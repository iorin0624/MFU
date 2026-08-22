from __future__ import annotations

import base64
import hashlib
import io
import secrets
import time
from datetime import datetime, timedelta

import qrcode
from flask import Blueprint, g, jsonify, render_template, request, session, url_for

from app.utils.admin_auth import (
    ADMIN_USERNAME,
    audit,
    establish_admin_session,
)
from app.utils.db import get_db

admin_qr_auth_bp = Blueprint("admin_qr_auth", __name__, url_prefix="/auth/admin/qr")
QR_TTL = timedelta(minutes=2)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now()


def _qr_creation_rate_limited() -> bool:
    """Limit QR image generation without treating it as a failed login."""
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT COUNT(*) AS total
        FROM admin_qr_login_challenges
        WHERE desktop_ip=%s AND created_at >= %s
        """,
        ((request.remote_addr or "")[:64], _now() - timedelta(minutes=10)),
    )
    total = int((cur.fetchone() or {}).get("total") or 0)
    db.close()
    return total >= 30


@admin_qr_auth_bp.post("/create")
def create_challenge():
    if _qr_creation_rate_limited():
        audit("QR_RATE_LIMIT")
        return jsonify(ok=False, error="試行回数が多すぎます。しばらく待ってください。"), 429

    token = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)
    now = _now()
    db = get_db()
    cur = db.cursor()
    previous_id = session.get("admin_qr_challenge_id")
    if previous_id:
        cur.execute(
            "UPDATE admin_qr_login_challenges SET status='expired' WHERE id=%s AND status='pending'",
            (previous_id,),
        )
    cur.execute(
        "UPDATE admin_qr_login_challenges SET status='expired' WHERE status='pending' AND expires_at < %s",
        (now,),
    )
    cur.execute(
        """
        INSERT INTO admin_qr_login_challenges
          (token_hash, desktop_nonce_hash, username, status, created_at, expires_at,
           desktop_ip, desktop_user_agent)
        VALUES (%s,%s,%s,'pending',%s,%s,%s,%s)
        """,
        (
            _hash(token), _hash(nonce), ADMIN_USERNAME, now, now + QR_TTL,
            (request.remote_addr or "")[:64], (request.user_agent.string or "")[:255],
        ),
    )
    challenge_id = cur.lastrowid
    db.commit()
    db.close()
    session["admin_qr_challenge_id"] = challenge_id
    session["admin_qr_desktop_nonce"] = nonce
    # Keep the secret in the URL fragment. Fragments are never sent in HTTP
    # request lines, reverse-proxy logs, access logs, or Referer headers.
    approve_url = url_for("admin_qr_auth.approve_page", _external=True, _scheme="https") + "#" + token
    image = qrcode.make(approve_url)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    audit("QR_CREATED", details={"challenge_id": challenge_id, "expires_seconds": 120})
    return jsonify(ok=True, qr_image=data_uri, approve_url=approve_url, expires_in=120)


@admin_qr_auth_bp.get("/approve")
def approve_page():
    return render_template("admin_qr_approve.html")


@admin_qr_auth_bp.post("/details")
def details():
    if session.get("user") != ADMIN_USERNAME:
        return jsonify(ok=False, error="管理者としてログインしてください。"), 401
    token = str((request.get_json(silent=True) or {}).get("token") or "")
    if len(token) < 40:
        return jsonify(ok=False, error="QRコードが不正です。"), 400
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id,status,created_at,expires_at,desktop_ip,desktop_user_agent FROM admin_qr_login_challenges WHERE token_hash=%s",
        (_hash(token),),
    )
    row = cur.fetchone()
    db.close()
    if not row:
        return jsonify(ok=False, error="QRコードが不正です。"), 404
    if row["status"] != "pending" or row["expires_at"] < _now():
        return jsonify(ok=False, error="このQRコードは期限切れ、または使用済みです。"), 410
    return jsonify(
        ok=True, challenge_id=row["id"], desktop_ip=row.get("desktop_ip"),
        desktop_user_agent=row.get("desktop_user_agent"),
        created_at=str(row.get("created_at")), expires_at=str(row.get("expires_at")),
        passkey_required=True,
    )


@admin_qr_auth_bp.post("/decision")
def approve():
    if session.get("user") != ADMIN_USERNAME:
        return jsonify(ok=False, error="管理者としてログインしてください。"), 401
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "")
    action = str(payload.get("action") or "").strip()
    if action not in {"approve", "reject"}:
        return jsonify(ok=False, error="不正な操作です。"), 400
    token_hash = _hash(token)
    if action == "approve":
        verified_hash = str(session.get("admin_qr_passkey_verified_token_hash") or "")
        try:
            verified_until = int(session.get("admin_qr_passkey_verified_until") or 0)
        except (TypeError, ValueError):
            verified_until = 0
        if verified_hash != token_hash or verified_until < int(time.time()):
            audit("QR_APPROVAL_REJECTED", details={"reason": "passkey_required"})
            return jsonify(ok=False, error="承認にはスマートフォン側でのパスキー認証が必要です。"), 403
    now = _now()
    phone_sid = str(session.get("admin_sid") or "")
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id,status,expires_at FROM admin_qr_login_challenges WHERE token_hash=%s FOR UPDATE",
        (token_hash,),
    )
    row = cur.fetchone()
    if not row or row["status"] != "pending" or row["expires_at"] < now:
        if row and row["status"] == "pending":
            cur.execute("UPDATE admin_qr_login_challenges SET status='expired' WHERE id=%s", (row["id"],))
            db.commit()
        db.close()
        audit("QR_EXPIRED")
        return jsonify(ok=False, error="このQRコードは期限切れ、または使用済みです。"), 410
    new_status = "approved" if action == "approve" else "rejected"
    cur.execute(
        "UPDATE admin_qr_login_challenges SET status=%s, approved_at=%s, approved_by_sid_hash=%s WHERE id=%s AND status='pending'",
        (new_status, now, _hash(phone_sid), row["id"]),
    )
    db.commit()
    db.close()
    if action == "approve":
        session.pop("admin_qr_passkey_verified_token_hash", None)
        session.pop("admin_qr_passkey_verified_until", None)
    audit(f"QR_{new_status.upper()}", details={"challenge_id": row["id"]})
    return jsonify(ok=True, approved=(new_status == "approved"))


@admin_qr_auth_bp.post("/status")
def status():
    challenge_id = session.get("admin_qr_challenge_id")
    nonce = session.get("admin_qr_desktop_nonce")
    if not challenge_id or not nonce:
        return jsonify(ok=False, status="missing"), 400
    now = _now()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM admin_qr_login_challenges WHERE id=%s AND desktop_nonce_hash=%s FOR UPDATE",
        (challenge_id, _hash(str(nonce))),
    )
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify(ok=False, status="missing"), 404
    if row["status"] == "pending" and row["expires_at"] < now:
        cur.execute("UPDATE admin_qr_login_challenges SET status='expired' WHERE id=%s", (challenge_id,))
        db.commit()
        db.close()
        audit("QR_EXPIRED", details={"challenge_id": challenge_id})
        return jsonify(ok=True, status="expired")
    if row["status"] == "approved":
        cur.execute(
            "UPDATE admin_qr_login_challenges SET status='consumed', consumed_at=%s WHERE id=%s AND status='approved'",
            (now, challenge_id),
        )
        if cur.rowcount != 1:
            db.rollback()
            db.close()
            return jsonify(ok=False, status="consumed"), 409
        cur.execute("SELECT nickname FROM users WHERE username=%s", (ADMIN_USERNAME,))
        user = cur.fetchone() or {}
        db.commit()
        db.close()
        session.pop("admin_qr_challenge_id", None)
        session.pop("admin_qr_desktop_nonce", None)
        establish_admin_session(method="qr_passkey", nickname=user.get("nickname"))
        target = session.pop("post_login_next", None) or "/upload"
        audit("QR_CONSUMED", details={"challenge_id": challenge_id})
        return jsonify(ok=True, status="approved", redirect=target)
    db.close()
    if row["status"] == "pending":
        g.mfu_skip_access_log = True
    return jsonify(ok=True, status=row["status"])
