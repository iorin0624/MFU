from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

from app import admin_required
from app.utils.db import get_db

from . import phone_diagnostics_bp
from .service import DiagnosticValidationError, MAX_BODY_BYTES, normalize_call_payload, verify_signed_body


PBX_HOST = os.getenv("PHONE_DIAGNOSTICS_PBX_HOST", os.getenv("PHONE_WHITELIST_PBX_HOST", "192.168.103.21"))
PBX_USER = os.getenv("PHONE_DIAGNOSTICS_PBX_USER", os.getenv("PHONE_WHITELIST_PBX_USER", "mfu-whitelist"))
PBX_KEY_PATH = os.getenv("PHONE_DIAGNOSTICS_SSH_KEY", os.getenv("PHONE_WHITELIST_SSH_KEY", "/mnt/mfu/ssh/mfu_freepbx_whitelist"))
PBX_KNOWN_HOSTS = os.getenv("PHONE_DIAGNOSTICS_KNOWN_HOSTS", os.getenv("PHONE_WHITELIST_KNOWN_HOSTS", "/mnt/mfu/ssh/known_hosts"))
PBX_TIMEOUT_SECONDS = max(5, int(os.getenv("PHONE_DIAGNOSTICS_SSH_TIMEOUT", "20")))
DETAIL_DURATION_SECONDS = 1800
PAGE_SIZE = 100


class DiagnosticCommandError(RuntimeError):
    pass


