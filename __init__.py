# =====================================
# 🔧 標準ライブラリ（上段に集約・アルファベット順）
# =====================================
import base64
import hmac
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import smtplib
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date as date_cls, timedelta, timezone
from email.mime.text import MIMEText
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse

# =====================================
# 🌐 外部ライブラリ（上段に集約）
# =====================================
import bcrypt
import psutil
import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from PIL import Image  # （将来の画像操作に備え、既存どおり保持）

# Flask & Werkzeug
from flask import (
    Flask, request, session, redirect, render_template, url_for, flash,
    send_from_directory, send_file, abort, jsonify, current_app, after_this_request, g, Response,
)
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename, safe_join

# =====================================
# 🛠️ アプリ内ユーティリティ（上段に集約）
# =====================================
from app.utils.auth import load_user
from app.utils.db import get_db
from app.utils.feature_access import (
    ensure_feature_access_schema,
    get_allowed_features,
    get_nav_items_for_user,
    has_feature,
    require_feature,
)
from app.utils.file_ops import generate_thumbnail, create_zip
from app.utils.upload_security import (
    DEFAULT_ALLOWED_EXTENSIONS,
    can_access_upload_record,
    cleanup_legacy_view_auth_keys,
    detect_mime_from_bytes,
    ensure_upload_password_schema,
    fetch_upload_access_record,
    grant_view_auth,
    has_view_auth,
    hash_upload_password,
    migrate_upload_password_if_needed,
    resolve_upload_subpath,
    sanitize_filename,
    validate_upload_file,
    verify_upload_password,
)
from app.utils.image import save_as_jpeg
from app.utils.logs import log_request_raw, get_fw_404_settings, save_fw_404_settings
from app.utils.message import generate_message
from app.utils.storage_info import get_storage_info
from app.utils.thumbs import enqueue_thumb_job
from app.utils.totp_util import get_totp_status
from app.utils.whois_util import get_netinfo
from app.albums import album_bp
from app.receipts import receipts_bp
from app.utils.mail import send_mail
from app.utils.upload_notifications import send_discord_upload_notification
from app.utils.fw_ban import ban_ip_cidr_via_ssh, normalize_ip_target
from app.chat.socketio_ext import socketio

# =====================================
# 🌏 タイムゾーン・定数
# =====================================
JST = timezone(timedelta(hours=9))
APP_DIR = os.path.abspath(os.path.dirname(__file__))
BASE_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))
STATIC_DIR = os.path.abspath(os.path.join(APP_DIR, "static"))
UPLOAD_BASE_DIR = os.path.join(BASE_DIR, "uploads")
tempfile.tempdir = "/mnt/mfu/tmp"  # 明示

INAPP_BROWSER_WARNING_ENABLED_KEY = "inapp_browser_warning_enabled"
INAPP_BROWSER_KEYWORDS_KEY = "inapp_browser_keywords"
INAPP_BROWSER_REFERRER_PREFIXES_KEY = "inapp_browser_referrer_prefixes"
INAPP_BROWSER_SKIP_PATHS_KEY = "inapp_browser_skip_paths"
INAPP_BROWSER_DEFAULT_ENABLED = "1"
INAPP_BROWSER_DEFAULT_KEYWORDS = ["Line/", "Instagram", "Twitter", "FBAN", "FBAV"]
INAPP_BROWSER_DEFAULT_REFERRER_PREFIXES = ["https://t.co/"]
INAPP_BROWSER_DEFAULT_SKIP_PATHS = [
    "/static",
    "/favicon",
    "/api",
    "/admin",
    "/maintenance",
    "/suc",
    "/external-login/",
    "/e/",
]
INAPP_BROWSER_SETTINGS_DEFAULTS = {
    INAPP_BROWSER_WARNING_ENABLED_KEY: INAPP_BROWSER_DEFAULT_ENABLED,
    INAPP_BROWSER_KEYWORDS_KEY: "\n".join(INAPP_BROWSER_DEFAULT_KEYWORDS),
    INAPP_BROWSER_REFERRER_PREFIXES_KEY: "\n".join(INAPP_BROWSER_DEFAULT_REFERRER_PREFIXES),
    INAPP_BROWSER_SKIP_PATHS_KEY: "\n".join(INAPP_BROWSER_DEFAULT_SKIP_PATHS),
}

# =====================================
# 🚀 Flask アプリ構成
# =====================================
app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, "templates"),
    static_folder=STATIC_DIR,
    static_url_path="/static",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


@app.get("/manifest.webmanifest")
def root_manifest():
    resp = jsonify(
        {
            "name": "Mimoria",
            "short_name": "Mimoria",
            "start_url": "/external-login/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#0d6efd",
            "icons": [
                {
                    "src": "/static/icons/image-size192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/static/icons/image-size512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        }
    )
    resp.mimetype = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

def create_app():
    return app

load_dotenv()
app.secret_key = os.environ.get("SECRET_KEY")


def _resolve_socketio_message_queue():
    queue_url = (os.environ.get("SOCKETIO_MESSAGE_QUEUE") or "").strip()
    if not queue_url:
        return None

    try:
        import importlib
        if importlib.util.find_spec("redis") is None:
            app.logger.warning("SOCKETIO_MESSAGE_QUEUE is set but redis package is not installed; fallback to local mode")
            return None
        redis = importlib.import_module("redis")
        redis.Redis.from_url(queue_url, socket_connect_timeout=1, socket_timeout=1).ping()
        return queue_url
    except Exception as exc:
        app.logger.warning("SOCKETIO_MESSAGE_QUEUE connection failed; fallback to local mode: %s", exc)
        return None


app.config["SOCKETIO_MESSAGE_QUEUE"] = _resolve_socketio_message_queue()

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=60)

app.config["SESSION_COOKIE_SECURE"] = True            # HTTPSのみ送信
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"         # CSRF対策の基本ライン

app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "900")) * 1024 * 1024
app.config["PAYOUT_PUBLIC_BASE_URL"] = os.environ.get("PAYOUT_PUBLIC_BASE_URL", "https://mfu.iori0624.jp").strip()
app.config["INVOICE_PDF_FONT_REGULAR"] = os.environ.get("INVOICE_PDF_FONT_REGULAR", "").strip()
app.config["INVOICE_PDF_FONT_BOLD"] = os.environ.get("INVOICE_PDF_FONT_BOLD", "").strip()

# 既定ホワイトリスト（必要なら config で上書き）
app.config.setdefault("UPLOAD_ALLOWED_EXTENSIONS", DEFAULT_ALLOWED_EXTENSIONS)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.user_loader(load_user)
socketio.init_app(app, message_queue=app.config["SOCKETIO_MESSAGE_QUEUE"])

if app.config["SOCKETIO_MESSAGE_QUEUE"]:
    app.logger.info("Socket.IO message queue enabled")
else:
    app.logger.warning("Socket.IO message queue is disabled; multi-worker chat delivery may fail")

# =====================================
# 🧠 補助関数群（上段へ集約）
# =====================================

from functools import wraps
from flask import session

def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("user") != "admin":
            return "管理者のみアクセス可能", 403
        return func(*args, **kwargs)
    return wrapper


def _cleanup_legacy_view_auth_keys(current_uuid=None):
    cleanup_legacy_view_auth_keys(current_uuid)


def _grant_view_auth(uuid):
    """閲覧許可を単一キー配下のUUID配列で管理する。"""
    grant_view_auth(uuid)


def _has_view_auth(uuid):
    """新旧のセッション形式を読み、必要なら新形式へ移行する。"""
    return has_view_auth(uuid)


CSRF_SESSION_KEY = "csrf_token"
_UPLOAD_SECURITY_SCHEMA_LOCK = threading.Lock()
_upload_security_schema_ready = False
_CSRF_PROTECTED_PREFIXES = (
    "/admin/users",
    "/admin/user-features",
    "/admin/features",
    "/admin/nav",
    "/admin/logs/404-ban",
    "/admin/mail-delivery/refresh",
    "/admin/maintenance",
    "/admin/settings/inapp-browser",
    "/admin/restart",
    "/admin/logs/export",
    "/templates",
    "/modes",
    "/upload_delete/",
)
_CSRF_PROTECTED_PATHS = {
    "/submit_upload",
    "/api/zip-stream",
    "/admin/fw/ban",
}
_CSRF_EXEMPT_PATHS = {
    "/login",
}


def _ensure_upload_security_schema_once():
    global _upload_security_schema_ready
    if _upload_security_schema_ready:
        return
    with _UPLOAD_SECURITY_SCHEMA_LOCK:
        if _upload_security_schema_ready:
            return
        ensure_upload_password_schema()
        _upload_security_schema_ready = True


def _get_upload_access_record(uuid):
    return fetch_upload_access_record(uuid)


def _can_access_upload_record(upload):
    return can_access_upload_record(upload, has_view_auth_func=_has_view_auth)


def _can_access_upload_uuid(uuid):
    upload = _get_upload_access_record(uuid)
    return bool(upload and _can_access_upload_record(upload))


def _get_csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _is_same_origin_request():
    expected = urlparse(request.host_url)
    for header_name in ("Origin", "Referer"):
        raw = request.headers.get(header_name)
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            return False
    return True


def _parse_comparable_source_url(raw_value):
    value = (raw_value or "").strip()
    if not value or value.lower() == "null":
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed


def _check_same_origin_from_headers():
    expected = urlparse(request.host_url or "")
    header_results = {}
    for header_name in ("Origin", "Referer"):
        parsed = _parse_comparable_source_url(request.headers.get(header_name))
        if not parsed:
            header_results[header_name] = "unavailable"
            continue
        is_match = parsed.scheme == expected.scheme and parsed.netloc == expected.netloc
        header_results[header_name] = "match" if is_match else "mismatch"
        if not is_match:
            return False, header_results
    return True, header_results


def _mask_csrf_token(token):
    token_str = str(token or "")
    if not token_str:
        return {"exists": False, "length": 0, "sha256_prefix": ""}
    return {
        "exists": True,
        "length": len(token_str),
        "sha256_prefix": hashlib.sha256(token_str.encode("utf-8")).hexdigest()[:8],
    }


