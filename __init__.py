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

# =====================================
# 🌐 外部ライブラリ（上段に集約）
# =====================================
import bcrypt
import psutil
import pyotp
import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from PIL import Image  # （将来の画像操作に備え、既存どおり保持）

# Flask & Werkzeug
from flask import (
    Flask, request, session, redirect, render_template, url_for, flash,
    send_from_directory, send_file, abort, jsonify, current_app, after_this_request, g, Response,
)
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename, safe_join

# =====================================
# 🛠️ アプリ内ユーティリティ（上段に集約）
# =====================================
from app.utils.auth import load_user
from app.utils.db import get_db
from app.utils.file_ops import sanitize_filename, generate_thumbnail, create_zip
from app.utils.image import save_as_jpeg
from app.utils.logs import log_request_raw
from app.utils.message import generate_message
from app.utils.storage_info import get_storage_info
from app.utils.thumbs import enqueue_thumb_job
from app.utils.totp_util import get_user_otp_secret
from app.utils.whois_util import get_netinfo
from app.albums import album_bp
from app.utils.mail import send_mail 

# =====================================
# 🌏 タイムゾーン・定数
# =====================================
JST = timezone(timedelta(hours=9))
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UPLOAD_BASE_DIR = os.path.join(BASE_DIR, "uploads")
tempfile.tempdir = "/mnt/mfu/tmp"  # 明示

# =====================================
# 🚀 Flask アプリ構成
# =====================================
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

load_dotenv()
app.secret_key = os.environ.get("SECRET_KEY")

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=60)

app.config["SESSION_COOKIE_SECURE"] = True            # HTTPSのみ送信
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"         # CSRF対策の基本ライン

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.user_loader(load_user)

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

