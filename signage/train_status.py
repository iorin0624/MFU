import json
import os
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user

from app.utils.db import get_db
from app.discord_notifications.repository import get_discord_webhook

from . import train_status_bp
from .train_alert import (
    canonical_route_url,
    catalog_route_map,
    route_setting_key,
    run_train_alert,
    send_test_alert,
)


TRAIN_STATUS_DATA_PATH = Path(
    os.environ.get(
        "TRAIN_STATUS_DATA_PATH",
        "/mnt/mfu/signage_archive/train-status/latest.json",
    )
)
TRAIN_STATUS_SYNC_STATUS_PATH = Path(
    os.environ.get(
        "TRAIN_STATUS_SYNC_STATUS_PATH",
        "/mnt/mfu/signage_archive/train-status/sync-status.json",
    )
)
TRAIN_STATUS_RAW_DATA_PATH = Path(
    os.environ.get(
        "TRAIN_STATUS_RAW_DATA_PATH",
        "/mnt/mfu/signage_archive/train-status/raw.json",
    )
)
TRAIN_STATUS_CATALOG_DATA_PATH = Path(
    os.environ.get(
        "TRAIN_STATUS_CATALOG_DATA_PATH",
        "/mnt/mfu/signage_archive/train-status/catalog.json",
    )
)
TRAIN_ALERT_STATE_PATH = Path(
    os.environ.get(
        "TRAIN_ALERT_STATE_PATH",
        "/mnt/mfu/signage_archive/train-status/alert-state.json",
    )
)
TRAIN_ALERT_STATUS_PATH = Path(
    os.environ.get(
        "TRAIN_ALERT_STATUS_PATH",
        "/mnt/mfu/signage_archive/train-status/alert-status.json",
    )
)
TRAIN_ALERT_CONFIG_SETTING = "SIGNAGE_TRAIN_ALERT_CONFIG"
TRAIN_ALERT_ROUTE_SETTING_PREFIX = "SIGNAGE_TRAIN_ALERT_ROUTE_"
PUBLIC_BASE_URL = os.environ.get("MFU_PUBLIC_BASE_URL", "https://mfu.iori0624.jp").rstrip("/")
TRAIN_STATUS_STALE_SECONDS = 25 * 60


def _is_logged_in():
    return bool(session.get("user")) or bool(
        getattr(current_user, "is_authenticated", False)
    )