def _log_csrf_debug(reason, **extra):
    expected = urlparse(request.host_url or "")
    origin_raw = request.headers.get("Origin")
    referer_raw = request.headers.get("Referer")
    origin_parsed = urlparse(origin_raw) if origin_raw else None
    referer_parsed = urlparse(referer_raw) if referer_raw else None
    payload = {
        "reason": reason,
        "path": request.path,
        "method": request.method,
        "host": request.host,
        "host_url": request.host_url,
        "url": request.url,
        "scheme": request.scheme,
        "is_secure": request.is_secure,
        "header_host": request.headers.get("Host"),
        "header_origin": origin_raw,
        "header_referer": referer_raw,
        "x_forwarded_proto": request.headers.get("X-Forwarded-Proto"),
        "x_forwarded_host": request.headers.get("X-Forwarded-Host"),
        "x_forwarded_port": request.headers.get("X-Forwarded-Port"),
        "x_forwarded_for": request.headers.get("X-Forwarded-For"),
        "remote_addr": request.remote_addr,
        "endpoint": request.endpoint,
        "blueprint": request.blueprint,
        "session_csrf_exists": bool(session.get(CSRF_SESSION_KEY)),
        "form_csrf_exists": bool(request.form.get("csrf_token")),
        "header_csrf_exists": bool(request.headers.get("X-CSRF-Token")),
        "expected_scheme": expected.scheme,
        "expected_netloc": expected.netloc,
        "origin_scheme": origin_parsed.scheme if origin_parsed else "",
        "origin_netloc": origin_parsed.netloc if origin_parsed else "",
        "referer_scheme": referer_parsed.scheme if referer_parsed else "",
        "referer_netloc": referer_parsed.netloc if referer_parsed else "",
    }
    payload.update(extra or {})
    app.logger.warning("CSRF_DEBUG %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _is_json_error_response():
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"


def _csrf_error(message, status=403):
    if _is_json_error_response():
        return jsonify({"ok": False, "error": "csrf_failed", "message": message}), status
    return message, status


def _requires_csrf_protection():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if request.path in _CSRF_EXEMPT_PATHS:
        return False
    if request.endpoint == "view_upload":
        # 公開導線のパスワードフォームは今回のCSRF必須対象外。
        return False
    if request.path in _CSRF_PROTECTED_PATHS:
        return True
    return request.path.startswith(_CSRF_PROTECTED_PREFIXES)


def _validate_csrf_request():
    session_token = session.get(CSRF_SESSION_KEY) or ""
    header_token = request.headers.get("X-CSRF-Token") or ""
    form_token = request.form.get("csrf_token") or ""
    json_token = ((request.get_json(silent=True) or {}).get("csrf_token") if request.is_json else "") or ""
    request_token = header_token or form_token or json_token or ""

    is_same_origin, origin_check = _check_same_origin_from_headers()
    if not is_same_origin:
        _log_csrf_debug(
            "origin_mismatch",
            origin_check=origin_check,
            session_token_meta=_mask_csrf_token(session_token),
            request_token_meta=_mask_csrf_token(request_token),
            header_token_meta=_mask_csrf_token(header_token),
            form_token_meta=_mask_csrf_token(form_token),
            json_token_meta=_mask_csrf_token(json_token),
        )
        return _csrf_error("CSRF origin check failed", 403)

    if not session_token or not request_token:
        _log_csrf_debug(
            "missing_token",
            origin_check=origin_check,
            session_token_meta=_mask_csrf_token(session_token),
            request_token_meta=_mask_csrf_token(request_token),
            header_token_meta=_mask_csrf_token(header_token),
            form_token_meta=_mask_csrf_token(form_token),
            json_token_meta=_mask_csrf_token(json_token),
        )
        return _csrf_error("CSRF token is missing", 400)
    if not hmac.compare_digest(str(session_token), str(request_token)):
        _log_csrf_debug(
            "invalid_token",
            origin_check=origin_check,
            session_token_meta=_mask_csrf_token(session_token),
            request_token_meta=_mask_csrf_token(request_token),
            header_token_meta=_mask_csrf_token(header_token),
            form_token_meta=_mask_csrf_token(form_token),
            json_token_meta=_mask_csrf_token(json_token),
        )
        return _csrf_error("CSRF token is invalid", 403)
    if origin_check.get("Origin") == "unavailable" and origin_check.get("Referer") == "unavailable":
        _log_csrf_debug(
            "origin_unavailable_token_valid",
            origin_check=origin_check,
            session_token_meta=_mask_csrf_token(session_token),
            request_token_meta=_mask_csrf_token(request_token),
            header_token_meta=_mask_csrf_token(header_token),
            form_token_meta=_mask_csrf_token(form_token),
            json_token_meta=_mask_csrf_token(json_token),
        )
    return None


@app.context_processor
def inject_csrf_token():
    return {
        "csrf_token": _get_csrf_token,
        "csrf_token_value": _get_csrf_token(),
    }


def _save_stream(file_storage, dest_path):
    """アップロードストリームを保存（最小実装）"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    file_storage.save(dest_path)
    return os.path.basename(dest_path)

def delayed_restart():
    try:
        print("[delayed_restart] 🔁 サービス再起動処理を開始")
        time.sleep(2)
        print("[delayed_restart] 📤 systemctl restart 実行中...")
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "mfu.service"],
            check=True,
            capture_output=True,
            text=True,
        )
        print("[delayed_restart] ✅ 再起動成功")
        print("[stdout]", result.stdout)
        print("[stderr]", result.stderr)
    except subprocess.CalledProcessError as e:
        print("[delayed_restart] ❌ 再起動失敗（subprocess.CalledProcessError）")
        print("[stderr]", e.stderr)
    except Exception as e:
        print(f"[delayed_restart] ❌ 再起動中に予期せぬエラー: {e}")

def is_maintenance_mode():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_mode'")
    row = cursor.fetchone()
    db.close()
    return row and row["value"] == "on"

def _normalize_multiline_list(lines):
    normalized = []
    seen = set()
    for line in lines or []:
        item = (line or "").strip()
        if not item:
            continue
        dedupe_key = item.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(item)
    return normalized


def _serialize_multiline_setting(lines):
    return "\n".join(_normalize_multiline_list(lines))


def _ensure_inapp_browser_settings_defaults():
    db = get_db()
    cursor = db.cursor()
    created = False
    try:
        for key, default_value in INAPP_BROWSER_SETTINGS_DEFAULTS.items():
            cursor.execute(
                "INSERT IGNORE INTO settings (`key`, `value`) VALUES (%s, %s)",
                (key, default_value),
            )
            created = created or bool(cursor.rowcount)
        if created:
            db.commit()
    finally:
        db.close()


def _get_setting_value(key, default=None):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT `value` FROM settings WHERE `key` = %s", (key,))
        row = cursor.fetchone()
        if not row:
            return default
        value = row.get("value")
        return default if value is None else value
    finally:
        db.close()


def _get_multiline_setting_list(key, default_list):
    value = _get_setting_value(key)
    if value is None or not str(value).strip():
        return _normalize_multiline_list(default_list)
    return _normalize_multiline_list(str(value).splitlines())


def _is_inapp_browser_request(req):
    skip_paths = _get_multiline_setting_list(INAPP_BROWSER_SKIP_PATHS_KEY, INAPP_BROWSER_DEFAULT_SKIP_PATHS)
    if any(req.path.startswith(path) for path in skip_paths):
        return False

    ua = (req.headers.get("User-Agent") or "").lower()
    referer = (req.headers.get("Referer") or "").lower()
    keywords = _get_multiline_setting_list(INAPP_BROWSER_KEYWORDS_KEY, INAPP_BROWSER_DEFAULT_KEYWORDS)
    referrer_prefixes = _get_multiline_setting_list(
        INAPP_BROWSER_REFERRER_PREFIXES_KEY,
        INAPP_BROWSER_DEFAULT_REFERRER_PREFIXES,
    )

    if any(keyword.lower() in ua for keyword in keywords):
        return True

    if any(referer.startswith(prefix.lower()) for prefix in referrer_prefixes):
        return True

    return req.cookies.get("InAppView") == "1"


def write_login_log(username, ip, tag="LOGIN"):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
        (ip, f"[{tag}] ユーザー: {username} がログインしました"),
    )
    db.commit()
    db.close()

def get_vcgencmd_info():
    """
    RPi専用の vcgencmd が無い環境（x86等）でも落ちずに情報を返す互換関数。
    優先: vcgencmd → psutil + lm-sensors の順。
    戻り値のキーは既存互換（temperature/voltage/throttled/clock）。
    """
    def run(cmd):
        try:
            return subprocess.check_output(["vcgencmd"] + cmd.split(), timeout=2).decode().strip()
        except Exception:
            return None

    # まず Raspberry Pi (vcgencmd) を試す
    t = run("measure_temp")
    if t is not None:
        v   = run("measure_volts") or "N/A"
        th  = run("get_throttled") or "throttled=0x0"
        clk = run("measure_clock arm") or ""
        return {"temperature": t, "voltage": v, "throttled": th, "clock": clk}

    # ここから x86 等の汎用パス（psutil/lm-sensors）
    # 温度
    temp_str = "取得不可"
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False) or {}
        cand = None
        for key in ("coretemp", "k10temp", "acpitz", "cpu-thermal"):
            if key in temps and temps[key]:
                vals = [x.current for x in temps[key] if isinstance(x.current, (int, float))]
                if vals:
                    cand = sum(vals) / len(vals)
                    break
        if cand is not None:
            temp_str = f"temp={cand:.1f}'C"
    except Exception:
        pass

    # 電圧・スロットルは非対応（RPi専用）
    volt_str = "N/A"
    throttled = "non-rpi"

    # 周波数
    try:
        f = psutil.cpu_freq()
        clock = f"frequency({int(f.current)}MHz)" if f else "frequency(unknown)"
    except Exception:
        clock = "frequency(unknown)"

    return {"temperature": temp_str, "voltage": volt_str, "throttled": throttled, "clock": clock}

def auto_end_maintenance():
    try:
        app.logger.info("🔁 メンテ時間到達 → モードOFF＆再起動フラグ作成")
        db = get_db()
        cursor = db.cursor()
        cursor.execute("UPDATE settings SET value = 'off' WHERE `key` = 'maintenance_mode'")
        cursor.execute("DELETE FROM settings WHERE `key` = 'maintenance_until'")
        db.commit()
        db.close()
        Path("/tmp/mfu_restart.flag").touch()
    except Exception as e:
        app.logger.error(f"[Auto Restart Error] {e}")

def schedule_restart_if_needed():
    try:
        db = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_mode'")
        mode = cursor.fetchone()
        cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_until'")
        until_row = cursor.fetchone()
        db.close()

        if mode and mode["value"] == "on" and until_row and until_row["value"]:
            try:
                utc_dt = dateutil_parser.isoparse(until_row["value"])
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                delay_sec = (utc_dt - now).total_seconds()
                if delay_sec > 0:
                    app.logger.info(f"⏱️ メンテ終了まで {delay_sec:.1f}秒 → 自動再起動をスケジュール")
                    threading.Timer(delay_sec, auto_end_maintenance).start()
            except Exception as e:
                app.logger.warning(f"[Timer Error] {e}")
    except Exception as e:
        app.logger.warning(f"[Schedule Init Error] {e}")

def check_and_create_flag():
    try:
        db = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_mode'")
        mode = cursor.fetchone()
        cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_until'")
        until_row = cursor.fetchone()
        cursor.close()
        db.close()

        if not mode or mode["value"] != "on":
            return
        if not until_row or not until_row["value"]:
            return

        until_utc = dateutil_parser.isoparse(until_row["value"]).replace(tzinfo=timezone.utc)
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

        if now_utc >= until_utc:
            flag_path = "/tmp/mfu_restart.flag"
            if not os.path.exists(flag_path):
                with open(flag_path, "w") as f:
                    f.write("1\n")
    except Exception as e:
        print(f"[Watcher Error] {e}")

def _cfg_storage_root():
    return current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")

def _cfg_albums_root():
    return current_app.config.get("ALBUMS_ROOT", "/mnt/mfu/mfu_albums")

def _cfg_tmp_root():
    return os.environ.get("TMPDIR", "/tmp")

def _progress_dir():
    d = os.path.join(_cfg_tmp_root(), "mfu-progress")
    os.makedirs(d, exist_ok=True)
    return d

def _progress_path(key: str):
    return os.path.join(_progress_dir(), f"{key}.json")

def _lock_path(key: str):
    return os.path.join(_progress_dir(), f"{key}.lock")

def _progress_write(key: str, data: dict):
    p = _progress_path(key)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)

def _progress_read(key: str):
    p = _progress_path(key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _progress_clear(key: str):
    for path in (_progress_path(key), _lock_path(key)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# UUID path 解析
_UUID32_RE  = re.compile(r"^[0-9a-f]{32}$")
_UUID4_RE   = re.compile(r"^[0-9a-fA-F-]{36}$")

def _resolve_relpath(rel: str):
    """
    受け取った相対パスを実ファイルパスに解決して返す。
      - uploads: <uuid32>/(original|thumb)/<filename>
      - albums : albums/<uuid4>/<uuid4>/<filename>
    許可しないものは None。
    """
    if not rel:
        return None
    rel = rel.lstrip("/").replace("\\", "/")

    # アルバム
    if rel.startswith("albums/"):
        parts = rel.split("/", 3)
        if len(parts) != 4:
            return None
        _, album_id, child_id, fname = parts
        if not (_UUID4_RE.match(album_id) and _UUID4_RE.match(child_id)):
            return None
        base = _cfg_albums_root()
        full = safe_join(base, album_id, child_id, fname)
        if not full:
            return None
        full = os.path.realpath(full)
        if not full.startswith(os.path.realpath(base) + os.sep):
            return None
        return full

    # 通常アップロード
    parts = rel.split("/", 2)
    if len(parts) != 3:
        return None
    uuid32, kind, fname = parts
    if not (_UUID32_RE.match(uuid32) and kind in ("original", "thumb")):
        return None
    base = _cfg_storage_root()
    full = safe_join(base, uuid32, kind, fname)
    if not full:
        return None
    full = os.path.realpath(full)
    if not full.startswith(os.path.realpath(base) + os.sep):
        return None
    return full

# =====================================
# 🔁 起動時のメンテ自動スケジュール
# =====================================
schedule_restart_if_needed()

# =====================================
# ① 認証／トップ
# =====================================
from flask import request, redirect, url_for

@login_manager.unauthorized_handler
def _unauthorized():
    path = request.full_path or request.path or "/"
    # 外部ログイン系は LINE へ（元URLを next に積む）
    if request.path.startswith("/external-login"):
        return redirect(url_for("external_login_user.line_login", next=request.full_path))
    # それ以外（管理系など）は従来どおり
    return redirect(url_for("login", next=path))

WELL_KNOWN_DIR = os.path.join(BASE_DIR, ".well-known")

@app.route("/.well-known/<path:filename>")
def well_known(filename):
    return send_from_directory(WELL_KNOWN_DIR, filename)

@app.route("/")
def index():
    return redirect(url_for("upload"))

@app.route("/login", methods=["GET", "POST"])
def login():
    def _preauth_active():
        preauth_user = session.get("preauth_user")
        expires_at = session.get("preauth_expires_at")
        if not preauth_user or not expires_at:
            return None
        if datetime.now() > expires_at:
            session.pop("preauth_user", None)
            session.pop("preauth_expires_at", None)
            session.pop("preauth_totp_attempts", None)
            session.pop("preauth_totp_locked_until", None)
            return None
        return preauth_user

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        client_ip = ip_address(request.remote_addr)

        # ローカル判定
        is_local = (
            client_ip in ip_network("192.168.103.0/24")
            or client_ip in ip_network("fe80::/10")
            or client_ip in ip_network("2404:7a81:bc40:2a00::/64")
            or client_ip in ip_network("2404:7a81:8ac1:1000::/64")
        )

        db = get_db()
        cursor = db.cursor(dictionary=True)

        session.clear()
        cursor.execute(
            "SELECT password_hash, nickname, webhook_url FROM users WHERE username = %s",
            (username,),
        )
        row = cursor.fetchone()
        db.close()

        if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            totp_status = get_totp_status(username)
            if totp_status.get("enabled") and totp_status.get("has_secret") and not is_local:
                session["preauth_user"] = username
                session["preauth_expires_at"] = datetime.now() + timedelta(minutes=5)
                session.pop("preauth_totp_attempts", None)
                session.pop("preauth_totp_locked_until", None)
                return render_template(
                    "login.html",
                    preauth_active=True,
                    preauth_username=username,
                    info="アプリOTPを入力してください。",
                )

            session["user"] = username
            session["nickname"] = row["nickname"]
            session.permanent = True
            write_login_log(username, request.remote_addr)

            if username == "admin" and row.get("webhook_url"):
                try:
                    login_time = datetime.now().strftime("%Y/%m/%d %H:%M")
                    login_ip = request.remote_addr
                    message = (
                        "👤 **管理者ログイン**\n"
                        f"📅 ログイン日時: {login_time}\n"
                        f"🌐 ログインIP: {login_ip}"
                    )
                    requests.post(row["webhook_url"], json={"content": message})
                except Exception:
                    pass

            return redirect(url_for("upload"))

        return render_template("login.html", error="ログイン失敗", username=username)

    preauth_user = _preauth_active()
    return render_template(
        "login.html",
        preauth_active=bool(preauth_user),
        preauth_username=preauth_user or "",
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# =====================================
# ② アップロード（画面 & 実処理）
# =====================================
@app.route("/upload")
def upload():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT mode, label FROM upload_modes WHERE username = %s", (username,))
    modes = cursor.fetchall()

    cursor.execute("SELECT default_mode FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    default_mode = user["default_mode"] if user and user["default_mode"] else ""

    storage = get_storage_info("/mnt/mfu") if username == "admin" else None
    vcgencmd = get_vcgencmd_info() if username == "admin" else None

    db.close()
    return render_template(
        "upload.html",
        modes=modes,
        default_mode=default_mode,
        storage=storage,
        vcgencmd=vcgencmd,
    )

@app.route("/submit_upload", methods=["POST"])
def submit_upload():
    # 依存はこのルート内で完結
    import os, re, shutil, threading, secrets, json
    from uuid import uuid4
    from datetime import datetime, timedelta, date as date_cls
    from concurrent.futures import ThreadPoolExecutor
    from flask import current_app

    if "user" not in session:
        return redirect(url_for("login"))

    title = request.form.get("title", "")
    date = request.form.get("date", datetime.now().strftime("%Y-%m-%d"))
    mode = request.form.get("mode", "")
    uploaded_files = request.files.getlist("photos")
    expire_at = (datetime.now() + timedelta(days=60)).date()
    expire_str = expire_at.strftime("%Y年%m月%d日")
    username = session.get("user", "default")

    if not uploaded_files:
        return "ファイルが選択されていません", 400

    # --- モード・ユーザ情報取得 ---
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM upload_modes WHERE username = %s AND mode = %s", (username, mode))
    mode_config = cursor.fetchone()
    cursor.execute("SELECT nickname, webhook_url, email, notify_method FROM users WHERE username = %s", (username,))
    user_info = cursor.fetchone()
    db.close()
    if not mode_config:
        return f"未定義のモードです: {mode}", 400

    nickname = (user_info or {}).get("nickname") or username

    # ▼ テンプレキー（未設定なら mode をそのまま使う）
    template_key = (mode_config.get("template_key") or "").strip() or mode

    # ▼ サムネ生成フラグ（1/0, '1'/'0', True/False いずれでも解釈）
    gt_val = mode_config.get("generate_thumbnails", 1)
    gen_thumbs = str(gt_val).lower() in ("1", "true", "t", "yes", "y")

    # =====================================
    # ① 事前準備
    # =====================================
    uid = uuid4().hex
    # パスワードはモード設定に従う（未指定なら空）
    password = secrets.token_hex(4) if mode_config.get("require_password") else ""
    password_hash = hash_upload_password(password) if password else None

    # 保存ルート（設定優先、なければ既定）
    storage_root = current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")
    base_dir = os.path.join(storage_root, uid)
    original_dir = os.path.join(base_dir, "original")
    thumb_dir = os.path.join(base_dir, "thumb")
    os.makedirs(original_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)

    # ファイル名の重複回避セット
    used_names = set()

    # =====================================
    # ② 保存前検証（拡張子・二重拡張子・MIME）
    # =====================================
    allowed_extensions = current_app.config.get("UPLOAD_ALLOWED_EXTENSIONS", DEFAULT_ALLOWED_EXTENSIONS)
    validated_files = []
    for fs in uploaded_files:
        original_name = sanitize_filename(fs.filename, used_names)
        head = fs.stream.read(8192)
        fs.stream.seek(0)

        detected_mime = detect_mime_from_bytes(head)
        ok, reason = validate_upload_file(
            filename=original_name,
            header_mime=fs.mimetype,
            detected_mime=detected_mime,
            allowed_extensions=allowed_extensions,
        )
        if not ok:
            return f"不正なファイルが含まれています ({original_name}): {reason}", 400

        validated_files.append((fs, original_name))

    # =====================================
    # ③ 保存処理
    # =====================================
    filenames, failed = [], []
    saved_count = 0

    def save_file_chunked(file_storage, save_path):
        try:
            with open(save_path, "wb") as f:
                shutil.copyfileobj(file_storage.stream, f, length=1 * 1024 * 1024)
            os.chmod(save_path, 0o640)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to save {file_storage.filename}: {e}")
            return False

    # ディレクトリ権限: rwx(rw-)---
    os.chmod(base_dir, 0o750)
    os.chmod(original_dir, 0o750)
    os.chmod(thumb_dir, 0o750)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for fs, original_name in validated_files:
            save_path = os.path.join(original_dir, original_name)
            futures.append((original_name, executor.submit(save_file_chunked, fs, save_path)))
        for original_name, fut in futures:
            if fut.result():
                filenames.append(original_name)
                saved_count += 1
            else:
                failed.append(original_name)

    # =====================================
    # ④ DB登録（uploads / files）
    #   ※ files には created_at 列が無い前提で INSERT を修正
    # =====================================
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO uploads (uuid, title, date, expire_at, mode, username, zip_filename, password, password_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (uid, title, date, expire_at, mode, username, "", password, password_hash),
    )
    upload_id = cur.lastrowid
    if filenames:
        cur.executemany(
            "INSERT INTO files (upload_id, filename) VALUES (%s, %s)",
            [(upload_id, name) for name in filenames],
        )
    db.commit()
    db.close()

    # =====================================
    # ⑤ テンプレートメッセージ生成＆保存（messages）
    # =====================================
    public_base = current_app.config.get("PUBLIC_BASE_URL")
    if not public_base:
        try:
            public_base = PUBLIC_BASE_URL  # グローバル定義があれば使用
        except NameError:
            public_base = request.url_root.rstrip("/")

    context = {
        "uid": uid,
        "title": title,
        "date": (date.strftime("%Y-%m-%d") if isinstance(date, (datetime, date_cls)) else str(date or "")),
        "expire": expire_str,
        "username": username,
        "nickname": nickname,
        "base_url": public_base.rstrip("/"),
        "link": f"{public_base.rstrip('/')}/view/{uid}" if mode_config.get("enable_download_url") else "",
        "download_url": f"{public_base.rstrip('/')}/d/{uid}",
        "manage_url": f"{public_base.rstrip('/')}/m/{uid}",
        "layer_upload_url": f"{public_base.rstrip('/')}/layer_upload/{uid}" if mode_config.get("enable_layer_upload_url") else "",
        "password": password or "",
        "count": saved_count,
    }

    try:
        message = generate_message(template_key, context, username=username)
    except Exception as e:
        message = f"[テンプレ生成失敗: {e}]"

    db = get_db(); cur = db.cursor()
    cur.execute("REPLACE INTO messages (uuid, mode, message) VALUES (%s, %s, %s)", (uid, template_key, message))
    db.commit(); db.close()

    # =====================================
    # ⑥ バックグラウンド：サムネ生成 → 通知
    # =====================================
    app_obj = current_app._get_current_object()

    def _runner():
        try:
            with app_obj.app_context():
                background_thumb_and_notify(
                    uid=uid,
                    filenames=filenames,
                    original_dir=original_dir,
                    thumb_dir=thumb_dir,
                    mode=template_key,      # テンプレキーに統一
                    context=context,
                    gen_thumbs=gen_thumbs
                )
        except Exception as e:
            try:
                app_obj.logger.warning(f"[submit_upload] background failed: {e}")
            except Exception:
                print(f"[submit_upload] background failed: {e}")

    threading.Thread(target=_runner, daemon=True).start()

    # =====================================
    # ⑦ 完了画面
    # =====================================
    return render_template(
        "done.html",
        uuid=uid, password=password, title=title,
        mode=mode, mode_label=mode_config.get("label", mode),
        date=date, message=message,
    )

# --- サムネ完了待ち → 通知（バックグラウンド） ---
def background_thumb_and_notify(uid, filenames, original_dir, thumb_dir, mode, context, gen_thumbs: bool):
    logger = getattr(app, "logger", None)

    # ▼ サムネ生成OFFならキュー投入も待機もスキップ
    if not gen_thumbs:
        try:
            db = get_db()
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT webhook_url, email, notify_method FROM users WHERE username = %s", (context["username"],))
            user = cursor.fetchone()
            db.close()

            notify_method = user.get("notify_method", "discord") if user else "discord"
            base_msg = generate_message(mode, context, username=context["username"])
            msg = base_msg + "\n（サムネイル生成はスキップしました）"

            send_discord_upload_notification(
                logger=(logger or app.logger),
                username=context["username"],
                notify_method=notify_method,
                webhook_url=user.get("webhook_url") if user else "",
                upload_id=uid,
                message=msg,
                context_label="normal upload",
            )

            if notify_method in ("email", "both") and user and user.get("email"):
                try:
                    # ★ mail.pyに統一
                    send_mail(
                        to=user["email"],
                        subject="ファイルアップロード通知",
                        body=msg,
                        event_uuid="notify",          # From: notify@mail.iori0624.jp
                        smtp_host="192.168.103.15",
                        smtp_port=25,
                        timeout=45,
                    )
                    (logger.info if logger else print)(f"メール通知送信完了 (uid={uid}, thumbs=off)")
                except Exception as e:
                    (logger.warning if logger else print)(f"[通知] メール失敗: {e}")
        except Exception as e:
            (logger.error if logger else print)(f"[通知] 例外: {e}")
        return

    # ▼ 既存のサムネ生成キュー投入～完了待ち（ON時のみ動作）
    try:
        enqueue_thumb_job("upload", uid, "thumb")
        (logger.info if logger else print)(f"enqueue_thumb_job done: upload/{uid}/thumb")
    except Exception as e:
        (logger.warning if logger else print)(f"[thumb] enqueue failed: {e}")

    def _count_ready():
        ready = 0
        for name in filenames:
            base, _ext = os.path.splitext(name)
            cand1 = os.path.join(thumb_dir, name)
            cand2 = os.path.join(thumb_dir, base + ".webp")
            if os.path.exists(cand1) or os.path.exists(cand2):
                ready += 1
        return ready

    expected = len(filenames)
    timeout_sec = max(120, min(1800, expected * 3))
    start = time.time()
    last_report = -1
    while True:
        done = _count_ready()
        pct = int(done * 100 / expected) if expected else 100
        if pct // 10 != last_report // 10:
            (logger.info if logger else print)(f"[thumb] progress {done}/{expected} ({pct}%) uid={uid}")
            last_report = pct
        if done >= expected:
            (logger.info if logger else print)(f"[thumb] all done {done}/{expected} uid={uid}")
            break
        if time.time() - start > timeout_sec:
            (logger.warning if logger else print)(f"[thumb] timeout {done}/{expected} uid={uid}")
            break
        time.sleep(1.0)

    # 完了通知（従来どおりの文面／件名で、送信のみ mail.py に統一）
    try:
        db = get_db()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT webhook_url, email, notify_method FROM users WHERE username = %s", (context["username"],))
        user = cursor.fetchone()
        db.close()

        notify_method = user.get("notify_method", "discord") if user else "discord"
        msg = generate_message(mode, context, username=context["username"]) + "\n（サムネイル生成が完了しました）"

        send_discord_upload_notification(
            logger=(logger or app.logger),
            username=context["username"],
            notify_method=notify_method,
            webhook_url=user.get("webhook_url") if user else "",
            upload_id=uid,
            message=msg,
            context_label="normal upload",
        )

        if notify_method in ("email", "both") and user and user.get("email"):
            try:
                # ★ mail.pyに統一
                send_mail(
                    to=user["email"],
                    subject="ファイルアップロード通知",
                    body=msg,
                    event_uuid="notify",      # From: notify@mail.iori0624.jp
                    smtp_host="192.168.103.15",
                    smtp_port=25,
                    timeout=45,
                )
                (logger.info if logger else print)(f"メール通知送信完了 (uid={uid})")
            except Exception as e:
                (logger.warning if logger else print)(f"[通知] メール失敗: {e}")
    except Exception as e:
        (logger.error if logger else print)(f"[通知] 例外: {e}")

# =====================================
# ③ 表示／配信
# =====================================
@app.route("/view/<uuid>", methods=["GET", "POST"])
def view_upload(uuid):
    upload = _get_upload_access_record(uuid)
    if not upload:
        return "指定されたデータが存在しません", 404

    if (upload.get("password") or "").strip() and not upload.get("password_hash"):
        migrate_upload_password_if_needed(upload)

    if _can_access_upload_record(upload):
        _grant_view_auth(uuid)

    generate_thumbnails = bool(upload.get("generate_thumbnails"))

    # パス未認証ならパス画面へ
    if request.method == "POST" and not _has_view_auth(uuid):
        input_pass = request.form.get("password", "")
        if not verify_upload_password(upload, input_pass):
            return render_template("view_password.html", uuid=uuid, error="パスワードが違います")
        _grant_view_auth(uuid)

    if not _has_view_auth(uuid):
        return render_template("view_password.html", uuid=uuid)

    # ファイル一覧
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT filename FROM files WHERE upload_id = %s ORDER BY filename ASC", (upload["id"],))
    files = [row["filename"] for row in cursor.fetchall()]
    db.close()

    # サムネ（存在するもののみ列挙）
    thumb_dir = f"/mnt/mfu/uploads/{uuid}/thumb"
    thumbnails = []
    if generate_thumbnails:
        for f in files:
            base, _ = os.path.splitext(f)
            webp_path = os.path.join(thumb_dir, base + ".webp")
            if os.path.exists(webp_path):
                thumbnails.append({"webp": base + ".webp", "fallback": f})
            else:
                fallback_path = os.path.join(thumb_dir, f)
                if os.path.exists(fallback_path):
                    thumbnails.append({"webp": None, "fallback": f})

    # ▼ サムネOFFのときはZIP一括DL（API方式）ボタンを表示
    show_zip_button = (not generate_thumbnails) and len(files) > 0
    # APIに渡す相対パス一覧（zip_stream.resolve_relpath が解決する仕様）
    all_relpaths = [f"uploads/{uuid}/original/{name}" for name in files]
    file_entries = [
        {
            "name": name,
            "url": url_for("uploaded_file", subpath=f"{uuid}/original/{name}"),
        }
        for name in files
    ]

    return render_template(
        "view.html",
        upload=upload,
        files=files,
        thumbnails=thumbnails,
        image_count=len(files),
        mode_label=upload["mode"],
        uuid=uuid,
        show_zip_button=show_zip_button,
        all_relpaths=all_relpaths,  # ← 追加
        file_entries=file_entries,
    )

@app.route("/upload/<path:subpath>")
@app.route("/uploads/<path:subpath>")
def uploaded_file(subpath: str):
    """
    /mnt/mfu/uploads をルートに、安全に実体ファイルを配信する。
    例: /uploads/layer_uploads/<...>/zip/2025年09月09日_21時39分_木野　諒さん.zip
    """
    # 実体の保存場所。未設定なら /mnt/mfu/uploads を既定に
    base_dir = Path(current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")).resolve()

    upload_ref = resolve_upload_subpath(subpath, allow_zip=True)
    if upload_ref:
        upload = _get_upload_access_record(upload_ref["uuid"])
        if not upload:
            abort(404)
        if not _can_access_upload_record(upload):
            abort(403)
        target = upload_ref["target"]
    else:
        normalized = (subpath or "").strip().lstrip("/")
        if not normalized.startswith("layer_uploads/"):
            abort(404)
        # 既存の layer_uploads 導線だけは、従来どおりの安全なパス検証のみ維持する。
        target = (base_dir / subpath).resolve()

    # 要求パスを正規化して実体パスへ
    # パストラバーサル等の防止: base_dir 配下かどうか確認
    try:
        # Python 3.11 なら is_relative_to が使えます
        if not target.is_relative_to(base_dir):
            abort(404)
    except AttributeError:
        # 互換: もし古いPythonならstartswithで代替
        if str(target).startswith(str(base_dir)) is False:
            abort(404)

    # ファイル実在チェック
    if not target.exists() or not target.is_file():
        abort(404)

    # ZIPなどはダウンロードさせる（日本語名も維持）
    as_attachment = target.suffix.lower() in {".zip", ".7z", ".rar"}
    return send_file(
        target,
        as_attachment=as_attachment,
        conditional=True,            # Range/If-Modified-Since 等を有効化
        download_name=target.name    # 非ASCII名も適切にContent-Dispositionへ
    )

@app.route("/view/<uuid>/zip", methods=["GET"])
def download_zip_for_upload(uuid):
    upload = _get_upload_access_record(uuid)
    if not upload:
        return "指定されたデータが存在しません", 404
    if not _can_access_upload_record(upload):
        return redirect(url_for("view_upload", uuid=uuid))
    _grant_view_auth(uuid)

    generate_thumbnails = bool(upload.get("generate_thumbnails"))

    # ファイル一覧
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT filename FROM files WHERE upload_id=%s ORDER BY filename ASC", (upload["id"],))
    rows = cursor.fetchall()
    db.close()
    filenames = [r["filename"] for r in rows]

    if not filenames:
        abort(404)

    # ZIP生成場所
    base_dir = os.path.join(UPLOAD_BASE_DIR, uuid)
    original_dir = os.path.join(base_dir, "original")
    zip_dir = os.path.join(base_dir, "zip")
    os.makedirs(zip_dir, exist_ok=True)

    # ファイル名（タイトルがあればそれを使う）
    safe_title = (upload["title"] or f"upload_{uuid}")[:60].replace("/", "_").replace("\\", "_")
    zip_path = os.path.join(zip_dir, f"{safe_title}.zip")

    # 既存ZIPがあれば再利用（更新したければ削除してね運用）
    if not os.path.exists(zip_path):
        import zipfile
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in filenames:
                src = os.path.join(original_dir, name)
                if os.path.isfile(src):
                    # ZIP内は素のファイル名で格納
                    zf.write(src, arcname=name)

    return send_file(zip_path, as_attachment=True, download_name=os.path.basename(zip_path))


# =======================================
# 管理: ユーザー一覧
# =======================================
@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT username, nickname, webhook_url, email FROM users ORDER BY username")
    users = cursor.fetchall()
    db.close()
    return render_template("admin_users.html", users=users)


# =======================================
# 管理: ユーザー追加
# =======================================
@app.route("/admin/users/add", methods=["GET", "POST"])
@admin_required
def admin_users_add():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        nickname = request.form["nickname"]
        webhook = request.form["webhook"]
        email = request.form["email"]
        notify_method = request.form["notify_method"]
        if not username or not password:
            return "ユーザー名とパスワードは必須です", 400
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, nickname, webhook_url, email, notify_method)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (username, hashed, nickname, webhook, email, notify_method),
        )
        db.commit()
        db.close()
        return redirect(url_for("admin_users"))
    return render_template("admin_user_form.html", action="add", user=None)


# =======================================
# 管理: ユーザー編集
# =======================================
@app.route("/admin/users/edit/<username>", methods=["GET", "POST"])
@admin_required
def admin_users_edit(username):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT username, nickname, webhook_url, email, notify_method FROM users WHERE username = %s",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        db.close()
        return "ユーザーが見つかりません", 404

    if request.method == "POST":
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]
        nickname = request.form["nickname"]
        webhook = request.form["webhook"]
        email = request.form["email"]
        notify_method = request.form["notify_method"]

        if password or confirm_password:
            if password != confirm_password:
                db.close()
                return "パスワードが一致しません", 400

        if password:
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cursor.execute(
                """
                UPDATE users SET password_hash=%s, nickname=%s, webhook_url=%s, email=%s, notify_method=%s
                WHERE username=%s
                """,
                (hashed, nickname, webhook, email, notify_method, username),
            )
        else:
            cursor.execute(
                """
                UPDATE users SET nickname=%s, webhook_url=%s, email=%s, notify_method=%s
                WHERE username=%s
                """,
                (nickname, webhook, email, notify_method, username),
            )

        db.commit()
        db.close()
        return redirect(url_for("admin_users"))

    db.close()
    return render_template("admin_user_form.html", action="edit", user=user)

# =======================================
# 管理: ユーザー削除
# =======================================
@app.post("/admin/users/<string:username>/delete", endpoint="admin_users_delete")
@admin_required
def admin_users_delete(username):
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute("DELETE FROM users WHERE username=%s LIMIT 1", (username,))
        db.commit()
        flash(f"ユーザー「{username}」を削除しました。", "success")
    except Exception as e:
        db.rollback()
        current_app.logger.exception("ユーザー削除エラー")
        flash("ユーザー削除に失敗しました。", "danger")
    finally:
        db.close()
    return redirect(url_for("admin_users"))

# =======================================
# 管理: ユーザー機能付与（ACL）
# =======================================
@app.route("/admin/user-features", methods=["GET", "POST"])
@admin_required
def admin_user_features():
    ensure_feature_access_schema()
    selected_user = (request.values.get("user_id") or "").strip()

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT username, nickname FROM users ORDER BY username")
    users = cur.fetchall()
    if not selected_user and users:
        selected_user = users[0]["username"]

    cur.execute(
        """
        SELECT feature_key, label, is_enabled_global, category, order_no, description
          FROM mfu_features
         WHERE deprecated=0
         ORDER BY category, order_no, feature_key
        """
    )
    features = cur.fetchall()

    grouped_features = []
    current_category = None
    for feature in features:
        category = feature.get("category") or "other"
        if category != current_category:
            grouped_features.append({"category": category, "features": []})
            current_category = category
        grouped_features[-1]["features"].append(feature)

    user_enabled = set()
    if selected_user:
        cur.execute(
            """
            SELECT feature_key
              FROM mfu_user_features
             WHERE user_id=%s AND is_enabled=1
            """,
            (selected_user,),
        )
        user_enabled = {row["feature_key"] for row in cur.fetchall()}

    if request.method == "POST" and selected_user:
        selected = set(request.form.getlist("features"))
        enabled_keys = {feature["feature_key"] for feature in features if feature["is_enabled_global"]}
        selected = selected & enabled_keys
        cur.execute(
            """
            UPDATE mfu_user_features
               SET is_enabled=0
             WHERE user_id=%s
            """,
            (selected_user,),
        )
        payload = [(selected_user, key, 1) for key in sorted(selected)]
        if payload:
            cur.executemany(
                """
                INSERT INTO mfu_user_features (user_id, feature_key, is_enabled)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE is_enabled=VALUES(is_enabled)
                """,
                payload,
            )
        db.commit()
        flash("機能付与を更新しました。", "success")
        db.close()
        return redirect(url_for("admin_user_features", user_id=selected_user))

    db.close()
    return render_template(
        "admin_user_features.html",
        users=users,
        selected_user=selected_user,
        features=features,
        grouped_features=grouped_features,
        user_enabled=user_enabled,
    )


# =======================================
# 管理: 機能キー管理
# =======================================
@app.route("/admin/features", methods=["GET", "POST"])
@admin_required
def admin_features():
    ensure_feature_access_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)

    if request.method == "POST":
        mode = (request.form.get("mode") or "").strip()
        feature_key = (request.form.get("feature_key") or "").strip()
        label = (request.form.get("label") or "").strip()
        category = (request.form.get("category") or "").strip() or "other"
        description = (request.form.get("description") or "").strip() or None
        is_enabled_global = 1 if request.form.get("is_enabled_global") else 0
        deprecated = 1 if request.form.get("deprecated") else 0
        try:
            order_no = int(request.form.get("order_no") or 0)
        except ValueError:
            order_no = 0

        if mode == "new":
            if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", feature_key):
                flash("機能キーは英数字と_のみ、64文字以内で入力してください。", "danger")
            elif not label:
                flash("ラベルは必須です。", "danger")
            else:
                cur.execute("SELECT 1 FROM mfu_features WHERE feature_key=%s", (feature_key,))
                if cur.fetchone():
                    flash("同じ機能キーが既に存在します。", "danger")
                else:
                    cur.execute(
                        """
                        INSERT INTO mfu_features
                        (feature_key, label, is_enabled_global, category, order_no, description, deprecated)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            feature_key,
                            label,
                            is_enabled_global,
                            category,
                            order_no,
                            description,
                            deprecated,
                        ),
                    )
                    db.commit()
                    flash("機能キーを追加しました。", "success")
                    db.close()
                    return redirect(url_for("admin_features"))
        elif mode == "update":
            if not feature_key:
                flash("機能キーが見つかりません。", "danger")
            elif not label:
                flash("ラベルは必須です。", "danger")
            else:
                cur.execute(
                    """
                    UPDATE mfu_features
                       SET label=%s,
                           is_enabled_global=%s,
                           category=%s,
                           order_no=%s,
                           description=%s,
                           deprecated=%s
                     WHERE feature_key=%s
                    """,
                    (
                        label,
                        is_enabled_global,
                        category,
                        order_no,
                        description,
                        deprecated,
                        feature_key,
                    ),
                )
                db.commit()
                flash("機能キーを更新しました。", "success")
                db.close()
                return redirect(url_for("admin_features"))

    cur.execute(
        """
        SELECT feature_key, label, is_enabled_global, category, order_no, description, deprecated
          FROM mfu_features
         ORDER BY order_no, feature_key
        """
    )
    features = cur.fetchall()
    db.close()
    return render_template("admin_features.html", features=features)


