# /mnt/mfu/app/utils/zip_stream.py
# ===[ 選択式ZIP: Python標準zipで一時ファイル生成 / UUID名 / 直下配置 / 進行状況ファイル ]===
import os
import re
import json
import uuid
import time
import zipfile
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Iterable, List, Tuple, Sequence

from werkzeug.utils import safe_join
from flask import (
    Blueprint, current_app, request, jsonify, send_file, after_this_request, session
)
from app.utils.upload_security import can_access_upload_record, fetch_upload_access_record, has_view_auth, resolve_upload_subpath

# ------------------------------------------------------------
# Blueprint（他モジュールで使っていればそのまま生かす）
zip_api = Blueprint("zip_api", __name__)
_PROGRESS_IO_LOCK = threading.RLock()

# ------------------------------------------------------------
# 設定ヘルパ
def _cfg_storage_root() -> str:
    # 通常アップロード保存先
    return current_app.config.get("STORAGE_ROOT", "/mnt/mfu/uploads")

def _cfg_albums_root() -> str:
    # （従来の単一ルート）アルバム保存先
    return current_app.config.get("ALBUMS_ROOT", "/mnt/mfu/mfu_albums")

def _cfg_album_multi_roots() -> List[str]:
    """
    アルバム保存先の候補を優先順で返す（SSD/HDD 両対応）。
    設定で明示されていればそれを優先し、無ければ既定の2ルートを含める。
    """
    roots: List[str] = []
    # 1) 既存 ALBUMS_ROOT
    base = _cfg_albums_root()
    if base:
        roots.append(base)

    # 2) 任意設定：HDD/SSD の別名キー
    alt_hdd = current_app.config.get("ALBUMS_ROOT_HDD")
    alt_ssd = current_app.config.get("ALBUMS_ROOT_SSD")
    for r in (alt_hdd, alt_ssd):
        if r and r not in roots:
            roots.append(r)

    # 3) 既定の HDD / SSD も最後に追加（重複排除）
    defaults = ["/mnt/maildata/mfu_albums", "/mnt/mfu/mfu_albums"]
    for r in defaults:
        if r and r not in roots:
            roots.append(r)

    return roots

def _cfg_tickets_root() -> str:
    # チケット保存先
    return current_app.config.get("TICKETS_ROOT", "/mnt/mfu/tickets_photo")

def _cfg_tmp_root() -> str:
    # 複数プロセス共有・書込可な場所
    return current_app.config.get("TMP_ROOT", "/mnt/mfu/tmp")

def _cfg_progress_ttl() -> int:
    # 進捗JSONを保持する秒数（UIが100%を確実に読めるように）
    return int(current_app.config.get("ZIP_PROGRESS_TTL", 60))

def _cfg_zip_file_ttl() -> int:
    # 通常ダウンロードへ渡す一時ZIPの保持秒数
    return int(current_app.config.get("ZIP_FILE_TTL", 6 * 60 * 60))

# ------------------------------------------------------------
# 進捗保存（使わない場合もあるが互換のため維持）
def _progress_dir() -> str:
    d = os.path.join(_cfg_tmp_root(), "mfu-progress")
    os.makedirs(d, exist_ok=True)
    return d

def _progress_path(key: str) -> str:
    return os.path.join(_progress_dir(), f"{key}.json")

def _lock_path(key: str) -> str:
    return os.path.join(_progress_dir(), f"{key}.lock")

