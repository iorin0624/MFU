"""Admin-only bridge to the Windows X follow collector.

Full account lists only pass through Socket.IO to a connected administrator.
Only changed @handles are retained in MFU notification history.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, abort, jsonify, render_template, request, session
from flask_socketio import disconnect, join_room

from app.utils.admin_auth import ADMIN_USERNAME, validate_admin_session
from app.utils.admin_passkey_stepup import require_admin_passkey
from app.utils.db import get_db


bp = Blueprint("x_ff_remote", __name__, url_prefix="/admin/x-ff-remote")
AGENT_NS = "/x-ff-agent"
ADMIN_NS = "/x-ff-admin"
AGENT_ROOM = "x-ff-windows-agent"
ADMIN_ROOM = "x-ff-admin-viewers"
_lock = threading.RLock()
_agent_sid = None
_last_seen = 0.0


def _admin():
    if session.get("user") != ADMIN_USERNAME or not validate_admin_session():
        abort(403)


def _db():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("""CREATE TABLE IF NOT EXISTS x_ff_remote_runs (
            job_id VARCHAR(64) PRIMARY KEY,
            mode VARCHAR(16) NOT NULL,
            status VARCHAR(24) NOT NULL,
            started_at DATETIME(6) NOT NULL,
            finished_at DATETIME(6) NULL,
            followers_count INT NULL,
            following_count INT NULL,
            added_count INT NULL,
            removed_count INT NULL,
            complete TINYINT(1) NULL,
            bridge_pending TINYINT(1) NOT NULL DEFAULT 0,
            error_code VARCHAR(80) NULL
        ) CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS x_ff_remote_agent (
            id TINYINT PRIMARY KEY,
            last_seen DATETIME(6) NULL,
            connected TINYINT(1) NOT NULL DEFAULT 0,
            busy TINYINT(1) NOT NULL DEFAULT 0,
            bridge_pending INT NOT NULL DEFAULT 0,
            last_power_press DATETIME(6) NULL
        ) CHARACTER SET utf8mb4""")
        cur.execute("INSERT IGNORE INTO x_ff_remote_agent (id) VALUES (1)")
        db.commit()
    finally:
        db.close()


def ensure_nav_item():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT GET_LOCK('x_ff_remote_nav', 5) AS acquired")
        if (cur.fetchone() or {}).get("acquired") != 1:
            return
        try:
            cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/admin/x-ff-remote",))
            if cur.fetchone():
                return
            cur.execute("SELECT id FROM mfu_nav_items WHERE parent_id IS NULL AND label LIKE %s ORDER BY id LIMIT 1", ("%システム%",))
            row = cur.fetchone()
            parent = row["id"] if row else None
            cur.execute("SELECT COALESCE(MAX(order_no), 0) AS n FROM mfu_nav_items WHERE parent_id <=> %s", (parent,))
            order = int(cur.fetchone()["n"]) + 10
            cur.execute("""INSERT INTO mfu_nav_items
                (parent_id,label,url,order_no,is_enabled,feature_key,open_in_new_tab,is_external)
                VALUES (%s,%s,%s,%s,1,NULL,0,0)""",
                (parent, "Xフォロー関係の遠隔取得", "/admin/x-ff-remote", order))
            db.commit()
        finally:
            cur.execute("SELECT RELEASE_LOCK('x_ff_remote_nav')")
            cur.fetchone()
    finally:
        db.close()


def _online():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT connected, last_seen FROM x_ff_remote_agent WHERE id=1")
        row = cur.fetchone() or {}
        last_seen = row.get("last_seen")
        return bool(row.get("connected") and last_seen and
                    (datetime.utcnow() - last_seen).total_seconds() < 45)
    finally:
        db.close()


def _bridge_pending():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT bridge_pending FROM x_ff_remote_agent WHERE id=1")
        return int((cur.fetchone() or {}).get("bridge_pending") or 0)
    finally:
        db.close()


def _agent_state(*, connected=True, busy=False, bridge_pending=0):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""UPDATE x_ff_remote_agent SET connected=%s,last_seen=UTC_TIMESTAMP(6),
            busy=%s,bridge_pending=%s WHERE id=1""", (connected, busy, bridge_pending))
        db.commit()
    finally:
        db.close()