def write_login_log(username, ip):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
        (ip, f"[LOGIN] ユーザー: {username} がログインしました"),
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
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        otp_code = request.form.get("otp_code")
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

        if "step" not in session:
            # 初回ログイン試行
            session.clear()
            cursor.execute(
                "SELECT password_hash, nickname, webhook_url FROM users WHERE username = %s",
                (username,),
            )
            row = cursor.fetchone()
            db.close()

            if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
                session["username"] = username
                session["nickname"] = row["nickname"]
                otp_secret = get_user_otp_secret(username)

                if otp_secret and not is_local:
                    session["step"] = "otp_check"
                    return render_template("login.html", otp_required=True, username=username)
                else:
                    session["user"] = username
                    session["login_expires_at"] = datetime.now() + timedelta(hours=24)
                    session["login_extension_count"] = 0
                    write_login_log(username, request.remote_addr)

                    if username == "admin" and row.get("webhook_url"):
                        try:
                            login_time = datetime.now().strftime("%Y/%m/%d %H:%M")
                            login_ip = request.remote_addr
                            message = f"👤 **管理者ログイン**\n📅 ログイン日時: {login_time}\n🌐 ログインIP: {login_ip}"
                            requests.post(row["webhook_url"], json={"content": message})
                        except Exception as e:
                            print(f"Discord通知エラー: {e}")

                    return redirect(url_for("upload"))

            return render_template("login.html", error="ログイン失敗")

        else:
            # OTP 確認
            username = session.get("username")
            otp_secret = get_user_otp_secret(username)

            if otp_secret:
                totp = pyotp.TOTP(otp_secret)
                if totp.verify(otp_code):
                    session.pop("step", None)
                    session["user"] = username
                    session["login_expires_at"] = datetime.now() + timedelta(hours=24)
                    session["login_extension_count"] = 0

                    db = get_db()
                    cursor = db.cursor(dictionary=True)
                    cursor.execute(
                        "SELECT webhook_url, nickname FROM users WHERE username = %s", (username,)
                    )
                    row = cursor.fetchone()
                    db.close()

                    session["nickname"] = row["nickname"]
                    write_login_log(username, request.remote_addr)

                    if username == "admin" and row and row.get("webhook_url"):
                        try:
                            login_time = datetime.now().strftime("%Y/%m/%d %H:%M")
                            login_ip = request.remote_addr
                            message = f"👤 **管理者ログイン**\n📅 ログイン日時: {login_time}\n🌐 ログインIP: {login_ip}"
                            requests.post(row["webhook_url"], json={"content": message})
                        except Exception as e:
                            print(f"Discord通知エラー: {e}")

                    return redirect(url_for("upload"))
                else:
                    return render_template(
                        "login.html", error="OTP認証失敗", otp_required=True, username=username
                    )
            else:
                session.clear()
                return redirect(url_for("login"))

    return render_template("login.html")

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

    # 保存ルート（設定優先、なければ既定）
    storage_root = current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")
    base_dir = os.path.join(storage_root, uid)
    original_dir = os.path.join(base_dir, "original")
    thumb_dir = os.path.join(base_dir, "thumb")
    os.makedirs(original_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)

    # ファイル名の重複回避セット
    used_names = set()

    def sanitize_filename(name: str, used: set[str]) -> str:
        # Windows禁止文字やパス要素を除去
        name = re.sub(r"[\\/:*?\"<>|]", "_", name)
        name = os.path.basename(name).strip() or "unnamed"
        root, ext = os.path.splitext(name)
        ext = ext.lower()
        candidate = f"{root}{ext}"
        i = 2
        while candidate in used:
            candidate = f"{root}_{i}{ext}"
            i += 1
        used.add(candidate)
        return candidate

    # =====================================
    # ② 保存処理
    # =====================================
    filenames, failed = [], []
    saved_count = 0

    def save_file_chunked(file_storage, save_path):
        try:
            with open(save_path, "wb") as f:
                shutil.copyfileobj(file_storage.stream, f, length=1 * 1024 * 1024)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to save {file_storage.filename}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for fs in uploaded_files:
            original_name = sanitize_filename(fs.filename, used_names)
            save_path = os.path.join(original_dir, original_name)
            futures.append((original_name, executor.submit(save_file_chunked, fs, save_path)))
        for original_name, fut in futures:
            if fut.result():
                filenames.append(original_name)
                saved_count += 1
            else:
                failed.append(original_name)

    # =====================================
    # ③ DB登録（uploads / files）
    #   ※ files には created_at 列が無い前提で INSERT を修正
    # =====================================
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO uploads (uuid, title, date, expire_at, mode, username, zip_filename, password)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (uid, title, date, expire_at, mode, username, "", password),
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
    # ④ テンプレートメッセージ生成＆保存（messages）
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
        "manage_url": f"{public_base.rstrip('/')}/m/{uid}?pw={password}",
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
    # ⑤ バックグラウンド：サムネ生成 → 通知
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
    # ⑥ 完了画面
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

            if notify_method in ("discord", "both") and user and user.get("webhook_url"):
                try:
                    requests.post(user["webhook_url"], json={"content": msg}, timeout=5)
                    (logger.info if logger else print)(f"Discord通知送信完了 (uid={uid}, thumbs=off)")
                except Exception as e:
                    (logger.warning if logger else print)(f"[通知] Discord失敗: {e}")

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

        if notify_method in ("discord", "both") and user and user.get("webhook_url"):
            try:
                requests.post(user["webhook_url"], json={"content": msg}, timeout=5)
                (logger.info if logger else print)(f"Discord通知送信完了 (uid={uid})")
            except Exception as e:
                (logger.warning if logger else print)(f"[通知] Discord失敗: {e}")

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
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM uploads WHERE uuid = %s", (uuid,))
    upload = cursor.fetchone()

    if not upload:
        return "指定されたデータが存在しません", 404

    # アップロード者 or 管理者は常時閲覧可
    if "user" in session and (session["user"] == "admin" or session["user"] == upload["username"]):
        session[f"view_auth_{uuid}"] = True

    # モード情報取得（パス要否 & サムネ生成要否）
    cursor.execute(
        "SELECT require_password, generate_thumbnails FROM upload_modes WHERE username=%s AND mode=%s LIMIT 1",
        (upload["username"], upload["mode"]),
    )
    mode_row = cursor.fetchone() or {}
    require_password = bool(mode_row.get("require_password"))
    generate_thumbnails = bool(mode_row.get("generate_thumbnails"))

    # パス不要 or 空パスなら自動許可
    if (not require_password) or (not upload.get("password")):
        session[f"view_auth_{uuid}"] = True

    # パス未認証ならパス画面へ
    if request.method == "POST" and not session.get(f"view_auth_{uuid}"):
        input_pass = request.form.get("password", "")
        if input_pass != (upload.get("password") or ""):
            db.close()
            return render_template("view_password.html", uuid=uuid, error="パスワードが違います")
        session[f"view_auth_{uuid}"] = True

    if not session.get(f"view_auth_{uuid}"):
        db.close()
        return render_template("view_password.html", uuid=uuid)

    # ファイル一覧
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

    # 要求パスを正規化して実体パスへ
    target = (base_dir / subpath).resolve()

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
    # 認可チェック
    if not session.get(f"view_auth_{uuid}"):
        # 未認証なら /view へ戻す（パスまたは権限で認証）
        return redirect(url_for("view_upload", uuid=uuid))

    # uploads レコード取得
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT id, title, username FROM uploads WHERE uuid=%s", (uuid,))
    upload = cursor.fetchone()
    if not upload:
        db.close()
        return "指定されたデータが存在しません", 404

    # モードのサムネフラグ確認（OFFのときのみボタンを想定）
    cursor.execute(
        "SELECT generate_thumbnails FROM upload_modes WHERE username=%s AND mode=(SELECT mode FROM uploads WHERE uuid=%s) LIMIT 1",
        (upload["username"], uuid),
    )
    mode_row = cursor.fetchone() or {}
    generate_thumbnails = bool(mode_row.get("generate_thumbnails"))
    db.close()

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
# 管理: ログ閲覧（高速化版：SQL事前絞り + TTLキャッシュ + has_nextページング）
# /suc/ アクセス除外表示（SQL＆Python両層で共通管理）
# =======================================
@app.route("/admin/logs")
@admin_required
def admin_logs():
    """
    クエリ:
      kind=LOGIN|LINE_LOGIN|SMTP
      exclude_local=1
      nonjp_only=1
      exclude_suc=1          ← /suc/配下のアクセスを除外表示
      exclude_3xx=1          ← 3xxレスポンスを除外表示（※今は強制ONにする）
      limit <= 1000 (デフォルト1000)
      page >= 1
      date=YYYY-MM-DD
    """
    from ipaddress import ip_address, ip_network, IPv4Network, IPv6Network
    import time

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
    ]
    EXCLUDE_PATH_SQL_LIKES = []
    for p in EXCLUDE_PATH_PREFIXES:
        if not p:
            continue
        EXCLUDE_PATH_SQL_LIKES.append(f"%Path: {p}%")
        EXCLUDE_PATH_SQL_LIKES.append(f"% {p}%")

    # --------- クエリ取得 ----------
    selected_date = (request.args.get("date") or "").strip()
    kind = (request.args.get("kind") or "").strip().upper()  # LOGIN / LINE_LOGIN / SMTP / ""

    # 生のクエリ値
    raw_exclude_local = request.args.get("exclude_local")
    raw_nonjp_only    = request.args.get("nonjp_only")
    raw_exclude_suc   = request.args.get("exclude_suc")
    raw_exclude_3xx   = request.args.get("exclude_3xx")  # UI用に残すだけ

    # まずは「値がある場合」の通常パース
    exclude_local = (raw_exclude_local or "").lower() in ("1", "true", "on", "yes")
    nonjp_only    = (raw_nonjp_only or "").lower() in ("1", "true", "on", "yes")
    exclude_suc   = (raw_exclude_suc or "").lower() in ("1", "true", "on", "yes")

    # ★ 3xx は常に非表示にする（クエリ指定は無視）
    exclude_3xx = True

    # ★初期設定★
    # クエリパラメータが一切無い最初のアクセスだけ、
    # ローカル除外 / /suc 除外 はデフォルトONにする。
    if not request.args:
        exclude_local = True
        exclude_suc = True
        # exclude_3xx は上で常に True にしているのでここでは触らない

    try:
        per_page = int(request.args.get("limit", "1000"))
    except ValueError:
        per_page = 1000
    per_page = max(1, min(1000, per_page))

    try:
        page = max(1, int(request.args.get("page", "1")))
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

    def _text_contains_excluded_path(text: str) -> bool:
        if not text:
            return False
        t = text
        for p in EXCLUDE_PATH_PREFIXES:
            if p and (f"Path: {p}" in t or f" {p}" in t):
                return True
        return False

    def _parse_status_code(text: str):
        """
        ログ本文からステータスコード(3桁)をざっくり抽出。
        例:
          'GET /foo 302 UA=...' → 302
          '302 album.view_child /album/...' → 302
        うまく取れなければ None。
        """
        if not text:
            return None

        m = re.search(r"(^|\s)(\d{3})(\s|$)", text)
        if not m:
            return None
        try:
            return int(m.group(2))
        except Exception:
            return None

    # ---- ① netinfo の TTL付きLRUキャッシュ（プロセス内） + リクエスト内重複排除 ----
    global _NETINFO_CACHE, _NETINFO_ORDER
    try:
        _NETINFO_CACHE
    except NameError:
        _NETINFO_CACHE = {}   # ip -> (netname,country,org,asname, expiry_ts)
        _NETINFO_ORDER = []   # LRU順
    TTL_SEC = 86400 * 7       # 7日
    LRU_MAX = 10000           # 最大1万IPまで

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
        now = time.time()
        exp = now + TTL_SEC
        tup = (
            ni.get("netname", ""),
            ni.get("country", ""),
            ni.get("org", ""),
            ni.get("asname", ""),
            exp,
        )
        _NETINFO_CACHE[ip] = tup
        _NETINFO_ORDER.append(ip)
        if len(_NETINFO_ORDER) > LRU_MAX:
            drop_ip = _NETINFO_ORDER.pop(0)
            _NETINFO_CACHE.pop(drop_ip, None)

    _req_seen = {}

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
        rec = {
            "netname": ni.get("netname", ""),
            "country": ni.get("country", ""),
            "org": ni.get("org", ""),
            "asname": ni.get("asname", ""),
        }
        _cache_put(ip, rec)
        _req_seen[ip] = rec
        return rec

    def enrich_row(r):
        ip = (r.get("ip") or "").strip()
        ni = get_netinfo_fast(ip) if ip else {
            "netname": "",
            "country": "",
            "org": "",
            "asname": "",
        }
        r["netname"] = ni.get("netname", "")
        r["country"] = ni.get("country", "")
        r["provider"] = (
            ni.get("org") or ni.get("asname") or ni.get("netname") or ""
        )
        return r

    # ---- ② SQLで事前にできるだけ絞る（kind / exclude_local / date / exclude_suc） ----
    db = get_db()
    cursor = db.cursor(dictionary=True)

    where = []
    params = []

    # 日付
    if selected_date and _valid_date(selected_date):
        where.append(
            "log_date >= %s AND log_date < DATE_ADD(%s, INTERVAL 1 DAY)"
        )
        params += [selected_date, selected_date]

    # kind（テキスト検索だが、まずはDBで粗く絞る）
    if kind == "LOGIN":
        where.append("INSTR(log_text,'[LOGIN]') > 0")
    elif kind == "LINE_LOGIN":
        where.append("INSTR(log_text,'[LINE_LOGIN]') > 0")
    elif kind == "SMTP":
        where.append("INSTR(log_text,'[SMTP]') > 0")

    # ローカル除外（IPは文字列格納想定）
    if exclude_local and LOCAL_SQL_LIKE_PREFIXES:
        placeholders = " OR ".join(
            ["ip LIKE %s"] * len(LOCAL_SQL_LIKE_PREFIXES)
        )
        where.append(f"NOT ({placeholders})")
        params.extend([p + "%" for p in LOCAL_SQL_LIKE_PREFIXES])

    # /suc/ 等パス除外
    if exclude_suc and EXCLUDE_PATH_SQL_LIKES:
        placeholders = " OR ".join(
            ["log_text LIKE %s"] * len(EXCLUDE_PATH_SQL_LIKES)
        )
        where.append(f"NOT ({placeholders})")
        params.extend(EXCLUDE_PATH_SQL_LIKES)

    base_sql = "SELECT id, log_date, ip, log_text FROM logs"
    if where:
        base_sql += " WHERE " + " AND ".join(where)
    base_sql += " ORDER BY id DESC"

    # ---- ③ ページング: has_next 方式で軽く。nonjp_only / 3xx除外 はPython側で間引き ----
    target_needed = per_page + 1
    scan_chunk = (
        max(per_page * 3, 1000) if nonjp_only else max(per_page, 500)
    )
    db_offset = 0
    accepted = 0
    page_rows = []
    has_next = False

    while True:
        cursor.execute(
            f"{base_sql} LIMIT %s OFFSET %s", params + [scan_chunk, db_offset]
        )
        rows = cursor.fetchall()
        if not rows:
            break

        for r in rows:
            ip = (r.get("ip") or "").strip()
            text = r.get("log_text") or ""

            # 3xx除外（ログ本文からステータスをざっくり抽出）
            if exclude_3xx:
                st = _parse_status_code(text)
                if st is not None and 300 <= st < 400:
                    continue

            # nonjp_only の場合はここで国判定
            if nonjp_only:
                tmp = enrich_row(dict(r))
                cc = (tmp.get("country") or "").upper()
                if not cc or cc == "JP":
                    continue
                r = tmp  # enrich 済み

            # SQL で取り切れなかったものの最終防衛
            if exclude_local and is_local_ip(ip):
                continue
            if exclude_suc and _text_contains_excluded_path(text):
                continue

            # start_index まではスキップ
            if accepted < start_index:
                accepted += 1
                continue

            # 収集（非 nonjp のときはここで enrich）
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
            # ここは「常にTrue」の状態をそのままUIへ渡しておく
            "exclude_3xx": exclude_3xx,
            "limit": per_page,
            "has_filters": bool(
                kind or exclude_local or nonjp_only or exclude_suc or exclude_3xx
            ),
        },
    )


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
    return render_template("admin_maintenance.html", current_mode=current_mode, current_until=current_until)