def _progress_write(key: str, data: dict):
    with _PROGRESS_IO_LOCK:
        p = _progress_path(key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

def _progress_read(key: str):
    with _PROGRESS_IO_LOCK:
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

def _unlock_only(key: str):
    """ロックのみ解除（進捗は残す）"""
    lp = _lock_path(key)
    try:
        if os.path.exists(lp):
            os.remove(lp)
    except Exception:
        pass

def _cleanup_progress_expired():
    """TTL超過の進捗JSON/ロック/一時ZIPを掃除"""
    ttl = _cfg_progress_ttl()
    zip_ttl = _cfg_zip_file_ttl()
    now = time.time()
    d = _progress_dir()
    try:
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            p = os.path.join(d, name)
            key = name.rsplit(".", 1)[0]
            info = _progress_read(key)
            if info is None:
                # 壊れていたら消す
                with _PROGRESS_IO_LOCK:
                    try: os.remove(p)
                    except Exception: pass
                continue

            # 完了ZIPのメタデータには権限・DL名も含むため、ZIP本体と同じ期間保持する。
            # エラーは画面表示に必要な短時間だけ残す。
            status = (info or {}).get("status")
            if status in ("done", "error"):
                mtime = os.path.getmtime(p)
                zip_exists = os.path.isfile(_zip_out_path(key))
                keep_for = zip_ttl if status == "done" and zip_exists else ttl
                if now - mtime > keep_for:
                    with _PROGRESS_IO_LOCK:
                        try: os.remove(p)
                        except Exception: pass
                    # 対応するロックも掃除
                    lp = _lock_path(key)
                    try:
                        if os.path.exists(lp):
                            os.remove(lp)
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        tmp_root = _cfg_tmp_root()
        for name in os.listdir(tmp_root):
            if not name.endswith(".zip"):
                continue
            p = os.path.join(tmp_root, name)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > zip_ttl:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass

# ------------------------------------------------------------
# パス解決（uploads / albums / tickets）
_UUID32_RE  = re.compile(r"^[0-9a-f]{32}$")
_UUID4_RE   = re.compile(r"^[0-9a-fA-F-]{36}$")
_INT_RE     = re.compile(r"^[0-9]+$")
_CTRL_RE    = re.compile(r"[\x00-\x1f\x7f]")
_STORED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi",
    ".mp3", ".aac", ".ogg", ".flac", ".pdf", ".zip", ".7z", ".rar",
}

def _resolve_relpath_internal(rel: str) -> Optional[str]:
    """
    受け取った相対パスを実ファイルに解決（不正時 None）。
      - uploads: <uuid32>/(original|thumb)/<fname>
                 または uploads/<uuid32>/(original|thumb)/<fname>
      - albums : albums/<uuid4>/<uuid4>/<fname(サブパス可)>  ← SSD/HDD の両ルートを探索
      - tickets: tickets/<batch_id>/original/<fname>
    """
    if not rel:
        return None

    rel = rel.lstrip("/").replace("\\", "/")

    # --- albums（SSD/HDD 両対応） ---
    if rel.startswith("albums/"):
        parts = rel.split("/", 3)  # ['albums', album_uuid, child_uuid, fname]
        if len(parts) != 4:
            return None
        _, album_id, child_id, fname = parts
        if not (_UUID4_RE.match(album_id) and _UUID4_RE.match(child_id) and fname):
            return None

        for base in _cfg_album_multi_roots():
            full = safe_join(base, album_id, child_id, fname)
            if not full:
                continue
            full_real = os.path.realpath(full)
            base_real = os.path.realpath(base)
            # base 配下かつファイルが存在する場合のみ採用
            if full_real.startswith(base_real + os.sep) and os.path.isfile(full_real):
                return full_real
        return None

    # --- tickets ---
    if rel.startswith("tickets/"):
        parts = rel.split("/", 3)  # ['tickets', batch_id, 'original', fname]
        if len(parts) != 4:
            return None
        _, batch_id, kind, fname = parts
        if not (_INT_RE.match(batch_id) and kind == "original" and fname):
            return None
        base = _cfg_tickets_root()
        full = safe_join(base, batch_id, kind, fname)
        if not full:
            return None
        full = os.path.realpath(full)
        base_real = os.path.realpath(base)
        if not full.startswith(base_real + os.sep) or not os.path.isfile(full):
            return None
        return full

    # --- uploads ('uploads/' プレフィックス許容) ---
    if rel.startswith("uploads/"):
        rel = rel[len("uploads/"):]  # 剥がす

    parts = rel.split("/", 2)  # [uuid32, kind, fname]
    if len(parts) != 3:
        return None
    uuid32, kind, fname = parts
    if not (_UUID32_RE.match(uuid32) and kind in ("original", "thumb") and fname):
        return None
    base = _cfg_storage_root()
    full = safe_join(base, uuid32, kind, fname)
    if not full:
        return None
    full = os.path.realpath(full)
    base_real = os.path.realpath(base)
    if not full.startswith(base_real + os.sep) or not os.path.isfile(full):
        return None
    return full