# =======================================
# 管理: 機能キー分割
# =======================================
@app.route("/admin/features/split", methods=["GET", "POST"])
@admin_required
def admin_features_split():
    ensure_feature_access_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT feature_key, label, category, is_enabled_global, deprecated
          FROM mfu_features
         ORDER BY category, feature_key
        """
    )
    features = cur.fetchall()

    if request.method == "POST":
        from_feature_key = (request.form.get("from_feature_key") or "").strip()
        to_raw = (request.form.get("to_feature_keys") or "").strip()
        policy = (request.form.get("old_feature_policy") or "keep").strip()
        raw_keys = [key for key in re.split(r"[\\s,]+", to_raw) if key]
        invalid_keys = [key for key in raw_keys if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key)]
        to_keys = sorted({key for key in raw_keys if key and key not in invalid_keys})
        if not from_feature_key:
            flash("分割元の機能キーを選択してください。", "danger")
        elif invalid_keys:
            flash("分割先の機能キーは英数字と_のみで入力してください。", "danger")
        elif not to_keys:
            flash("分割先の機能キーを入力してください。", "danger")
        else:
            cur.execute(
                "SELECT 1 FROM mfu_features WHERE feature_key=%s",
                (from_feature_key,),
            )
            if not cur.fetchone():
                flash("分割元の機能キーが存在しません。", "danger")
            else:
                to_keys = [key for key in to_keys if key != from_feature_key]
                if not to_keys:
                    flash("分割先の機能キーが分割元と同一です。", "danger")
                    db.close()
                    return render_template("admin_features_split.html", features=features)
                cur.execute(
                    """
                    SELECT DISTINCT user_id
                      FROM mfu_user_features
                     WHERE feature_key=%s AND is_enabled=1
                    """,
                    (from_feature_key,),
                )
                users = [row["user_id"] for row in cur.fetchall()]
                for key in to_keys:
                    cur.execute(
                        "SELECT 1 FROM mfu_features WHERE feature_key=%s",
                        (key,),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """
                            INSERT INTO mfu_features
                            (feature_key, label, is_enabled_global, category, order_no, description, deprecated)
                            VALUES (%s, %s, 1, 'other', 0, NULL, 0)
                            """,
                            (key, key),
                        )
                payload = []
                for user_id in users:
                    for key in to_keys:
                        payload.append((user_id, key, 1))
                if payload:
                    cur.executemany(
                        """
                        INSERT INTO mfu_user_features (user_id, feature_key, is_enabled)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE is_enabled=VALUES(is_enabled)
                        """,
                        payload,
                    )

                if policy == "deprecate":
                    cur.execute(
                        "UPDATE mfu_features SET deprecated=1 WHERE feature_key=%s",
                        (from_feature_key,),
                    )
                elif policy == "disable_global":
                    cur.execute(
                        "UPDATE mfu_features SET is_enabled_global=0 WHERE feature_key=%s",
                        (from_feature_key,),
                    )
                elif policy == "delete":
                    cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                          FROM mfu_user_features
                         WHERE feature_key=%s
                        """,
                        (from_feature_key,),
                    )
                    row = cur.fetchone()
                    if row and row.get("cnt"):
                        flash("旧キーが付与済みのため削除できません。", "warning")
                    else:
                        cur.execute(
                            "DELETE FROM mfu_features WHERE feature_key=%s",
                            (from_feature_key,),
                        )

                db.commit()
                flash("機能キーの分割処理を完了しました。", "success")
                db.close()
                return redirect(url_for("admin_features_split"))

    db.close()
    return render_template("admin_features_split.html", features=features)


