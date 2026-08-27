# /app/utils/ext_api_uploads.py
# ------------------------------------------------------------
# MFU: Windowsクライアント連携用（アップロード機能拡張API）
# 保存先は /mnt/mfu/uploads/<uuid>/{original, thumb}
# - /api/ext/up/create   : アップロード枠を作成し uuid 発行（DB: uploads へ登録）
# - /api/ext/up/original : 原本ファイルを保存＆ files テーブルへ登録
# - /api/ext/up/thumb    : 生成済みサムネ (webp) を保存（表示は既存の view が利用）
#
# 認証:
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
import hashlib
import os
import uuid as _uuid
import secrets
import threading
import re
import tempfile
from datetime import datetime, timedelta
from typing import Optional, Tuple
from flask import Blueprint, request, jsonify, abort, current_app, g
from app.utils.upload_security import (
    AUTH_PASSWORD,
    create_upload_access_token_hash,
    hash_upload_password,
    normalize_upload_auth_method,
)
from app.utils.upload_security import detect_mime_from_bytes
from app.utils.uploader_auth import (
    TOKEN_SCOPE_DESKTOP,
    TOKEN_SCOPE_IOS,
    verify_uploader_token,
)
from app.utils.ios_upload_images import (
    IOSUploadImageError,
    convert_heif_to_jpeg,
    looks_like_heif,
)
from app.utils.thumbs import enqueue_thumb_job

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

# ---- Blueprint ----
ext_up = Blueprint("ext_up", __name__, url_prefix="/api/ext/up")
ios_up = Blueprint("ios_up", __name__, url_prefix="/api/ios-upload/v1")