def ensure_phone_diagnostics_schema() -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS phone_rtp_diagnostics (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                event_id VARCHAR(96) NOT NULL UNIQUE,
                uniqueid VARCHAR(96) NOT NULL DEFAULT '',
                linkedid VARCHAR(96) NOT NULL DEFAULT '',
                started_at DATETIME NULL,
                ended_at DATETIME NOT NULL,
                duration INT NOT NULL DEFAULT 0,
                billsec INT NOT NULL DEFAULT 0,
                direction VARCHAR(16) NOT NULL DEFAULT 'outbound',
                endpoint VARCHAR(32) NOT NULL,
                remote_number VARCHAR(32) NOT NULL DEFAULT '',
                channel_name VARCHAR(160) NOT NULL DEFAULT '',
                read_codec VARCHAR(64) NOT NULL DEFAULT '',
                write_codec VARCHAR(64) NOT NULL DEFAULT '',
                rtp_source VARCHAR(128) NOT NULL DEFAULT '',
                rtp_dest VARCHAR(128) NOT NULL DEFAULT '',
                sip_remote_addr VARCHAR(128) NOT NULL DEFAULT '',
                sip_call_id VARCHAR(255) NOT NULL DEFAULT '',
                local_packets BIGINT NULL,
                remote_packets BIGINT NULL,
                local_lost BIGINT NULL,
                remote_lost BIGINT NULL,
                local_loss_pct DECIMAL(9,4) NULL,
                remote_loss_pct DECIMAL(9,4) NULL,
                rx_jitter_ms DECIMAL(12,3) NULL,
                tx_jitter_ms DECIMAL(12,3) NULL,
                remote_avg_jitter_ms DECIMAL(12,3) NULL,
                rtt_ms DECIMAL(12,3) NULL,
                tx_mes DECIMAL(12,4) NULL,
                rx_mes DECIMAL(12,4) NULL,
                quality_status VARCHAR(16) NOT NULL DEFAULT 'unknown',
                raw_metrics LONGTEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_phone_rtp_diag_started (started_at),
                INDEX idx_phone_rtp_diag_quality (quality_status, started_at),
                INDEX idx_phone_rtp_diag_remote (remote_number, started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS phone_rtp_detail_sessions (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                session_id CHAR(32) NOT NULL UNIQUE,
                endpoint VARCHAR(32) NOT NULL DEFAULT '10610',
                status VARCHAR(24) NOT NULL DEFAULT 'requested',
                contact_ip VARCHAR(64) NOT NULL DEFAULT '',
                started_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                completed_at DATETIME NULL,
                initiated_by VARCHAR(128) NOT NULL DEFAULT '',
                packet_count_to_phone BIGINT NULL,
                packet_count_from_phone BIGINT NULL,
                max_gap_to_phone_ms DECIMAL(12,3) NULL,
                max_gap_from_phone_ms DECIMAL(12,3) NULL,
                gaps_to_phone_over_100ms BIGINT NULL,
                gaps_from_phone_over_100ms BIGINT NULL,
                summary_json LONGTEXT NOT NULL,
                error_message VARCHAR(500) NOT NULL DEFAULT '',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_phone_rtp_detail_started (started_at),
                INDEX idx_phone_rtp_detail_status (status, expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    finally:
        db.close()


def ensure_phone_diagnostics_nav_item() -> None:
    db = get_db()
    cur = None
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT GET_LOCK('mfu_phone_diagnostics_nav', 10) AS acquired")
        if int((cur.fetchone() or {}).get("acquired") or 0) != 1:
            raise RuntimeError("通話品質診断メニューの更新ロックを取得できませんでした")
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/admin/phone-diagnostics",))
        existing = cur.fetchone()
        if not existing:
            cur.execute(
                "SELECT parent_id FROM mfu_nav_items WHERE url=%s LIMIT 1",
                ("/admin/phone-whitelist",),
            )
            parent_id = (cur.fetchone() or {}).get("parent_id")
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (%s, %s, %s, 66, 1, NULL, 0, 0)
                """,
                (parent_id, "📊 通話品質診断", "/admin/phone-diagnostics"),
            )
        db.commit()
    finally:
        try:
            if cur is not None:
                cur.execute("SELECT RELEASE_LOCK('mfu_phone_diagnostics_nav')")
        except Exception:
            pass
        db.close()


def _secret() -> str:
    return (os.getenv("PHONE_DIAGNOSTICS_SECRET") or "").strip()


def _client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return (forwarded or request.remote_addr or "-")[:64]


def _internal_request_body() -> bytes:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        abort(413)
    allowed = {item.strip() for item in os.getenv("PHONE_DIAGNOSTICS_ALLOWED_IPS", "192.168.103.21,127.0.0.1").split(",") if item.strip()}
    if request.remote_addr not in allowed:
        abort(403)
    body = request.get_data(cache=True)
    if len(body) > MAX_BODY_BYTES:
        abort(413)
    verify_signed_body(
        body,
        request.headers.get("X-MFU-Timestamp", ""),
        request.headers.get("X-MFU-Signature", ""),
        _secret(),
    )
    return body


def _audit(action: str, result: str, details: dict[str, Any], error: str = "") -> None:
    payload = {"action": action, "result": result, "details": details}
    if error:
        payload["error"] = error[:500]
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO logs
                (log_date, ip, method, path, status, endpoint, username, latency_ms, log_text)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, 0, %s)
            """,
            (
                _client_ip(), request.method, f"/admin/phone-diagnostics/audit/{action}",
                200 if result == "ok" else 500, "phone_diagnostics.audit",
                str(session.get("user") or "unknown")[:128],
                ("PHONE_DIAGNOSTICS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))[:4000],
            ),
        )
        db.commit()
    finally:
        db.close()


def _pbx_command(command: str) -> dict[str, Any]:
    if not re.fullmatch(r"diagnostics (?:start|stop|status)(?: [a-f0-9]{32})?", command):
        raise DiagnosticCommandError("許可されていない診断コマンドです")
    ssh = [
        "/usr/bin/ssh", "-T", "-i", PBX_KEY_PATH,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={PBX_KNOWN_HOSTS}",
        "-o", "ConnectTimeout=5", f"{PBX_USER}@{PBX_HOST}", command,
    ]
    try:
        result = subprocess.run(ssh, text=True, capture_output=True, timeout=PBX_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiagnosticCommandError(f"FreePBX診断コマンドに接続できません: {exc}") from exc
    if result.returncode != 0:
        raise DiagnosticCommandError((result.stderr or result.stdout or "FreePBX診断コマンドが失敗しました").strip()[:500])
    try:
        parsed = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise DiagnosticCommandError("FreePBXから不正な応答を受信しました") from exc
    if not isinstance(parsed, dict):
        raise DiagnosticCommandError("FreePBXから不正な応答を受信しました")
    return parsed


@phone_diagnostics_bp.post("/internal/phone-diagnostics/calls")
def receive_call_diagnostic():
    try:
        body = _internal_request_body()
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise DiagnosticValidationError("JSONオブジェクトが必要です")
        item = normalize_call_payload(payload)
    except (DiagnosticValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    columns = list(item.keys())
    placeholders = ",".join(["%s"] * len(columns))
    updates = ",".join(f"{column}=VALUES({column})" for column in columns if column != "event_id")
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            f"INSERT INTO phone_rtp_diagnostics ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}",
            tuple(item[column] for column in columns),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "event_id": item["event_id"], "quality": item["quality_status"]})


@phone_diagnostics_bp.post("/internal/phone-diagnostics/sessions")
def receive_detail_session():
    try:
        body = _internal_request_body()
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise DiagnosticValidationError("JSONオブジェクトが必要です")
        session_id = str(payload.get("session_id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise DiagnosticValidationError("session_idが不正です")
    except (DiagnosticValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    status = str(payload.get("status") or "completed")[:24]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE phone_rtp_detail_sessions
               SET status=%s, contact_ip=%s, completed_at=NOW(),
                   packet_count_to_phone=%s, packet_count_from_phone=%s,
                   max_gap_to_phone_ms=%s, max_gap_from_phone_ms=%s,
                   gaps_to_phone_over_100ms=%s, gaps_from_phone_over_100ms=%s,
                   summary_json=%s, error_message=%s
             WHERE session_id=%s
            """,
            (
                status, str(payload.get("contact_ip") or "")[:64],
                summary.get("packet_count_to_phone"), summary.get("packet_count_from_phone"),
                summary.get("max_gap_to_phone_ms"), summary.get("max_gap_from_phone_ms"),
                summary.get("gaps_to_phone_over_100ms"), summary.get("gaps_from_phone_over_100ms"),
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                str(payload.get("error") or "")[:500], session_id,
            ),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "session_id": session_id})


@phone_diagnostics_bp.get("/admin/phone-diagnostics")
@admin_required
def index():
    page = max(1, int(request.args.get("page", "1") or 1))
    quality = request.args.get("quality", "").strip()
    if quality not in {"", "good", "warning", "bad", "unknown"}:
        quality = ""
    remote = re.sub(r"[^0-9*#+]", "", request.args.get("remote", ""))[:32]
    where = []
    params: list[Any] = []
    if quality:
        where.append("quality_status=%s")
        params.append(quality)
    if remote:
        where.append("remote_number LIKE %s")
        params.append(f"%{remote}%")
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(f"SELECT COUNT(*) AS count FROM phone_rtp_diagnostics{where_sql}", tuple(params))
        total = int((cur.fetchone() or {}).get("count") or 0)
        page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, page_count)
        cur.execute(
            f"SELECT * FROM phone_rtp_diagnostics{where_sql} ORDER BY COALESCE(started_at, ended_at) DESC LIMIT %s OFFSET %s",
            tuple(params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]),
        )
        calls = cur.fetchall()
        cur.execute(
            "SELECT quality_status, COUNT(*) AS count FROM phone_rtp_diagnostics "
            "WHERE ended_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY quality_status"
        )
        counts = {row["quality_status"]: int(row["count"]) for row in cur.fetchall()}
        cur.execute("SELECT * FROM phone_rtp_detail_sessions ORDER BY started_at DESC LIMIT 20")
        sessions = cur.fetchall()
    finally:
        db.close()

    for call in calls:
        try:
            call["metrics"] = json.loads(call.get("raw_metrics") or "{}")
        except (TypeError, json.JSONDecodeError):
            call["metrics"] = {}
    now = datetime.now()
    active = next((row for row in sessions if row["status"] in {"requested", "running"} and row["expires_at"] > now), None)
    return render_template(
        "admin_phone_diagnostics.html", calls=calls, sessions=sessions, active_session=active,
        counts=counts, page=page, page_count=page_count, total=total, quality=quality, remote=remote,
    )


@phone_diagnostics_bp.get("/admin/phone-diagnostics/<int:item_id>")
@admin_required
def detail(item_id: int):
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM phone_rtp_diagnostics WHERE id=%s", (item_id,))
        item = cur.fetchone()
    finally:
        db.close()
    if not item:
        abort(404)
    try:
        item["metrics"] = json.loads(item.get("raw_metrics") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["metrics"] = {}
    return render_template("admin_phone_diagnostic_detail.html", item=item)


@phone_diagnostics_bp.post("/admin/phone-diagnostics/session/start")
@admin_required
def start_detail_session():
    session_id = uuid.uuid4().hex
    now = datetime.now()
    expires = now + timedelta(seconds=DETAIL_DURATION_SECONDS)
    actor = str(session.get("user") or "unknown")[:128]
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM phone_rtp_detail_sessions WHERE status IN ('requested','running') AND expires_at>NOW()")
        if int((cur.fetchone() or [0])[0]) > 0:
            flash("詳細診断はすでに実行中です。", "warning")
            return redirect(url_for("phone_diagnostics.index"))
        cur.execute(
            """
            INSERT INTO phone_rtp_detail_sessions
                (session_id, endpoint, status, started_at, expires_at, initiated_by, summary_json)
            VALUES (%s, '10610', 'requested', %s, %s, %s, '{}')
            """,
            (session_id, now, expires, actor),
        )
        db.commit()
    finally:
        db.close()
    try:
        result = _pbx_command(f"diagnostics start {session_id}")
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "UPDATE phone_rtp_detail_sessions SET status='running', contact_ip=%s WHERE session_id=%s",
                (str(result.get("contact_ip") or "")[:64], session_id),
            )
            db.commit()
        finally:
            db.close()
        _audit("detail_start", "ok", {"session_id": session_id, "contact_ip": result.get("contact_ip")})
        flash("30分間の詳細診断を開始しました。音声内容やSIP認証情報は保存しません。", "success")
    except DiagnosticCommandError as exc:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "UPDATE phone_rtp_detail_sessions SET status='failed', completed_at=NOW(), error_message=%s WHERE session_id=%s",
                (str(exc)[:500], session_id),
            )
            db.commit()
        finally:
            db.close()
        _audit("detail_start", "error", {"session_id": session_id}, str(exc))
        flash(f"詳細診断を開始できませんでした: {exc}", "danger")
    return redirect(url_for("phone_diagnostics.index"))


@phone_diagnostics_bp.post("/admin/phone-diagnostics/session/<session_id>/stop")
@admin_required
def stop_detail_session(session_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        abort(404)
    try:
        _pbx_command(f"diagnostics stop {session_id}")
        _audit("detail_stop", "ok", {"session_id": session_id})
        flash("詳細診断の停止を要求しました。集計完了まで数秒かかります。", "success")
    except DiagnosticCommandError as exc:
        _audit("detail_stop", "error", {"session_id": session_id}, str(exc))
        flash(f"詳細診断を停止できませんでした: {exc}", "danger")
    return redirect(url_for("phone_diagnostics.index"))


@phone_diagnostics_bp.get("/admin/phone-diagnostics/export")
@admin_required
def export_csv():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM phone_rtp_diagnostics ORDER BY COALESCE(started_at, ended_at) DESC LIMIT 10000")
        rows = cur.fetchall()
    finally:
        db.close()
    output = io.StringIO(newline="")
    fields = [
        "started_at", "ended_at", "remote_number", "duration", "billsec", "read_codec", "write_codec",
        "local_packets", "local_lost", "local_loss_pct", "remote_packets", "remote_lost", "remote_loss_pct",
        "rx_jitter_ms", "tx_jitter_ms", "remote_avg_jitter_ms", "rtt_ms", "tx_mes", "rx_mes",
        "quality_status", "rtp_source", "rtp_dest", "uniqueid",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    filename = datetime.now().strftime("phone_diagnostics_%Y%m%d_%H%M%S.csv")
    return Response(
        "\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