# =======================================
# 管理: ナビ項目編集
# =======================================
@app.route("/admin/nav")
@admin_required
def admin_nav_list():
    ensure_feature_access_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, parent_id, label, url, order_no, is_enabled,
               feature_key, open_in_new_tab, is_external
          FROM mfu_nav_items
         ORDER BY COALESCE(parent_id, 0), order_no, id
        """
    )
    items = cur.fetchall()
    db.close()

    parents = []
    children_map = {}
    for item in items:
        if item["parent_id"] is None:
            parents.append(item)
        else:
            children_map.setdefault(item["parent_id"], []).append(item)
    parents.sort(key=lambda x: (x.get("order_no", 0), x["id"]))
    for parent in parents:
        children = children_map.get(parent["id"], [])
        children.sort(key=lambda x: (x.get("order_no", 0), x["id"]))
        parent["children"] = children

    return render_template("admin_nav_list.html", nav_items=parents)


def _load_nav_item(item_id: int | None):
    if item_id is None:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM mfu_nav_items WHERE id=%s", (item_id,))
    item = cur.fetchone()
    db.close()
    return item


@app.route("/admin/nav/new", methods=["GET", "POST"])
@app.route("/admin/nav/<int:item_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_nav_form(item_id=None):
    ensure_feature_access_schema()
    item = _load_nav_item(item_id)

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, label
          FROM mfu_nav_items
         WHERE parent_id IS NULL
         ORDER BY order_no, id
        """
    )
    parents = cur.fetchall()
    cur.execute("SELECT feature_key, label FROM mfu_features ORDER BY feature_key")
    features = cur.fetchall()

    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        url_value = (request.form.get("url") or "").strip()
        parent_id_raw = (request.form.get("parent_id") or "").strip()
        feature_key = (request.form.get("feature_key") or "").strip() or None
        try:
            order_no = int(request.form.get("order_no") or 0)
        except ValueError:
            order_no = 0
        is_enabled = 1 if request.form.get("is_enabled") else 0
        open_in_new_tab = 1 if request.form.get("open_in_new_tab") else 0
        is_external = 1 if request.form.get("is_external") else 0

        parent_id = int(parent_id_raw) if parent_id_raw else None
        if item_id and parent_id == item_id:
            flash("自身を親に設定することはできません。", "danger")
        elif not label or not url_value:
            flash("ラベルとURLは必須です。", "danger")
        elif not is_external and not url_value.startswith("/"):
            flash("内部リンクは / で始まるURLのみ許可されます。", "danger")
        else:
            if item_id:
                cur.execute(
                    """
                    UPDATE mfu_nav_items
                       SET label=%s,
                           url=%s,
                           parent_id=%s,
                           order_no=%s,
                           is_enabled=%s,
                           feature_key=%s,
                           open_in_new_tab=%s,
                           is_external=%s
                     WHERE id=%s
                    """,
                    (
                        label,
                        url_value,
                        parent_id,
                        order_no,
                        is_enabled,
                        feature_key,
                        open_in_new_tab,
                        is_external,
                        item_id,
                    ),
                )
                db.commit()
                db.close()
                flash("ナビ項目を更新しました。", "success")
                return redirect(url_for("admin_nav_list"))

            cur.execute(
                """
                INSERT INTO mfu_nav_items
                (label, url, parent_id, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    label,
                    url_value,
                    parent_id,
                    order_no,
                    is_enabled,
                    feature_key,
                    open_in_new_tab,
                    is_external,
                ),
            )
            db.commit()
            db.close()
            flash("ナビ項目を追加しました。", "success")
            return redirect(url_for("admin_nav_list"))

    db.close()
    return render_template(
        "admin_nav_form.html",
        item=item,
        parents=parents,
        features=features,
    )


@app.post("/admin/nav/<int:item_id>/delete")
@admin_required
def admin_nav_delete(item_id: int):
    ensure_feature_access_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) AS cnt FROM mfu_nav_items WHERE parent_id=%s", (item_id,))
    row = cur.fetchone()
    if row and row.get("cnt"):
        db.close()
        flash("子項目があるため削除できません。先に子項目を削除してください。", "warning")
        return redirect(url_for("admin_nav_list"))
    cur.execute("DELETE FROM mfu_nav_items WHERE id=%s", (item_id,))
    db.commit()
    db.close()
    flash("ナビ項目を削除しました。", "success")
    return redirect(url_for("admin_nav_list"))

@app.context_processor
def inject_feature_context():
    is_external_login = any(
        session.get(key)
        for key in (
            "ext_user_id",
            "ext_login_user_id",
            "ext_user_line_id",
            "ext_user_social_id",
        )
    )
    is_mfu_login = bool(session.get("user")) or bool(getattr(current_user, "is_authenticated", False))
    nav_mode = "external" if is_external_login else ("mfu" if is_mfu_login else "mfu")

    user_id = session.get("user")
    return {
        "allowed_features": get_allowed_features(user_id),
        "has_feature": has_feature,
        "nav_items": get_nav_items_for_user(user_id),
        "is_external_login": is_external_login,
        "is_mfu_login": is_mfu_login,
        "nav_mode": nav_mode,
    }


@app.errorhandler(403)
def handle_forbidden(_error):
    return render_template("errors/403.html"), 403

# =======================================
# 管理: ログ閲覧（高速化版：SQL事前絞り + TTLキャッシュ + has_nextページング）
# /suc/ アクセス除外表示（SQL＆Python両層で共通管理）
# =======================================
_ADMIN_LOGS_EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _admin_logs_html_result_path(job_id: str) -> str:
    return os.path.join(_progress_dir(), f"{job_id}.html")


def _gc_adminlogs_jobs(ttl_seconds: int = 1800):
    now = time.time()
    root = _progress_dir()
    cutoff = now - ttl_seconds
    try:
        names = os.listdir(root)
    except Exception:
        return

    candidates = []
    for name in names:
        if not name.startswith("adminlogs_"):
            continue
        if not (name.endswith('.json') or name.endswith('.lock') or name.endswith('.html')):
            continue
        path = os.path.join(root, name)
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            continue
        if mtime < cutoff:
            candidates.append((mtime, path))

    for _, path in sorted(candidates, key=lambda x: x[0]):
        try:
            os.remove(path)
        except Exception:
            pass


def _build_admin_logs_html(args_dict: dict) -> str:
    """
    クエリ:
      kind=LOGIN|LINE_LOGIN|SMTP
      smtp_filter=ALL|AUTHFAIL|REJECT|DISCONNECT|SENT
      smtp_tag=1
      exclude_local=1
      nonjp_only=1
      exclude_suc=1          ← /suc/配下のアクセスを除外表示
      exclude_3xx=1          ← 3xxレスポンスを除外表示（※今は強制ONにする）
      limit <= 1000 (デフォルト1000)
      page >= 1
      date=YYYY-MM-DD
      search_date_from=YYYY-MM-DD
      search_date_to=YYYY-MM-DD
    """
    from ipaddress import ip_address, ip_network, IPv4Network, IPv6Network

    def _arg(name: str, default: str = "") -> str:
        return (args_dict.get(name, default) or "").strip()

    # --------- ローカル扱いネットを1か所で管理 ----------
    LOCAL_NETS = [
        "127.0.0.1",
        "192.168.103.0/24",
        "2404:7a81:bc40:2a00::/64",
        "2404:7a81:8ac1:1000::/64",
    ]
    LOCAL_NETS_OBJ = [ip_network(c) for c in LOCAL_NETS]

    def _like_prefixes_for_networks(networks):
        """
        /24 や /64 など 8/16ビット境界のネットだけ SQL LIKE 前方一致で粗除外。
        それ以外は SQL 最適化せず Python 側で is_local_ip が判定。
        """
        prefs = []
        for n in networks:
            if isinstance(n, IPv4Network) and n.prefixlen % 8 == 0:
                octs = str(n.network_address).split(".")[: n.prefixlen // 8]
                prefs.append(".".join(octs) + ".")
            elif isinstance(n, IPv6Network) and n.prefixlen % 16 == 0:
                hexts = n.network_address.exploded.split(":")[: n.prefixlen // 16]
                prefs.append(":".join(hexts) + ":")
        return prefs

    LOCAL_SQL_LIKE_PREFIXES = _like_prefixes_for_networks(LOCAL_NETS_OBJ)

    # --------- パス除外（/suc/など）も1か所で管理 ----------
    EXCLUDE_PATH_PREFIXES = [
        "/suc/",
        "/tickets/thumb/",
        "/tickets/preview/",
        "/tickets/api/status/",
        "/tickets/dl/",
        "/tickets/api/zip/",
        "/tickets/api/files/",
        "/apple-touch-icon",
        "/external-login/api/notifications/",
        "/manifest.webmanifest",
        "/sw.js",
        "/chat/api/room-presence/ping",
        "/api/mfu-notifications/unread-count",
        "/chat/api/room-presence/enter",
        "/chat/api/push/bootstrap ",
        "/external-login/api/events/chat-unread-counts",
        "/chat/api/",
        "/external-login/api/notifications",
        "/profile",
        "/admin/logs/status",
#        "",
#        "",
#        "",
#        "",
#        "",
#        "",
#        "",
#        "",

    ]
    EXCLUDE_PATH_SQL_LIKES = []
    for p in EXCLUDE_PATH_PREFIXES:
        if not p:
            continue
        EXCLUDE_PATH_SQL_LIKES.append(f"%Path: {p}%")
        EXCLUDE_PATH_SQL_LIKES.append(f"% {p}%")

    # --------- クエリ取得 ----------
    selected_date = _arg("date")
    kind = _arg("kind").upper()  # LOGIN / LINE_LOGIN / SMTP / ""
    smtp_filter = _arg("smtp_filter").upper()
    if smtp_filter not in ("ALL", "AUTHFAIL", "REJECT", "DISCONNECT", "SENT"):
        smtp_filter = "ALL"
    if kind != "SMTP":
        smtp_filter = "ALL"
    smtp_tag = _arg("smtp_tag").lower() in ("1", "true", "on", "yes")

    # 検索（サーバー側）
    search_keyword = _arg("search_keyword")
    search_mode = _arg("search_mode", "and").lower()
    if search_mode not in ("and", "or"):
        search_mode = "and"
    search_ip = _arg("search_ip")
    search_status = _arg("search_status")
    search_method = _arg("search_method")
    search_path = _arg("search_path")
    search_endpoint = _arg("search_endpoint")
    search_user = _arg("search_user")
    search_ua = _arg("search_ua")
    search_date_from = _arg("search_date_from")
    search_date_to = _arg("search_date_to")

    # 生のクエリ値
    raw_exclude_local = args_dict.get("exclude_local")
    raw_nonjp_only = args_dict.get("nonjp_only")
    raw_exclude_suc = args_dict.get("exclude_suc")

    # まずは「値がある場合」の通常パース
    exclude_local = (raw_exclude_local or "").lower() in ("1", "true", "on", "yes")
    nonjp_only = (raw_nonjp_only or "").lower() in ("1", "true", "on", "yes")
    exclude_suc = (raw_exclude_suc or "").lower() in ("1", "true", "on", "yes")

    # ★ 3xx は常に非表示にする（クエリ指定は無視）
    exclude_3xx = True

    # ★初期設定★
    # クエリパラメータが一切無い最初のアクセスだけ、
    # ローカル除外 / /suc 除外 はデフォルトONにする。
    if not args_dict:
        exclude_local = True
        exclude_suc = True

    try:
        per_page = int(args_dict.get("limit", "1000") or "1000")
    except ValueError:
        per_page = 1000
    per_page = max(1, min(1000, per_page))

    try:
        page = max(1, int(args_dict.get("page", "1") or "1"))
    except ValueError:
        page = 1
    start_index = (page - 1) * per_page

    # --------- ユーティリティ ----------
    def _valid_date(s: str) -> bool:
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except Exception:
            return False

    def is_local_ip(ip_str: str) -> bool:
        try:
            ipobj = ip_address(ip_str)
        except Exception:
            return False
        return any(ipobj in net for net in LOCAL_NETS_OBJ)

    def is_valid_ip(ip_str: str) -> bool:
        try:
            ip_address(ip_str)
            return True
        except Exception:
            return False

    def _text_contains_excluded_path(text: str) -> bool:
        if not text:
            return False
        t = text
        for p in EXCLUDE_PATH_PREFIXES:
            if p and (f"Path: {p}" in t or f" {p}" in t):
                return True
        return False

    def _split_keywords(text: str):
        if not text:
            return []
        return [t for t in re.split(r"[\s\u3000]+", text.strip()) if t]

    def _parse_method(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", text)
        return m.group(1) if m else ""

    def _parse_path(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b\s+([^\s\"]+)", text)
        if m:
            return m.group(2)
        m = re.search(r"path[=:]\"?([^\s\"]+)\"?", text, re.I)
        return m.group(1) if m else ""

    def _parse_endpoint(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"\bendpoint[=:]\"?([^\s\"]+)\"?", text, re.I)
        if m:
            return m.group(1)
        m = re.search(r"\bep[=:]\"?([^\s\"]+)\"?", text, re.I)
        return m.group(1) if m else ""

    def _parse_user(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"\buser(?:name)?[=:]\s*\"([^\"]*)\"", text, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"\buser(?:name)?[=:]\"?([\w.@:-]+)\"?", text, re.I)
        return m.group(1).strip() if m else ""

    def _parse_ua(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"\bua[=:]\"([^\"]+)\"", text, re.I)
        if m:
            return m.group(1)
        m = re.search(r"\bUser-Agent:\"([^\"]+)\"", text, re.I)
        return m.group(1) if m else ""

    def _parse_status_code(text: str):
        if not text:
            return None
        m = re.search(r"(^|\s)(\d{3})(\s|$)", text)
        if not m:
            return None
        try:
            return int(m.group(2))
        except Exception:
            return None

    SMTP_FILTER_LIKES = {
        "AUTHFAIL": ["%SASL%", "%auth fail%", "%authentication failed%", "%AUTH FAILED%", "%AUTH=fail%"],
        "REJECT": ["%NOQUEUE: reject%", "% reject:%", "% reject %"],
        "DISCONNECT": ["%lost connection%", "%disconnect%", "%timeout%"],
        "SENT": ["%status=sent%", "%送信OK%", "%sent=%"],
    }
    SMTP_FILTER_REGEX = {
        "AUTHFAIL": re.compile(r"(SASL|auth(?:entication)? failed|auth[=\s:]+fail|AUTH FAILED)", re.I),
        "REJECT": re.compile(r"(NOQUEUE:\s*reject|reject:\s|reject\s)", re.I),
        "DISCONNECT": re.compile(r"(lost connection|disconnect|timed out|timeout)", re.I),
        "SENT": re.compile(r"(status=sent|送信OK|\bsent\b)", re.I),
    }

    def _smtp_match(text: str, filt: str) -> bool:
        if not filt or filt == "ALL":
            return True
        rx = SMTP_FILTER_REGEX.get(filt)
        if not rx:
            return True
        return bool(rx.search(text or ""))

    def _search_match(text: str, ip_str: str) -> bool:
        if not any([search_keyword, search_ip, search_status, search_method, search_path, search_endpoint, search_user, search_ua]):
            return True
        haystack = f"{ip_str} {text}".lower()
        terms = _split_keywords(search_keyword)
        if terms:
            if search_mode == "or":
                if not any(term.lower() in haystack for term in terms):
                    return False
            else:
                if not all(term.lower() in haystack for term in terms):
                    return False
        if search_ip and search_ip.lower() not in (ip_str or "").lower():
            return False
        if search_status:
            status = _parse_status_code(text)
            if not status or search_status not in str(status):
                return False
        if search_method:
            method = _parse_method(text)
            if search_method.lower() not in method.lower():
                return False
        if search_path:
            path = _parse_path(text)
            if search_path.lower() not in path.lower():
                return False
        if search_endpoint:
            endpoint = _parse_endpoint(text)
            if search_endpoint.lower() not in endpoint.lower():
                return False
        if search_user:
            user = _parse_user(text)
            if search_user.lower() not in user.lower():
                return False
        if search_ua:
            ua = _parse_ua(text)
            if search_ua.lower() not in ua.lower():
                return False
        return True

    global _NETINFO_CACHE, _NETINFO_ORDER
    try:
        _NETINFO_CACHE
    except NameError:
        _NETINFO_CACHE = {}
        _NETINFO_ORDER = []
    TTL_SEC = 86400 * 7
    LRU_MAX = 10000

    def _cache_get(ip: str):
        now = time.time()
        ent = _NETINFO_CACHE.get(ip)
        if not ent:
            return None
        if ent[4] < now:
            _NETINFO_CACHE.pop(ip, None)
            try:
                _NETINFO_ORDER.remove(ip)
            except ValueError:
                pass
            return None
        try:
            _NETINFO_ORDER.remove(ip)
        except ValueError:
            pass
        _NETINFO_ORDER.append(ip)
        return {"netname": ent[0], "country": ent[1], "org": ent[2], "asname": ent[3]}

    def _cache_put(ip: str, ni: dict):
        exp = time.time() + TTL_SEC
        tup = (ni.get("netname", ""), ni.get("country", ""), ni.get("org", ""), ni.get("asname", ""), exp)
        _NETINFO_CACHE[ip] = tup
        _NETINFO_ORDER.append(ip)
        if len(_NETINFO_ORDER) > LRU_MAX:
            drop_ip = _NETINFO_ORDER.pop(0)
            _NETINFO_CACHE.pop(drop_ip, None)

    _req_seen = {}

    def _netinfo_record(ni: dict) -> dict:
        return {"netname": ni.get("netname", ""), "country": ni.get("country", ""), "org": ni.get("org", ""), "asname": ni.get("asname", "")}

    def get_netinfo_fast(ip: str) -> dict:
        if not ip:
            return {"netname": "", "country": "", "org": "", "asname": ""}
        if ip in _req_seen:
            return _req_seen[ip]
        hit = _cache_get(ip)
        if hit is not None:
            _req_seen[ip] = hit
            return hit
        try:
            ni = get_netinfo(ip) or {}
        except Exception:
            ni = {}
        rec = _netinfo_record(ni)
        _cache_put(ip, rec)
        _req_seen[ip] = rec
        return rec

    def get_netinfo_bulk(ips: list[str]) -> dict[str, dict]:
        resolved: dict[str, dict] = {}
        misses = []
        for ip in ips:
            if not ip:
                continue
            if ip in _req_seen:
                resolved[ip] = _req_seen[ip]
                continue
            hit = _cache_get(ip)
            if hit is not None:
                _req_seen[ip] = hit
                resolved[ip] = hit
                continue
            misses.append(ip)

        if not misses:
            return resolved

        from concurrent.futures import as_completed

        workers = min(16, max(4, len(misses)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(get_netinfo, ip): ip for ip in misses}
            for fut in as_completed(fut_map):
                ip = fut_map[fut]
                try:
                    ni = fut.result() or {}
                except Exception:
                    ni = {}
                rec = _netinfo_record(ni)
                _cache_put(ip, rec)
                _req_seen[ip] = rec
                resolved[ip] = rec

        return resolved

    def enrich_row(r):
        ip = (r.get("ip") or "").strip()
        ni = get_netinfo_fast(ip) if ip else {"netname": "", "country": "", "org": "", "asname": ""}
        r["netname"] = ni.get("netname", "")
        r["country"] = ni.get("country", "")
        r["provider"] = ni.get("org") or ni.get("asname") or ni.get("netname") or ""
        r["ip_valid"] = bool(ip and is_valid_ip(ip))
        r["ip_version"] = ip_address(ip).version if r["ip_valid"] else None
        return r

    db = get_db()
    cursor = db.cursor(dictionary=True)

    where = []
    params = []

    if selected_date and _valid_date(selected_date):
        where.append("log_date >= %s AND log_date < DATE_ADD(%s, INTERVAL 1 DAY)")
        params += [selected_date, selected_date]

    if search_date_from and not _valid_date(search_date_from):
        search_date_from = ""
    if search_date_to and not _valid_date(search_date_to):
        search_date_to = ""
    if search_date_from and search_date_to and search_date_from > search_date_to:
        search_date_from, search_date_to = search_date_to, search_date_from
    if search_date_from and search_date_to:
        where.append("log_date >= %s AND log_date < DATE_ADD(%s, INTERVAL 1 DAY)")
        params += [search_date_from, search_date_to]
    elif search_date_from:
        where.append("log_date >= %s")
        params.append(search_date_from)
    elif search_date_to:
        where.append("log_date < DATE_ADD(%s, INTERVAL 1 DAY)")
        params.append(search_date_to)

    if kind == "LOGIN":
        where.append("INSTR(log_text,'[LOGIN]') > 0")
    elif kind == "LINE_LOGIN":
        where.append("INSTR(log_text,'[LINE_LOGIN]') > 0")
    elif kind == "SMTP":
        where.append("INSTR(log_text,'[SMTP]') > 0")
    if smtp_tag:
        where.append("INSTR(log_text,'[SMTP]') > 0")

    if kind == "SMTP" and smtp_filter != "ALL":
        likes = SMTP_FILTER_LIKES.get(smtp_filter, [])
        if likes:
            placeholders = " OR ".join(["log_text LIKE %s"] * len(likes))
            where.append(f"({placeholders})")
            params.extend(likes)

    if exclude_local and LOCAL_SQL_LIKE_PREFIXES:
        placeholders = " OR ".join(["ip LIKE %s"] * len(LOCAL_SQL_LIKE_PREFIXES))
        where.append(f"NOT ({placeholders})")
        params.extend([p + "%" for p in LOCAL_SQL_LIKE_PREFIXES])

    if exclude_suc and EXCLUDE_PATH_SQL_LIKES:
        placeholders = " OR ".join(["log_text LIKE %s"] * len(EXCLUDE_PATH_SQL_LIKES))
        where.append(f"NOT ({placeholders})")
        params.extend(EXCLUDE_PATH_SQL_LIKES)

    search_terms = _split_keywords(search_keyword)
    if search_terms:
        if search_mode == "or":
            or_parts = []
            for term in search_terms:
                or_parts.append("(log_text LIKE %s OR ip LIKE %s)")
                params.extend([f"%{term}%", f"%{term}%"])
            where.append("(" + " OR ".join(or_parts) + ")")
        else:
            for term in search_terms:
                where.append("(log_text LIKE %s OR ip LIKE %s)")
                params.extend([f"%{term}%", f"%{term}%"])

    if search_ip:
        where.append("ip LIKE %s")
        params.append(f"%{search_ip}%")
    if search_status:
        where.append("log_text LIKE %s")
        params.append(f"%{search_status}%")
    if search_method:
        where.append("log_text LIKE %s")
        params.append(f"%{search_method}%")
    if search_path:
        where.append("log_text LIKE %s")
        params.append(f"%{search_path}%")
    if search_endpoint:
        where.append("log_text LIKE %s")
        params.append(f"%{search_endpoint}%")
    if search_user:
        where.append("log_text LIKE %s")
        params.append(f"%{search_user}%")
    if search_ua:
        where.append("log_text LIKE %s")
        params.append(f"%{search_ua}%")

    base_sql = "SELECT id, log_date, ip, log_text FROM logs"
    if where:
        base_sql += " WHERE " + " AND ".join(where)
    base_sql += " ORDER BY id DESC"

    target_needed = per_page + 1
    scan_chunk = max(per_page * 3, 1000) if nonjp_only else max(per_page, 500)
    db_offset = 0
    accepted = 0
    page_rows = []
    has_next = False

    while True:
        cursor.execute(f"{base_sql} LIMIT %s OFFSET %s", params + [scan_chunk, db_offset])
        rows = cursor.fetchall()
        if not rows:
            break

        bulk_netinfo = {}
        if nonjp_only:
            nonjp_ips = []
            seen_ips = set()
            for r in rows:
                ip = (r.get("ip") or "").strip()
                if not ip or ip in seen_ips or ip in ("-", "—") or not is_valid_ip(ip):
                    continue
                seen_ips.add(ip)
                nonjp_ips.append(ip)
            bulk_netinfo = get_netinfo_bulk(nonjp_ips)

        for r in rows:
            ip = (r.get("ip") or "").strip()
            text = r.get("log_text") or ""

            if exclude_3xx:
                st = _parse_status_code(text)
                if st is not None and 300 <= st < 400:
                    continue

            if kind == "SMTP" and smtp_filter != "ALL" and not _smtp_match(text, smtp_filter):
                continue

            if not _search_match(text, ip):
                continue

            if nonjp_only:
                if not ip or ip.strip() in ("-", "—") or not is_valid_ip(ip.strip()):
                    continue
                tmp = dict(r)
                ni = bulk_netinfo.get(ip) or get_netinfo_fast(ip)
                tmp["netname"] = ni.get("netname", "")
                tmp["country"] = ni.get("country", "")
                tmp["provider"] = ni.get("org") or ni.get("asname") or ni.get("netname") or ""
                tmp["ip_valid"] = True
                tmp["ip_version"] = ip_address(ip).version
                cc = (tmp.get("country") or "").upper()
                if kind == "SMTP":
                    if not cc or cc in ("JP", "ZZ", "不明", "UNKNOWN"):
                        continue
                else:
                    if not cc or cc == "JP":
                        continue
                r = tmp

            if exclude_local and is_local_ip(ip):
                continue
            if exclude_suc and _text_contains_excluded_path(text):
                continue

            if accepted < start_index:
                accepted += 1
                continue

            if len(page_rows) < target_needed:
                if not nonjp_only:
                    r = enrich_row(r)
                page_rows.append(r)

            if len(page_rows) >= target_needed:
                break

        if len(page_rows) >= target_needed:
            break
        db_offset += len(rows)

    db.close()

    if len(page_rows) > per_page:
        has_next = True
        page_rows = page_rows[:per_page]

    total_pages = page + (1 if has_next else 0)

    return render_template(
        "admin_logs.html",
        logs=page_rows,
        selected_date=selected_date if selected_date and _valid_date(selected_date) else "",
        now=datetime.utcnow,
        timedelta=timedelta,
        current_page=page,
        total_pages=total_pages,
        filters={
            "kind": kind,
            "exclude_local": exclude_local,
            "nonjp_only": nonjp_only,
            "exclude_suc": exclude_suc,
            "smtp_filter": smtp_filter,
            "smtp_tag": smtp_tag,
            "search_keyword": search_keyword,
            "search_mode": search_mode,
            "search_ip": search_ip,
            "search_status": search_status,
            "search_method": search_method,
            "search_path": search_path,
            "search_endpoint": search_endpoint,
            "search_user": search_user,
            "search_ua": search_ua,
            "search_date_from": search_date_from,
            "search_date_to": search_date_to,
            "exclude_3xx": exclude_3xx,
            "limit": per_page,
            "has_filters": bool(kind or exclude_local or nonjp_only or exclude_suc or exclude_3xx or (kind == "SMTP" and smtp_filter != "ALL") or smtp_tag or search_keyword or search_ip or search_status or search_method or search_path or search_endpoint or search_user or search_ua or search_date_from or search_date_to),
        },
    )


def _run_admin_logs_job(job_id: str, args_dict: dict, user_id: str | None):
    _progress_write(job_id, {
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "args": args_dict,
        "requested_by": user_id,
    })
    try:
        with app.app_context():
            with app.test_request_context("/admin/logs/sync", query_string=args_dict):
                if user_id:
                    session["user"] = user_id
                html = _build_admin_logs_html(args_dict)
        result_path = _admin_logs_html_result_path(job_id)
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(html)
        _progress_write(job_id, {
            "status": "done",
            "finished_at": datetime.utcnow().isoformat(),
            "result_path": result_path,
            "args": args_dict,
            "requested_by": user_id,
        })
    except Exception as e:
        _progress_write(job_id, {
            "status": "error",
            "error_message": str(e),
            "finished_at": datetime.utcnow().isoformat(),
            "args": args_dict,
            "requested_by": user_id,
        })
        app.logger.exception("admin logs async job failed: %s", job_id)


@app.route("/admin/logs")
@admin_required
def admin_logs():
    return redirect(url_for("admin_logs_async", **request.args), code=302)


@app.route("/admin/logs/sync")
@admin_required
def admin_logs_sync():
    html = _build_admin_logs_html(request.args.to_dict(flat=True))
    return Response(html, content_type="text/html; charset=utf-8")


@app.route("/admin/logs/async")
@admin_required
def admin_logs_async():
    _gc_adminlogs_jobs(ttl_seconds=1800)
    args_dict = request.args.to_dict(flat=True)
    args_dict_no_job = dict(args_dict)
    args_dict_no_job.pop("job", None)
    user_id = session.get("user")
    existing_job = (request.args.get("job") or "").strip()
    if existing_job and existing_job.startswith("adminlogs_"):
        st = _progress_read(existing_job)
        same_args = bool(st and (st.get("args") or {}) == args_dict_no_job)
        if st and st.get("status") in ("queued", "running", "error") and same_args:
            job_id = existing_job
        else:
            job_id = f"adminlogs_{secrets.token_hex(16)}"
    else:
        job_id = f"adminlogs_{secrets.token_hex(16)}"

    status = _progress_read(job_id)
    if not status:
        _progress_write(job_id, {
            "status": "queued",
            "created_at": datetime.utcnow().isoformat(),
            "args": args_dict_no_job,
            "requested_by": user_id,
        })
        _ADMIN_LOGS_EXECUTOR.submit(_run_admin_logs_job, job_id, args_dict_no_job, user_id)

    retry_args = dict(args_dict_no_job)
    return render_template(
        "admin/logs_loading.html",
        job_id=job_id,
        status_url=url_for("admin_logs_status", job=job_id),
        result_url=url_for("admin_logs_result", job=job_id),
        retry_url=url_for("admin_logs_async", **retry_args),
    )


@app.route("/admin/logs/status")
@admin_required
def admin_logs_status():
    job_id = (request.args.get("job") or "").strip()
    if not job_id:
        return jsonify({"status": "not_found"}), 404

    status_data = _progress_read(job_id)
    if not status_data:
        return jsonify({"status": "not_found"}), 404

    return jsonify({
        "status": status_data.get("status", "unknown"),
        "message": status_data.get("message", ""),
        "finished_at": status_data.get("finished_at"),
        "error_message": status_data.get("error_message", ""),
    })


@app.route("/admin/logs/result")
@admin_required
def admin_logs_result():
    job_id = (request.args.get("job") or "").strip()
    if not job_id:
        return redirect(url_for("admin_logs_async"), code=302)

    status_data = _progress_read(job_id)
    if not status_data:
        return redirect(url_for("admin_logs_async"), code=302)

    st = status_data.get("status")
    if st == "done":
        result_path = status_data.get("result_path") or _admin_logs_html_result_path(job_id)
        if result_path and os.path.exists(result_path):
            return send_file(result_path, mimetype="text/html; charset=utf-8")
        return Response("<h2>結果ファイルが見つかりません。</h2>", status=404, content_type="text/html; charset=utf-8")

    if st == "error":
        msg = status_data.get("error_message") or "ログの生成に失敗しました。"
        retry_url = url_for("admin_logs_async", **(status_data.get("args") or {}))
        return Response(f"<h2>ログの生成に失敗しました</h2><p>{msg}</p><p><a href='{retry_url}'>再試行</a></p>", content_type="text/html; charset=utf-8", status=500)

    return redirect(url_for("admin_logs_async", job=job_id, **(status_data.get("args") or {})), code=302)


@app.route("/admin/logs/404-ban", methods=["GET", "POST"])
@admin_required
def admin_logs_404_ban_settings():
    if request.method == "POST":
        try:
            payload = {
                "short_window_sec": request.form.get("short_window_sec", ""),
                "short_threshold": request.form.get("short_threshold", ""),
                "ip_window_sec": request.form.get("ip_window_sec", ""),
                "ip_threshold": request.form.get("ip_threshold", ""),
                "cooldown_sec": request.form.get("cooldown_sec", ""),
                "ipv4_prefix": request.form.get("ipv4_prefix", "24"),
            }
            save_fw_404_settings(payload)
            flash("404アクセスBAN判定の設定を更新しました。", "success")
        except Exception as e:
            flash(f"設定の更新に失敗しました: {e}", "danger")
        return redirect(url_for("admin_logs_404_ban_settings"))

    settings = get_fw_404_settings()
    return render_template("admin_404_ban_settings.html", settings=settings)


# =======================================
# 管理: メール送信ログ（配送結果）
# =======================================
@app.route("/admin/mail-delivery")
@admin_required
def admin_mail_delivery_logs():
    from app.utils.mail_delivery import ensure_mail_delivery_schema

    ensure_mail_delivery_schema()

    status = (request.args.get("status") or "all").strip().lower()
    if status not in ("all", "sent", "bounced", "deferred", "unknown", "queued", "failed", "partial"):
        status = "all"

    search = (request.args.get("q") or "").strip()
    try:
        per_page = int(request.args.get("limit", "200"))
    except ValueError:
        per_page = 200
    per_page = max(20, min(500, per_page))
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    offset = (page - 1) * per_page

    where_clauses = ["1=1"]
    params: list = []
    if status != "all":
        where_clauses.append("l.last_delivery_status = %s")
        params.append(status)
    if search:
        like = f"%{search}%"
        where_clauses.append(
            "("             "l.message_id LIKE %s OR l.mfu_mail_uuid LIKE %s OR l.subject LIKE %s OR l.to_addresses LIKE %s "             "OR EXISTS (SELECT 1 FROM mfu_mail_delivery_recipients AS sr WHERE sr.mail_log_id = l.id AND sr.recipient LIKE %s)"             ")"
        )
        params.extend([like, like, like, like, like])
    where_sql = "WHERE " + " AND ".join(where_clauses)

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        f"""
        SELECT l.id,
               l.mfu_mail_uuid,
               l.message_id,
               l.to_addresses,
               l.subject,
               l.submit_status,
               l.submit_at,
               l.last_delivery_status,
               l.last_delivery_detail,
               l.last_delivery_queue_id,
               l.last_delivery_checked_at,
               COALESCE(rs.recipient_count, 0) AS recipient_count,
               COALESCE(rs.success_count, 0) AS success_count,
               COALESCE(rs.failure_count, 0) AS failure_count,
               COALESCE(rs.deferred_count, 0) AS deferred_count,
               COALESCE(rs.queued_count, 0) AS queued_count
          FROM mfu_mail_delivery_log AS l
          LEFT JOIN (
                SELECT mail_log_id,
                       COUNT(*) AS recipient_count,
                       SUM(CASE WHEN delivery_status = 'sent' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN delivery_status IN ('bounced', 'failed') THEN 1 ELSE 0 END) AS failure_count,
                       SUM(CASE WHEN delivery_status = 'deferred' THEN 1 ELSE 0 END) AS deferred_count,
                       SUM(CASE WHEN delivery_status IN ('queued', 'unknown') THEN 1 ELSE 0 END) AS queued_count
                  FROM mfu_mail_delivery_recipients
                 GROUP BY mail_log_id
          ) AS rs
            ON rs.mail_log_id = l.id
          {where_sql}
         ORDER BY l.submit_at DESC, l.id DESC
         LIMIT %s OFFSET %s
        """,
        (*params, per_page, offset),
    )
    rows = cur.fetchall() or []
    row_ids = [row["id"] for row in rows if row.get("id")]
    recipient_map = {}
    if row_ids:
        placeholders = ", ".join(["%s"] * len(row_ids))
        cur.execute(
            f"""
            SELECT mail_log_id,
                   recipient,
                   recipient_type,
                   submit_status,
                   delivery_status,
                   delivery_detail,
                   delivery_queue_id,
                   delivery_checked_at
              FROM mfu_mail_delivery_recipients
             WHERE mail_log_id IN ({placeholders})
             ORDER BY mail_log_id ASC, FIELD(recipient_type, 'to', 'cc', 'bcc'), id ASC
            """,
            row_ids,
        )
        recipient_rows = cur.fetchall() or []
        recipient_map = {}
        for recipient_row in recipient_rows:
            recipient_map.setdefault(recipient_row["mail_log_id"], []).append(recipient_row)

    for row in rows:
        submit_at = row.get("submit_at")
        row["submit_at_ts"] = int(submit_at.timestamp()) if submit_at else None
        row["recipient_rows"] = recipient_map.get(row["id"], [])

    cur.execute(
        f"SELECT COUNT(*) AS cnt FROM mfu_mail_delivery_log AS l {where_sql}",
        params,
    )
    total = (cur.fetchone() or {}).get("cnt", 0)
    db.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "admin_mail_delivery_logs.html",
        rows=rows,
        filters={
            "status": status,
            "q": search,
            "limit": per_page,
        },
        page=page,
        total_pages=total_pages,
        total=total,
    )


@app.route("/admin/mail-delivery/refresh", methods=["POST"])
@admin_required
def admin_mail_delivery_refresh():
    from app.utils.mail_delivery import poll_mail_delivery_statuses

    max_rows = request.args.get("max_rows")
    timeout_sec = request.args.get("timeout_sec")
    if max_rows is None:
        payload = request.get_json(silent=True) or {}
        max_rows = payload.get("max_rows")
        if timeout_sec is None:
            timeout_sec = payload.get("timeout_sec")
    try:
        max_rows_int = int(max_rows or 200)
    except (TypeError, ValueError):
        max_rows_int = 200
    max_rows_int = max(20, min(500, max_rows_int))

    try:
        timeout_sec_int = int(timeout_sec) if timeout_sec is not None else None
    except (TypeError, ValueError):
        timeout_sec_int = None

    summary = poll_mail_delivery_statuses(max_rows=max_rows_int, timeout_sec=timeout_sec_int)
    summary["max_rows"] = max_rows_int
    return jsonify(summary)


# =======================================
# 管理: メンテナンスモード
# =======================================
@app.route("/admin/maintenance", methods=["GET", "POST"])
@admin_required
def admin_maintenance():
    db = get_db()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        new_mode = "on" if request.form.get("maintenance_mode") == "on" else "off"
        until_raw = request.form.get("maintenance_until")
        square_env_payment = (request.form.get("square_env_payment") or "").upper()
        square_env_external = (request.form.get("square_env_external") or "").upper()

        if until_raw:
            try:
                until_dt = datetime.strptime(until_raw, "%Y-%m-%dT%H:%M")
                until_str = until_dt.astimezone(timezone.utc).isoformat()
            except Exception:
                flash("日付形式が正しくありません。", "danger")
                db.close()
                return redirect(url_for("admin_maintenance"))
        else:
            until_str = None

        cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_mode'")
        _prev = (cursor.fetchone() or {}).get("value", "off")

        cursor.execute("REPLACE INTO settings (`key`, `value`) VALUES ('maintenance_mode', %s)", (new_mode,))
        if until_str is not None:
            cursor.execute("REPLACE INTO settings (`key`, `value`) VALUES ('maintenance_until', %s)", (until_str,))
        else:
            cursor.execute("DELETE FROM settings WHERE `key` = 'maintenance_until'")

        if square_env_payment in ("SANDBOX", "PRODUCTION"):
            cursor.execute(
                "REPLACE INTO settings (`key`, `value`) VALUES ('square_env_payment', %s)",
                (square_env_payment,),
            )
        if square_env_external in ("SANDBOX", "PRODUCTION"):
            cursor.execute(
                "REPLACE INTO settings (`key`, `value`) VALUES ('square_env_external', %s)",
                (square_env_external,),
            )

        db.commit()
        db.close()

        flash("メンテナンス設定を更新しました。", "success")
        return redirect(url_for("admin_maintenance"))

    cursor.execute("SELECT `value` FROM settings WHERE `key` = 'maintenance_mode'")
    current_mode = (cursor.fetchone() or {}).get("value", "off")

    cursor.execute("SELECT `value` FROM settings WHERE `key` = 'maintenance_until'")
    until_val = (cursor.fetchone() or {}).get("value")

    current_until = ""
    if until_val:
        try:
            dt = datetime.fromisoformat(until_val).astimezone()
            current_until = dt.strftime("%Y-%m-%dT%H:%M")
        except:
            pass

    db.close()
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT `value` FROM settings WHERE `key` = 'square_env_payment'")
    square_env_payment_val = (cursor.fetchone() or {}).get("value")
    cursor.execute("SELECT `value` FROM settings WHERE `key` = 'square_env_external'")
    square_env_external_val = (cursor.fetchone() or {}).get("value")
    db.close()
    fallback_square_env = os.environ.get("SQUARE_ENV", "SANDBOX")
    current_square_env_payment = (square_env_payment_val or fallback_square_env).upper()
    current_square_env_external = (square_env_external_val or fallback_square_env).upper()
    return render_template(
        "admin_maintenance.html",
        current_mode=current_mode,
        current_until=current_until,
        current_square_env_payment=current_square_env_payment,
        current_square_env_external=current_square_env_external,
    )

# =======================================
# 管理: 再起動
# =======================================
@app.route("/admin/settings/inapp-browser", methods=["GET", "POST"])
@admin_required
def admin_inapp_browser_settings():
    _ensure_inapp_browser_settings_defaults()
    db = get_db()
    cursor = db.cursor(dictionary=True)

    def _save_inapp_browser_settings(payload):
        for key, value in payload.items():
            cursor.execute(
                "REPLACE INTO settings (`key`, `value`) VALUES (%s, %s)",
                (key, value),
            )

    if request.method == "POST":
        action = request.form.get("action") or "save"
        if action == "reset":
            _save_inapp_browser_settings(INAPP_BROWSER_SETTINGS_DEFAULTS)
            db.commit()
            db.close()
            flash("アプリ内ブラウザ警告設定を初期値に戻しました。", "success")
            return redirect(url_for("admin_inapp_browser_settings"))

        payload = {
            INAPP_BROWSER_WARNING_ENABLED_KEY: "1" if request.form.get("inapp_browser_warning_enabled") else "0",
            INAPP_BROWSER_KEYWORDS_KEY: _serialize_multiline_setting(
                (request.form.get("inapp_browser_keywords") or "").splitlines()
            ),
            INAPP_BROWSER_REFERRER_PREFIXES_KEY: _serialize_multiline_setting(
                (request.form.get("inapp_browser_referrer_prefixes") or "").splitlines()
            ),
            INAPP_BROWSER_SKIP_PATHS_KEY: _serialize_multiline_setting(
                (request.form.get("inapp_browser_skip_paths") or "").splitlines()
            ),
        }
        _save_inapp_browser_settings(payload)
        db.commit()
        db.close()
        flash("アプリ内ブラウザ警告設定を更新しました。", "success")
        return redirect(url_for("admin_inapp_browser_settings"))

    current_settings = {}
    for key, default_value in INAPP_BROWSER_SETTINGS_DEFAULTS.items():
        cursor.execute("SELECT `value` FROM settings WHERE `key` = %s", (key,))
        row = cursor.fetchone() or {}
        value = row.get("value")
        current_settings[key] = default_value if value is None else value

    db.close()
    return render_template(
        "admin_inapp_browser_settings.html",
        current_settings=current_settings,
        default_enabled=INAPP_BROWSER_DEFAULT_ENABLED,
        default_keywords_text=INAPP_BROWSER_SETTINGS_DEFAULTS[INAPP_BROWSER_KEYWORDS_KEY],
        default_referrer_prefixes_text=INAPP_BROWSER_SETTINGS_DEFAULTS[INAPP_BROWSER_REFERRER_PREFIXES_KEY],
        default_skip_paths_text=INAPP_BROWSER_SETTINGS_DEFAULTS[INAPP_BROWSER_SKIP_PATHS_KEY],
    )


@app.route("/admin/restart", methods=["POST"])
@admin_required
def admin_restart():
    threading.Thread(target=delayed_restart).start()
    flash("サーバーの再起動を実行しました（約2秒後に反映されます）", "info")
    return redirect(url_for("admin_maintenance"))

# =====================================
# ⑤ テンプレ／モード
# =====================================
@app.route("/templates")
def template_index():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    templates = []
    MODES = {}  # 参照のため空定義（別管理ならここは無視される）
    for mode_key, mode_label in MODES.items():
        cursor.execute("SELECT 1 FROM message_templates WHERE username = %s AND mode = %s", (username, mode_key))
        exists = cursor.fetchone()
        templates.append({"mode": mode_key, "label": mode_label, "exists": bool(exists)})

    db.close()
    return render_template("template_index.html", templates=templates)

@app.route("/templates/<mode>", methods=["GET", "POST"])
def template_edit(mode):
    if "user" not in session:
        return redirect(url_for("login"))

    db = get_db()
    cursor = db.cursor()

    if request.method == "POST":
        new_template = request.form["template"]
        cursor.execute(
            """
            REPLACE INTO message_templates (username, mode, template)
            VALUES (%s, %s, %s)
            """,
            (session["user"], mode, new_template),
        )
        db.commit()
        db.close()
        return redirect(url_for("template_index"))

    cursor.execute(
        "SELECT template FROM message_templates WHERE username = %s AND mode = %s",
        (session["user"], mode),
    )
    row = cursor.fetchone()
    db.close()

    return render_template("template_edit.html", mode=mode, template=row[0] if row else "")

@app.route("/modes", methods=["GET", "POST"])
def mode_list():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        new_default = request.form.get("default_mode")
        cursor.execute("UPDATE users SET default_mode = %s WHERE username = %s", (new_default, username))
        db.commit()

    cursor.execute("SELECT * FROM upload_modes WHERE username = %s ORDER BY mode", (username,))
    modes = cursor.fetchall()

    cursor.execute("SELECT default_mode FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    current_default = user["default_mode"] if user else ""

    db.close()
    return render_template("mode_list.html", modes=modes, default_mode=current_default)

@app.route("/modes/add", methods=["GET", "POST"])
def mode_add():
    if "user" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        mode = request.form["mode"]
        label = request.form["label"]
        template_key = request.form["template_key"]
        enable_download_url = bool(request.form.get("enable_download_url"))
        require_password = bool(request.form.get("require_password"))
        enable_layer_upload_url = bool(request.form.get("enable_layer_upload_url"))
        generate_thumbnails = bool(request.form.get("generate_thumbnails"))

        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO upload_modes
            (username, mode, label, enable_download_url, require_password, enable_layer_upload_url, generate_thumbnails, template_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (session["user"], mode, label, enable_download_url, require_password, enable_layer_upload_url, generate_thumbnails, template_key),
        )
        db.commit()
        db.close()
        return redirect(url_for("mode_list"))

    return render_template("mode_form.html", action="add", mode_data=None)

@app.route("/modes/edit/<mode>", methods=["GET", "POST"])
def mode_edit_combined(mode):
    if "user" not in session:
        return redirect(url_for("login"))
    username = session["user"]

    db = get_db()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT * FROM upload_modes WHERE username = %s AND mode = %s", (username, mode))
    mode_data = cursor.fetchone()

    cursor.execute("SELECT template FROM message_templates WHERE username = %s AND mode = %s", (username, mode))
    tpl_row = cursor.fetchone()
    template = tpl_row["template"] if tpl_row else ""

    if mode_data is None:
        generated_mode = "mode_" + uuid.uuid4().hex[:12]
        mode = generated_mode

    if request.method == "POST":
        label = request.form["label"]
        template_text = request.form["template"]
        enable_download_url = bool(request.form.get("enable_download_url"))
        require_password = bool(request.form.get("require_password"))
        enable_layer_upload_url = bool(request.form.get("enable_layer_upload_url"))
        generate_thumbnails = bool(request.form.get("generate_thumbnails"))

        cursor.execute(
            """
            INSERT INTO upload_modes
            (username, mode, label, enable_download_url, require_password, enable_layer_upload_url, generate_thumbnails, template_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                label = VALUES(label),
                enable_download_url = VALUES(enable_download_url),
                require_password = VALUES(require_password),
                enable_layer_upload_url = VALUES(enable_layer_upload_url),
                generate_thumbnails = VALUES(generate_thumbnails),
                template_key = VALUES(template_key)
            """,
            (username, mode, label, enable_download_url, require_password, enable_layer_upload_url, generate_thumbnails, mode),
        )

        cursor.execute(
            """
            REPLACE INTO message_templates (username, mode, template)
            VALUES (%s, %s, %s)
            """,
            (username, mode, template_text),
        )

        db.commit()
        db.close()
        return redirect(url_for("mode_list"))

    db.close()
    return render_template("mode_edit_combined.html",
                           action="edit" if mode_data else "add",
                           mode=mode,
                           mode_data=mode_data,
                           template=template)

@app.post("/modes/delete/<mode>")
def mode_delete(mode):
    if "user" not in session:
        return redirect(url_for("login"))
    username = session["user"]

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM upload_modes WHERE username = %s AND mode = %s", (username, mode))
    cursor.execute("DELETE FROM message_templates WHERE username = %s AND mode = %s", (username, mode))
    cursor.execute("UPDATE users SET default_mode = NULL WHERE username = %s AND default_mode = %s", (username, mode))
    db.commit()
    db.close()
    return redirect(url_for("mode_list"))

# =====================================
# ⑥ API（デバッグ／センサー／CPU）
# =====================================
@app.route("/speedtest", methods=["GET"])
@admin_required
def speedtest_page():
    return render_template("speedtest.html")

@app.route("/api/vcgencmd")
@admin_required
def api_vcgencmd():
    # RPi → vcgencmd、その他 → psutil/lm-sensors にフォールバック

    def parse_throttled(hex_str):
        try:
            val = int(str(hex_str).replace("throttled=", ""), 16)
            messages = []
            if val & (1 << 0):  messages.append("現在: 電圧低下中")
            if val & (1 << 1):  messages.append("現在: 周波数制限中")
            if val & (1 << 2):  messages.append("現在: 温度スロットル中")
            if val & (1 << 16): messages.append("過去: 電圧低下あり")
            if val & (1 << 17): messages.append("過去: 周波数制限あり")
            if val & (1 << 18): messages.append("過去: 温度スロットルあり")
            return messages if messages else ["正常"]
        except Exception as e:
            return [f"解析失敗: {e}"]

    def run(cmd):
        try:
            return subprocess.check_output(["vcgencmd"] + cmd.split(), timeout=2).decode().strip()
        except Exception:
            return None

    # --- RPi (vcgencmd) が使える場合 ---
    throttled_raw = run("get_throttled")
    if throttled_raw is not None:
        clock_raw = run("measure_clock arm") or ""
        try:
            clock_hz = int(clock_raw.split("=")[-1]) if "frequency" in clock_raw else 0
        except Exception:
            clock_hz = 0

        def format_clock(hz):
            if hz >= 1_000_000_000: return f"{hz/1_000_000_000:.2f} GHz"
            if hz >= 1_000_000:     return f"{hz/1_000_000:.0f} MHz"
            return f"{hz} Hz"

        return {
            "temperature":    run("measure_temp") or "取得不可",
            "voltage":        run("measure_volts") or "N/A",
            "throttled_raw":  throttled_raw,
            "throttled_human": parse_throttled(throttled_raw),
            "clock_raw":      clock_raw,
            "clock_human":    format_clock(clock_hz),
        }

    # --- ここから x86 等のフォールバック ---
    def format_clock_mhz(mhz):
        if not mhz: return "不明"
        return f"{mhz/1000:.2f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"

    # 温度
    temp_human = "取得不可"
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False) or {}
        for key in ("coretemp", "k10temp", "acpitz", "cpu-thermal"):
            if key in temps and temps[key]:
                vals = [x.current for x in temps[key] if isinstance(x.current, (int, float))]
                if vals:
                    temp_human = f"temp={sum(vals)/len(vals):.1f}'C"
                    break
    except Exception:
        pass

    # 周波数
    freq = psutil.cpu_freq()

    return {
        "temperature":     temp_human,
        "voltage":         "N/A",
        "throttled_raw":   "non-rpi",
        "throttled_human": ["非対応（Raspberry Pi 専用機能）"],
        "clock_raw":       f"frequency({int(freq.current)}MHz)" if freq else "frequency(unknown)",
        "clock_human":     format_clock_mhz(freq.current if freq else None),
    }

@app.route("/api/cpu_usage")
@admin_required
def api_cpu_usage():
    try:
        usage = psutil.cpu_percent(interval=0.5, percpu=True)
        return {"cpu": usage}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/api/temp_sensor")
@admin_required
def temp_sensor():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT token, secret FROM switchbot_tokens ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    db.close()
    if not row:
        return {"error": "SwitchBotトークンが未登録です"}, 500

    token = row["token"]
    secret = row["secret"]

    def generate_headers():
        t = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        body = ""
        string_to_sign = token + t + nonce + body
        sign = base64.b64encode(hmac.new(
            secret.encode("utf-8"),
            msg=string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()).decode("utf-8")
        return {"Authorization": token, "sign": sign, "nonce": nonce, "t": t, "Content-Type": "application/json"}

    def get_status(device_id):
        try:
            headers = generate_headers()
            url = f"https://api.switch-bot.com/v1.1/devices/{device_id}/status"
            res = requests.get(url, headers=headers, timeout=10)
            data = res.json()
            if data.get("statusCode") == 100:
                b = data["body"]
                return {"temperature": b.get("temperature"), "humidity": b.get("humidity"), "device_id": device_id}
            else:
                return {"error": f"APIエラー: statusCode {data.get('statusCode')}", "device_id": device_id}
        except Exception as e:
            return {"error": f"通信エラー: {str(e)}", "device_id": device_id}

    return {"indoor": get_status("DD25F897C8B8"), "outdoor": get_status("E8DD055523AE")}

# =====================================
# ⑦ 共通フック（before/after_request）
# =====================================
@app.before_request
def before_every_request():
    g._req_start = time.time()

    try:
        _ensure_upload_security_schema_once()
    except Exception as exc:
        app.logger.warning(f"upload security schema ensure failed: {exc}")

    if _requires_csrf_protection():
        csrf_error = _validate_csrf_request()
        if csrf_error:
            return csrf_error

    # ★ 管理パス(/admin...) は admin 以外には 404 を返す
    #    - 未ログイン
    #    - 一般ユーザー
    #    どちらも 404 にすることで /admin の存在自体を隠す
    if request.path.startswith("/admin"):
        if session.get("user") != "admin":
            abort(404)

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_mode'")
    mode = cursor.fetchone()
    cursor.execute("SELECT value FROM settings WHERE `key` = 'maintenance_until'")
    until_row = cursor.fetchone()
    db.close()

    until_time = None
    if until_row and until_row["value"]:
        try:
            utc_dt = dateutil_parser.isoparse(until_row["value"])
            until_time = utc_dt.astimezone(JST)
        except Exception as e:
            app.logger.warning(f"[Timer Parse Error] {e}")

    if mode and mode["value"] == "on":
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        if until_time and now >= until_time.astimezone(timezone.utc):
            flag_path = "/tmp/mfu_restart.flag"
            if os.path.exists(flag_path):
                os.remove(flag_path)
                threading.Thread(target=delayed_restart).start()
                return "🌀 自動再起動中...", 503
            else:
                threading.Thread(target=auto_end_maintenance).start()
                return "🌀 メンテナンス解除中...", 503

        if session.get("user") != "admin" and not request.path.startswith(("/login", "/static", "/favicon", "/api", "/payout")):
            return render_template("maintenance.html", until_time=until_time), 503

    # アプリ内ブラウザ（LINE/X/Instagram）への警告
    if "user" not in session:
        _ensure_inapp_browser_settings_defaults()
        warning_enabled = str(
            _get_setting_value(INAPP_BROWSER_WARNING_ENABLED_KEY, INAPP_BROWSER_DEFAULT_ENABLED)
            or INAPP_BROWSER_DEFAULT_ENABLED
        ).strip()
        if warning_enabled == "1" and _is_inapp_browser_request(request):
            return render_template("inapp_warning.html"), 200

@app.after_request
def finalize_response(response):
    # --- 1) No-Cache ヘッダ ---
    try:
        if (request.endpoint or "") != "static":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception as e:
        app.logger.warning(f"after_request(no-cache) failed: {e}")

    # --- 2) アクセスログ（委譲） ---
    try:
        log_access(request, response, session, endpoint=request.endpoint)
    except Exception as e:
        app.logger.warning(f"log_access failed: {e}")

    return response

# ─────────────────────────────────────────
# 管理: ノードメトリクス集約表示（103.16 / 103.15）
# ─────────────────────────────────────────
@app.route("/admin/nodes")
@admin_required
def admin_nodes():
    token = os.environ.get("NODE_METRICS_TOKEN", "")  # 任意。未設定なら無認証
    headers = {"X-Node-Token": token} if token else {}

    targets = [
        {"name": "103.15 (Raspberry Pi)", "url": "http://192.168.103.15:5055/metrics"},
        {"name": "103.16 (x86)",   "url": "http://192.168.103.16:5055/metrics"},
        {"name": "103.17 (MySQL)", "url": "http://192.168.103.17:5055/metrics"},
        {"name": "103.21 (FreePBX)", "url": "http://192.168.103.21:5055/metrics"},

    ]

    results = []
    for t in targets:
        info = {
            "name": t["name"],
            "url": t["url"],
            "ok": False,
            "data": {"host": "unknown", "os": "unknown", "os_version": "unknown"},
            "error": None,
        }
        try:
            r = requests.get(t["url"], headers=headers, timeout=2)
            r.raise_for_status()
            data = r.json() or {}
            data["host"] = data.get("host") or "unknown"
            data["os"] = data.get("os") or "unknown"
            data["os_version"] = data.get("os_version") or "unknown"
            if "runtime" in data:
                data["runtime"] = data.get("runtime")
            info["data"] = data
            info["ok"] = True
        except Exception as e:
            info["error"] = str(e)
        results.append(info)

    return render_template("admin_nodes.html", nodes=results, now=int(time.time()))

# ─────────────────────────────────────────
# 管理: ノードメトリクス JSON（/admin/nodes/data）
# ─────────────────────────────────────────
@app.route("/admin/nodes/data")
@admin_required
def admin_nodes_data():
    token = os.environ.get("NODE_METRICS_TOKEN", "")
    headers = {"X-Node-Token": token} if token else {}

    targets = [
        {"name": "103.15 (Raspberry Pi)", "url": "http://192.168.103.15:5055/metrics"},
        {"name": "103.16 (x86)",   "url": "http://192.168.103.16:5055/metrics"},
        {"name": "103.17 (MySQL)", "url": "http://192.168.103.17:5055/metrics"},
        {"name": "103.21 (FreePBX)", "url": "http://192.168.103.21:5055/metrics"},
    ]

    results = []
    for t in targets:
        info = {
            "name": t["name"],
            "url": t["url"],
            "ok": False,
            "data": {"host": "unknown", "os": "unknown", "os_version": "unknown"},
            "error": None,
        }
        try:
            r = requests.get(t["url"], headers=headers, timeout=2)
            r.raise_for_status()
            data = r.json() or {}
            data["host"] = data.get("host") or "unknown"
            data["os"] = data.get("os") or "unknown"
            data["os_version"] = data.get("os_version") or "unknown"
            if "runtime" in data:
                data["runtime"] = data.get("runtime")
            info["data"] = data
            info["ok"] = True
        except Exception as e:
            info["error"] = str(e)
        results.append(info)

    return jsonify({"nodes": results, "now": int(time.time())})

# ─────────────────────────────────────────
# 管理: アクセスログから即BAN（103.15へSSH実行）
# POST /admin/fw/ban  {cidr:"146.70.194.0/24"} または {ip:"146.70.194.236"}
# 戻り: {"ok":true,"status":"added|already|ok","target":"146.70.194.0/24",...}
# ─────────────────────────────────────────
@app.post("/admin/fw/ban")
@admin_required
def admin_fw_ban():
    data = request.get_json(silent=True) or request.form
    cidr_raw = (data.get("cidr") or "").strip()
    ip_raw = (data.get("ip") or "").strip()

    try:
        target = normalize_ip_target(cidr=cidr_raw, ip=ip_raw)
    except ValueError as e:
        abort(400, str(e))
    except Exception:
        abort(400, "CIDR/IPの形式が不正です")

    result = ban_ip_cidr_via_ssh(target)

    if result.get("ok"):
        return jsonify(**result), 200

    if result.get("status") == "timeout":
        return jsonify(**result), 504

    current_app.logger.error(
        "FW ban failed: status=%s, target=%s, stdout=%s, stderr=%s",
        result.get("status"),
        result.get("target"),
        result.get("stdout", ""),
        result.get("stderr", ""),
    )
    return jsonify(**result), 500

# =======================================
# 管理: 直近2000件の生アクセスログをCSVダウンロード
# =======================================
@app.route("/admin/logs/export", methods=["GET", "POST"])
@admin_required
def admin_logs_export():
    """
    直近2000件の logs テーブルを CSV でダウンロード。
    フィルタは一切かけず、「生」の id/log_date/ip/log_text を吐く。
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id, log_date, ip, log_text FROM logs ORDER BY id DESC LIMIT 3000"
    )
    rows = cur.fetchall()
    db.close()

    # CSV生成（UTF-8 / 改行は LF）
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "log_date", "ip", "log_text"])
    for r in rows:
        dt = r["log_date"]
        if isinstance(dt, datetime):
            dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt_str = str(dt)
        writer.writerow([
            r["id"],
            dt_str,
            r.get("ip") or "",
            r.get("log_text") or "",
        ])
    csv_text = buf.getvalue()
    buf.close()

    # ダウンロード用レスポンス
    fname = datetime.now(JST).strftime("access_logs_%Y%m%d_%H%M%S.csv")
    resp = Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