# ---- ユーティリティ ----
def _auth_required() -> str:
    """MFU Windows Uploader 専用トークンを検証し、紐づく username を返す。"""
    token_row = verify_uploader_token(
        allowed_scopes={TOKEN_SCOPE_DESKTOP, TOKEN_SCOPE_IOS}
    )
    if not token_row:
        abort(401)
    username = (token_row.get("username") or "").strip()
    if not username:
        abort(403)
    g.uploader_username = username
    g.uploader_token_id = int(token_row.get("id") or 0)
    g.uploader_token_scope = str(token_row.get("scope") or TOKEN_SCOPE_DESKTOP)
    return username


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
        raw = str(d).strip()
        date_format = "%Y%m%d" if re.fullmatch(r"\d{8}", raw) else "%Y-%m-%d"
        dt = datetime.strptime(raw, date_format)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _unique_path(dst_dir: str, filename: str) -> Tuple[str, str]:
    """同名が存在したら (n) を付け、空ファイルを原子的に予約する。"""
    root, ext = os.path.splitext(filename)
    candidate = filename
    n = 1
    while True:
        full = os.path.join(dst_dir, candidate)
        try:
            fd = os.open(full, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            os.close(fd)
            return full, candidate
        except FileExistsError:
            candidate = f"{root}({n}){ext}"
            n += 1


_transfer_schema_lock = threading.Lock()
_transfer_schema_ready = False


def _ensure_transfer_schema() -> None:
    global _transfer_schema_ready
    if _transfer_schema_ready:
        return
    with _transfer_schema_lock:
        if _transfer_schema_ready:
            return
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_file_transfers (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    upload_id BIGINT NOT NULL,
                    client_file_id VARCHAR(64) NOT NULL,
                    original_filename VARCHAR(255) NOT NULL,
                    saved_filename VARCHAR(255) NULL,
                    expected_sha256 CHAR(64) NOT NULL,
                    actual_sha256 CHAR(64) NULL,
                    file_size BIGINT UNSIGNED NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    error_message VARCHAR(1024) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_upload_file_transfer (upload_id, client_file_id),
                    INDEX ix_upload_file_transfer_status (status, updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            db.commit()
            _transfer_schema_ready = True
        finally:
            cur.close()
            db.close()


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


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


def _resolve_mode_config(username: str, mode: str) -> Optional[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """SELECT username, mode, label, enable_download_url, require_password, auth_method,
                  enable_layer_upload_url, generate_thumbnails, template_key
           FROM upload_modes
           WHERE mode = %s
             AND (username = %s OR username IS NULL OR username = '' OR username = '*')
           ORDER BY
             CASE
               WHEN username = %s THEN 0
               WHEN username = '*' THEN 1
               WHEN username = '' THEN 2
               WHEN username IS NULL THEN 3
               ELSE 9
             END
           LIMIT 1
        """,
        (mode, username, username),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _enabled(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _reconcile_upload_thumbnails(username: str, uuid32: str) -> dict:
    """原本に対応するWebPの不足を確認し、モード設定ONの場合だけ生成ジョブを登録する。"""
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, mode, username FROM uploads WHERE uuid=%s AND username=%s LIMIT 1",
            (uuid32, username),
        )
        upload_row = cur.fetchone()
        if not upload_row:
            return {"ok": False, "error": "unknown uuid", "status": 404}
        cur.execute("SELECT filename FROM files WHERE upload_id=%s ORDER BY id", (upload_row["id"],))
        filenames = [str(row.get("filename") or "") for row in (cur.fetchall() or [])]
    finally:
        cur.close()
        db.close()

    thumb_dir = os.path.join(UPLOAD_BASE_DIR, uuid32, "thumb")
    missing: list[str] = []
    for filename in filenames:
        name_wo_ext, _ = os.path.splitext(filename)
        if not os.path.isfile(os.path.join(thumb_dir, name_wo_ext + ".webp")):
            missing.append(filename)

    mode_cfg = _resolve_mode_config(username, upload_row["mode"]) or {}
    server_generation_enabled = _enabled(mode_cfg.get("generate_thumbnails"))
    queued = False
    if missing and server_generation_enabled:
        enqueue_thumb_job("upload", uuid32, "thumb")
        queued = True

    return {
        "ok": True,
        "uuid": uuid32,
        "original_count": len(filenames),
        "thumbnail_count": len(filenames) - len(missing),
        "missing_count": len(missing),
        "server_generation_enabled": server_generation_enabled,
        "queued": queued,
    }

# ---- ルーティング ----
def _ensure_upload_row(uuid32: str, username: str) -> Optional[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, username FROM uploads WHERE uuid=%s", (uuid32,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    if (row.get("username") or "").strip() != username:
        abort(403)
    return row


@ios_up.route("/create", methods=["POST"])
@ext_up.route("/create", methods=["POST"])
def create_upload():
    """アップロード枠の作成"""
    username = _auth_required()
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    raw_date = data.get("date")
    date_str = _parse_date(raw_date)
    if getattr(g, "uploader_token_scope", "") == TOKEN_SCOPE_IOS:
        normalized_raw = str(raw_date or "").strip()
        if normalized_raw and normalized_raw not in {
            date_str,
            date_str.replace("-", ""),
        }:
            return jsonify({"ok": False, "error": "date must be yyyymmdd"}), 400
    mode = (data.get("mode") or "").strip()
    if getattr(g, "uploader_token_scope", "") == TOKEN_SCOPE_IOS and not title:
        return jsonify({"ok": False, "error": "missing title"}), 400
    if not mode:
        return jsonify({"ok": False, "error": "missing mode"}), 400

    mode_config = _resolve_mode_config(username, mode)
    if not mode_config:
        return jsonify({"ok": False, "error": "unknown mode"}), 400

    auth_method = normalize_upload_auth_method(
        mode_config.get("auth_method"),
        require_password=mode_config.get("require_password"),
    )

    uuid32 = _uuid.uuid4().hex
    password = secrets.token_hex(4) if auth_method == AUTH_PASSWORD else ""
    password_hash = hash_upload_password(password) if password else None
    access_token_hash = create_upload_access_token_hash(uuid32, auth_method)
    _mk_dirs(uuid32)
    expire_at = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT INTO uploads (uuid, title, date, expire_at, mode, username, zip_filename, password, password_hash, auth_method, access_token_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (uuid32, title, date_str, expire_at, mode, username, "", password, password_hash, auth_method, access_token_hash),
    )
    db.commit()
    cur.close()
    return jsonify({"ok": True, "uuid": uuid32, "password": password})


@ios_up.route("/original", methods=["POST"])
@ext_up.route("/original", methods=["POST"])
def push_original():
    """原本ファイルの保存"""
    username = _auth_required()
    uuid32 = (request.form.get("uuid") or "").strip()
    file = request.files.get("file")
    if not uuid32 or not file:
        return jsonify({"ok": False, "error": "missing uuid/file"}), 400

    upload_row = _ensure_upload_row(uuid32, username)
    if upload_row is None:
        return jsonify({"ok": False, "error": "unknown uuid"}), 404
    upload_id = int(upload_row["id"])

    o_dir = os.path.join(UPLOAD_BASE_DIR, uuid32, "original")
    os.makedirs(o_dir, exist_ok=True)
    safe_name = sanitize_filename(file.filename or "file", set())

    client_file_id = str(request.form.get("client_file_id") or "").strip()
    expected_sha256 = str(request.form.get("sha256") or "").strip().lower()
    expected_size_raw = str(request.form.get("file_size") or "").strip()
    verified_request = bool(client_file_id or expected_sha256 or expected_size_raw)
    if verified_request:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", client_file_id):
            return jsonify({"ok": False, "error": "invalid client_file_id"}), 400
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return jsonify({"ok": False, "error": "invalid sha256"}), 400
        try:
            expected_size = int(expected_size_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid file_size"}), 400
        if expected_size < 0:
            return jsonify({"ok": False, "error": "invalid file_size"}), 400

        _ensure_transfer_schema()
        lock_name = "mfu_up_{}_{}".format(
            upload_id,
            hashlib.sha256(client_file_id.encode("utf-8")).hexdigest()[:24],
        )
        db = get_db()
        cur = db.cursor(dictionary=True)
        tmp_path = ""
        lock_acquired = False
        try:
            cur.execute("SELECT GET_LOCK(%s, 60) AS acquired", (lock_name,))
            lock_row = cur.fetchone() or {}
            lock_acquired = int(lock_row.get("acquired") or 0) == 1
            if not lock_acquired:
                return jsonify({"ok": False, "error": "transfer lock timeout"}), 503

            cur.execute(
                "SELECT * FROM upload_file_transfers WHERE upload_id=%s AND client_file_id=%s LIMIT 1",
                (upload_id, client_file_id),
            )
            transfer = cur.fetchone()
            if transfer and (
                str(transfer.get("expected_sha256") or "").lower() != expected_sha256
                or int(transfer.get("file_size") if transfer.get("file_size") is not None else -1) != expected_size
                or str(transfer.get("original_filename") or "") != safe_name
            ):
                return jsonify({"ok": False, "error": "client_file_id metadata conflict"}), 409

            if not transfer:
                final_path, saved_name = _unique_path(o_dir, safe_name)
                now = datetime.now()
                try:
                    cur.execute(
                        """
                        INSERT INTO upload_file_transfers (
                            upload_id, client_file_id, original_filename, saved_filename,
                            expected_sha256, file_size, status, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                        """,
                        (upload_id, client_file_id, safe_name, saved_name, expected_sha256, expected_size, now, now),
                    )
                    db.commit()
                except Exception:
                    try:
                        os.remove(final_path)
                    except OSError:
                        pass
                    raise
            else:
                saved_name = str(transfer.get("saved_filename") or "")
                if not saved_name:
                    final_path, saved_name = _unique_path(o_dir, safe_name)
                    cur.execute(
                        "UPDATE upload_file_transfers SET saved_filename=%s, status='pending', updated_at=%s WHERE id=%s",
                        (saved_name, datetime.now(), transfer["id"]),
                    )
                    db.commit()
                else:
                    final_path = os.path.join(o_dir, saved_name)

            if os.path.isfile(final_path) and os.path.getsize(final_path) == expected_size:
                actual_sha256, actual_size = _hash_file(final_path)
                if actual_sha256 == expected_sha256 and actual_size == expected_size:
                    cur.execute(
                        "SELECT id FROM files WHERE upload_id=%s AND filename=%s LIMIT 1",
                        (upload_id, saved_name),
                    )
                    if not cur.fetchone():
                        cur.execute("INSERT INTO files (upload_id, filename) VALUES (%s,%s)", (upload_id, saved_name))
                    cur.execute(
                        """
                        UPDATE upload_file_transfers
                           SET actual_sha256=%s, status='success', error_message=NULL, updated_at=%s
                         WHERE upload_id=%s AND client_file_id=%s
                        """,
                        (actual_sha256, datetime.now(), upload_id, client_file_id),
                    )
                    db.commit()
                    return jsonify({
                        "ok": True,
                        "saved": saved_name,
                        "uuid": uuid32,
                        "sha256": actual_sha256,
                        "file_size": actual_size,
                        "already_uploaded": True,
                    })

            fd, tmp_path = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=o_dir)
            digest = hashlib.sha256()
            actual_size = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = file.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    actual_size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            actual_sha256 = digest.hexdigest()
            if actual_size != expected_size or actual_sha256 != expected_sha256:
                cur.execute(
                    """
                    UPDATE upload_file_transfers
                       SET actual_sha256=%s, status='failed', error_message=%s, updated_at=%s
                     WHERE upload_id=%s AND client_file_id=%s
                    """,
                    (
                        actual_sha256,
                        f"checksum mismatch expected_size={expected_size} actual_size={actual_size}",
                        datetime.now(),
                        upload_id,
                        client_file_id,
                    ),
                )
                db.commit()
                os.remove(tmp_path)
                tmp_path = ""
                return jsonify({
                    "ok": False,
                    "error": "checksum mismatch",
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                }), 422

            os.replace(tmp_path, final_path)
            tmp_path = ""
            os.chmod(final_path, 0o640)
            cur.execute(
                "SELECT id FROM files WHERE upload_id=%s AND filename=%s LIMIT 1",
                (upload_id, saved_name),
            )
            if not cur.fetchone():
                cur.execute("INSERT INTO files (upload_id, filename) VALUES (%s,%s)", (upload_id, saved_name))
            cur.execute(
                """
                UPDATE upload_file_transfers
                   SET actual_sha256=%s, status='success', error_message=NULL, updated_at=%s
                 WHERE upload_id=%s AND client_file_id=%s
                """,
                (actual_sha256, datetime.now(), upload_id, client_file_id),
            )
            db.commit()
            return jsonify({
                "ok": True,
                "saved": saved_name,
                "uuid": uuid32,
                "sha256": actual_sha256,
                "file_size": actual_size,
                "already_uploaded": False,
            })
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if lock_acquired:
                try:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                    cur.fetchone()
                except Exception:
                    pass
            cur.close()
            db.close()

    is_ios = getattr(g, "uploader_token_scope", "") == TOKEN_SCOPE_IOS
    header = file.stream.read(8192)
    file.stream.seek(0)
    detected_mime = detect_mime_from_bytes(header)
    heif_input = looks_like_heif(header)
    if is_ios and detected_mime not in {"image/jpeg", "image/png", "image/heif-bmff"}:
        return jsonify({"ok": False, "error": "JPEG / PNG / HEIC / HEIF の写真だけを送信できます。"}), 400

    if is_ios and heif_input:
        fd, source_path = tempfile.mkstemp(prefix=".ios-upload-", suffix=".heic", dir=o_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = file.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            _full, real_name = convert_heif_to_jpeg(source_path, o_dir, safe_name)
        except IOSUploadImageError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422
        finally:
            try:
                os.remove(source_path)
            except OSError:
                pass
    else:
        full, real_name = _unique_path(o_dir, safe_name)
        try:
            file.save(full)
        except Exception:
            try:
                os.remove(full)
            except OSError:
                pass
            raise
        os.chmod(full, 0o640)

    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO files (upload_id, filename) VALUES (%s,%s)", (upload_id, real_name))
    db.commit()
    cur.close()
    return jsonify({
        "ok": True,
        "saved": real_name,
        "uuid": uuid32,
        "converted_to_jpeg": bool(is_ios and heif_input),
    })


@ext_up.route("/thumb", methods=["POST"])
def push_thumb():
    """生成済みサムネの保存（webp 推奨）"""
    username = _auth_required()
    uuid32 = (request.form.get("uuid") or "").strip()
    base_name = (request.form.get("base") or "").strip()
    file = request.files.get("file")
    if not uuid32 or not base_name or not file:
        return jsonify({"ok": False, "error": "missing uuid/base/file"}), 400

    upload_row = _ensure_upload_row(uuid32, username)
    if upload_row is None:
        return jsonify({"ok": False, "error": "unknown uuid"}), 404

    t_dir = os.path.join(UPLOAD_BASE_DIR, uuid32, "thumb")
    os.makedirs(t_dir, exist_ok=True)

    base_safe = sanitize_filename(base_name, set())
    name_wo_ext, _ = os.path.splitext(base_safe)
    save_name = f"{name_wo_ext}.webp"
    full = os.path.join(t_dir, save_name)
    file.save(full)
    return jsonify({"ok": True, "saved": save_name, "uuid": uuid32})


@ext_up.route("/reconcile-thumbnails", methods=["POST"])
def reconcile_thumbnails():
    """通知を発生させず、不足サムネイルのサーバー補完を依頼する。"""
    username = _auth_required()
    data = request.get_json(silent=True) or {}
    uuid32 = str(data.get("uuid") or "").strip()
    if not uuid32:
        return jsonify({"ok": False, "error": "missing uuid"}), 400
    result = _reconcile_upload_thumbnails(username, uuid32)
    status = int(result.pop("status", 200))
    return jsonify(result), status


@ios_up.route("/config", methods=["GET"])
@ext_up.route("/modes", methods=["GET"])
def list_modes():
    """指定ユーザーの upload_modes 一覧を返すAPI。"""
    username = _auth_required()
    include_global = (request.args.get("include_global", "1").lower() in ("1", "true", "yes"))

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT default_mode FROM users WHERE username = %s", (username,))
    urow = cur.fetchone() or {}
    default_mode = (urow.get("default_mode") or "")

    if include_global:
        cur.execute(
            """SELECT username, mode, label, enable_download_url, require_password, auth_method,
                      enable_layer_upload_url, generate_thumbnails, template_key
               FROM upload_modes
               WHERE (username = %s OR username IS NULL OR username = '' OR username = '*')
               ORDER BY mode""",
            (username,),
        )
    else:
        cur.execute(
            """SELECT username, mode, label, enable_download_url, require_password, auth_method,
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
    return jsonify({
        "ok": True,
        "username": username,
        "default_mode": default_mode,
        "modes": modes,
        "accepted_extensions": ["jpg", "jpeg", "png", "heic", "heif"],
        "date_format": "yyyymmdd",
        "api_version": 1,
    })


def db_ping():
    """DB接続確認API"""
    username = _auth_required()
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


_ios_completion_schema_ready = False
_ios_completion_schema_lock = threading.Lock()


def _claim_ios_completion(upload_id: int) -> bool:
    """Return True only for the first iOS completion request for an upload."""
    global _ios_completion_schema_ready
    if not _ios_completion_schema_ready:
        with _ios_completion_schema_lock:
            if not _ios_completion_schema_ready:
                db = get_db()
                cur = db.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ios_shortcut_upload_completions (
                        upload_id INT NOT NULL PRIMARY KEY,
                        completed_at DATETIME NOT NULL,
                        CONSTRAINT fk_ios_shortcut_upload_completion_upload
                          FOREIGN KEY (upload_id) REFERENCES uploads(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                db.commit()
                cur.close()
                db.close()
                _ios_completion_schema_ready = True

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT IGNORE INTO ios_shortcut_upload_completions (upload_id, completed_at) "
            "VALUES (%s, UTC_TIMESTAMP())",
            (int(upload_id),),
        )
        claimed = cur.rowcount > 0
        db.commit()
        return claimed
    finally:
        cur.close()
        db.close()


# __init__.py 側にある通知・完了画面用関数をインポート
from app import _prepare_upload_completion, background_thumb_and_notify


@ios_up.route("/done", methods=["POST"])
@ext_up.route("/done", methods=["POST"])
def mark_upload_done():
    """アップロード完了通知API"""
    username = _auth_required()
    data = request.get_json(silent=True) or {}
    uuid32 = (data.get("uuid") or "").strip()
    if not uuid32:
        return jsonify({"ok": False, "error": "missing uuid"}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM uploads WHERE uuid=%s AND username=%s",
        (uuid32, username),
    )
    up = cur.fetchone()
    if not up:
        cur.close(); db.close()
        return jsonify({"ok": False, "error": "unknown uuid"}), 404

    upload_id = up["id"]
    cur.execute("SELECT filename FROM files WHERE upload_id=%s ORDER BY id", (upload_id,))
    rows = cur.fetchall() or []
    filenames = [r["filename"] for r in rows]

    cur.close(); db.close()

    up["uuid"] = uuid32
    prepared = _prepare_upload_completion(up, filenames)
    mode_cfg = prepared.get("mode_config") or {}
    gen_thumbs = _enabled(mode_cfg.get("generate_thumbnails"))
    context = prepared.get("context") or {}
    template_key = str(prepared.get("template_key") or up["mode"])

    base_dir = os.path.join(UPLOAD_BASE_DIR, uuid32)
    original_dir = os.path.join(base_dir, "original")
    thumb_dir = os.path.join(base_dir, "thumb")

    from flask import current_app
    app_obj = current_app._get_current_object()

    def _runner():
        try:
            with app_obj.app_context():
                background_thumb_and_notify(
                    uuid32, filenames, original_dir, thumb_dir, template_key, context, gen_thumbs
                )
        except Exception as e:
            try:
                app_obj.logger.error(f"[done] background notify failed: {e}")
            except Exception:
                print(f"[done] background notify failed: {e}")

    notification_started = True
    if getattr(g, "uploader_token_scope", "") == TOKEN_SCOPE_IOS:
        notification_started = _claim_ios_completion(int(upload_id))
    if notification_started:
        threading.Thread(target=_runner, daemon=True).start()
    public_base = str(context.get("base_url") or "https://mfu.iori0624.jp").rstrip("/")
    return jsonify({
        "ok": True,
        "completion_url": f"{public_base}/upload/done/{uuid32}",
        "view_url": str(context.get("link") or f"{public_base}/view/{uuid32}"),
        "message": str(prepared.get("message") or ""),
        "uploaded_count": len(filenames),
        "notification_started": notification_started,
    })
