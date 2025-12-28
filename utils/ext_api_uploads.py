# /app/utils/ext_api_uploads.py
# ------------------------------------------------------------
# MFU: Windowsクライアント連携用（アップロード機能拡張API）
# 保存先は /mnt/mfu/uploads/<uuid>/{original, thumb}
# - /api/ext/up/create   : アップロード枠を作成し uuid 発行（DB: uploads へ登録）
# - /api/ext/up/original : 原本ファイルを保存＆ files テーブルへ登録
# - /api/ext/up/thumb    : 生成済みサムネ (webp) を保存（表示は既存の view が利用）
#
# 認証:
#   環境変数 MFU_EXT_API_KEY がセットされている場合のみ Bearer チェックを有効化。
#   未設定（空文字）の場合は認証をスキップ（LAN 内テスト等）。
#
# 既存アプリへの組み込み:
#   from app.utils.ext_api_uploads import ext_up
#   app.register_blueprint(ext_up)
#
# 依存:
#   - app.utils.db.get_db() : MySQL接続 (conn/cursor)
#   - app.utils.file_ops.sanitize_filename(name, denyset) : ファイル名サニタイズ
#   - uploads, files テーブルスキーマ（一般的な想定に合わせています）
# ------------------------------------------------------------

from __future__ import annotations
import os
import uuid as _uuid
import secrets
import threading
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
from flask import Blueprint, request, jsonify, abort, current_app

# ---- 可変部: 既存ユーティリティの取り込み（無ければフォールバック） ----
try:
    from app.utils.db import get_db  # type: ignore
except Exception:
    # フォールバック: ダミーを定義（本番では必ず既存の get_db を使ってください）
    def get_db():
        raise RuntimeError("get_db() is not available. Ensure app.utils.db is importable.")

try:
    from app.utils.file_ops import sanitize_filename  # type: ignore
except Exception:
    # 最低限のフォールバック（全角→そのまま、禁則文字を下線へ）
    def sanitize_filename(name: str, denyset: set[str] | None = None) -> str:
        deny = denyset or set()
        # Windows禁止文字や制御文字など簡易除去
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1F]", "_", name)
        name = name.strip().strip(".")  # 先頭末尾のドットなども避ける
        for d in deny:
            name = name.replace(d, "_")
        return name or "file"

# ---- 設定 ----
UPLOAD_BASE_DIR = "/mnt/mfu/uploads"
API_KEY = os.environ.get("MFU_EXT_API_KEY", "").strip()

# ---- Blueprint ----
ext_up = Blueprint("ext_up", __name__, url_prefix="/api/ext/up")

# ---- ユーティリティ ----
def _auth_required() -> None:
    """API_KEY が空でなければ Bearer 認証を要求."""
    if not API_KEY:
        return  # 認証スキップ（テスト・LAN用途）
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401)
    token = auth.split(" ", 1)[1]
    if token != API_KEY:
        abort(403)


def _mk_dirs(uuid32: str) -> str:
    """uuid の original / thumb ディレクトリを作成してベースパスを返す."""
    base = os.path.join(UPLOAD_BASE_DIR, uuid32)
    os.makedirs(os.path.join(base, "original"), exist_ok=True)
    os.makedirs(os.path.join(base, "thumb"), exist_ok=True)
    return base


def _parse_date(d: Optional[str]) -> str:
    """UIから来る日付文字列（YYYY-MM-DD 期待）をバリデートして文字列で返す."""
    if not d:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _unique_path(dst_dir: str, filename: str) -> Tuple[str, str]:
    """同名が存在したら (n) を付けて衝突回避。"""
    root, ext = os.path.splitext(filename)
    candidate = filename
    full = os.path.join(dst_dir, candidate)
    n = 1
    while os.path.exists(full):
        candidate = f"{root}({n}){ext}"
        full = os.path.join(dst_dir, candidate)
        n += 1
    return full, candidate