# =====================================
# 🔷 BLUEPRINT REGISTRATION
# =====================================
from app.albums import album_bp
app.register_blueprint(album_bp, url_prefix='/album')

from app.albums.api_ext import album_api_up   # ← 追加
app.register_blueprint(album_api_up)          # ← 追加

from app.utils.upload_history import upload_history_bp
app.register_blueprint(upload_history_bp)

from app.utils.account_manage import account_bp
app.register_blueprint(account_bp)

from app.utils.layer_reply import layer_reply_bp
app.register_blueprint(layer_reply_bp)

from app.otp.routes import otp_bp
app.register_blueprint(otp_bp)

from app.routes.mfa_routes import mfa_bp
app.register_blueprint(mfa_bp)

from app.routes.timer_routes import timer_bp
app.register_blueprint(timer_bp)

from app.utils.ext_api_uploads import ext_up; app.register_blueprint(ext_up)

from app.utils.zip_stream import zip_api
app.register_blueprint(zip_api)

from .utils.service_logs import bp_service_logs
app.register_blueprint(bp_service_logs)

from app.routes.webauthn_routes import webauthn_bp
app.register_blueprint(webauthn_bp)

from app.tickets import tickets_bp
app.register_blueprint(tickets_bp)

from .payment import bp as payment_bp
app.register_blueprint(payment_bp)