# ------------------------------------------------------------
# ZIP作成（token名で固定出力）
def _zip_out_path(key: str) -> str:
    return os.path.join(_cfg_tmp_root(), f"{key}.zip")

def _gather_files(src_list: Iterable[str]) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    for p in src_list:
        try:
            st = os.stat(p)
            if os.path.isfile(p):
                out.append((p, st.st_size))
        except Exception:
            continue
    return out

def _safe_archive_name(value: str) -> str:
    """ZIP内の相対パスを、日本語を保持したまま安全化する。"""
    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        part = _CTRL_RE.sub("", part).strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part.replace("..", "‥"))
    return "/".join(parts) or "file"


def _unique_archive_entries(entries: Sequence[Tuple[str, str]]) -> List[Tuple[str, str, int]]:
    """存在するファイルだけを残し、ZIP内の重複名を解消する。"""
    out: List[Tuple[str, str, int]] = []
    used: set[str] = set()
    for arcname, path in entries:
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
        except Exception:
            continue

        safe_name = _safe_archive_name(arcname)
        directory, basename = os.path.split(safe_name)
        stem, ext = os.path.splitext(basename)
        candidate = safe_name
        suffix = 1
        while candidate.casefold() in used:
            suffix += 1
            renamed = f"{stem}_{suffix}{ext}"
            candidate = f"{directory}/{renamed}" if directory else renamed
        used.add(candidate.casefold())
        out.append((candidate, path, size))
    return out


def _make_zip_entries_internal(
    entries: Sequence[Tuple[str, str]],
    key: str,
    *,
    download_name: Optional[str] = None,
    access: Optional[dict] = None,
) -> Optional[str]:
    """(ZIP内パス, 絶対パス) の一覧から共通形式のZIPと進捗を作成する。"""
    files = _unique_archive_entries(entries)
    total = len(files)
    total_bytes = sum(size for _, _, size in files)
    common_progress = {
        "download_name": _safe_archive_name(download_name or f"{key}.zip").replace("/", "／"),
        "access": access or {"type": "bearer"},
    }

    if not files:
        _progress_write(key, {
            **common_progress,
            "status": "error",
            "message": "対象ファイルがありません",
            "total_files": 0,
            "processed_files": 0,
            "total_bytes": 0,
            "processed_bytes": 0,
            "percent": 0,
        })
        return None

    started = time.time()
    _progress_write(key, {
        **common_progress,
        "status": "running",
        "total_files": total,
        "processed_files": 0,
        "total_bytes": total_bytes,
        "processed_bytes": 0,
        "started_ts": started,
        "percent": 0,
    })

    # 出力先は key 固定
    os.makedirs(_cfg_tmp_root(), exist_ok=True)
    out_path = _zip_out_path(key)

    # 同名があれば消す
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except Exception:
        pass

    processed_files = 0
    processed_bytes = 0

    try:
        with zipfile.ZipFile(out_path, "w", allowZip64=True) as zf:
            for arcname, src, sz in files:
                extension = os.path.splitext(src)[1].lower()
                compression = zipfile.ZIP_STORED if extension in _STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
                zf.write(src, arcname=arcname, compress_type=compression)

                processed_files += 1
                processed_bytes += sz

                # ---- 進捗計算：最終ファイルで必ず100% ----
                if total_bytes > 0:
                    pct = int(processed_bytes * 100 / total_bytes)
                else:
                    pct = 100

                if processed_files < total:
                    pct = min(99, pct)  # 中間は最大でも99%
                else:
                    pct = 100          # 最後に100%

                elapsed = max(0.001, time.time() - started)
                speed = processed_bytes / elapsed
                eta = (total_bytes - processed_bytes) / speed if speed > 0 else 0

                _progress_write(key, {
                    **common_progress,
                    "status": "running",
                    "total_files": total,
                    "processed_files": processed_files,
                    "total_bytes": total_bytes,
                    "processed_bytes": processed_bytes,
                    "started_ts": started,
                    "eta_seconds": int(eta),
                    "percent": pct,
                })
    except Exception as e:
        _progress_write(key, {
            **common_progress,
            "status": "error",
            "message": str(e),
            "total_files": total,
            "processed_files": processed_files,
            "total_bytes": total_bytes,
            "processed_bytes": processed_bytes,
            "started_ts": started,
            "percent": 0,
        })
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return None

    _progress_write(key, {
        **common_progress,
        "status": "done",
        "total_files": total,
        "processed_files": total,
        "total_bytes": total_bytes,
        "processed_bytes": total_bytes,
        "started_ts": started,
        "eta_seconds": 0,
        "percent": 100,
        "completed_ts": time.time(),
    })
    return out_path