def _login_required(api=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if _is_logged_in():
                return view(*args, **kwargs)
            if api:
                response = jsonify({"ok": False, "error": "ログインが必要です。"})
                response.status_code = 401
                response.headers["Cache-Control"] = "no-store"
                return response
            return redirect(url_for("login", next=request.full_path.rstrip("?")))

        return wrapper

    return decorator


def _read_json_object(path):
    try:
        with path.open("r", encoding="utf-8") as source_file:
            value = json.load(source_file)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _admin_discord_webhook():
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT webhook_url
              FROM users
             WHERE username=%s
               AND webhook_url IS NOT NULL
               AND webhook_url <> ''
             LIMIT 1
            """,
            ("admin",),
        )
        row = cursor.fetchone()
        legacy = str((row or {}).get("webhook_url") or "").strip()
        return get_discord_webhook("train_status", legacy)
    finally:
        db.close()


def _train_alert_config():
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT `value` FROM settings WHERE `key`=%s LIMIT 1",
            (TRAIN_ALERT_CONFIG_SETTING,),
        )
        root = cursor.fetchone()
        cursor.execute(
            """
            SELECT `value`
              FROM settings
             WHERE LEFT(`key`, %s)=%s
             ORDER BY `key`
            """,
            (len(TRAIN_ALERT_ROUTE_SETTING_PREFIX), TRAIN_ALERT_ROUTE_SETTING_PREFIX),
        )
        route_rows = cursor.fetchall()
    finally:
        db.close()

    enabled = False
    try:
        root_value = json.loads(str((root or {}).get("value") or "{}"))
        enabled = bool(int(root_value.get("e", 0)))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        current_app.logger.warning("SIGNAGE_TRAIN_ALERT_CONFIG_INVALID using_defaults=1")

    selected_urls = []
    for row in route_rows:
        url = canonical_route_url((row or {}).get("value"))
        if url:
            selected_urls.append(url)
    return {"enabled": enabled, "selected_urls": sorted(set(selected_urls))}


def _save_train_alert_config(enabled, selected_urls):
    db = get_db()
    try:
        cursor = db.cursor()
        cursor.execute(
            "DELETE FROM settings WHERE LEFT(`key`, %s)=%s",
            (len(TRAIN_ALERT_ROUTE_SETTING_PREFIX), TRAIN_ALERT_ROUTE_SETTING_PREFIX),
        )
        if selected_urls:
            cursor.executemany(
                "INSERT INTO settings (`key`, `value`) VALUES (%s, %s)",
                [(route_setting_key(url), url) for url in sorted(selected_urls)],
            )
        cursor.execute(
            """
            INSERT INTO settings (`key`, `value`) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)
            """,
            (TRAIN_ALERT_CONFIG_SETTING, json.dumps({"e": int(enabled)}, separators=(",", ":"))),
        )
        db.commit()
    finally:
        db.close()


def _csrf_token():
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _require_admin():
    if session.get("user") != "admin":
        abort(404)


def _safe_transit_url(value):
    value = str(value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "transit.yahoo.co.jp":
        return None
    return value


def _line_kind(status):
    status = str(status or "")
    if "運転見合わせ" in status or "運転を見合わせ" in status:
        return "suspended"
    if "遅延" in status or "遅れ" in status:
        return "delayed"
    return "other"


def _iso_from_mtime(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
    except OSError:
        return None


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def load_train_status_snapshot(now=None):
    now = now or datetime.now().astimezone()
    payload = _read_json_object(TRAIN_STATUS_DATA_PATH)
    sync_status = _read_json_object(TRAIN_STATUS_SYNC_STATUS_PATH) or {}
    raw_lines = payload.get("lines", []) if payload else []
    lines = []

    if isinstance(raw_lines, list):
        for raw in raw_lines:
            if not isinstance(raw, dict) or raw.get("error"):
                continue
            line_name = str(raw.get("line_name") or "").strip()
            status = str(raw.get("status") or "").strip()
            message = str(raw.get("message") or "").strip()
            if not line_name or not (status or message):
                continue
            affected_source = raw.get("affected_lines")
            affected_lines = []
            if isinstance(affected_source, list):
                affected_lines = sorted(
                    {
                        str(value).strip()
                        for value in affected_source
                        if value is not None and str(value).strip()
                    }
                )
            kind = _line_kind(status)
            lines.append(
                {
                    "line_name": line_name,
                    "notice_id": str(raw.get("notice_id") or "").strip(),
                    "status": status or "運行情報",
                    "message": message,
                    "update_time_text": str(raw.get("update_time_text") or "").strip(),
                    "publish_time": str(raw.get("publish_time") or "").strip(),
                    "affected_lines": affected_lines,
                    "kind": kind,
                }
            )

    priority = {"suspended": 0, "delayed": 1, "other": 2}
    lines.sort(key=lambda line: (priority[line["kind"]], line["line_name"]))
    fetched_at = sync_status.get("fetched_at") or _iso_from_mtime(TRAIN_STATUS_DATA_PATH)
    fetched_dt = _parse_iso(fetched_at)
    if fetched_dt and fetched_dt.tzinfo is None:
        fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
    age_seconds = None
    if fetched_dt:
        age_seconds = max(0, int((now - fetched_dt.astimezone(now.tzinfo)).total_seconds()))
    sync_ok = bool(sync_status.get("ok", payload is not None))
    stale = payload is None or age_seconds is None or age_seconds > TRAIN_STATUS_STALE_SECONDS
    warning = None
    if payload is None:
        warning = "運行情報をまだ取得できていません。"
    elif not sync_ok or stale:
        warning = "最新情報を取得できていません。最後に取得できた情報を表示しています。"

    counts = {
        "total": len(lines),
        "suspended": sum(line["kind"] == "suspended" for line in lines),
        "delayed": sum(line["kind"] == "delayed" for line in lines),
        "other": sum(line["kind"] == "other" for line in lines),
    }
    return {
        "ok": payload is not None,
        "source_updated": str(payload.get("updated") or "").strip() if payload else "",
        "source_url": _safe_transit_url(payload.get("source")) if payload else None,
        "fetched_at": fetched_at,
        "age_seconds": age_seconds,
        "stale": stale,
        "warning": warning,
        "counts": counts,
        "lines": lines,
    }


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@train_status_bp.get("/train-status")
@_login_required()
def index():
    return _no_store(
        make_response(
            render_template(
                "signage/train_status.html",
                snapshot=load_train_status_snapshot(),
            )
        )
    )


@train_status_bp.get("/train-status/data")
@_login_required(api=True)
def data():
    return _no_store(jsonify(load_train_status_snapshot()))


@train_status_bp.post("/train-status/notify/analyze")
def analyze_notifications():
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        abort(404)
    raw_payload = _read_json_object(TRAIN_STATUS_RAW_DATA_PATH)
    summary_payload = _read_json_object(TRAIN_STATUS_DATA_PATH)
    catalog_payload = _read_json_object(TRAIN_STATUS_CATALOG_DATA_PATH)
    if raw_payload is None or summary_payload is None or catalog_payload is None:
        response = jsonify({"ok": False, "error": "通知判定用データを準備中です。"})
        response.status_code = 503
        return _no_store(response)
    result = run_train_alert(
        config=_train_alert_config(),
        raw_payload=raw_payload,
        summary_payload=summary_payload,
        catalog_payload=catalog_payload,
        state_path=TRAIN_ALERT_STATE_PATH,
        status_path=TRAIN_ALERT_STATUS_PATH,
        webhook_url=_admin_discord_webhook(),
        page_url=f"{PUBLIC_BASE_URL}/train-status",
        logger=current_app.logger,
    )
    return _no_store(jsonify({"ok": True, "status": result}))


@train_status_bp.route("/admin/signage/train-status", methods=["GET", "POST"])
def alert_settings():
    _require_admin()
    csrf_token = _csrf_token()
    catalog_payload = _read_json_object(TRAIN_STATUS_CATALOG_DATA_PATH)
    catalog = catalog_route_map(catalog_payload)

    if request.method == "POST":
        supplied_token = str(request.form.get("csrf_token") or "")
        if not supplied_token or not secrets.compare_digest(csrf_token, supplied_token):
            abort(400, "Invalid CSRF token")
        action = str(request.form.get("action") or "")
        if action == "test_notification":
            webhook_url = _admin_discord_webhook()
            if not webhook_url:
                flash("adminアカウントのDiscord Webhookが未設定です。", "danger")
            else:
                try:
                    send_test_alert(webhook_url, f"{PUBLIC_BASE_URL}/train-status")
                    flash("Discordへ鉄道運行情報のテストカードを送信しました。", "success")
                except Exception as exc:
                    current_app.logger.exception("鉄道運行情報のテスト通知に失敗しました。")
                    flash(f"Discordテスト通知に失敗しました：{exc}", "danger")
            return redirect(url_for("train_status.alert_settings"))

        if action != "save_alert_config":
            abort(400, "Unknown action")
        if len(catalog) < 100:
            flash("路線カタログを取得できていないため、設定を保存できません。", "danger")
            return redirect(url_for("train_status.alert_settings"))

        enabled = bool(request.form.get("enabled"))
        submitted = request.form.getlist("selected_route")
        selected_urls = set()
        invalid_count = 0
        for value in submitted:
            url = canonical_route_url(value)
            if not url or url not in catalog:
                invalid_count += 1
                continue
            selected_urls.add(url)
        if invalid_count:
            flash("選択内容に不正な路線が含まれているため、保存しませんでした。", "danger")
            return redirect(url_for("train_status.alert_settings"))
        if enabled and not selected_urls:
            flash("通知を有効にする場合は、監視対象路線を1つ以上選択してください。", "danger")
            return redirect(url_for("train_status.alert_settings"))

        _save_train_alert_config(enabled, selected_urls)
        TRAIN_ALERT_STATE_PATH.unlink(missing_ok=True)
        TRAIN_ALERT_STATUS_PATH.unlink(missing_ok=True)
        current_app.logger.info(
            "SIGNAGE_TRAIN_ALERT_CONFIG_UPDATED actor=admin enabled=%s route_count=%s",
            int(enabled),
            len(selected_urls),
        )
        flash(
            "鉄道運行情報の通知設定を保存しました。次回取得時に現在状態を基準として記録します。",
            "success",
        )
        return redirect(url_for("train_status.alert_settings"))

    config = _train_alert_config()
    selected = set(config["selected_urls"])
    groups = {
        "kanto": sorted(
            (route for route in catalog.values() if route["group"] == "kanto"),
            key=lambda route: route["line_name"],
        ),
        "shinkansen": sorted(
            (route for route in catalog.values() if route["group"] == "shinkansen"),
            key=lambda route: route["line_name"],
        ),
    }
    response = make_response(
        render_template(
            "signage/train_alert_settings.html",
            config=config,
            selected_urls=selected,
            route_groups=groups,
            catalog_count=len(catalog),
            discord_configured=bool(_admin_discord_webhook()),
            alert_status=_read_json_object(TRAIN_ALERT_STATUS_PATH) or {},
            sync_status=_read_json_object(TRAIN_STATUS_SYNC_STATUS_PATH) or {},
            csrf_token_value=csrf_token,
        )
    )
    return _no_store(response)


@train_status_bp.get("/admin/signage/train-status/status")
def alert_status():
    _require_admin()
    return _no_store(
        jsonify(
            {
                "ok": True,
                "status": _read_json_object(TRAIN_ALERT_STATUS_PATH) or {},
                "sync": _read_json_object(TRAIN_STATUS_SYNC_STATUS_PATH) or {},
            }
        )
    )