# =======================================
# 管理: 再起動
# =======================================
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

@app.route("/modes/delete/<mode>")
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

        if session.get("user") != "admin" and not request.path.startswith(("/login", "/static", "/favicon", "/api")):
            return render_template("maintenance.html", until_time=until_time), 503

    # アプリ内ブラウザ（LINE/X/Instagram）への警告
    if "user" not in session:
        skip_paths = ("/static", "/favicon", "/api", "/admin", "/maintenance", "/suc","/external-login/",)
        if not any(request.path.startswith(p) for p in skip_paths):
            ua = request.headers.get("User-Agent", "")
            ref = request.headers.get("Referer", "")

            #元コード↓
            inapp_keywords = ["Line/", "Instagram", "Twitter", "FBAN", "FBAV"]
            #inapp_keywords = ["Instagram", "Twitter", "FBAN", "FBAV"]
            inapp = any(k in ua for k in inapp_keywords)

            # 👇 X (旧Twitter) のアプリ内ブラウザは UA では判定できないので Ref で判定
            if ref.startswith("https://t.co/"):
                inapp = True

            if inapp or request.cookies.get("InAppView") == "1":
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
        info = {"name": t["name"], "url": t["url"], "ok": False, "data": None, "error": None}
        try:
            r = requests.get(t["url"], headers=headers, timeout=2)
            r.raise_for_status()
            info["data"] = r.json()
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
        info = {"name": t["name"], "url": t["url"], "ok": False, "data": None, "error": None}
        try:
            r = requests.get(t["url"], headers=headers, timeout=2)
            r.raise_for_status()
            info["data"] = r.json()
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
    ip_raw   = (data.get("ip") or "").strip()

    # 1) 入力をIPv4 CIDRに正規化（IPだけ来たら /24 へ丸める＝運用方針）
    try:
        if cidr_raw:
            net = ip_network(cidr_raw, strict=False)
            if net.version != 4:
                abort(400, "IPv4のみ対応")
            target = net.with_prefixlen
        elif ip_raw:
            ipobj = ip_address(ip_raw)
            if ipobj.version != 4:
                abort(400, "IPv4のみ対応")
            target = ip_network(f"{ipobj}/24", strict=False).with_prefixlen
        else:
            abort(400, "cidr または ip が必要です")
    except Exception:
        abort(400, "CIDR/IPの形式が不正です")

    # 2) 103.15 へSSHで ipset add → netfilter-persistent save
    host = "192.168.103.15"
    user = "root"  # sudo運用なら非rootでもOK（下のコマンドにsudo付与＆NOPASSWD設定）

    remote_cmd = (
        "PATH=/usr/sbin:/sbin:/usr/bin:/bin; "
        f"if ipset -q test badhosts {target}; then "
        "  echo ALREADY; "
        "else "
        f"  ipset add badhosts {target} -exist && echo ADDED; "
        "fi; "
        "netfilter-persistent save >/dev/null 2>&1 || true"
    )

    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        remote_cmd,
    ]

    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired as e:
        return jsonify(ok=False, status="timeout", target=target, message=str(e)), 504

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 0:
        if "ADDED" in stdout:
            status = "added"
        elif "ALREADY" in stdout:
            status = "already"
        else:
            status = "ok"
        return jsonify(ok=True, status=status, target=target, stdout=stdout, stderr=stderr), 200
    else:
        current_app.logger.error("FW ban failed: rc=%s, target=%s, stdout=%s, stderr=%s",
                                 proc.returncode, target, stdout, stderr)
        return jsonify(ok=False, status="error", rc=proc.returncode, target=target,
                       stdout=stdout, stderr=stderr), 500

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

from app.routes.timer_routes import timer_bp
app.register_blueprint(timer_bp)

from app.utils.ext_api_uploads import ext_up; app.register_blueprint(ext_up)

from app.utils.zip_stream import zip_api
app.register_blueprint(zip_api)

from .utils.service_logs import bp_service_logs
app.register_blueprint(bp_service_logs)

from app.tickets import tickets_bp
app.register_blueprint(tickets_bp)

from .payment import bp as payment_bp
app.register_blueprint(payment_bp)

from app.utils.logs import log_request_raw, write_login_log, log_access

from app.external_login_user.routes import bp as ext_login_bp, init_oauth as init_line_oauth
init_line_oauth(app)
app.register_blueprint(ext_login_bp, url_prefix="/external-login")
app.register_blueprint(ext_login_bp, url_prefix="/e", name="external_login_user_short")

from app.s_u_calendar.routes import s_u_calendar_bp
app.register_blueprint(s_u_calendar_bp, url_prefix="/suc")