def _make_zip_file_internal(abs_paths: Iterable[str], key: str) -> Optional[str]:
    """従来の絶対パス一覧を、直下配置の共通ZIPとして作成する。"""
    entries = [(os.path.basename(path), path) for path in abs_paths]
    return _make_zip_entries_internal(entries, key)

# ------------------------------------------------------------
# 連打防止（使っていない場合も互換のため残す）
def _acquire_lock(key: str) -> bool:
    lp = _lock_path(key)
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False

def _safe_download_key(value: str) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z._:-]{8,}", key):
        return uuid.uuid4().hex
    return key

def _has_album_access(album_id: str) -> bool:
    if session.get("user") == "admin":
        return True
    allowed = session.get("album_auth_ids") or []
    return album_id in allowed or bool(session.get(f"auth_{album_id}"))


def _job_access_allowed(progress: Optional[dict]) -> bool:
    access = (progress or {}).get("access") or {"type": "bearer"}
    access_type = access.get("type")
    if access_type == "admin":
        return session.get("user") == "admin"
    if access_type == "album":
        return all(_has_album_access(str(album_id)) for album_id in access.get("album_ids") or [])
    if access_type == "upload":
        for upload_id in access.get("upload_ids") or []:
            upload = fetch_upload_access_record(str(upload_id))
            if not upload or not can_access_upload_record(upload, has_view_auth_func=has_view_auth):
                return False
        return True
    return True


def _resolve_zip_request_paths(relpaths: list) -> tuple[list[str], list[str], dict]:
    abs_list = []
    bad_paths = []
    album_ids: set[str] = set()
    upload_ids: set[str] = set()
    for rel in relpaths:
        rel_value = str(rel).lstrip("/").replace("\\", "/")
        upload_ref = resolve_upload_subpath(rel_value, allow_zip=True)
        if upload_ref:
            upload = fetch_upload_access_record(upload_ref["uuid"])
            if not upload or not can_access_upload_record(upload, has_view_auth_func=has_view_auth):
                raise PermissionError(str(rel))
            upload_ids.add(upload_ref["uuid"])
        elif rel_value.startswith("albums/"):
            parts = rel_value.split("/", 3)
            if len(parts) != 4 or not _has_album_access(parts[1]):
                raise PermissionError(str(rel))
            album_ids.add(parts[1])
        p = resolve_relpath(rel_value)
        if not p or not os.path.isfile(p):
            bad_paths.append(rel)
            continue
        abs_list.append(p)
    if album_ids:
        access = {"type": "album", "album_ids": sorted(album_ids)}
    elif upload_ids:
        access = {"type": "upload", "upload_ids": sorted(upload_ids)}
    else:
        access = {"type": "bearer"}
    return abs_list, bad_paths, access