def _running_job():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT job_id FROM x_ff_remote_runs WHERE status IN ('queued','running') ORDER BY started_at DESC LIMIT 1")
        row = cur.fetchone()
        return row["job_id"] if row else None
    finally:
        db.close()


def _latest():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM x_ff_remote_runs ORDER BY started_at DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            for key, value in row.items():
                if isinstance(value, datetime):
                    row[key] = value.isoformat() + "Z"
        return row
    finally:
        db.close()


@bp.get("")
def page():
    _admin()
    return render_template("x_ff_remote.html")


@bp.get("/state")
def state():
    _admin()
    return jsonify(ok=True, online=_online(), bridgePending=_bridge_pending(),
                   configured=bool(os.getenv("X_FF_AGENT_TOKEN")), latest=_latest())


@bp.post("/start")
def start():
    _admin()
    mode = str((request.get_json(silent=True) or {}).get("mode") or "")
    if mode not in {"all", "followers", "following"}:
        return jsonify(ok=False, error="invalid_mode"), 400
    if not _online():
        return jsonify(ok=False, error="agent_offline"), 409
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT GET_LOCK('x_ff_remote_start', 3) AS acquired")
        if (cur.fetchone() or {}).get("acquired") != 1:
            return jsonify(ok=False, error="start_lock_busy"), 409
        try:
            cur.execute("SELECT job_id FROM x_ff_remote_runs WHERE status IN ('queued','running') LIMIT 1")
            if cur.fetchone():
                return jsonify(ok=False, error="job_running"), 409
            job_id = secrets.token_hex(16)
            cur.execute("INSERT INTO x_ff_remote_runs (job_id,mode,status,started_at) VALUES (%s,%s,'queued',UTC_TIMESTAMP(6))", (job_id, mode))
            db.commit()
        finally:
            cur.execute("SELECT RELEASE_LOCK('x_ff_remote_start')")
            cur.fetchone()
    finally:
        db.close()
    from app.chat.socketio_ext import socketio
    socketio.emit("command", {"kind": "start", "jobId": job_id, "mode": mode}, room=AGENT_ROOM, namespace=AGENT_NS)
    return jsonify(ok=True, jobId=job_id)


