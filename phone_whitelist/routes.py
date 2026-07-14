from __future__ import annotations

import base64
import csv
import io
import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Response, flash, redirect, render_template, request, session, url_for
from mysql.connector import IntegrityError

from app import admin_required
from app.utils.db import get_db

from . import phone_whitelist_bp
from .service import (
    MAX_CSV_BYTES,
    WhitelistValidationError,
    build_pbx_payload,
    parse_csv_bytes,
    validate_entry,
)


PBX_HOST = os.getenv("PHONE_WHITELIST_PBX_HOST", "192.168.103.21")
PBX_USER = os.getenv("PHONE_WHITELIST_PBX_USER", "mfu-whitelist")
PBX_KEY_PATH = os.getenv("PHONE_WHITELIST_SSH_KEY", "/mnt/mfu/ssh/mfu_freepbx_whitelist")
PBX_KNOWN_HOSTS = os.getenv("PHONE_WHITELIST_KNOWN_HOSTS", "/mnt/mfu/ssh/known_hosts")
PBX_TIMEOUT_SECONDS = max(5, int(os.getenv("PHONE_WHITELIST_SSH_TIMEOUT", "20")))
DB_LOCK_NAME = "mfu_phone_whitelist_update"
PAGE_SIZE = 100
JST = timezone(timedelta(hours=9))
MANUAL_UNTIL = datetime(9999, 12, 31, 23, 59, 59)
MANUAL_EPOCH = 4_102_444_800
SETTING_COLUMNS = {
    "anonymous_allowed": "anonymous_allowed_until",
    "whitelist_disabled": "whitelist_disabled_until",
}
DURATION_SECONDS = {"900": 900, "1800": 1800, "3600": 3600, "10800": 10800}


class PhoneWhitelistError(RuntimeError):
    pass