# ------------------------------------------------------------
# ★公開関数（既存呼び出し互換）-------------------------------
def resolve_relpath(rel: str) -> Optional[str]:
    """Back-compat: tickets / albums / uploads の相対→絶対解決"""
    return _resolve_relpath_internal(rel)

def make_zip_file(abs_paths: Iterable[str], key: str):
    """
    Back-compat: tickets 側の想定シグネチャと同じ。
    TMP_ROOT/<key>.zip を生成して、その絶対パスを返す。
    """
    return _make_zip_file_internal(abs_paths, key)

def make_zip_entries(
    entries: Sequence[Tuple[str, str]],
    key: str,
    *,
    download_name: Optional[str] = None,
    access: Optional[dict] = None,
):
    """階層付きZIPを作る共通公開関数。"""
    return _make_zip_entries_internal(
        entries,
        _safe_download_key(key),
        download_name=download_name,
        access=access,
    )


def start_zip_entries_job(
    entries: Sequence[Tuple[str, str]],
    *,
    key: Optional[str] = None,
    download_name: Optional[str] = None,
    access: Optional[dict] = None,
) -> str:
    """共通ZIP生成をバックグラウンド開始し、進捗・DL用キーを返す。"""
    job_key = _safe_download_key(key or uuid.uuid4().hex)
    if not _acquire_lock(job_key):
        raise FileExistsError(job_key)
    app = current_app._get_current_object()
    frozen_entries = [(str(arcname), str(path)) for arcname, path in entries]
    progress_base = {
        "status": "queued",
        "total_files": len(frozen_entries),
        "processed_files": 0,
        "total_bytes": 0,
        "processed_bytes": 0,
        "percent": 0,
        "download_name": download_name or f"{job_key}.zip",
        "access": access or {"type": "bearer"},
    }
    _progress_write(job_key, progress_base)

    def worker():
        with app.app_context():
            try:
                _make_zip_entries_internal(
                    frozen_entries,
                    job_key,
                    download_name=download_name,
                    access=access,
                )
            except Exception as exc:
                app.logger.exception("ZIP background job failed: key=%s", job_key)
                _progress_write(job_key, {**progress_base, "status": "error", "message": str(exc)})
            finally:
                _unlock_only(job_key)

    threading.Thread(target=worker, name=f"zip-{job_key[:12]}", daemon=True).start()
    return job_key


def read_zip_progress(key: str) -> Optional[dict]:
    return _progress_read(_safe_download_key(key))


__all__ = [
    "zip_api", "resolve_relpath", "make_zip_file", "make_zip_entries",
    "start_zip_entries_job", "read_zip_progress",
]

# ------------------------------------------------------------
# （任意）/api/zip-stream エンドポイントを使っている場合の互換
@zip_api.route("/api/zip-stream", methods=["POST"])
def api_zip_stream():
    # 入口でTTL超過の進捗を掃除
    _cleanup_progress_expired()

    data = request.get_json(silent=True) or {}
    relpaths = data.get("paths") or []
    if not isinstance(relpaths, list) or not relpaths:
        return "paths が未指定です", 400

    try:
        abs_list, bad_paths, access = _resolve_zip_request_paths(relpaths)
    except PermissionError as exc:
        return jsonify({"ok": False, "error": "unauthorized_path", "path": str(exc)}), 403

    if not abs_list:
        return jsonify({"ok": False, "error": "no_valid_files", "bad_paths": bad_paths}), 400

    key = request.headers.get("X-Idempotency-Key") or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9A-Za-z._:-]{8,}", key):
        key = uuid.uuid4().hex

    if not _acquire_lock(key):
        prog = _progress_read(key)
        return jsonify({"ok": False, "error": "already_in_progress", "progress": prog}), 409

    entries = [(os.path.basename(path), path) for path in abs_list]
    path = _make_zip_entries_internal(entries, key, access=access)
    if not path:
        _unlock_only(key)  # 失敗時もロック解除
        return jsonify({"ok": False, "error": "zip_failed"}), 500

    # このAPIは「即ダウンロード＆後始末」仕様（進捗はTTLまで残す）
    dl_name = f"{uuid.uuid4().hex}.zip"

    @after_this_request
    def _cleanup(resp):
        try:
            if os.path.exists(path):
                os.remove(path)  # ZIP本体は削除
        except Exception:
            pass
        _unlock_only(key)  # ロックは解除（進捗は残す）
        return resp

    response = send_file(
        path, as_attachment=True, download_name=dl_name,
        mimetype="application/zip", conditional=True,
        max_age=0, etag=False, last_modified=datetime.now(timezone.utc)
    )
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</api/zip-prepare>; rel="successor-version"'
    return response