app.register_blueprint(receipts_bp)

from app.records import records_api_bp, records_bp
app.register_blueprint(records_bp, url_prefix="/records")
app.register_blueprint(records_api_bp)

from app.invoice import invoice_bp
app.register_blueprint(invoice_bp)

from app.utils.logs import log_request_raw, get_fw_404_settings, save_fw_404_settings, write_login_log, log_access

from app.external_login_user.routes import bp as ext_login_bp, init_oauth as init_line_oauth
from app.external_login_user.sw_blueprint import sw_bp
from app.external_login_user.notifications import mfu_notifications_bp
init_line_oauth(app)
app.register_blueprint(ext_login_bp, url_prefix="/external-login")
app.register_blueprint(ext_login_bp, url_prefix="/e", name="external_login_user_short")
app.register_blueprint(sw_bp)
app.register_blueprint(mfu_notifications_bp)

from app.s_u_calendar.routes import s_u_calendar_bp
app.register_blueprint(s_u_calendar_bp, url_prefix="/suc")

from app.chat import chat_bp
app.register_blueprint(chat_bp)

from app.profile import profile_bp
app.register_blueprint(profile_bp)

from app.fare import fare_bp
app.register_blueprint(fare_bp)

from app.shipment_tracking import shipment_tracking_bp
app.register_blueprint(shipment_tracking_bp)

from app.bank_account import register_bank_account
register_bank_account(app)

try:
    _ensure_upload_security_schema_once()
except Exception as exc:
    app.logger.warning(f"upload security schema init skipped: {exc}")

try:
    from app.shipment_tracking.services import ensure_nav_item, ensure_shipment_tracking_schema

    ensure_shipment_tracking_schema()
    ensure_nav_item()
except Exception as exc:
    app.logger.warning(f"shipment tracking schema/nav init skipped: {exc}")