@bp.post("/cancel")
def cancel():
    _admin()
    from app.chat.socketio_ext import socketio
    job_id = _running_job()
    if not job_id:
        return jsonify(ok=False, error="no_active_job"), 409
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""UPDATE x_ff_remote_runs SET status='cancelled',finished_at=UTC_TIMESTAMP(6)
            WHERE job_id=%s AND status IN ('queued','running')""", (job_id,))
        db.commit()
    finally:
        db.close()
    socketio.emit("command", {"kind": "cancel", "jobId": job_id}, room=AGENT_ROOM, namespace=AGENT_NS)
    socketio.emit("result", {"jobId": job_id, "status": "cancelled", "counts": {}, "diffByType": {}},
                  room=ADMIN_ROOM, namespace=ADMIN_NS)
    return jsonify(ok=True)


@bp.post("/sleep")
def sleep_pc():
    _admin()
    rejected = require_admin_passkey("x_ff_sleep")
    if rejected:
        return rejected
    with _lock:
        if not _online() or _running_job() or _bridge_pending():
            return jsonify(ok=False, error="agent_offline_or_busy"), 409
    from app.chat.socketio_ext import socketio
    socketio.emit("command", {"kind": "sleep"}, room=AGENT_ROOM, namespace=AGENT_NS)
    return jsonify(ok=True)


@bp.post("/power")
def power_pc():
    _admin()
    rejected = require_admin_passkey("x_ff_power")
    if rejected:
        return rejected
    if (request.get_json(silent=True) or {}).get("confirmedOff") is not True:
        return jsonify(ok=False, error="confirm_pc_is_off_required"), 400
    device_id = os.getenv("X_FF_SWITCHBOT_DEVICE_ID", "").strip()
    if not device_id:
        return jsonify(ok=False, error="switchbot_device_unconfigured"), 503
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
        return jsonify(ok=False, error="switchbot_device_invalid"), 503
    if _online():
        return jsonify(ok=False, error="agent_already_online"), 409
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT token, secret FROM switchbot_tokens ORDER BY id DESC LIMIT 1")
        credentials = cur.fetchone() or {}
    finally:
        db.close()
    token, secret = credentials.get("token"), credentials.get("secret")
    if not token or not secret:
        return jsonify(ok=False, error="switchbot_credentials_missing"), 503
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT last_power_press FROM x_ff_remote_agent WHERE id=1 FOR UPDATE")
        row = cur.fetchone() or {}
        previous = row.get("last_power_press")
        if previous and (datetime.utcnow() - previous).total_seconds() < 120:
            db.rollback()
            return jsonify(ok=False, error="power_cooldown"), 429
        cur.execute("UPDATE x_ff_remote_agent SET last_power_press=UTC_TIMESTAMP(6) WHERE id=1")
        db.commit()
    finally:
        db.close()
    now_ms = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    signature = base64.b64encode(hmac.new(secret.encode(), (token + now_ms + nonce).encode(), hashlib.sha256).digest()).decode()
    try:
        response = requests.post(
            f"https://api.switch-bot.com/v1.1/devices/{device_id}/commands",
            headers={"Authorization": token, "sign": signature, "t": now_ms, "nonce": nonce},
            json={"command": "press", "parameter": "default", "commandType": "command"}, timeout=12,
        )
        response.raise_for_status()
        if response.json().get("statusCode") != 100:
            raise ValueError("SwitchBot rejected the command")
    except (requests.RequestException, ValueError):
        return jsonify(ok=False, error="switchbot_command_failed"), 502
    return jsonify(ok=True, note="press_accepted_boot_not_confirmed")


def register_socketio(socketio):
    @socketio.on("connect", namespace=ADMIN_NS)
    def admin_connect(auth=None):
        if session.get("user") != ADMIN_USERNAME or not validate_admin_session():
            return False
        join_room(ADMIN_ROOM)
        return True

    @socketio.on("connect", namespace=AGENT_NS)
    def agent_connect(auth=None):
        global _agent_sid, _last_seen
        expected = os.getenv("X_FF_AGENT_TOKEN", "")
        supplied = str((auth or {}).get("token") or "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return False
        with _lock:
            _agent_sid = request.sid
            _last_seen = time.monotonic()
        _agent_state()
        join_room(AGENT_ROOM)
        socketio.emit("agent_state", {"online": True}, room=ADMIN_ROOM, namespace=ADMIN_NS)
        return True

    @socketio.on("disconnect", namespace=AGENT_NS)
    def agent_disconnect(reason=None):
        global _agent_sid, _last_seen
        with _lock:
            if _agent_sid != request.sid:
                return
            _agent_sid = None
            _last_seen = 0
        _agent_state(connected=False)
        socketio.emit("agent_state", {"online": False}, room=ADMIN_ROOM, namespace=ADMIN_NS)

    @socketio.on("heartbeat", namespace=AGENT_NS)
    def heartbeat(data=None):
        global _last_seen
        with _lock:
            if request.sid != _agent_sid:
                disconnect()
                return
            _last_seen = time.monotonic()
        _agent_state(busy=bool((data or {}).get("busy")),
                     bridge_pending=max(0, min(100, int((data or {}).get("bridgePending") or 0))))
        socketio.emit("agent_state", {"online": True, "busy": bool((data or {}).get("busy")),
                                       "bridgePending": int((data or {}).get("bridgePending") or 0)},
                      room=ADMIN_ROOM, namespace=ADMIN_NS)

    @socketio.on("hello", namespace=AGENT_NS)
    def hello(data=None):
        if request.sid != _agent_sid:
            return
        reported_job = str((data or {}).get("currentJob") or "")
        db = get_db()
        try:
            cur = db.cursor(dictionary=True)
            cur.execute("SELECT job_id, mode, status FROM x_ff_remote_runs WHERE status IN ('queued','running') ORDER BY started_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row or row["job_id"] == reported_job:
                return
            if row["status"] == "queued":
                socketio.emit("command", {"kind": "start", "jobId": row["job_id"], "mode": row["mode"]},
                              room=request.sid, namespace=AGENT_NS)
            else:
                cur.execute("""UPDATE x_ff_remote_runs SET status='error',
                    finished_at=UTC_TIMESTAMP(6),error_code='agent_restarted'
                    WHERE job_id=%s AND status='running'""", (row["job_id"],))
                db.commit()
                socketio.emit("result", {"jobId": row["job_id"], "status": "error",
                                         "errorCode": "agent_restarted", "counts": {}, "diffByType": {}},
                              room=ADMIN_ROOM, namespace=ADMIN_NS)
        finally:
            db.close()

    @socketio.on("progress", namespace=AGENT_NS)
    def progress(data):
        if request.sid != _agent_sid or not isinstance(data, dict):
            return
        if data.get("jobId") != _running_job():
            return
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("UPDATE x_ff_remote_runs SET status='running' WHERE job_id=%s AND status='queued'", (data.get("jobId"),))
            db.commit()
        finally:
            db.close()
        safe = {key: data.get(key) for key in ("jobId", "type", "count", "expectedCount", "pass", "status")}
        socketio.emit("progress", safe, room=ADMIN_ROOM, namespace=ADMIN_NS)

    @socketio.on("result", namespace=AGENT_NS)
    def result(data):
        if request.sid != _agent_sid or not isinstance(data, dict):
            return
        job_id = str(data.get("jobId") or "")
        if job_id != _running_job():
            return
        status = str(data.get("status") or "error")
        if status not in {"success", "incomplete", "error", "cancelled"}:
            status = "error"
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        diff = data.get("diff") if isinstance(data.get("diff"), dict) else {}
        by_type = data.get("diffByType") if isinstance(data.get("diffByType"), dict) else {}
        def count(value):
            return value if isinstance(value, int) and 0 <= value <= 2_000_000 else None
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("""UPDATE x_ff_remote_runs SET status=%s,finished_at=UTC_TIMESTAMP(6),
                followers_count=%s,following_count=%s,added_count=%s,
                removed_count=%s,complete=%s,bridge_pending=%s,error_code=%s WHERE job_id=%s""",
                (status, count(counts.get("followers")),
                 count(counts.get("following")), count(diff.get("addedCount")),
                 count(diff.get("removedCount")), status == "success",
                 bool(data.get("bridgePending")), str(data.get("errorCode") or "")[:80], job_id))
            db.commit()
        finally:
            db.close()
        # Full handles are ephemeral: broadcast to currently connected admins only.
        transient = {"jobId": job_id, "status": status, "counts": counts, "diff": diff,
                     "diffByType": by_type,
                     "ownerHandle": str(data.get("ownerHandle") or "")[:15],
                     "bridgePending": bool(data.get("bridgePending")),
                     "errorCode": str(data.get("errorCode") or "")[:80]}
        socketio.emit("result", transient, room=ADMIN_ROOM, namespace=ADMIN_NS)
        if status in {"success", "incomplete", "error"}:
            try:
                from app.utils.notification_service import publish_common_notification
                from app.x_ff_notification import build_notification
                description, fields = build_notification(status, counts, by_type)
                publish_common_notification(
                    recipient_username=ADMIN_USERNAME, feature_key="x_follow_changes", kind="x_follow_changes",
                    severity="success" if status == "success" else "warning",
                    title="Xフォロー関係の遠隔取得" if status == "success" else "Xフォロー関係の取得に注意",
                    description=description, fields=fields,
                    target_url="/mfu-notifications/",
                    actions=[{"label": "取得画面を開く", "url": "/admin/x-ff-remote"}],
                    dedup_key=f"x_ff_remote:{job_id}",
                )
            except Exception:
                # Notification failure must not discard the acquisition result.
                pass