def _ensure_upload_row(uuid32: str) -> Optional[int]:
    """uploads.uuid = uuid32 の行を取得して upload_id（int）を返す。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM uploads WHERE uuid=%s", (uuid32,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    if isinstance(row, dict):
        return int(row.get("id"))
    return int(row[0])

# ---- ルーティング ----
@ext_up.route("/create", methods=["POST"])
def create_upload():
    """アップロード枠の作成"""
    _auth_required()
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    date_str = _parse_date(data.get("date"))
    mode = (data.get("mode") or "").strip()
    username = (data.get("username") or "").strip()
    require_password = bool(data.get("require_password"))

    if not mode or not username:
        return jsonify({"ok": False, "error": "missing mode/username"}), 400

    uuid32 = _uuid.uuid4().hex
    password = secrets.token_hex(4) if require_password else ""
    _mk_dirs(uuid32)
    expire_at = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO uploads (uuid, title, date, expire_at, mode, username, zip_filename, password)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (uuid32, title, date_str, expire_at, mode, username, "", password),
    )
    db.commit()
    cur.close()
    return jsonify({"ok": True, "uuid": uuid32, "password": password})


@ext_up.route("/original", methods=["POST"])
def push_original():
    """原本ファイルの保存"""
    _auth_required()
    uuid32 = (request.form.get("uuid") or "").strip()
    file = request.files.get("file")
    if not uuid32 or not file:
        return jsonify({"ok": False, "error": "missing uuid/file"}), 400

    upload_id = _ensure_upload_row(uuid32)
    if upload_id is None:
        return jsonify({"ok": False, "error": "unknown uuid"}), 404

    o_dir = os.path.join(UPLOAD_BASE_DIR, uuid32, "original")
    os.makedirs(o_dir, exist_ok=True)
    safe_name = sanitize_filename(file.filename or "file", set())
    full, real_name = _unique_path(o_dir, safe_name)
    file.save(full)

    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO files (upload_id, filename) VALUES (%s,%s)", (upload_id, real_name))
    db.commit()
    cur.close()
    return jsonify({"ok": True, "saved": real_name, "uuid": uuid32})


@ext_up.route("/thumb", methods=["POST"])
def push_thumb():
    """生成済みサムネの保存（webp 推奨）"""
    _auth_required()
    uuid32 = (request.form.get("uuid") or "").strip()
    base_name = (request.form.get("base") or "").strip()
    file = request.files.get("file")
    if not uuid32 or not base_name or not file:
        return jsonify({"ok": False, "error": "missing uuid/base/file"}), 400

    upload_id = _ensure_upload_row(uuid32)
    if upload_id is None:
        return jsonify({"ok": False, "error": "unknown uuid"}), 404

    t_dir = os.path.join(UPLOAD_BASE_DIR, uuid32, "thumb")
    os.makedirs(t_dir, exist_ok=True)

    base_safe = sanitize_filename(base_name, set())
    name_wo_ext, _ = os.path.splitext(base_safe)
    save_name = f"{name_wo_ext}.webp"
    full = os.path.join(t_dir, save_name)
    file.save(full)
    return jsonify({"ok": True, "saved": save_name, "uuid": uuid32})


@ext_up.route("/modes", methods=["GET"])
def list_modes():
    """指定ユーザーの upload_modes 一覧を返すAPI。"""
    _auth_required()
    username = (request.args.get("username") or "").strip()
    include_global = (request.args.get("include_global", "1").lower() in ("1", "true", "yes"))
    if not username:
        return jsonify({"ok": False, "error": "missing username"}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT default_mode FROM users WHERE username = %s", (username,))
    urow = cur.fetchone() or {}
    default_mode = (urow.get("default_mode") or "")

    if include_global:
        cur.execute(
            """SELECT username, mode, label, enable_download_url, require_password,
                      enable_layer_upload_url, generate_thumbnails, template_key
               FROM upload_modes
               WHERE (username = %s OR username IS NULL OR username = '' OR username = '*')
               ORDER BY mode""",
            (username,),
        )
    else:
        cur.execute(
            """SELECT username, mode, label, enable_download_url, require_password,
                      enable_layer_upload_url, generate_thumbnails, template_key
               FROM upload_modes
               WHERE username = %s
               ORDER BY mode""",
            (username,),
        )

    rows = cur.fetchall() or []
    cur.close()

    merged = {}
    for r in rows:
        m = r.get("mode")
        if not m:
            continue
        if m not in merged:
            merged[m] = r
        else:
            prev = merged[m]
            prev_user = (prev.get("username") or "")
            cur_user = (r.get("username") or "")
            if prev_user in ("", None, "*") and cur_user not in ("", None, "*"):
                merged[m] = r

    modes = list(merged.values())
    return jsonify({"ok": True, "username": username, "default_mode": default_mode, "modes": modes})


@ext_up.route("/dbping", methods=["GET"])
def db_ping():
    """DB接続確認API"""
    _auth_required()
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1")
        one = cur.fetchone()
        cur.execute("SELECT VERSION()")
        ver = cur.fetchone()
        cur.close()
        return jsonify({"ok": True, "select1": one[0] if one else None, "version": ver[0] if ver else ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# __init__.py 側にある通知関数をインポート（ファイル構成に合わせてパス調整）
from app import background_thumb_and_notify


@ext_up.route("/done", methods=["POST"])
def mark_upload_done():
    """アップロード完了通知API"""
    from datetime import date as date_cls
    _auth_required()
    data = request.get_json(silent=True) or {}
    uuid32 = (data.get("uuid") or "").strip()
    if not uuid32:
        return jsonify({"ok": False, "error": "missing uuid"}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id, mode, username, title, date, expire_at, password FROM uploads WHERE uuid=%s",
        (uuid32,),
    )
    up = cur.fetchone()
    if not up:
        cur.close(); db.close()
        return jsonify({"ok": False, "error": "unknown uuid"}), 404

    upload_id = up["id"]
    cur.execute("SELECT filename FROM files WHERE upload_id=%s ORDER BY id", (upload_id,))
    rows = cur.fetchall() or []
    filenames = [r["filename"] for r in rows]

    cur.execute("SELECT nickname FROM users WHERE username = %s", (up["username"],))
    urow = cur.fetchone() or {}
    nickname = (urow.get("nickname") or "").strip() or up["username"]

    # ★ generate_thumbnails を取得
    cur.execute(
        "SELECT enable_download_url, enable_layer_upload_url, generate_thumbnails "
        "FROM upload_modes WHERE username=%s AND mode=%s",
        (up["username"], up["mode"]),
    )
    mrow = cur.fetchone() or {}
    enable_download_url = bool(mrow.get("enable_download_url"))
    enable_layer_upload_url = bool(mrow.get("enable_layer_upload_url"))
    gen_thumbs = bool(mrow.get("generate_thumbnails"))
    cur.close(); db.close()

    d = up.get("date")
    d_str = d.strftime("%Y-%m-%d") if isinstance(d, (datetime, date_cls)) else str(d or "")
    ex = up.get("expire_at")
    expire_str = ex.strftime("%Y-%m-%d") if isinstance(ex, (datetime, date_cls)) else str(ex or "")

    base = "https://mfu.iori0624.jp".rstrip("/")
    link_url = f"{base}/view/{uuid32}" if enable_download_url else ""
    layer_url = f"{base}/layer_upload/{uuid32}" if enable_layer_upload_url else ""

    context = {
        "title": up.get("title") or "",
        "date": d_str,
        "link": link_url,
        "password": up.get("password") or "",
        "layer_upload_url": layer_url,
        "expire": expire_str,
        "username": up.get("username"),
        "nickname": nickname,
    }

    base_dir = os.path.join(UPLOAD_BASE_DIR, uuid32)
    original_dir = os.path.join(base_dir, "original")
    thumb_dir = os.path.join(base_dir, "thumb")

    from flask import current_app
    app_obj = current_app._get_current_object()

    def _runner():
        try:
            with app_obj.app_context():
                background_thumb_and_notify(
                    uuid32, filenames, original_dir, thumb_dir, up["mode"], context, gen_thumbs
                )
        except Exception as e:
            try:
                app_obj.logger.error(f"[done] background notify failed: {e}")
            except Exception:
                print(f"[done] background notify failed: {e}")

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True})