@zip_api.route("/api/zip-prepare", methods=["POST"])
def api_zip_prepare():
    """
    ZIPを作成し、ブラウザ標準ダウンロード用URLを返す。
    /api/zip-stream と違い、巨大ZIPを fetch().blob() に載せないため端末側の固まりを避ける。
    """
    _cleanup_progress_expired()

    data = request.get_json(silent=True) or {}
    relpaths = data.get("paths") or []
    if not isinstance(relpaths, list) or not relpaths:
        return jsonify({"ok": False, "error": "paths_required"}), 400

    try:
        abs_list, bad_paths, access = _resolve_zip_request_paths(relpaths)
    except PermissionError as exc:
        return jsonify({"ok": False, "error": "unauthorized_path", "path": str(exc)}), 403

    if not abs_list:
        return jsonify({"ok": False, "error": "no_valid_files", "bad_paths": bad_paths}), 400

    key = _safe_download_key(request.headers.get("X-Idempotency-Key") or uuid.uuid4().hex)
    if not _acquire_lock(key):
        prog = _progress_read(key)
        return jsonify({"ok": False, "error": "already_in_progress", "progress": prog}), 409

    entries = [(os.path.basename(path), path) for path in abs_list]
    path = _make_zip_entries_internal(entries, key, access=access)
    _unlock_only(key)
    if not path:
        return jsonify({"ok": False, "error": "zip_failed"}), 500

    return jsonify({
        "ok": True,
        "key": key,
        "download_url": f"/api/zip-download/{key}",
        "download_name": f"{key}.zip",
        "size": os.path.getsize(path),
        "bad_paths": bad_paths,
    })

@zip_api.route("/api/zip-download/<key>", methods=["GET"])
def api_zip_download(key: str):
    _cleanup_progress_expired()
    safe_key = _safe_download_key(key)
    if safe_key != key:
        return jsonify({"ok": False, "error": "invalid_key"}), 400
    path = _zip_out_path(safe_key)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "zip_not_found"}), 404
    progress = _progress_read(safe_key)
    if safe_key.startswith("album-") and not progress:
        return jsonify({"ok": False, "error": "zip_metadata_not_found"}), 404
    if not _job_access_allowed(progress):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    download_name = (progress or {}).get("download_name") or f"{safe_key}.zip"
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/zip",
        conditional=True,
        max_age=0,
        etag=False,
        last_modified=datetime.now(timezone.utc),
    )

@zip_api.route("/api/zip-progress", methods=["GET"])
def api_zip_progress():
    # ポーリング時にも TTL 超過の進捗を掃除
    _cleanup_progress_expired()

    key = request.args.get("key", "")
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400
    prog = _progress_read(key)
    if not prog:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if not _job_access_allowed(prog):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if prog.get("status") == "done":
        prog["processed_bytes"] = prog.get("total_bytes", 0)
        prog["processed_files"] = prog.get("total_files", 0)
        prog["percent"] = 100
        prog["eta_seconds"] = 0
    return jsonify(prog), 200