def ensure_phone_whitelist_schema() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS phone_whitelist_entries (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                phone_number VARCHAR(16) NOT NULL UNIQUE,
                name VARCHAR(100) NOT NULL DEFAULT '',
                note VARCHAR(500) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_phone_whitelist_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS phone_whitelist_sync_state (
                id TINYINT NOT NULL PRIMARY KEY,
                last_synced_at DATETIME NULL,
                entry_count INT NOT NULL DEFAULT 0,
                updated_by VARCHAR(128) NOT NULL DEFAULT '',
                last_result VARCHAR(32) NOT NULL DEFAULT '',
                message VARCHAR(500) NOT NULL DEFAULT ''
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        for column in ("whitelist_disabled_until", "anonymous_allowed_until"):
            cur.execute(f"SHOW COLUMNS FROM phone_whitelist_sync_state LIKE '{column}'")
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE phone_whitelist_sync_state ADD COLUMN {column} DATETIME NULL")
        cur.execute("SELECT id FROM phone_whitelist_sync_state WHERE id=1")
        if cur.fetchone() is None:
            cur.execute(
                """
                INSERT IGNORE INTO phone_whitelist_entries (phone_number, name, note)
                VALUES (%s, %s, %s)
                """,
                ("08093242655", "テスト番号", "初期ホワイトリスト"),
            )
            cur.execute("SELECT COUNT(*) AS count FROM phone_whitelist_entries")
            count = int((cur.fetchone() or {}).get("count") or 0)
            cur.execute(
                """
                INSERT INTO phone_whitelist_sync_state
                    (id, last_synced_at, entry_count, updated_by, last_result, message)
                VALUES (1, NOW(), %s, %s, %s, %s)
                """,
                (count, "system", "initialized", "FreePBXの既存番号を初期データとして取り込みました"),
            )
        db.commit()
    finally:
        db.close()


def ensure_phone_whitelist_nav_item() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/admin/phone-whitelist",))
        if cur.fetchone() is None:
            cur.execute(
                """
                SELECT id FROM mfu_nav_items
                 WHERE label LIKE %s AND parent_id IS NULL
                 ORDER BY id LIMIT 1
                """,
                ("%システム系%",),
            )
            parent = cur.fetchone()
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (%s, %s, %s, %s, 1, NULL, 0, 0)
                """,
                ((parent or {}).get("id"), "📞 着信ホワイトリスト", "/admin/phone-whitelist", 65),
            )
        db.commit()
    finally:
        db.close()


def _actor() -> str:
    return str(session.get("user") or "unknown")[:128]


def _client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return (forwarded or request.remote_addr or "-")[:64]


def _audit(action: str, result: str, details: dict[str, Any], error: str = "") -> None:
    payload = {"action": action, "result": result, "details": details}
    if error:
        payload["error"] = error[:500]
    log_text = "PHONE_WHITELIST " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    db = None
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO logs
                (log_date, ip, method, path, status, endpoint, username, latency_ms, log_text)
            VALUES
                (NOW(), %s, %s, %s, %s, %s, %s, 0, %s)
            """,
            (
                _client_ip(),
                request.method,
                f"/admin/phone-whitelist/audit/{action}",
                200 if result == "ok" else 500,
                "phone_whitelist.audit",
                _actor(),
                log_text[:4000],
            ),
        )
        db.commit()
    except Exception:
        if db:
            db.rollback()
    finally:
        if db:
            db.close()


def _until_epoch(value: datetime | None) -> int:
    if not value:
        return 0
    if value.year >= 2099:
        return MANUAL_EPOCH
    aware = value.replace(tzinfo=JST) if value.tzinfo is None else value.astimezone(JST)
    return max(0, int(aware.timestamp()))


def _fetch_settings(cur) -> dict[str, Any]:
    cur.execute(
        "SELECT whitelist_disabled_until, anonymous_allowed_until "
        "FROM phone_whitelist_sync_state WHERE id=1"
    )
    return cur.fetchone() or {}


def _apply_to_pbx(entries: list[dict[str, Any]], settings: dict[str, Any] | None = None) -> str:
    settings = settings or {}
    command = [
        "/usr/bin/ssh", "-T", "-i", PBX_KEY_PATH,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={PBX_KNOWN_HOSTS}",
        "-o", "ConnectTimeout=5", f"{PBX_USER}@{PBX_HOST}",
    ]
    try:
        result = subprocess.run(
            command,
            input=build_pbx_payload(
                entries,
                whitelist_disabled_until=_until_epoch(settings.get("whitelist_disabled_until")),
                anonymous_allowed_until=_until_epoch(settings.get("anonymous_allowed_until")),
            ),
            text=True,
            capture_output=True,
            timeout=PBX_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PhoneWhitelistError("FreePBXへの反映がタイムアウトしました") from exc
    except OSError as exc:
        raise PhoneWhitelistError(f"FreePBX反映コマンドを起動できません: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "反映に失敗しました").strip()
        raise PhoneWhitelistError(f"FreePBXへの反映に失敗しました: {message[:500]}")
    output = (result.stdout or "").strip()
    if not output.startswith("OK "):
        raise PhoneWhitelistError(f"FreePBXから不正な応答が返りました: {output[:500]}")
    return output


def _fetch_sync_entries(cur) -> list[dict[str, str]]:
    cur.execute("SELECT phone_number, name FROM phone_whitelist_entries ORDER BY phone_number")
    return [
        {"phone_number": str(row["phone_number"]), "name": str(row.get("name") or "")}
        for row in cur.fetchall()
    ]


def _run_locked_change(change: Callable[[Any], dict[str, Any]], actor: str) -> tuple[dict[str, Any], int]:
    db = get_db()
    locked = False
    remote_applied = False
    before_entries: list[dict[str, str]] = []
    before_settings: dict[str, Any] = {}
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT GET_LOCK(%s, 15) AS acquired", (DB_LOCK_NAME,))
        locked = int((cur.fetchone() or {}).get("acquired") or 0) == 1
        if not locked:
            raise PhoneWhitelistError("別の更新処理が実行中です。少し待ってから再試行してください")
        before_entries = _fetch_sync_entries(cur)
        before_settings = _fetch_settings(cur)
        details = change(cur)
        after_entries = _fetch_sync_entries(cur)
        after_settings = _fetch_settings(cur)
        response = _apply_to_pbx(after_entries, after_settings)
        remote_applied = True
        cur.execute(
            """
            UPDATE phone_whitelist_sync_state
               SET last_synced_at=NOW(), entry_count=%s, updated_by=%s,
                   last_result='ok', message=%s
             WHERE id=1
            """,
            (len(after_entries), actor, response[:500]),
        )
        db.commit()
        remote_applied = False
        return details, len(after_entries)
    except Exception as exc:
        db.rollback()
        if remote_applied:
            try:
                _apply_to_pbx(before_entries, before_settings)
            except Exception as rollback_exc:
                raise PhoneWhitelistError(
                    f"MFU.2の保存に失敗し、FreePBXの自動切り戻しにも失敗しました: {rollback_exc}"
                ) from exc
        raise
    finally:
        if locked:
            try:
                release_cur = db.cursor()
                release_cur.execute("SELECT RELEASE_LOCK(%s)", (DB_LOCK_NAME,))
                release_cur.fetchone()
            except Exception:
                pass
        db.close()


def _render_index(*, preview_entries=None, csv_payload=""):
    query = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        where = ""
        params: list[Any] = []
        if query:
            where = " WHERE phone_number LIKE %s OR name LIKE %s OR note LIKE %s"
            like = f"%{query}%"
            params.extend([like, like, like])
        cur.execute(f"SELECT COUNT(*) AS count FROM phone_whitelist_entries{where}", tuple(params))
        total = int((cur.fetchone() or {}).get("count") or 0)
        offset = (page - 1) * PAGE_SIZE
        cur.execute(
            f"""
            SELECT id, phone_number, name, note, created_at, updated_at
              FROM phone_whitelist_entries
              {where}
             ORDER BY phone_number
             LIMIT %s OFFSET %s
            """,
            tuple(params + [PAGE_SIZE, offset]),
        )
        entries = cur.fetchall()
        cur.execute("SELECT * FROM phone_whitelist_sync_state WHERE id=1")
        sync_state = cur.fetchone() or {}
    finally:
        db.close()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    now = datetime.now(JST).replace(tzinfo=None)
    for setting, column in SETTING_COLUMNS.items():
        until = sync_state.get(column)
        sync_state[f"{setting}_active"] = bool(until and until > now)
        sync_state[f"{setting}_manual"] = bool(until and until.year >= 2099)
    return render_template(
        "admin_phone_whitelist.html",
        entries=entries, total=total, query=query, page=page, pages=pages,
        sync_state=sync_state, preview_entries=preview_entries, csv_payload=csv_payload,
    )


@phone_whitelist_bp.get("/admin/phone-whitelist")
@admin_required
def index():
    return _render_index()


@phone_whitelist_bp.post("/admin/phone-whitelist/add")
@admin_required
def add_entry():
    try:
        entry = validate_entry(request.form.get("phone_number"), request.form.get("name"), request.form.get("note"))

        def change(cur):
            cur.execute(
                "INSERT INTO phone_whitelist_entries (phone_number, name, note) VALUES (%s, %s, %s)",
                (entry["phone_number"], entry["name"], entry["note"]),
            )
            return {"after": entry}

        details, count = _run_locked_change(change, _actor())
        details["entry_count"] = count
        _audit("add", "ok", details)
        flash("電話番号を登録し、FreePBXへ反映しました", "success")
    except IntegrityError:
        _audit("add", "error", {"input": request.form.get("phone_number", "")}, "重複番号")
        flash("同じ電話番号が既に登録されています", "danger")
    except (WhitelistValidationError, PhoneWhitelistError) as exc:
        _audit("add", "error", {"input": request.form.get("phone_number", "")}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.post("/admin/phone-whitelist/<int:entry_id>/edit")
@admin_required
def edit_entry(entry_id: int):
    try:
        entry = validate_entry(request.form.get("phone_number"), request.form.get("name"), request.form.get("note"))

        def change(cur):
            cur.execute("SELECT phone_number, name, note FROM phone_whitelist_entries WHERE id=%s", (entry_id,))
            before = cur.fetchone()
            if not before:
                raise WhitelistValidationError("対象の電話番号が見つかりません")
            cur.execute(
                "UPDATE phone_whitelist_entries SET phone_number=%s, name=%s, note=%s WHERE id=%s",
                (entry["phone_number"], entry["name"], entry["note"], entry_id),
            )
            return {"id": entry_id, "before": before, "after": entry}

        details, count = _run_locked_change(change, _actor())
        details["entry_count"] = count
        _audit("edit", "ok", details)
        flash("変更を保存し、FreePBXへ反映しました", "success")
    except IntegrityError:
        _audit("edit", "error", {"id": entry_id}, "重複番号")
        flash("同じ電話番号が既に登録されています", "danger")
    except (WhitelistValidationError, PhoneWhitelistError) as exc:
        _audit("edit", "error", {"id": entry_id}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.post("/admin/phone-whitelist/<int:entry_id>/delete")
@admin_required
def delete_entry(entry_id: int):
    try:
        def change(cur):
            cur.execute("SELECT phone_number, name, note FROM phone_whitelist_entries WHERE id=%s", (entry_id,))
            before = cur.fetchone()
            if not before:
                raise WhitelistValidationError("対象の電話番号が見つかりません")
            cur.execute("DELETE FROM phone_whitelist_entries WHERE id=%s", (entry_id,))
            return {"id": entry_id, "before": before}

        details, count = _run_locked_change(change, _actor())
        details["entry_count"] = count
        _audit("delete", "ok", details)
        flash("電話番号を削除し、FreePBXへ反映しました", "success")
    except (WhitelistValidationError, PhoneWhitelistError) as exc:
        _audit("delete", "error", {"id": entry_id}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.post("/admin/phone-whitelist/import/preview")
@admin_required
def import_preview():
    upload = request.files.get("csv_file")
    try:
        if not upload or not upload.filename:
            raise WhitelistValidationError("CSVファイルを選択してください")
        data = upload.read(MAX_CSV_BYTES + 1)
        entries = parse_csv_bytes(data)
        payload = base64.b64encode(data).decode("ascii")
        _audit("csv_preview", "ok", {"filename": upload.filename, "rows": len(entries)})
        return _render_index(preview_entries=entries, csv_payload=payload)
    except WhitelistValidationError as exc:
        _audit("csv_preview", "error", {"filename": getattr(upload, "filename", "")}, str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.post("/admin/phone-whitelist/import/apply")
@admin_required
def import_apply():
    raw_payload = request.form.get("csv_payload") or ""
    try:
        if len(raw_payload) > ((MAX_CSV_BYTES + 2) // 3) * 4 + 8:
            raise WhitelistValidationError("CSVデータが大きすぎます")
        try:
            data = base64.b64decode(raw_payload, validate=True)
        except ValueError as exc:
            raise WhitelistValidationError("CSV確認データが不正です。再度ファイルを選択してください") from exc
        entries = parse_csv_bytes(data)

        def change(cur):
            cur.execute("SELECT phone_number FROM phone_whitelist_entries")
            existing = {str(row["phone_number"]) for row in cur.fetchall()}
            inserted = 0
            updated = 0
            for entry in entries:
                if entry["phone_number"] in existing:
                    updated += 1
                else:
                    inserted += 1
                cur.execute(
                    """
                    INSERT INTO phone_whitelist_entries (phone_number, name, note)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name), note=VALUES(note)
                    """,
                    (entry["phone_number"], entry["name"], entry["note"]),
                )
            return {"rows": len(entries), "inserted": inserted, "updated": updated}

        details, count = _run_locked_change(change, _actor())
        details["entry_count"] = count
        _audit("csv_import", "ok", details)
        flash(f"CSVを反映しました（追加{details['inserted']}件、更新{details['updated']}件）", "success")
    except (WhitelistValidationError, PhoneWhitelistError) as exc:
        _audit("csv_import", "error", {}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.post("/admin/phone-whitelist/settings")
@admin_required
def update_setting():
    setting = (request.form.get("setting") or "").strip()
    enabled = request.form.get("enabled") == "1"
    duration = (request.form.get("duration") or "3600").strip()
    if setting not in SETTING_COLUMNS:
        flash("切替項目が不正です", "danger")
        return redirect(url_for("phone_whitelist.index"))
    try:
        if enabled:
            if duration == "manual":
                until = MANUAL_UNTIL
            elif duration in DURATION_SECONDS:
                until = datetime.now(JST).replace(tzinfo=None) + timedelta(seconds=DURATION_SECONDS[duration])
            else:
                raise WhitelistValidationError("有効期間が不正です")
        else:
            until = None
        column = SETTING_COLUMNS[setting]

        def change(cur):
            cur.execute(f"SELECT {column} AS value FROM phone_whitelist_sync_state WHERE id=1")
            before = (cur.fetchone() or {}).get("value")
            cur.execute(f"UPDATE phone_whitelist_sync_state SET {column}=%s WHERE id=1", (until,))
            return {
                "setting": setting,
                "before": before.isoformat(sep=" ") if before else None,
                "after": until.isoformat(sep=" ") if until else None,
                "duration": duration if enabled else "off",
            }

        details, count = _run_locked_change(change, _actor())
        details["entry_count"] = count
        _audit("setting_change", "ok", details)
        label = "ホワイトリスト無効化" if setting == "whitelist_disabled" else "非通知着信の許可"
        flash(f"{label}を{'有効' if enabled else '解除'}にしました", "success")
    except (WhitelistValidationError, PhoneWhitelistError) as exc:
        _audit("setting_change", "error", {"setting": setting, "enabled": enabled}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))


@phone_whitelist_bp.get("/admin/phone-whitelist/export")
@admin_required
def export_csv():
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT phone_number, name, note FROM phone_whitelist_entries ORDER BY phone_number")
        rows = cur.fetchall()
    finally:
        db.close()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["phone_number", "name", "note"])
    for row in rows:
        writer.writerow([row["phone_number"], row["name"], row["note"]])
    data = "\ufeff" + output.getvalue()
    _audit("csv_export", "ok", {"rows": len(rows)})
    filename = datetime.now().strftime("phone_whitelist_%Y%m%d_%H%M%S.csv")
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@phone_whitelist_bp.post("/admin/phone-whitelist/sync")
@admin_required
def sync_now():
    try:
        details, count = _run_locked_change(lambda _cur: {"manual": True}, _actor())
        details["entry_count"] = count
        _audit("sync", "ok", details)
        flash("現在の全件をFreePBXへ再反映しました", "success")
    except PhoneWhitelistError as exc:
        _audit("sync", "error", {}, str(exc))
        flash(str(exc), "danger")
    return redirect(url_for("phone_whitelist.index"))
