# -*- coding: utf-8 -*-
"""
アルバム機能 / 動画モード: 連番保存 + 変換(H.264/AAC) + ポスター生成 + 個別DL 対応フル版
+ 写真ストレージ(SSD/HDD)のアルバム単位切替（外部リンク変更なし / DB管理 / 物理移動UI付き）

- 動画アップロード: {child_id}_YYYYMMDD_HHMM_NNNN.ext の連番で保存
- ffmpeg/ffprobe が見つかれば、*.web.mp4（H.264/AAC）と *.poster.jpg をバックグラウンド生成
- 一覧は .web.mp4 を優先再生（未生成なら converting=True をテンプレ側へ渡す）
- 画像(通常/加工)と動画のパスはルートで分離:
    画像:  /mnt/mfu/mfu_albums/<album>/<child>  ← SSD/HDDをDBで切替（物理移動UIあり）
    動画:  /mnt/maildata/mfu_album_movie/<album>/<child>（固定）
"""

from flask import (
    Blueprint, render_template, render_template_string, request, redirect,
    url_for, session, send_from_directory, send_file, abort, current_app, flash, jsonify, Response
)
from werkzeug.utils import secure_filename
import os
import re
import time
import shutil
import uuid
import json
import secrets
from functools import wraps
from datetime import datetime
import subprocess
import shlex

from mysql.connector import errors as MySQLErrors
from app.utils.db import get_db  #
from flask import g  # 追加

# 外部ユーティリティ（既存プロジェクトのモジュールを利用）
from app.albums.photo_namer import get_datetime_from_image
from app.utils.thumbs import enqueue_thumb_job, get_files_with_thumbs
from app.utils.push import send_push
from app.external_login_user.utils import _get_ext_user_by_social

album_bp = Blueprint('album', __name__, template_folder='templates')
print("✅ album.routes (movie 連番 & 変換 & 個別DL + SSD/HDD切替) loaded")


ALBUM_AUTH_SESSION_KEY = "album_auth_ids"
ALBUM_AUTH_MAX_ITEMS = 120


def _grant_album_auth(album_id: str) -> None:
    _cleanup_legacy_album_auth_keys(current_album_id=album_id)
    allowed = session.get(ALBUM_AUTH_SESSION_KEY) or []
    if album_id in allowed:
        return
    allowed = (allowed + [album_id])[-ALBUM_AUTH_MAX_ITEMS:]
    session[ALBUM_AUTH_SESSION_KEY] = allowed


def _has_album_auth(album_id: str) -> bool:
    allowed = session.get(ALBUM_AUTH_SESSION_KEY) or []
    if album_id in allowed:
        return True
    legacy_key = f"auth_{album_id}"
    if session.get(legacy_key):
        _grant_album_auth(album_id)
        session.pop(legacy_key, None)
        return True
    return False


def _cleanup_legacy_album_auth_keys(current_album_id: str | None = None) -> None:
    for key in list(session.keys()):
        if not key.startswith("auth_"):
            continue
        if current_album_id and key == f"auth_{current_album_id}":
            continue
        session.pop(key, None)


def _get_ext_user_nickname() -> str | None:
    ext_social_id = session.get("ext_user_social_id")
    if not ext_social_id:
        return None
    try:
        ext_user = _get_ext_user_by_social(ext_social_id)
    except Exception:
        return None
    if not ext_user:
        return None
    nickname = (ext_user.get("nickname") or "").strip()
    return nickname or None

def _is_ext_logged_in() -> bool:
    return bool(session.get("ext_user_social_id"))

def _fetch_event_process_members(event_id: int) -> list[dict]:
    """イベント参加者一覧を取得（process フラグ含む）"""
    rows: list[dict] = []
    try:
        conn = get_db()
        try:
            cur = conn.cursor(dictionary=True)
        except Exception:
            cur = conn.cursor()
        cur.execute(
            """
            SELECT
              m.user_id,
              COALESCE(m.process, 0) AS process,
              u.nickname,
              u.email
            FROM mfu_event_member m
            JOIN external_login_user u ON u.id = m.user_id
            WHERE m.event_id=%s
              AND m.status='approved'
              AND COALESCE(m.is_canceled, 0)=0
            ORDER BY u.nickname ASC, m.user_id ASC
            """,
            (event_id,),
        )
        fetched = cur.fetchall() or []
        if fetched and not isinstance(fetched[0], dict):
            cols = [d[0] for d in cur.description]
            for r in fetched:
                rows.append({cols[i]: r[i] for i in range(len(cols))})
        else:
            rows = fetched
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        return []
    return rows or []

def _fetch_album_process_status_map(album_id: str, child_id: str) -> dict[int, dict]:
    """album_process の request/complete 状態を ext_user_id ごとに取得"""
    _ensure_album_process_table()
    rows = db_get_all(
        """
        SELECT ext_user_id, request_flag, complete_flag
          FROM album_process
         WHERE album_id=%s AND child_id=%s
        """,
        (album_id, child_id),
    )
    status_map: dict[int, dict] = {}
    for r in rows or []:
        ext_user_id = int(r.get("ext_user_id"))
        status_map[ext_user_id] = {
            "request_flag": int(r.get("request_flag", 0)),
            "complete_flag": int(r.get("complete_flag", 0)),
        }
    return status_map

def _build_event_album_target_urls(event_id: int, album_id: str, child_id: str) -> dict[str, str]:
    """イベント参加者向けの通知URLを返す。"""
    def _to_uuid_str(v):
        import uuid
        if isinstance(v, str):
            s = v.strip()
            try:
                uuid.UUID(s)
                return s
            except Exception:
                return None
        if isinstance(v, (bytes, bytearray)):
            try:
                return str(uuid.UUID(bytes=bytes(v)))
            except Exception:
                try:
                    return str(uuid.UUID(hex=v.hex()))
                except Exception:
                    return None
        return None

    ev_uuid_str = None
    try:
        try:
            ev_row = db_get_one("SELECT event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
            ev_b = ev_row.get("event_uuid") if isinstance(ev_row, dict) else None
        except Exception:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
                r = cur.fetchone()
                ev_b = r[0] if r else None
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
        ev_uuid_str = _to_uuid_str(ev_b)
    except Exception as e:
        current_app.logger.warning("notify(event_uuid) failed: %s", e)
        ev_uuid_str = None

    if ev_uuid_str:
        try:
            return {
                "absolute_url": url_for(
                    'external_login_user.event_album_direct',
                    event_uuid=ev_uuid_str,
                    child_id=child_id,
                    _external=True,
                ),
                "relative_url": url_for(
                    'external_login_user.event_album_direct',
                    event_uuid=ev_uuid_str,
                    child_id=child_id,
                    _external=False,
                ),
            }
        except Exception:
            return {
                "absolute_url": url_for('external_login_user.view_event', event_uuid=ev_uuid_str, _external=True),
                "relative_url": url_for('external_login_user.view_event', event_uuid=ev_uuid_str, _external=False),
            }
    return {
        "absolute_url": url_for('album.view_child', album_id=album_id, child_id=child_id, _external=True),
        "relative_url": url_for('album.view_child', album_id=album_id, child_id=child_id, _external=False),
    }


def _build_event_album_link(event_id: int, album_id: str, child_id: str) -> str:
    """イベント参加者向けのメール本文URLを返す。"""
    return _build_event_album_target_urls(event_id, album_id, child_id)["absolute_url"]


def _notify_requester_process_completion(
    album_id: str,
    child_id: str,
    request_by_id: int | None,
    meta: dict,
    event_meta: dict | None,
) -> None:
    if not request_by_id:
        return
    if not event_meta or not event_meta.get("event_id"):
        return
    pending_sql = (
        """
        SELECT COUNT(*) AS cnt
          FROM album_process
         WHERE album_id=%s AND child_id=%s
           AND request_by=%s
           AND request_flag=1
           AND complete_flag=0
        """
    )
    pending_row = db_get_one(
        pending_sql,
        (album_id, child_id, request_by_id),
    )
    pending_cnt = int(pending_row.get("cnt", 0)) if pending_row else 0
    if pending_cnt != 0:
        return
    requester_row = db_get_one(
        "SELECT email FROM external_login_user WHERE id=%s LIMIT 1",
        (request_by_id,),
    )
    requester_email = (requester_row.get("email") or "").strip() if requester_row else ""
    if not requester_email:
        return
    current_app.logger.info(
        "notify: pre_send kind=process_all_done album_id=%s child_id=%s request_by=%s recipients=%s recipients_count=%s pending_sql=%s",
        album_id,
        child_id,
        requester_email,
        [requester_email],
        1,
        "request_flag=1 AND complete_flag=0",
    )
    album_name = meta.get("album_name", "アルバム")
    child_name = next((c.get("name") for c in meta.get("children", []) if c.get("folder") == child_id), child_id)
    link = _build_event_album_link(int(event_meta["event_id"]), album_id, child_id)
    subject = f"【加工完了】{album_name}"
    body = (
        f"{album_name} の「{child_name}」について、依頼した加工回しが完了しました。\n\n"
        f"アクセスはこちら:\n{link}\n\n"
        "このメールはイベント参加者（承認済み）のみへ自動通知しています。"
    )
    try:
        from app.utils.mail import send_mail
    except Exception:
        try:
            from app.mail import send_mail
        except Exception:
            send_mail = None
    if send_mail:
        send_mail(requester_email, subject, body)

def _fetch_event_notification_contacts(event_id: int, user_ids: list[int]) -> list[dict]:
    if not user_ids:
        return []
    placeholders = ",".join(["%s"] * len(user_ids))
    sql = (
        "SELECT m.user_id, u.nickname, u.email, "
        "       COALESCE(u.notify_album_process, 1) AS notify_album_process "
        "  FROM mfu_event_member m "
        "  JOIN external_login_user u ON u.id = m.user_id "
        f" WHERE m.event_id=%s AND m.user_id IN ({placeholders}) "
        " ORDER BY u.nickname ASC, m.user_id ASC"
    )
    return db_get_all(sql, (event_id, *user_ids)) or []


def _fetch_push_subscribed_ext_user_ids(user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    normalized_ids: list[int] = []
    seen: set[int] = set()
    for user_id in user_ids:
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        normalized_ids.append(value)
    if not normalized_ids:
        return set()
    placeholders = ",".join(["%s"] * len(normalized_ids))
    sql = (
        "SELECT DISTINCT actor_id "
        "  FROM chat_push_subscriptions "
        " WHERE actor_type='external_user_id' "
        f"   AND actor_id IN ({placeholders})"
    )
    rows = db_get_all(sql, tuple(str(uid) for uid in normalized_ids)) or []
    subscribed_ids: set[int] = set()
    for row in rows:
        try:
            subscribed_ids.add(int(row.get("actor_id")))
        except (TypeError, ValueError, AttributeError):
            continue
    return subscribed_ids

# =============================================================================
# 定数 / 設定
# =============================================================================
# --- 写真(静止画/加工)のルート（切替対象） ---
SSD_ROOT = '/mnt/mfu/mfu_albums'                 # 従来のSSD保存先
HDD_ROOT = '/mnt/maildata/mfu_albums'            # 長期保管HDD保存先
DEFAULT_STORAGE = 'ssd'                           # 新規はSSDに作成

# --- 互換のため残す（直接は使わず、storage_child_dir経由で解決） ---
ALBUM_ROOT = SSD_ROOT

# --- 動画のルート（固定・切替対象外） ---
MOVIE_ROOT = '/mnt/mfu/mfu_album_movie'

# 追加：動画サブディレクトリ
MOVIE_ORIG_SUB = 'original'
MOVIE_ENC_SUB  = 'encoded'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'heic', 'webp'}
ALLOWED_MOVIE_EXTS = {'mp4', 'mov', 'm4v', 'webm'}
MAX_MOVIE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# ffmpeg/ffprobe の検出（未導入なら None）
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# 加工ロック TTL（秒）
LOCK_TTL_SEC = 1800  # 30分

# =============================================================================
# セキュリティヘッダ
# =============================================================================
@album_bp.after_app_request
def _album_after_app_request(resp):
    try:
        resp.headers.setdefault('Referrer-Policy', 'no-referrer')
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
    except Exception:
        pass
    return resp

# =============================================================================
# ユーティリティ
# =============================================================================
def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_movie(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_MOVIE_EXTS

# ---------- SSD/HDD 両方を見るためのヘルパ ----------
def _get_roots_for_album(album_id: str):
    """アルバムの主ルートと副ルート（フェイルオーバ順）を返す（1リクエスト内キャッシュ）"""
    cache = getattr(g, "_album_roots_cache", None)
    if cache is None:
        cache = g._album_roots_cache = {}
    roots = cache.get(album_id)
    if roots:
        return roots
    _storage, cur_root = _get_album_storage(album_id)
    other_root = HDD_ROOT if cur_root == SSD_ROOT else SSD_ROOT
    roots = (cur_root, other_root)
    cache[album_id] = roots
    return roots

def _prefer_existing_child_dir(album_id: str, child_id: str, mode: str):
    """主ルートに子が無ければ副ルートを返す（view 用ディレクトリ決定）"""
    if (mode or "").lower() == "movie":
        return os.path.join(MOVIE_ROOT, album_id, child_id)
    primary, secondary = _get_roots_for_album(album_id)
    p = os.path.join(primary, album_id, child_id)
    if os.path.isdir(p):
        return p
    s = os.path.join(secondary, album_id, child_id)
    if os.path.isdir(s):
        return s
    return p  # どちらも無ければ主ルート側を返す（後で作成される）

def _open_path_anyroot(album_id: str, child_id: str, filename: str, mode: str = "normal") -> str | None:
    """SSD/HDD どちらかで見つかった絶対パスを返す（send_file 用）"""
    if (mode or "").lower() == "movie":
        path = os.path.join(MOVIE_ROOT, album_id, child_id, filename)
        return path if os.path.isfile(path) else None
    primary, secondary = _get_roots_for_album(album_id)
    p = os.path.join(primary, album_id, child_id, filename)
    if os.path.isfile(p):
        return p
    s = os.path.join(secondary, album_id, child_id, filename)
    return s if os.path.isfile(s) else None

def _count_child_media_items(album_id: str, child_id: str, mode: str) -> int:
    """子アルバム内の表示対象メディア件数を返す（親一覧向け）。"""
    normalized_mode = (mode or "normal").lower()

    if normalized_mode == "movie":
        orig_dir = os.path.join(storage_child_dir(album_id, child_id, mode='movie'), 'original')
        if not os.path.isdir(orig_dir):
            return 0
        return sum(
            1
            for fname in os.listdir(orig_dir)
            if os.path.isfile(os.path.join(orig_dir, fname))
            and allowed_movie(fname)
            and not fname.endswith('.web.mp4')
        )

    roots = _get_roots_for_album(album_id)
    seen = set()
    count = 0
    for root in roots:
        child_path = os.path.join(root, album_id, child_id)
        if not os.path.isdir(child_path):
            continue
        for fname in os.listdir(child_path):
            full = os.path.join(child_path, fname)
            if not os.path.isfile(full) or not allowed_file(fname):
                continue

            if normalized_mode == "process":
                if not fname.startswith("latest."):
                    continue
            elif fname.startswith("latest."):
                continue

            if fname in seen:
                continue
            seen.add(fname)
            count += 1
    return count

def resolve_thumb_url(album_id: str, child_id: str, original_filename: str) -> str:
    base, _ = os.path.splitext(original_filename)
    roots = _get_roots_for_album(album_id)  # ← 追加：毎回DB行かない
    for cand in (f"{base}.webp", f"{base}.jpg", f"{base}.jpeg"):
        for root in roots:
            p = os.path.join(root, album_id, child_id, 'thumbs', cand)
            if os.path.isfile(p):
                return url_for('album.album_thumb', album_id=album_id, child_id=child_id, filename=cand)
    return url_for('album.image', album_id=album_id, child_id=child_id, filename=original_filename)

# 追加：encoded/original 内の実ファイルを解決（パストラバーサル防止）
def _movie_find_abs(album_id: str, child_id: str, name: str) -> str | None:
    base = storage_child_dir(album_id, child_id, mode='movie')  # .../<album>/<child>
    # サブパスも受けるが basename も使って安全に探索
    raw = name
    fname = os.path.basename(name)

    # 1) encoded 優先（ベース名で）
    cand = os.path.join(base, 'encoded', fname)
    if os.path.isfile(cand):
        return cand
    # 2) original
    cand = os.path.join(base, 'original', fname)
    if os.path.isfile(cand):
        return cand
    # 3) 明示サブパスにも対応（encoded/xxx や original/xxx をそのまま受ける）
    p = os.path.normpath(os.path.join(base, raw))
    if p.startswith(base) and os.path.isfile(p):
        return p
    return None


# ------------------ DB helpers ------------------
def db_get_one(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        conn.close()

def db_get_all(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()

def db_exec(sql, params=()):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()
    finally:
        conn.close()

def is_album_readonly(album_id: str) -> bool:
    """HDD保管中（アーカイブ）なら True"""
    storage, _ = _get_album_storage(album_id)
    return storage == 'hdd'

def _movie_dir(album_id: str, child_id: str) -> str:
    return os.path.join(MOVIE_ROOT, album_id, child_id)

def _movie_subdir(album_id: str, child_id: str, sub: str) -> str:
    p = os.path.join(_movie_dir(album_id, child_id), sub)
    os.makedirs(p, exist_ok=True)
    return p

# =============================================================================
# ストレージ保存先(SSD/HDD)のDB管理
# =============================================================================
DDL_ALBUM_STORAGE = """
CREATE TABLE IF NOT EXISTS album_storage (
  album_id   VARCHAR(64) PRIMARY KEY,
  storage    ENUM('ssd','hdd') NOT NULL,
  abs_root   VARCHAR(255) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def _ensure_album_storage_table():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(DDL_ALBUM_STORAGE)
        db.commit()
    finally:
        db.close()

# =============================================================================
# 加工回し（イベント向け）依頼/完了の状態管理
# =============================================================================
DDL_ALBUM_PROCESS = """
CREATE TABLE IF NOT EXISTS album_process (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ext_user_id   BIGINT UNSIGNED NOT NULL,
  request_by    BIGINT UNSIGNED NULL,
  album_id      VARCHAR(64) NOT NULL,
  child_id      VARCHAR(64) NOT NULL,
  request_flag  TINYINT(1) NOT NULL DEFAULT 0,
  complete_flag TINYINT(1) NOT NULL DEFAULT 0,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_album_process (ext_user_id, album_id, child_id),
  INDEX idx_album_process_album (album_id, child_id),
  INDEX idx_album_process_user (ext_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def _ensure_album_process_table():
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(DDL_ALBUM_PROCESS)
            try:
                cur.execute("ALTER TABLE album_process ADD COLUMN request_by BIGINT UNSIGNED NULL")
            except MySQLErrors.ProgrammingError:
                pass
        db.commit()
    finally:
        db.close()

def _set_album_storage(album_id: str, storage: str):
    """SSD/HDD のどちらかを設定し、abs_root を同期"""
    root = SSD_ROOT if storage == "ssd" else HDD_ROOT
    _ensure_album_storage_table()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "REPLACE INTO album_storage (album_id, storage, abs_root) VALUES (%s, %s, %s)",
                (album_id, storage, root),
            )
        db.commit()
    finally:
        db.close()

def _get_album_storage(album_id: str):
    """
    戻り: (storage, abs_root)
    - DBが使えれば DB 優先
    - 使えなければ 実ディレクトリの存在で推定（フォールバック）
    - 何も無ければ DEFAULT_STORAGE を採用
    """
    # まずはDBに触る前に「既にディレクトリがどちらにあるか」を用意
    ssd_exists = os.path.isdir(os.path.join(SSD_ROOT, album_id))
    hdd_exists = os.path.isdir(os.path.join(HDD_ROOT, album_id))

    # DBトライ（テーブル作成含む）— 失敗しても絶対に例外を外へ飛ばさない
    try:
        # 既存コード踏襲（テーブル作成）
        _ensure_album_storage_table()
        db = get_db()
        try:
            with db.cursor(dictionary=True) as cur:
                cur.execute("SELECT storage, abs_root FROM album_storage WHERE album_id=%s", (album_id,))
                row = cur.fetchone()
            if row:
                return row["storage"], row["abs_root"]
        finally:
            db.close()
    except Exception as e:
        # ログだけ吐いてフォールバック
        try:
            current_app.logger.warning("album_storage lookup failed, fallback to FS: %s", e)
        except Exception:
            pass

    # ここからフォールバック推定（DBに頼らない）
    if ssd_exists and not hdd_exists:
        return ("ssd", SSD_ROOT)
    if hdd_exists and not ssd_exists:
        return ("hdd", HDD_ROOT)
    if ssd_exists and hdd_exists:
        # 両方にあるならとりあえずSSDを主に（運用上の期待に寄せる）
        return ("ssd", SSD_ROOT)

    # どこにも無ければデフォルト採用
    return (DEFAULT_STORAGE, SSD_ROOT if DEFAULT_STORAGE == "ssd" else HDD_ROOT)

def _photos_root(album_id: str) -> str:
    """写真用の実ルート（album_idごとにDBで解決）"""
    _, root = _get_album_storage(album_id)
    return root

# ------------------ メタ情報 ------------------
_def_token_bytes = 32  # 256bit
def _new_token_hex() -> str:
    return secrets.token_bytes(_def_token_bytes).hex()  # 64文字 hex

def load_meta(album_id: str):
    album = db_get_one("SELECT id, album_name, owner, access_token FROM albums WHERE id=%s", (album_id,))
    if not album:
        return None
    children = db_get_all(
        "SELECT id, name, folder, mode FROM album_children WHERE album_id=%s ORDER BY name ASC",
        (album_id,)
    )
    return {
        "album_name": album["album_name"],
        "owner": album["owner"],
        "access_token": album["access_token"],
        "children": [{"id": c["id"], "name": c["name"], "folder": c["folder"], "mode": c["mode"]} for c in children]
    }

def create_album_row(album_id: str, album_name: str, owner: str) -> str:
    access_token = _new_token_hex()
    db_exec(
        "INSERT INTO albums (id, album_name, owner, access_token) VALUES (%s,%s,%s,%s)",
        (album_id, album_name, owner, access_token)
    )
    return access_token

def regenerate_access_token(album_id: str) -> str:
    new_token = _new_token_hex()
    db_exec("UPDATE albums SET access_token=%s WHERE id=%s", (new_token, album_id))
    return new_token

def list_albums_for_user(user: str):
    return db_get_all(
        "SELECT id, album_name AS name, owner, access_token FROM albums WHERE owner=%s ORDER BY album_name ASC",
        (user,)
    )

def list_albums_for_admin():
    return db_get_all(
        "SELECT id, album_name AS name, owner, access_token FROM albums ORDER BY album_name ASC"
    )

def add_child_row(album_id: str, child_name: str, mode: str):
    """mode は normal / process / movie を許可"""
    child_uuid = str(uuid.uuid4())
    norm_mode = (mode if mode in ('normal', 'process', 'movie') else 'normal')
    db_exec(
        "INSERT INTO album_children (id, album_id, name, folder, mode) VALUES (%s,%s,%s,%s,%s)",
        (child_uuid, album_id, child_name, child_uuid, norm_mode)
    )
    return child_uuid

def delete_child_row(album_id: str, child_id: str):
    db_exec("DELETE FROM album_children WHERE album_id=%s AND folder=%s", (album_id, child_id))

def delete_album_row(album_id: str):
    db_exec("DELETE FROM albums WHERE id=%s", (album_id,))

# ------------------ ストレージルート切替（実解決） ------------------
def storage_child_dir(album_id: str, child_id: str, mode: str | None = None) -> str:
    """
    子アルバムのフルパスを返す。
    - movie: MOVIE_ROOT（固定）
    - その他: album_storage を参照し SSD/HDD を解決
    """
    if (mode or '').lower() == 'movie':
        base = MOVIE_ROOT
    else:
        base = _photos_root(album_id)
    return os.path.join(base, album_id, child_id)

# ------------------ 加工ロック（DB管理 + lock.json） ------------------
# CREATE TABLE IF NOT EXISTS album_locks (
#   album_id   CHAR(36) NOT NULL,
#   child_id   CHAR(36) NOT NULL,
#   username   VARCHAR(64) NOT NULL,
#   acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
#   expires_at  TIMESTAMP NOT NULL,
#   PRIMARY KEY (album_id, child_id),
#   KEY idx_expires (expires_at)
# ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

def try_acquire_lock_db(album_id: str, child_id: str, username: str, ttl_sec: int = LOCK_TTL_SEC):
    conn = get_db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "DELETE FROM album_locks WHERE album_id=%s AND child_id=%s AND expires_at < NOW()",
            (album_id, child_id)
        )
        conn.commit()
        try:
            cur.execute(
                "INSERT INTO album_locks (album_id, child_id, username, acquired_at, expires_at) "
                "VALUES (%s,%s,%s, NOW(), DATE_ADD(NOW(), INTERVAL %s SECOND))",
                (album_id, child_id, username, ttl_sec)
            )
            conn.commit()
            return True, "ロック取得に成功しました。"
        except MySQLErrors.IntegrityError as e:
            if getattr(e, 'errno', None) == 1062:
                cur.execute(
                    "SELECT username, expires_at FROM album_locks WHERE album_id=%s AND child_id=%s",
                    (album_id, child_id)
                )
                row = cur.fetchone() or {}
                holder = row.get('username', '不明')
                exp = row.get('expires_at')
                if holder == username:
                    cur.execute(
                        "UPDATE album_locks SET acquired_at=NOW(), expires_at=DATE_ADD(NOW(), INTERVAL %s SECOND) "
                        "WHERE album_id=%s AND child_id=%s",
                        (ttl_sec, album_id, child_id)
                    )
                    conn.commit()
                    return True, "既存ロックを更新しました。"
                until = exp.strftime('%H:%M') if isinstance(exp, datetime) else '不明'
                return False, f"現在 {holder} さんが加工中です（〜{until}）。"
            raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

def release_lock_db(album_id: str, child_id: str, username: str | None, force: bool = False):
    """※ 元コードの DB_POOL 参照は未定義のため、get_db() を使うよう修正"""
    conn = get_db()
    try:
        cur = conn.cursor()
        if force or not username:
            cur.execute("DELETE FROM album_locks WHERE album_id=%s AND child_id=%s", (album_id, child_id))
        else:
            cur.execute(
                "DELETE FROM album_locks WHERE album_id=%s AND child_id=%s AND username=%s",
                (album_id, child_id, username)
            )
        conn.commit()
        return cur.rowcount
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

# ------------------ ffprobe/ffmpeg ヘルパ ------------------
def ensure_web_mp4(input_path: str):
    """
    original/ を基準に encoded/<basename>.web.mp4 を生成。
    方針:
      - 可能なら VAAPI(h264_vaapi, iHD) を使用
      - 失敗時は libx264 へフォールバック
      - 音声は AAC（既にAACなら copy）
      - 既に H.264+AAC の MP4 なら remux（映像copy）
    戻り値: 出力予定パス（生成を非同期起動）/ 失敗時 None
    """
    if not FFMPEG or not FFPROBE:
        return None

    def _popen(args: list[str], *, vaapi: bool = False):
        # VAAPI使用時は iHD を強制（サービス側未設定でもOK）
        env = os.environ.copy()
        if vaapi:
            env.setdefault("LIBVA_DRIVER_NAME", "iHD")
        # バックグラウンドで静かに起動
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

    v, a = _probe_streams(input_path)
    vcodec = (v.get("codec_name") or "").lower()
    acodec = (a.get("codec_name") or "").lower()
    has_audio = bool(a)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    parent    = os.path.dirname(os.path.dirname(input_path))  # …/<child>/
    out_dir   = os.path.join(parent, MOVIE_ENC_SUB)
    os.makedirs(out_dir, exist_ok=True)
    out_path  = os.path.join(out_dir, base_name + ".web.mp4")
    if os.path.exists(out_path):
        return out_path

    # 既に H.264 + AAC の場合は remux（映像copy）
    if vcodec in ("h264", "avc1"):
        args = [
            FFMPEG, "-hide_banner", "-nostdin", "-y",
            "-i", input_path,
            "-map", "0:v:0", "-c:v", "copy",
        ]
        if has_audio:
            if acodec in ("aac", "mp4a"):
                args += ["-map", "0:a:0", "-c:a", "copy"]
            else:
                args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart", out_path]
        try:
            _popen(args)
            return out_path
        except Exception as e:
            current_app.logger.warning("remux(copy) failed: %s", e)
            return None

    # ── VAAPI（h264_vaapi, iHD）第一候補 ───────────────────────────
    vaapi_args = [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va",
        "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a:0?",
        # 59.94/60fps 等の可変FPS崩しを避ける
        "-fps_mode:v", "passthrough", "-vsync", "passthrough",
        # VAAPIパイプライン内で等倍スケール＆SAR統一
        "-vf", "scale_vaapi=w=trunc(iw/2)*2:h=trunc(ih/2)*2,setsar=1",
        "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "22", "-g", "120", "-bf", "2",
        "-profile:v", "high", "-tag:v", "avc1",
        "-movflags", "+faststart",
    ]
    if has_audio:
        if acodec in ("aac", "mp4a"):
            vaapi_args += ["-c:a", "copy"]
        else:
            vaapi_args += ["-c:a", "aac", "-b:a", "128k"]
    else:
        vaapi_args += ["-an"]
    vaapi_args += [out_path]

    try:
        _popen(vaapi_args, vaapi=True)
        return out_path
    except Exception as e:
        current_app.logger.info("VAAPI encode failed, fallback to libx264: %s", e)

    # ── フォールバック: libx264 ───────────────────────────────────
    x264_args = [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-i", input_path,
        "-map", "0:v:0",
        "-vf", "scale=w=trunc(iw/2)*2:h=trunc(ih/2)*2,setsar=1",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "21", "-preset", "medium",
        "-g", "120", "-bf", "2", "-tag:v", "avc1",
        "-movflags", "+faststart",
    ]
    if has_audio:
        if acodec in ("aac", "mp4a"):
            x264_args += ["-map", "0:a:0", "-c:a", "copy"]
        else:
            x264_args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
    else:
        x264_args += ["-an"]
    x264_args += [out_path]

    try:
        _popen(x264_args)
        return out_path
    except Exception as e:
        current_app.logger.warning("encode failed: %s", e)
        return None

def try_make_poster(input_path: str):
    """動画の1秒地点から poster.jpg をバックグラウンド生成。ffmpeg 未導入なら何もしない。"""
    if not FFMPEG:
        return None
    base, _ = os.path.splitext(input_path)
    out_path = base + ".poster.jpg"
    if os.path.exists(out_path):
        return out_path
    args = [FFMPEG, "-y", "-ss", "00:00:01", "-i", input_path, "-frames:v", "1",
            "-vf", "scale=iw*min(1\\,1280/iw):-2", out_path]
    try:
        subprocess.Popen(args)
        return out_path
    except Exception as e:
        current_app.logger.warning("try_make_poster failed: %s", e)
        return None

# =============================================================================
# イベント関係（既存）
# =============================================================================

def _ensure_event_gate_columns():
    """albums に event_id / access_mode を安全に追加（既存DBに存在しなければ）"""
    try:
        row = db_get_one(
            "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='albums' AND COLUMN_NAME=%s",
            ('event_id',)
        )
        if not row or int(row.get('c', 0)) == 0:
            db_exec("ALTER TABLE albums ADD COLUMN event_id BIGINT UNSIGNED NULL AFTER owner")
            try:
                db_exec("CREATE INDEX idx_albums_event_id ON albums(event_id)")
            except Exception:
                pass

        row = db_get_one(
            "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='albums' AND COLUMN_NAME=%s",
            ('access_mode',)
        )
        if not row or int(row.get('c', 0)) == 0:
            db_exec("ALTER TABLE albums ADD COLUMN access_mode ENUM('token','event') NOT NULL DEFAULT 'token' AFTER event_id")
    except Exception as e:
        current_app.logger.exception("albums schema ensure failed (compat alter): %s", e)

@album_bp.record_once
def _albums_on_register(state):
    try:
        _ensure_event_gate_columns()
    except Exception as e:
        state.app.logger.exception("albums schema ensure failed: %s", e)

def _fetch_album_meta(album_id: str):
    """albums から owner, access_token, event_id, access_mode を取得"""
    row = db_get_one(
        "SELECT id, owner, access_token, event_id, access_mode FROM albums WHERE id=%s",
        (album_id,)
    )
    if not row:
        return None
    row["access_mode"] = row.get("access_mode") or "token"
    return row

def _uuid_bytes_to_str(b):
    try:
        return str(uuid.UUID(bytes=b))
    except Exception:
        try:
            return str(uuid.UUID(hex=b.hex()))
        except Exception:
            return None

def _event_join_url(event_id: int) -> str | None:
    ev = db_get_one("SELECT event_uuid FROM mfu_event WHERE id=%s", (event_id,))
    if not ev: return None
    ev_uuid_str = _uuid_bytes_to_str(ev.get("event_uuid"))
    if not ev_uuid_str: return None
    return url_for('external_login_user.join_event', event_uuid=ev_uuid_str)

def _is_event_member_approved(event_id: int) -> bool:
    sid = session.get("ext_user_social_id")
    if not sid:
        session["after_login_redirect"] = url_for('album.album_access', album_id=session.get("_gate_album_id"), _external=True)
        return False
    u = db_get_one("SELECT id FROM external_login_user WHERE social_id=%s", (sid,))
    if not u:
        session["ext_user_onboarding"] = True
        session["after_login_redirect"] = url_for('album.album_access', album_id=session.get("_gate_album_id"), _external=True)
        return False
    ext_user_id = int(u["id"])
    mem = db_get_one(
        "SELECT status, COALESCE(is_canceled,0) AS is_canceled FROM mfu_event_member WHERE event_id=%s AND user_id=%s",
        (event_id, ext_user_id),
    )
    return bool(mem and mem.get("status") == "approved" and int(mem.get("is_canceled") or 0) == 0)

def event_gate(view_func):
    """イベント連携アルバム用ゲート（access_mode='event' だけ処理）。他はスルー"""
    @wraps(view_func)
    def _wrapped(album_id, *args, **kwargs):
        session["_gate_album_id"] = album_id  # ログイン後の戻り先
        meta = _fetch_album_meta(album_id)
        if not meta:
            return "アルバムが存在しません", 404

        # 管理者/オーナーは常に通す
        if session.get('user') == 'admin' or (session.get('user') and session.get('user') == meta.get('owner')):
            _grant_album_auth(album_id)
            return redirect(url_for('album.album_home', album_id=album_id))

        # イベントモード：承認済みのみ
        if meta.get("access_mode") == "event" and meta.get("event_id"):
            if _is_event_member_approved(int(meta["event_id"])):  # 承認済み
                _grant_album_auth(album_id)
                return redirect(url_for('album.album_home', album_id=album_id))
            flash('このアルバムはイベント参加者専用です。まずは参加申請/承認を受けてください。', 'warning')
            join_url = _event_join_url(int(meta["event_id"]))
            return redirect(join_url) if join_url else ("イベントが見つかりません", 404)

        # tokenモードは既存処理へ
        return view_func(album_id, *args, **kwargs)
    return _wrapped

# =============================================================================
# 移動進捗トラッキング（JSON）＋ 非同期移動API
# =============================================================================
import threading

def _move_progress_dir() -> str:
    d = os.path.join('/mnt/mfu/tmp', 'mfu-move-progress')  # zip_stream と同じ領域系でOK
    os.makedirs(d, exist_ok=True)
    return d

def _move_progress_path(album_id: str) -> str:
    return os.path.join(_move_progress_dir(), f"{album_id}.json")

def _move_progress_write(album_id: str, data: dict):
    p = _move_progress_path(album_id)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass

def _move_progress_read(album_id: str):
    p = _move_progress_path(album_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _gather_all_files(src: str):
    """コピー対象の全ファイル一覧と総バイトを先に集計"""
    files = []
    total_bytes = 0
    for base, _dirs, fnames in os.walk(src):
        for name in fnames:
            sp = os.path.join(base, name)
            try:
                st = os.stat(sp)
            except FileNotFoundError:
                continue
            if os.path.isfile(sp):
                files.append(sp)
                total_bytes += st.st_size
    return files, total_bytes

def _copy_tree_with_progress(src: str, dst_tmp: str, album_id: str):
    """
    進捗JSONを書きつつディレクトリをコピーする。
    - percent は中間最大99、完了で100
    """
    files, total_bytes = _gather_all_files(src)
    total_files = len(files)
    processed_files = 0
    processed_bytes = 0
    started = time.time()

    # 初期進捗
    _move_progress_write(album_id, {
        "status": "running",
        "total_files": total_files,
        "processed_files": 0,
        "total_bytes": total_bytes,
        "processed_bytes": 0,
        "percent": 0,
        "started_ts": started,
    })

    # 実コピー（ディレクトリは mkdir しつつ、ファイルは copy2）
    for src_file in files:
        rel = os.path.relpath(src_file, src)
        dst_file = os.path.join(dst_tmp, rel)
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        shutil.copy2(src_file, dst_file)

        # 進捗更新
        try:
            sz = os.path.getsize(src_file)
        except Exception:
            sz = 0
        processed_files += 1
        processed_bytes += sz

        if total_bytes > 0:
            pct = int(processed_bytes * 100 / total_bytes)
        else:
            pct = int(processed_files * 100 / max(1, total_files))

        if processed_files < total_files:
            pct = min(99, pct)
        else:
            pct = 100

        _move_progress_write(album_id, {
            "status": "running",
            "total_files": total_files,
            "processed_files": processed_files,
            "total_bytes": total_bytes,
            "processed_bytes": processed_bytes,
            "percent": pct,
            "started_ts": started,
        })

def _finalize_move_progress(album_id: str, ok: bool, message: str = ""):
    info = _move_progress_read(album_id) or {}
    info.update({
        "status": "done" if ok else "error",
        "percent": 100 if ok else info.get("percent", 0),
        "message": message,
        "completed_ts": time.time(),
    })
    _move_progress_write(album_id, info)

def _move_album_physical_photos_safe_with_progress(album_id: str, dest_storage: str):
    """
    _move_album_physical_photos_safe と同等だが、コピー部分を進捗付きで実施。
    """
    cur_storage, cur_root = _get_album_storage(album_id)
    dst_root = SSD_ROOT if dest_storage == "ssd" else HDD_ROOT
    if cur_root == dst_root:
        _finalize_move_progress(album_id, False, "すでに目的地にあります")
        return False, "すでに目的地にあります"

    src_dir = os.path.join(cur_root, album_id)
    if not os.path.isdir(src_dir):
        _finalize_move_progress(album_id, False, "移動元が存在しません")
        return False, "移動元が存在しません"

    os.makedirs(dst_root, exist_ok=True)
    final_dst = os.path.join(dst_root, album_id)
    if os.path.exists(final_dst):
        _finalize_move_progress(album_id, False, "移動先に同名ディレクトリが既に存在します")
        return False, "移動先に同名ディレクトリが既に存在します"

    ts = time.strftime("%Y%m%d_%H%M%S")
    dst_tmp = os.path.join(dst_root, f"{album_id}.moving_{ts}")
    if os.path.exists(dst_tmp):
        shutil.rmtree(dst_tmp, ignore_errors=True)
    os.makedirs(dst_tmp, exist_ok=True)

    try:
        # 1) コピー（進捗出力）
        _copy_tree_with_progress(src_dir, dst_tmp, album_id)

        # 2) 検証
        src_count, src_bytes = _count_files_and_bytes(src_dir)
        dst_count, dst_bytes = _count_files_and_bytes(dst_tmp)
        if src_count != dst_count or src_bytes != dst_bytes:
            raise RuntimeError(f"検証NG: files {src_count}->{dst_count}, bytes {src_bytes}->{dst_bytes}")

        # 3) 宛先へ原子的切替
        os.rename(dst_tmp, final_dst)

        # 4) 旧データ退避
        backup_name = f"{album_id}.backup_{ts}"
        backup_path = os.path.join(cur_root, backup_name)
        os.rename(src_dir, backup_path)

        # 5) DB切替
        _set_album_storage(album_id, dest_storage)

        msg = f"切替完了。旧データは {backup_path} にバックアップとして残しました。確認後に手動削除してください。"
        _finalize_move_progress(album_id, True, msg)
        return True, msg

    except Exception as e:
        try:
            if os.path.isdir(dst_tmp):
                shutil.rmtree(dst_tmp, ignore_errors=True)
        except Exception:
            pass
        _finalize_move_progress(album_id, False, f"移動失敗: {e}")
        return False, f"移動失敗: {e}"

# ---- 非同期開始APIと進捗取得API ----
_move_threads: dict[str, threading.Thread] = {}



# =============================================================================
# ルート：アクセス/トップ/子作成
# =============================================================================
@album_bp.route('/<album_id>/regenerate_token', methods=['POST'])
def regenerate_token(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    if not (user == 'admin' or user == meta.get('owner')):
        return '再発行権限がありません', 403

    regenerate_access_token(album_id)
    flash('アクセストークンを再発行しました', 'success')

    redirect_target = 'album.admin_create_album' if user == 'admin' else 'album.create_album'
    return redirect(url_for(redirect_target))

@album_bp.route('/access/<album_id>', methods=['GET', 'POST'])
@event_gate
def album_access(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    if session.get('user') == 'admin' or session.get('user') == meta.get('owner'):
        _grant_album_auth(album_id)
        return redirect(url_for('album.album_home', album_id=album_id))

    token = request.args.get('token')
    if token and token == meta.get('access_token'):
        _grant_album_auth(album_id)
        return redirect(url_for('album.album_home', album_id=album_id))

    if request.method == 'POST':
        return 'このアルバムはトークン式です', 403

    return render_template('access.html', album_id=album_id)

@album_bp.route('/<album_id>/')
def album_home(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    is_admin = (user == 'admin')
    is_owner = user and meta.get('owner') == user
    is_authed = _has_album_auth(album_id)
    if not (is_admin or is_owner or is_authed):
        return redirect(url_for('album.album_access', album_id=album_id))

    ext_user_nickname = _get_ext_user_nickname()
    album_meta = _fetch_album_meta(album_id)
    show_extlogin_nav = bool((album_meta or {}).get("access_mode") == "event" and _is_ext_logged_in())

    # ★追加：加工ロック一覧を取得
    processing_list = []
    try:
        conn = get_db()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT child_id, username, expires_at
              FROM album_locks
             WHERE album_id=%s
             ORDER BY expires_at DESC
        """, (album_id,))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()

        # child_id → 子アルバム名 に変換
        child_map = {c["folder"]: c["name"] for c in meta.get("children", [])}

        for r in rows:
            processing_list.append({
                "child_id": r["child_id"],
                "child_name": child_map.get(r["child_id"], "(不明)"),
                "username": r["username"],
                "expires_at": r["expires_at"].strftime("%H:%M")
            })
    except Exception as e:
        processing_list = []

    # ★HDD保管中フラグ
    storage, _ = _get_album_storage(album_id)
    is_readonly = (storage == 'hdd')

    completed_process_children = set()
    event_meta = _fetch_album_meta(album_id)
    if event_meta and event_meta.get("event_id"):
        for child in meta.get("children", []):
            if child.get("mode") != "process":
                continue
            status_map = _fetch_album_process_status_map(album_id, child.get("folder"))
            requested_ids = [
                ext_user_id
                for ext_user_id, status in status_map.items()
                if int(status.get("request_flag", 0)) == 1
            ]
            if requested_ids and all(
                int(status_map.get(ext_user_id, {}).get("complete_flag", 0)) == 1
                for ext_user_id in requested_ids
            ):
                completed_process_children.add(child.get("folder"))

    for child in meta.get("children", []):
        mode = child.get("mode") or "normal"
        child["media_count"] = _count_child_media_items(album_id, child.get("folder"), mode)
        child["media_unit"] = "本" if mode == "movie" else "枚"

    return render_template(
        'album_home.html',
        album_id=album_id, meta=meta,
        is_admin=is_admin, is_owner=is_owner,
        is_readonly=is_readonly,
        processing_list=processing_list,   # ★追加
        ext_user_nickname=ext_user_nickname,
        completed_process_children=completed_process_children,
        show_extlogin_nav=show_extlogin_nav,
    )

@album_bp.route('/<album_id>/create_child', methods=['POST'])
def create_child(album_id):
    meta = load_meta(album_id)
    if not meta:
        return redirect(url_for('album.album_access', album_id=album_id))

    # ★HDD保管中は子作成を禁止（アーカイブ扱い）
    if is_album_readonly(album_id):
        flash('このアルバムはHDD保管中（アーカイブ）のため子アルバムを追加できません。', 'warning')
        return redirect(url_for('album.album_home', album_id=album_id))

    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))

    folder_name = (request.form.get('child_name') or '').strip()
    selected_mode = (request.form.get('child_mode') or 'normal').strip().lower()
    if selected_mode not in ('normal', 'process', 'movie'):
        selected_mode = 'normal'

    if not folder_name:
        flash('子アルバム名を入力してください', 'danger')
        return redirect(url_for('album.album_home', album_id=album_id))

    for child in meta.get('children', []):
        if child.get('name') == folder_name:
            flash('既に存在します', 'danger')
            return redirect(url_for('album.album_home', album_id=album_id))

    child_uuid = add_child_row(album_id, folder_name, selected_mode)
    os.makedirs(storage_child_dir(album_id, child_uuid, selected_mode), exist_ok=True)

    flash('子アルバムを作成しました', 'success')
    return redirect(url_for('album.album_home', album_id=album_id))

# =============================================================================
# アップロード（画像/加工/動画）
# =============================================================================
@album_bp.route('/<album_id>/upload/<child_id>', methods=['GET', 'POST'])
def upload_child(album_id, child_id):
    from PIL import Image

    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    # ★HDD保管中（アーカイブ）はアップロード画面も処理もブロック
    try:
        storage, _ = _get_album_storage(album_id)  # 例: ('ssd' | 'hdd', <path等>)
    except Exception:
        storage = None
    if storage == 'hdd':
        flash('このアルバムはHDD保管中（アーカイブ）のため写真・動画を追加できません。', 'warning')
        return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

    child_meta = next((c for c in meta["children"] if c["folder"] == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"
    child_path = storage_child_dir(album_id, child_id, mode)
    lock_path = os.path.join(child_path, 'lock.json')

    lock = None
    if os.path.exists(lock_path):
        try:
            with open(lock_path, 'r') as f:
                lock = json.load(f)
        except Exception:
            lock = None

    # ─────────────────────────────────────────────────────────────
    # ★通知ヘルパ（このルート内だけで完結）
    #   条件: albums.event_id があり access_mode='event'
    #   宛先: mfu_event_member.approved の external_login_user.email
    #   フィルタ: 各ユーザーの notify_album_upload / notify_album_process を反映
    #   リンク: /external-login/events/<UUID>/album 直リンク
    #   クールタイム: kind=='upload' は 5分間抑止（process_doneは除外）
    # ─────────────────────────────────────────────────────────────
    def _notify_event_members(kind: str, saved_names: list[str] | None = None) -> None:
        def _preview_recipients(items: list[str], head: int = 5, tail: int = 2) -> list[str]:
            if len(items) <= (head + tail):
                return items
            return items[:head] + [f"...({len(items) - (head + tail)} omitted)..."] + items[-tail:]

        try:
            # イベント連携の判定
            meta_ev = _fetch_album_meta(album_id)
            if not meta_ev or meta_ev.get('access_mode') != 'event' or not meta_ev.get('event_id'):
                current_app.logger.info("notify: skip (album not event) album_id=%s", album_id)
                return

            album_name = meta.get("album_name", "アルバム")
            child_name = (child_meta or {}).get("name") or child_id
            event_id = int(meta_ev["event_id"])
            current_app.logger.info("notify: event album detected album_id=%s event_id=%s", album_id, event_id)

            # 承認済み参加者 + 通知設定カラムも取得
            sql = (
                "SELECT m.user_id AS ext_user_id,"
                "       u.email,"
                "       COALESCE(u.nickname, '') AS nickname,"
                "       COALESCE(u.notify_album_upload, 1)  AS notify_album_upload,"
                "       COALESCE(u.notify_album_process, 1) AS notify_album_process "
                "  FROM mfu_event_member m "
                "  JOIN external_login_user u ON u.id = m.user_id "
                " WHERE m.event_id=%s AND m.status='approved' "
            )

            rows = []
            try:
                from app.utils.db import get_db  # 低レベル
                conn = get_db()
                try:
                    cur = conn.cursor(dictionary=True)
                except Exception:
                    cur = conn.cursor()
                cur.execute(sql, (event_id,))
                fetched = cur.fetchall() or []
                if fetched and not isinstance(fetched[0], dict):
                    cols = [d[0] for d in cur.description]
                    for r in fetched:
                        rows.append({cols[i]: r[i] for i in range(len(cols))})
                else:
                    rows = fetched
                try:
                    cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            except Exception:
                # フォールバック：高レベル
                try:
                    from app.utils.db import db_get_all as _db_get_all
                    rows = _db_get_all(sql, (event_id,))
                except Exception as e2:
                    current_app.logger.warning("notify(db) failed: %s", e2)
                    return

            recipients_total = len(rows)

            # ★ process モードの通知は「未完了のみ」へ絞り込み
            if mode == "process" and kind in ("upload", "process_done"):
                status_map = _fetch_album_process_status_map(album_id, child_id)
                filtered = []
                for r in rows:
                    try:
                        ext_user_id = int(r.get("ext_user_id"))
                    except (TypeError, ValueError):
                        continue
                    status = status_map.get(ext_user_id)
                    if not status:
                        continue
                    if int(status.get("request_flag", 0)) != 1:
                        continue
                    if int(status.get("complete_flag", 0)) == 1:
                        continue
                    filtered.append(r)
                rows = filtered
                current_app.logger.info(
                    "notify: filter kind=%s album_id=%s child_id=%s sql_condition=%s candidates=%s",
                    kind,
                    album_id,
                    child_id,
                    "request_flag=1 AND complete_flag=0",
                    len(rows),
                )

            def _is_kind_enabled(recipient: dict) -> bool:
                if kind == "upload":
                    return int(recipient.get("notify_album_upload", 1) or 0) == 1
                if kind == "process_done":
                    return int(recipient.get("notify_album_process", 1) or 0) == 1
                return True

            recipients = [r for r in rows if _is_kind_enabled(r)]
            notify_kind_excluded_count = max(0, len(rows) - len(recipients))
            should_send_admin_push = kind in ("upload", "process_done")
            if not recipients and not should_send_admin_push:
                current_app.logger.info(
                    "notify: skip no recipients kind=%s album_id=%s child_id=%s recipients_total=%s",
                    kind,
                    album_id,
                    child_id,
                    recipients_total,
                )
                return

            mail_recipients = [
                str(r.get("email") or "").strip()
                for r in recipients
                if str(r.get("email") or "").strip()
            ]
            push_candidate_user_ids = []
            for r in recipients:
                try:
                    ext_user_id = int(r.get("ext_user_id") or 0)
                except (TypeError, ValueError):
                    ext_user_id = 0
                if ext_user_id > 0:
                    push_candidate_user_ids.append(ext_user_id)

            push_subscribed_user_ids = _fetch_push_subscribed_ext_user_ids(push_candidate_user_ids)
            push_recipients = [ext_user_id for ext_user_id in push_candidate_user_ids if ext_user_id in push_subscribed_user_ids]
            push_subscription_excluded_count = max(0, len(push_candidate_user_ids) - len(push_recipients))

            current_app.logger.info(
                "notify: recipients(after filter)=%d album_id=%s child_id=%s mail_recipients=%d push_candidates=%d push_recipients=%d notify_kind_excluded=%d push_subscription_excluded=%d",
                len(recipients),
                album_id,
                child_id,
                len(mail_recipients),
                len(push_candidate_user_ids),
                len(push_recipients),
                notify_kind_excluded_count,
                push_subscription_excluded_count,
            )
            request_by_email = None
            if _is_ext_logged_in():
                ext_user_id = session.get("ext_user_id")
                if ext_user_id:
                    me = db_get_one("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (int(ext_user_id),))
                    request_by_email = (me or {}).get("email")
            current_app.logger.info(
                "notify: pre_send kind=%s album_id=%s child_id=%s request_by=%s recipients_count=%s recipients=%s sql_condition=%s",
                kind,
                album_id,
                child_id,
                request_by_email,
                len(mail_recipients),
                _preview_recipients(mail_recipients),
                "request_flag=1 AND complete_flag=0" if (mode == "process" and kind in ("upload", "process_done")) else "n/a",
            )

            # ★ クールタイム（uploadのみ対象／process_doneは対象外）
            cooldown_state_path = os.path.join(child_path, ".notify_state.json")
            cooldown_bucket = None
            if kind == "upload":
                COOLDOWN_SEC = 300  # 5分
                now_ts = int(time.time())
                cooldown_bucket = now_ts // COOLDOWN_SEC
                last_upload_ts = None
                try:
                    if os.path.exists(cooldown_state_path):
                        with open(cooldown_state_path, "r", encoding="utf-8") as sf:
                            st = json.load(sf) or {}
                            if st.get("last_upload_ts") is not None:
                                last_upload_ts = int(st.get("last_upload_ts"))
                except Exception:
                    last_upload_ts = None
                if last_upload_ts is not None and (now_ts - last_upload_ts) < COOLDOWN_SEC:
                    remain = COOLDOWN_SEC - (now_ts - last_upload_ts)
                    current_app.logger.info("notify: cooldown skip kind=%s remain=%ss album_id=%s child_id=%s",
                                            kind, remain, album_id, child_id)
                    return

            target_urls = _build_event_album_target_urls(event_id, album_id, child_id)
            absolute_target_url = target_urls["absolute_url"]
            relative_target_url = target_urls["relative_url"]
            admin_target_url = url_for(
                'album.view_child',
                album_id=album_id,
                child_id=child_id,
                _external=False,
            )

            if kind == "upload":
                action = f"{len(saved_names or [])}件の写真/動画が追加されました"
                push_kind = "album_upload"
                title = f"【アルバム更新】{album_name}"
                body_text = f"{album_name} の「{child_name}」に{action}。"
            elif kind == "process_done":
                action = "加工済み写真が更新されました"
                push_kind = "album_process_done"
                title = f"【加工完了】{album_name}"
                body_text = f"{album_name} の「{child_name}」の{action}"
            else:
                action = "アルバムが更新されました"
                push_kind = "album_update"
                title = f"【アルバム更新】{album_name}"
                body_text = f"{album_name} の「{child_name}」が更新されました。"

            body = f"""{body_text}

アクセスはこちら（アルバム直リンク）:
{absolute_target_url}

このメールはイベント参加者（承認済み）のみへ自動通知しています。"""

            process_done_state = "na"
            if kind == "process_done":
                latest_path = os.path.join(child_path, "latest.jpg")
                try:
                    process_done_state = str(int(os.path.getmtime(latest_path)))
                except Exception:
                    process_done_state = str(int(time.time()))

            def _build_push_dedup_key(ext_user_id: int) -> str:
                if kind == "upload":
                    bucket = cooldown_bucket if cooldown_bucket is not None else int(time.time()) // 300
                    return f"album:{album_id}:{child_id}:upload:{ext_user_id}:{bucket}"[:191]
                if kind == "process_done":
                    return f"album:{album_id}:{child_id}:process_done:{ext_user_id}:{process_done_state}"[:191]
                return f"album:{album_id}:{child_id}:{kind}:{ext_user_id}"[:191]

            def _build_admin_push_dedup_key() -> str:
                if kind == "upload":
                    bucket = cooldown_bucket if cooldown_bucket is not None else int(time.time()) // 300
                    return f"album:{album_id}:{child_id}:upload:mfu:admin:{bucket}"[:191]
                if kind == "process_done":
                    return f"album:{album_id}:{child_id}:process_done:mfu:admin:{process_done_state}"[:191]
                return f"album:{album_id}:{child_id}:{kind}:mfu:admin"[:191]

            try:
                from app.utils.mail import send_mail  # 既存の mail.py を利用
            except Exception:
                try:
                    from app.mail import send_mail
                except Exception:
                    current_app.logger.warning("notify: send_mail import failed")
                    send_mail = None

            # 送信
            sent_ok = False
            mail_failed_count = 0
            if send_mail:
                for to in mail_recipients:
                    try:
                        current_app.logger.info("notify: send -> %s", to)
                        send_mail(to, title, body)
                        sent_ok = True
                    except Exception as e:
                        mail_failed_count += 1
                        current_app.logger.warning("notify send failed to %s: %s", to, e)

            push_failed_count = 0
            push_skipped_count = 0
            dedup_key_samples: list[str] = []
            for ext_user_id in push_recipients:
                dedup_key = _build_push_dedup_key(ext_user_id)
                if len(dedup_key_samples) < 3:
                    dedup_key_samples.append(dedup_key)
                try:
                    push_result = send_push(
                        recipient_type="external_user_id",
                        recipient_value=ext_user_id,
                        title=title,
                        body=body_text,
                        target_url=relative_target_url,
                        kind=push_kind,
                        sender_label="アルバム",
                        dedup_key=dedup_key,
                        event_id=event_id,
                        create_in_app=True,
                        send_web_push=True,
                    )
                    if bool(push_result.get("created")) or bool(push_result.get("ok")):
                        sent_ok = True
                    delivery = push_result.get("delivery") or {}
                    web_push_status = str(delivery.get("web_push") or "")
                    in_app_status = str(delivery.get("in_app") or "")
                    if push_result.get("duplicate") or (web_push_status == "skipped" and in_app_status in {"duplicate", "skipped"}):
                        push_skipped_count += 1
                except Exception as e:
                    push_failed_count += 1
                    current_app.logger.warning(
                        "notify push failed kind=%s album_id=%s child_id=%s ext_user_id=%s: %s",
                        kind,
                        album_id,
                        child_id,
                        ext_user_id,
                        e,
                    )

            admin_push_result = {
                "attempted": False,
                "ok": False,
                "duplicate": False,
                "in_app": "not_attempted",
                "web_push": "not_attempted",
                "failure": "",
                "dedup_key": "",
            }
            if should_send_admin_push:
                admin_push_result["attempted"] = True
                admin_dedup_key = _build_admin_push_dedup_key()
                admin_push_result["dedup_key"] = admin_dedup_key
                try:
                    push_result = send_push(
                        recipient_type="mfu_username",
                        recipient_value="admin",
                        title=title,
                        body=body_text,
                        target_url=admin_target_url,
                        kind=push_kind,
                        sender_label="アルバム",
                        dedup_key=admin_dedup_key,
                        event_id=event_id,
                        create_in_app=True,
                        send_web_push=True,
                    )
                    delivery = push_result.get("delivery") or {}
                    admin_push_result["ok"] = bool(push_result.get("created")) or bool(push_result.get("ok"))
                    admin_push_result["duplicate"] = bool(push_result.get("duplicate"))
                    admin_push_result["in_app"] = str(delivery.get("in_app") or "")
                    admin_push_result["web_push"] = str(delivery.get("web_push") or "")
                    if admin_push_result["ok"]:
                        sent_ok = True
                    current_app.logger.info(
                        "notify admin push result admin_push_kind=%s album_id=%s child_id=%s admin_target_url=%s "
                        "dedup_key=%s in_app=%s web_push=%s duplicate=%s ok=%s",
                        kind,
                        album_id,
                        child_id,
                        admin_target_url,
                        admin_dedup_key,
                        admin_push_result["in_app"],
                        admin_push_result["web_push"],
                        admin_push_result["duplicate"],
                        admin_push_result["ok"],
                    )
                except Exception as e:
                    admin_push_result["failure"] = str(e)
                    current_app.logger.warning(
                        "notify admin push failed admin_push_kind=%s album_id=%s child_id=%s admin_target_url=%s error=%s",
                        kind,
                        album_id,
                        child_id,
                        admin_target_url,
                        e,
                    )

            # ★クールタイム記録（uploadのみ・送信が1件以上成功時）
            if kind == "upload" and sent_ok:
                try:
                    now_ts = int(time.time())
                    tmp = cooldown_state_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as tf:
                        json.dump({"last_upload_ts": now_ts}, tf, ensure_ascii=False, indent=2)
                    os.replace(tmp, cooldown_state_path)
                except Exception as e:
                    current_app.logger.warning("notify(cooldown write) failed: %s", e)

            current_app.logger.info(
                "notify: summary kind=%s album_id=%s child_id=%s recipients_total=%s mail_recipients_count=%s push_recipients_count=%s "
                "notify_kind_excluded_count=%s push_subscription_excluded_count=%s "
                "relative_target_url=%s absolute_target_url=%s admin_target_url=%s dedup_key_samples=%s dedup_key_count=%s "
                "mail_failed_count=%s push_failed_count=%s push_skipped_count=%s admin_push_attempted=%s admin_push_dedup_key=%s "
                "admin_push_in_app=%s admin_push_web_push=%s admin_push_duplicate=%s admin_push_ok=%s admin_push_failure=%s",
                kind,
                album_id,
                child_id,
                recipients_total,
                len(mail_recipients),
                len(push_recipients),
                notify_kind_excluded_count,
                push_subscription_excluded_count,
                relative_target_url,
                absolute_target_url,
                admin_target_url,
                dedup_key_samples,
                len(push_recipients),
                mail_failed_count,
                push_failed_count,
                push_skipped_count,
                admin_push_result["attempted"],
                admin_push_result["dedup_key"],
                admin_push_result["in_app"],
                admin_push_result["web_push"],
                admin_push_result["duplicate"],
                admin_push_result["ok"],
                admin_push_result["failure"],
            )

        except Exception as e:
            current_app.logger.warning("notify(inner) failed: %s", e)

    if request.method == 'POST':
        files = request.files.getlist('file')
        if not files or files[0].filename == '':
            flash('ファイルが選択されていません', 'danger')
            return redirect(request.url)

        # === movie：連番保存 + 変換/ポスターは「キュー投入」（ワーカーが1本ずつ処理） ===
        if mode == "movie":
            os.makedirs(child_path, exist_ok=True)
            orig_dir = os.path.join(child_path, 'original')
            enc_dir  = os.path.join(child_path, 'encoded')
            os.makedirs(orig_dir, exist_ok=True)
            os.makedirs(enc_dir, exist_ok=True)

            MOVIE_QUEUE_DIR = '/mnt/mfu/movie_queue'
            os.makedirs(MOVIE_QUEUE_DIR, exist_ok=True)

            saved = 0
            saved_names = []

            # 既存の最大連番を original/ から取得（*.web.mp4 は対象外）
            pat = re.compile(rf"^{re.escape(child_id)}_\d{{8}}_\d{{4}}_(\d{{4}})\.[A-Za-z0-9]+$", re.I)
            next_seq = 1
            for name in os.listdir(orig_dir):
                m = pat.match(name)
                if m:
                    try:
                        next_seq = max(next_seq, int(m.group(1)) + 1)
                    except ValueError:
                        pass

            stamp = time.strftime("%Y%m%d_%H%M")  # アップロード時刻(分まで)

            for i, file in enumerate(files):
                if not file or not file.filename:
                    continue
                if not allowed_movie(file.filename):
                    continue

                # サイズチェック
                try:
                    file.stream.seek(0, os.SEEK_END)
                    size = file.stream.tell()
                    file.stream.seek(0)
                    if size > MAX_MOVIE_SIZE:
                        current_app.logger.warning("movie too large (>%s): %s", MAX_MOVIE_SIZE, file.filename)
                        continue
                except Exception:
                    try:
                        file.stream.seek(0)
                    except Exception:
                        pass

                ext = secure_filename(file.filename).rsplit('.', 1)[-1].lower()
                seq = next_seq + i
                save_name = f"{child_id}_{stamp}_{seq:04d}.{ext}"
                dst_path = os.path.join(orig_dir, save_name)

                file.save(dst_path)
                saved += 1
                saved_names.append(save_name)

                # エンコードはキュー投入（ワーカー処理）
                try:
                    base_name, _ = os.path.splitext(save_name)
                    job = {
                        "type": "movie_encode",
                        "album_id": album_id,
                        "child_id": child_id,
                        "src": dst_path,
                        "enc_dir": enc_dir,
                        "basename": base_name,
                        "out_ext": ".web.mp4",
                        "make_poster": True,
                        "priority": seq,
                        "created_ts": int(time.time()),
                    }
                    job_name = f"{int(time.time()*1000)}_{seq:04d}_{uuid.uuid4().hex}.json"
                    with open(os.path.join(MOVIE_QUEUE_DIR, job_name), "w", encoding="utf-8") as jf:
                        json.dump(job, jf, ensure_ascii=False)
                except Exception as e:
                    current_app.logger.warning('movie job enqueue failed: %s', e)

            flash(f'動画を{saved}件アップロードしました（順番にエンコード待ち）', 'success')

            # ★アップロード受領の時点で通知（エンコード完了通知はワーカー側で追加可）
            try:
                if saved_names:
                    _notify_event_members('upload', saved_names)
            except Exception as e:
                current_app.logger.warning("notify(movie) failed: %s", e)

            return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

        # === process：最新1枚 ===
        if mode == "process":
            file = files[0]
            os.makedirs(child_path, exist_ok=True)
            had_latest = any(f.startswith('latest.') for f in os.listdir(child_path))
            save_path = os.path.join(child_path, "latest.jpg")
            history_dir = os.path.join(child_path, 'history')
            os.makedirs(history_dir, exist_ok=True)
            for f in os.listdir(child_path):
                if f.startswith('latest.'):
                    timestamp = time.strftime('%Y%m%d_%H%M%S')
                    try:
                        shutil.move(os.path.join(child_path, f), os.path.join(history_dir, f"{timestamp}_{f}"))
                    except Exception:
                        pass
            file.stream.seek(0)
            try:
                img = Image.open(file.stream).convert("RGB")
                img.save(save_path, format="JPEG")
            except Exception:
                file.stream.seek(0)
                file.save(save_path)
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass
            try:
                release_lock_db(album_id, child_id, username=None, force=True)
            except Exception:
                pass
            history_json = os.path.join(child_path, "history.json")
            record = {
                "user": (lock or {}).get("user", "不明"),
                "timestamp": int(time.time()),
                "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            try:
                if os.path.exists(history_json):
                    with open(history_json, 'r') as f:
                        history_list = json.load(f)
                else:
                    history_list = []
            except Exception:
                history_list = []
            history_list.append(record)
            try:
                tmp = history_json + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(history_list, f, ensure_ascii=False, indent=2)
                os.replace(tmp, history_json)
            except Exception:
                pass

            # ★アップロード時に完了チェック＆未完了の人へ通知（イベントログイン時のみ）
            try:
                event_meta = _fetch_album_meta(album_id)
                is_event_login = bool(_is_ext_logged_in() and event_meta and event_meta.get("access_mode") == "event")
                if is_event_login and event_meta and event_meta.get("event_id"):
                    event_id = int(event_meta["event_id"])
                    ext_user_id = session.get("ext_user_id")
                    if not ext_user_id:
                        ext_social_id = session.get("ext_user_social_id")
                        if ext_social_id:
                            ext_user = _get_ext_user_by_social(ext_social_id)
                            if ext_user:
                                ext_user_id = ext_user.get("id")
                    if ext_user_id:
                        _ensure_album_process_table()
                        prev_complete_flag = None
                        request_by_id = None
                        try:
                            prev_row = db_get_one(
                                """
                                SELECT complete_flag, request_by
                                  FROM album_process
                                 WHERE ext_user_id=%s AND album_id=%s AND child_id=%s
                                """,
                                (int(ext_user_id), album_id, child_id),
                            )
                            if prev_row:
                                prev_complete_flag = int(prev_row.get("complete_flag", 0))
                                try:
                                    request_by_id = int(prev_row.get("request_by"))
                                except (TypeError, ValueError):
                                    request_by_id = None
                        except Exception:
                            prev_complete_flag = None
                            request_by_id = None
                        db_exec(
                            """
                            INSERT INTO album_process (ext_user_id, album_id, child_id, request_flag, complete_flag)
                            VALUES (%s, %s, %s, 0, 1)
                            ON DUPLICATE KEY UPDATE
                              request_flag=request_flag,
                              complete_flag=1
                            """,
                            (int(ext_user_id), album_id, child_id),
                        )
                        if prev_complete_flag != 1:
                            _notify_requester_process_completion(album_id, child_id, request_by_id, meta, event_meta)

            except Exception as e:
                current_app.logger.warning("notify(process upload) failed: %s", e)

            # ★加工完了の通知（クールタイム対象外）
            try:
                _notify_event_members('process_done', ['latest.jpg'])
            except Exception as e:
                current_app.logger.warning("notify(process) failed: %s", e)

        # === normal：連番保存（従来） ===
        else:
            os.makedirs(child_path, exist_ok=True)
            existing = [f for f in os.listdir(child_path) if allowed_file(f) and f.startswith(child_id)]
            numbers = [int(f.split("_")[-1].split(".")[0]) for f in existing if "_" in f and f.split("_")[-1].split(".")[0].isdigit()]
            next_num = max(numbers, default=0) + 1

            saved_names = []

            for i, file in enumerate(files):
                if not allowed_file(file.filename):
                    continue
                ext = secure_filename(file.filename).rsplit('.', 1)[-1].lower()
                dt = get_datetime_from_image(file)
                file.stream.seek(0)
                filename = f"{child_id}_{dt}_{next_num + i:04d}.{ext}"
                file.save(os.path.join(child_path, filename))
                saved_names.append(filename)

            try:
                enqueue_thumb_job("album", album_id, child_id)
            except Exception:
                pass

            # ★アップロード完了の通知（5分クールタイム対象＋通知設定フィルタ適用）
            try:
                if saved_names:
                    _notify_event_members('upload', saved_names)
            except Exception as e:
                current_app.logger.warning("notify(normal) failed: %s", e)

        flash('ファイルをアップロードしました', 'success')
        return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

    return render_template('upload_child.html', album_id=album_id, child_id=child_id, meta=meta, mode=mode, lock=lock)


# =============================================================================
# ビュー（画像/加工/動画）
# =============================================================================
@album_bp.route('/<album_id>/view/<child_id>', methods=['GET', 'POST'])
def view_child(album_id, child_id):
    """
    子アルバムビュー
    - mode == 'movie' の場合:
        * 変換済み encoded/*.web.mp4 を優先して再生URLに採用
        * .web.mp4 が未生成なら converting=True を渡す（テンプレ側で「変換中…」表示）
        * 一覧は「アップロード順（連番名）」で昇順ソート
          期待ファイル名: {child_id}_YYYYMMDD_HHMM_NNNN.ext
    - それ以外は静止画/加工。SSD/HDD 両方を探索して表示
    """
    import re
    from datetime import datetime

    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    if not child_meta:
        return '子アルバムが存在しません', 404

    mode = child_meta.get("mode", "normal")
    album_meta = _fetch_album_meta(album_id)
    show_extlogin_nav = bool((album_meta or {}).get("access_mode") == "event" and _is_ext_logged_in())

    # ★HDD保管中フラグ（テンプレ側で作成/追加UIをブロックするために渡す）
    try:
        storage, _ = _get_album_storage(album_id)  # 'ssd' or 'hdd'
    except Exception:
        storage = None
    is_readonly = (storage == 'hdd')

    # SSD/HDD のどちらか実体がある方を優先
    child_path = _prefer_existing_child_dir(album_id, child_id, mode)
    os.makedirs(child_path, exist_ok=True)

    # 加工ロック（静止画側で使用）
    lock_path = os.path.join(child_path, 'lock.json')
    lock = None
    lock_expired = False
    if os.path.exists(lock_path):
        try:
            with open(lock_path, 'r') as f:
                lock = json.load(f)
            lock_expired = (time.time() - lock.get('timestamp', 0)) > LOCK_TTL_SEC
        except Exception:
            lock = None

    username = session.get("user")
    is_admin = (username == "admin")
    is_owner = (meta.get("owner") == username)

    # 削除ハンドリング（管理者/所有者のみ）
    if request.method == 'POST' and (is_admin or is_owner):
        to_delete = request.form.getlist('delete')
        deleted = 0
        for filename in to_delete:
            file_path = _open_path_anyroot(album_id, child_id, filename, mode='normal')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted += 1
                except Exception as e:
                    current_app.logger.warning("削除失敗 %s: %s", file_path, e)
        flash(f"{deleted} 件のファイルを削除しました", "success")
        return redirect(request.url)

    # 🔽 resolve_thumb_url を大量に呼んでも DB を叩かないように、ルート一覧を一度だけ取得して使い回すローカル関数
    def _thumb_with_cached_roots(_album_id: str, _child_id: str, original_filename: str, _roots: tuple) -> str:
        base, _ = os.path.splitext(original_filename)
        for cand in (f"{base}.webp", f"{base}.jpg", f"{base}.jpeg"):
            for r in _roots:
                p = os.path.join(r, _album_id, _child_id, 'thumbs', cand)
                if os.path.isfile(p):
                    return url_for('album.album_thumb', album_id=_album_id, child_id=_child_id, filename=cand)
        # サムネが無いときは元画像
        return url_for('album.image', album_id=_album_id, child_id=_child_id, filename=original_filename)

    videos = []
    files = []

    if mode == "process":
        # 加工専用: latest.* を両ルートから探索 → 一番新しい1枚を表示
        candidates = []
        roots = _get_roots_for_album(album_id)  # ← 1回だけ取得して使い回す
        for root in roots:
            p = os.path.join(root, album_id, child_id)
            if not os.path.isdir(p):
                continue
            for f in os.listdir(p):
                if f.startswith("latest.") and allowed_file(f):
                    candidates.append((p, f))
        if candidates:
            candidates.sort(key=lambda t: os.path.getmtime(os.path.join(t[0], t[1])), reverse=True)
            f = candidates[0][1]
            files = [{
                "name": f,
                "thumb": url_for('album.image', album_id=album_id, child_id=child_id, filename=f)
            }]

    elif mode == "movie":
        # ---- 動画一覧：encoded/*.web.mp4 優先、未生成は converting=True ----
        fname_re = re.compile(
            rf"^{re.escape(child_id)}_(\d{{8}})_(\d{{4}})_(\d{{4}})\.[A-Za-z0-9]+$"
        )

        def sort_key(name: str, directory: str):
            full = os.path.join(directory, name)
            m = fname_re.match(name)
            if m:
                yyyymmdd, hhmm, seq = m.groups()
                try:
                    dt = datetime.strptime(yyyymmdd + hhmm, "%Y%m%d%H%M")
                except ValueError:
                    dt = datetime.fromtimestamp(os.path.getmtime(full))
                return (dt, int(seq))
            return (datetime.fromtimestamp(os.path.getmtime(full)), 99999)

        # original/ と encoded/ を使用
        orig_dir = os.path.join(child_path, 'original')
        enc_dir  = os.path.join(child_path, 'encoded')
        os.makedirs(orig_dir, exist_ok=True)
        os.makedirs(enc_dir, exist_ok=True)

        # 原本一覧（encoded は参照のみ）
        raw_files = []
        for f in os.listdir(orig_dir):
            full = os.path.join(orig_dir, f)
            if not os.path.isfile(full):
                continue
            if f.endswith(".web.mp4"):
                continue
            if allowed_movie(f):
                raw_files.append(f)

        raw_files.sort(key=lambda n: sort_key(n, orig_dir))

        for f in raw_files:
            base, _ = os.path.splitext(f)
            web_name = base + ".web.mp4"
            web_path = os.path.join(enc_dir, web_name)
            web_exists = os.path.exists(web_path)

            # poster は encoded/ → original/ → 無し の順で探す
            poster = base + ".poster.jpg"
            poster_url = None
            for d in (enc_dir, orig_dir):
                if os.path.exists(os.path.join(d, poster)):
                    poster_url = url_for('album.movie_poster', album_id=album_id, child_id=child_id, filename=poster)
                    break

            # 再生URLは、変換済みがあればそれ（encoded/）、無ければ原本（original/）
            use_name = web_name if web_exists else f

            videos.append({
                "name": f,
                "poster": poster_url,
                "raw": url_for('album.movie_raw', album_id=album_id, child_id=child_id, filename=use_name),
                "web": url_for('album.movie_raw', album_id=album_id, child_id=child_id, filename=web_name),
                "converting": (not web_exists)
            })

        return render_template(
            "view_child_movie.html",
            album_id=album_id,
            child_id=child_id,
            meta=meta,
            videos=videos,
            mode=mode,
            session=session,
            is_admin=is_admin,
            is_owner=is_owner,
            # ★追加：HDD保管中フラグをテンプレへ渡す
            is_readonly=is_readonly,
            show_extlogin_nav=show_extlogin_nav,
        )

    else:
        # 通常モード（静止画）: 両ルートをマージ
        seen = set()
        merged = []
        roots = _get_roots_for_album(album_id)  # ← 1回だけ取得して使い回す
        for root in roots:
            p = os.path.join(root, album_id, child_id)
            if not os.path.isdir(p):
                continue
            for f in os.listdir(p):
                full = os.path.join(p, f)
                if not os.path.isfile(full):
                    continue
                if not allowed_file(f) or f.startswith("latest."):
                    continue
                if f in seen:
                    continue
                seen.add(f)
                # resolve_thumb_url を大量呼び出ししない（roots を使い回す）
                merged.append({"name": f, "thumb": _thumb_with_cached_roots(album_id, child_id, f, roots)})
        files = sorted(merged, key=lambda x: x["name"])

    zip_token = session.pop('zip_token', None)

    # 加工履歴（静止画用）— どちらか存在する側から読む
    history_list = []
    history_path = os.path.join(child_path, "history.json")
    if os.path.exists(history_path):
        try:
            with open(history_path, 'r') as f:
                history_list = json.load(f)
        except Exception as e:
            current_app.logger.warning("加工履歴読み込みエラー: %s", e)

    # 画像ビュー（通常/加工）
    event_meta = _fetch_album_meta(album_id)
    is_event_album = bool(event_meta and event_meta.get("access_mode") == "event")
    is_event_login = bool(_is_ext_logged_in() and is_event_album)
    event_process_members = []
    current_ext_user_id = None
    current_user_process_status = None
    if is_event_album and event_meta and event_meta.get("event_id"):
        event_process_members = _fetch_event_process_members(int(event_meta["event_id"]))
        status_map = _fetch_album_process_status_map(album_id, child_id)
        for member in event_process_members:
            ext_user_id = int(member.get("user_id"))
            status = status_map.get(ext_user_id, {})
            member["request_flag"] = int(status.get("request_flag", 0))
            member["complete_flag"] = int(status.get("complete_flag", 0))
        if is_event_login:
            current_ext_user_id = session.get("ext_user_id")
            if not current_ext_user_id:
                ext_social_id = session.get("ext_user_social_id")
                if ext_social_id:
                    ext_user = _get_ext_user_by_social(ext_social_id)
                    if ext_user:
                        current_ext_user_id = ext_user.get("id")
        try:
            current_ext_user_id = int(current_ext_user_id) if current_ext_user_id else None
        except (TypeError, ValueError):
            current_ext_user_id = None
        current_user_process_status = status_map.get(current_ext_user_id) if current_ext_user_id else None
    if mode == "process" and is_event_album:
        template_name = "view_child_process_event.html"
    else:
        template_name = "view_child_process.html" if mode == "process" else "view_child.html"
    return render_template(
        template_name,
        album_id=album_id,
        child_id=child_id,
        files=files,
        videos=videos,
        image_count=len(files),
        meta=meta,
        mode=mode,
        lock=lock,
        lock_expired=lock_expired,
        zip_token=zip_token,
        is_admin=is_admin,
        is_owner=is_owner,
        session=session,
        history_list=history_list,
        # ★追加：HDD保管中フラグをテンプレへ渡す
        is_readonly=is_readonly,
        ext_user_nickname=_get_ext_user_nickname(),
        is_event_login=is_event_login,
        event_process_members=event_process_members,
        current_ext_user_id=current_ext_user_id,
        current_user_process_status=current_user_process_status,
        show_extlogin_nav=show_extlogin_nav,
    )

# =============================================================================
# サーブ（画像/動画/ポスター/ダウンロード）
# =============================================================================
@album_bp.route('/<album_id>/thumb/<child_id>/<filename>')
def album_thumb(album_id, child_id, filename):
    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))
    # 両ルートを探索して実体から返す
    for root in _get_roots_for_album(album_id):
        p = os.path.join(root, album_id, child_id, 'thumbs', filename)
        if os.path.isfile(p):
            return send_file(p, conditional=True)
    abort(404)

@album_bp.route('/<album_id>/<child_id>/process_status', methods=['POST'])
def update_process_status(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return jsonify({"ok": False, "error": "album_not_found"}), 404

    is_authed = _has_album_auth(album_id)
    if not (is_authed or session.get('user') == 'admin' or _is_ext_logged_in()):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    try:
        ext_user_id = int(data.get("ext_user_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid ext_user_id"}), 400

    request_flag = 1 if data.get("request_flag") else 0
    complete_flag = 1 if data.get("complete_flag") else 0
    prev_complete_flag = None
    requester_id = None
    if _is_ext_logged_in():
        requester_id = session.get("ext_user_id")
        if not requester_id:
            ext_social_id = session.get("ext_user_social_id")
            if ext_social_id:
                ext_user = _get_ext_user_by_social(ext_social_id)
                if ext_user:
                    requester_id = ext_user.get("id")
    if requester_id is not None:
        try:
            requester_id = int(requester_id)
        except (TypeError, ValueError):
            requester_id = None

    event_meta = _fetch_album_meta(album_id)
    if not event_meta or not event_meta.get("event_id"):
        return jsonify({"ok": False, "error": "event_not_found"}), 404

    # イベント参加者かどうかチェック
    row = db_get_one(
        "SELECT id FROM mfu_event_member WHERE event_id=%s AND user_id=%s",
        (int(event_meta["event_id"]), ext_user_id),
    )
    if not row:
        return jsonify({"ok": False, "error": "member_not_found"}), 404

    _ensure_album_process_table()
    try:
        prev_row = db_get_one(
            """
            SELECT complete_flag
              FROM album_process
             WHERE ext_user_id=%s AND album_id=%s AND child_id=%s
            """,
            (ext_user_id, album_id, child_id),
        )
        if prev_row:
            prev_complete_flag = int(prev_row.get("complete_flag", 0))
    except Exception:
        prev_complete_flag = None
    db_exec(
        """
        INSERT INTO album_process (ext_user_id, album_id, child_id, request_by, request_flag, complete_flag)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          request_flag=VALUES(request_flag),
          complete_flag=VALUES(complete_flag),
          request_by=IF(VALUES(request_flag)=1, VALUES(request_by), request_by)
        """,
        (ext_user_id, album_id, child_id, requester_id, request_flag, complete_flag),
    )
    if complete_flag == 1:
        child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
        mode = child_meta.get("mode", "normal") if child_meta else "normal"
        child_path = _prefer_existing_child_dir(album_id, child_id, mode)
        lock_path = os.path.join(child_path, 'lock.json')
        lock_user = None
        try:
            if os.path.exists(lock_path):
                with open(lock_path, 'r') as f:
                    lock_user = (json.load(f) or {}).get("user")
        except Exception:
            lock_user = None
        current_username = None
        if _is_ext_logged_in():
            current_username = _get_ext_user_nickname()
        if not current_username:
            current_username = session.get("user")
        if lock_user and current_username and lock_user == current_username:
            try:
                release_lock_db(album_id, child_id, username=current_username, force=False)
            except Exception:
                pass
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass
    try:
        if request_flag == 1 and complete_flag == 1 and prev_complete_flag == 0:
            request_row = db_get_one(
                """
                SELECT request_by
                  FROM album_process
                 WHERE ext_user_id=%s AND album_id=%s AND child_id=%s
                """,
                (ext_user_id, album_id, child_id),
            )
            request_by_id = None
            if request_row:
                try:
                    request_by_id = int(request_row.get("request_by"))
                except (TypeError, ValueError):
                    request_by_id = None
            _notify_requester_process_completion(album_id, child_id, request_by_id, meta, event_meta)
    except Exception as e:
        current_app.logger.warning("process completion notify failed: %s", e)
    return jsonify({"ok": True})

@album_bp.route('/<album_id>/<child_id>/process_request', methods=['POST'])
def request_process(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return jsonify({"ok": False, "error": "album_not_found"}), 404

    is_authed = _has_album_auth(album_id)
    if not (is_authed or session.get('user') == 'admin' or _is_ext_logged_in()):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    members = data.get("members") or []
    if not isinstance(members, list):
        return jsonify({"ok": False, "error": "invalid_members"}), 400

    event_meta = _fetch_album_meta(album_id)
    if not event_meta or not event_meta.get("event_id"):
        return jsonify({"ok": False, "error": "event_not_found"}), 404

    event_id = int(event_meta["event_id"])
    event_members = _fetch_event_process_members(event_id)
    valid_user_ids = {int(m.get("user_id")) for m in event_members}

    _ensure_album_process_table()

    requester_id = None
    if _is_ext_logged_in():
        requester_id = session.get("ext_user_id")
        if not requester_id:
            ext_social_id = session.get("ext_user_social_id")
            if ext_social_id:
                ext_user = _get_ext_user_by_social(ext_social_id)
                if ext_user:
                    requester_id = ext_user.get("id")
    if requester_id is not None:
        try:
            requester_id = int(requester_id)
        except (TypeError, ValueError):
            requester_id = None

    request_targets: list[int] = []
    for m in members:
        try:
            ext_user_id = int(m.get("ext_user_id"))
        except (TypeError, ValueError):
            continue
        if ext_user_id not in valid_user_ids:
            continue
        request_flag = 1 if m.get("request_flag") else 0
        complete_flag = 1 if m.get("complete_flag") else 0
        db_exec(
            """
            INSERT INTO album_process (ext_user_id, album_id, child_id, request_by, request_flag, complete_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              request_flag=VALUES(request_flag),
              complete_flag=VALUES(complete_flag),
              request_by=IF(VALUES(request_flag)=1, VALUES(request_by), request_by)
            """,
            (ext_user_id, album_id, child_id, requester_id, request_flag, complete_flag),
        )
        if request_flag == 1 and complete_flag == 0:
            request_targets.append(ext_user_id)

    sent_count = 0
    if request_targets:
        contacts = _fetch_event_notification_contacts(event_id, request_targets)
        album_name = meta.get("album_name", "アルバム")
        child_name = next((c.get("name") for c in meta.get("children", []) if c.get("folder") == child_id), child_id)
        link = _build_event_album_link(event_id, album_id, child_id)
        subject = f"【加工依頼】{album_name}"
        body = (
            f"{album_name} の「{child_name}」について加工のご協力をお願いします。\n\n"
            f"アクセスはこちら:\n{link}\n\n"
            "このメールはイベント参加者（承認済み）のみへ自動通知しています。"
        )

        try:
            from app.utils.mail import send_mail
        except Exception:
            try:
                from app.mail import send_mail
            except Exception:
                current_app.logger.warning("notify: send_mail import failed")
                send_mail = None

        if send_mail:
            recipients = [
                c.get("email") for c in contacts
                if c.get("email") and int(c.get("notify_album_process", 1)) == 1
            ]
            request_by_email = None
            if requester_id:
                requester = db_get_one("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (requester_id,))
                request_by_email = (requester or {}).get("email")
            current_app.logger.info(
                "notify: pre_send kind=process_request album_id=%s child_id=%s request_by=%s recipients_count=%s recipients=%s sql_condition=%s",
                album_id,
                child_id,
                request_by_email,
                len(recipients),
                recipients,
                "request_flag=1 AND complete_flag=0",
            )
            for c in contacts:
                if not c.get("email"):
                    continue
                if int(c.get("notify_album_process", 1)) != 1:
                    continue
                try:
                    send_mail(c["email"], subject, body)
                    sent_count += 1
                except Exception as e:
                    current_app.logger.warning("process request mail failed to %s: %s", c.get("email"), e)

    return jsonify({"ok": True, "sent": sent_count})

@album_bp.route('/<album_id>/image/<child_id>/<filename>')
def image(album_id, child_id, filename):
    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))
    abs_path = _open_path_anyroot(album_id, child_id, filename, mode='normal')
    if not abs_path:
        abort(404)
    return send_file(abs_path, conditional=True)

# 置き換え：動画本体
@album_bp.route('/<album_id>/movie/raw/<child_id>/<path:filename>')
def movie_raw(album_id, child_id, filename):
    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))
    abs_path = _movie_find_abs(album_id, child_id, filename)
    if not abs_path:
        abort(404)
    return send_file(abs_path, conditional=True)

# 置き換え：ポスター
@album_bp.route('/<album_id>/movie/poster/<child_id>/<path:filename>')
def movie_poster(album_id, child_id, filename):
    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))
    abs_path = _movie_find_abs(album_id, child_id, filename)
    if not abs_path:
        abort(404)
    return send_file(abs_path, conditional=True)

# 置き換え：ダウンロード
@album_bp.route('/<album_id>/movie/download/<child_id>/<path:filename>')
def movie_download(album_id, child_id, filename):
    if not _has_album_auth(album_id) and session.get('user') != 'admin':
        return redirect(url_for('album.album_access', album_id=album_id))
    abs_path = _movie_find_abs(album_id, child_id, filename)
    if not abs_path:
        abort(404)
    return send_file(abs_path, as_attachment=True,
                     download_name=os.path.basename(abs_path),
                     conditional=True)

# =============================================================================
# アルバム作成・管理・削除
# =============================================================================
@album_bp.route('/create_album', methods=['GET', 'POST'])
def create_album():
    return redirect(url_for('album.admin_create_album'))

@album_bp.route('/<album_id>/delete_album', methods=['POST'])
def delete_album(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    if session.get('user') != 'admin' and session.get('user') != meta.get('owner'):
        return '削除権限がありません', 403

    # 写真はSSD/HDDのどちらにも存在し得る。動画は固定。
    for root in (SSD_ROOT, HDD_ROOT, MOVIE_ROOT):
        album_path = os.path.join(root, album_id)
        if os.path.exists(album_path):
            shutil.rmtree(album_path, ignore_errors=True)

    delete_album_row(album_id)
    flash('親アルバムを削除しました', 'success')

    if session.get('user') == 'admin':
        return redirect(url_for('album.admin_create_album'))
    else:
        return redirect(url_for('album.create_album'))

@album_bp.route('/admin_create_album/', methods=['GET', 'POST'])
def admin_create_album():
    if not session.get('user'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        album_name = request.form['album_name']
        album_id = str(uuid.uuid4())
        owner = session.get('user')

        # 新規はSSDに作成し、DBへ保存先登録
        os.makedirs(os.path.join(SSD_ROOT, album_id), exist_ok=True)
        _set_album_storage(album_id, 'ssd')

        # 動画側の親フォルダも用意（空でOK）
        os.makedirs(os.path.join(MOVIE_ROOT, album_id), exist_ok=True)

        access_token = create_album_row(album_id, album_name, owner)

        return render_template(
            'admin_created.html',
            album_id=album_id,
            password="",
            domain='mfu.iori0624.jp',
            access_url=url_for('album.album_access', album_id=album_id, _external=True) + f'?token={access_token}'
        )

    user = session.get('user')
    rows = list_albums_for_admin() if user == 'admin' else list_albums_for_user(user)

    album_list = []
    for r in rows:
        album_list.append({
            "id": r["id"],
            "name": r["name"],
            "owner": r["owner"],
            "password": "(未設定)",
            "access_token": r.get("access_token", "")
        })

    album_list.sort(key=lambda x: x['name'])
    return render_template('admin_create_album.html', album_list=album_list)

# =============================================================================
# 子アルバム削除
# =============================================================================
@album_bp.route('/<album_id>/delete_child/<child_id>', methods=['POST'])
def delete_child(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    if user != 'admin' and user != meta.get('owner'):
        return '削除権限がありません', 403

    try:
        release_lock_db(album_id, child_id, username=None, force=True)
    except Exception:
        pass

    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"

    child_path = storage_child_dir(album_id, child_id, mode)
    if os.path.exists(child_path):
        try:
            shutil.rmtree(child_path)
        except Exception as e:
            flash(f'子アルバムの削除に失敗しました: {e}', 'danger')
            return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

    delete_child_row(album_id, child_id)
    flash('子アルバムを削除しました', 'success')
    return redirect(url_for('album.album_home', album_id=album_id))

# =============================================================================
# 加工用ダウンロード／ロック解除
# =============================================================================
@album_bp.route('/download_latest/<album_id>/<child_id>', methods=['POST'])
def download_latest(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    child_meta = next((c for c in meta["children"] if c["folder"] == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"
    if mode != "process":
        return "このアルバムは加工専用モードではありません", 400

    # 両ルートから最新画像を探索
    latest = None
    latest_mtime = -1
    latest_path = None
    for root in _get_roots_for_album(album_id):
        p = os.path.join(root, album_id, child_id)
        if not os.path.isdir(p):
            continue
        # latest.* 優先
        for f in os.listdir(p):
            if f.startswith('latest.') and allowed_file(f):
                fp = os.path.join(p, f)
                mt = os.path.getmtime(fp)
                if mt > latest_mtime:
                    latest, latest_mtime, latest_path = f, mt, fp
        # 無ければ通常画像の最新も候補に（上書きしない：latest.* を優先）
        if not latest:
            pics = [f for f in os.listdir(p) if allowed_file(f)]
            for f in pics:
                fp = os.path.join(p, f)
                mt = os.path.getmtime(fp)
                if mt > latest_mtime:
                    latest, latest_mtime, latest_path = f, mt, fp

    if not latest_path:
        return "最新画像が存在しません", 404

    username = (request.form.get("username") or "").strip()
    if not username:
        return "加工者名は必須です。", 400

    ok, msg = try_acquire_lock_db(album_id, child_id, username, ttl_sec=LOCK_TTL_SEC)
    if not ok:
        return msg, 409

    # lock.json は表示側の child_path（片側）に作る
    lock_path = os.path.join(_prefer_existing_child_dir(album_id, child_id, mode), 'lock.json')
    try:
        with open(lock_path, 'w') as f:
            json.dump({"user": username, "timestamp": int(time.time())}, f, ensure_ascii=False)
    except Exception:
        pass

    return send_file(latest_path, as_attachment=True, download_name=latest)

@album_bp.route('/<album_id>/unlock/<child_id>', methods=['POST'])
def unlock_any(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    is_admin = (user == 'admin')
    is_owner = (user == meta.get('owner'))
    is_authed = _has_album_auth(album_id)
    if not (is_admin or is_owner or is_authed):
        return redirect(url_for('album.album_access', album_id=album_id))

    try:
        release_lock_db(album_id, child_id, username=user or None, force=False)
    except Exception:
        pass

    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"
    child_path = _prefer_existing_child_dir(album_id, child_id, mode)

    lock_path = os.path.join(child_path, 'lock.json')
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
            flash('🔓 ロックを解除しました', 'success')
        except Exception as e:
            flash(f'解除に失敗しました: {e}', 'danger')
    else:
        flash('ロックは存在しません', 'info')

    return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

@album_bp.route('/<album_id>/force_unlock/<child_id>', methods=['POST'])
def force_unlock(album_id, child_id):
    if session.get('user') != 'admin':
        return abort(403)

    try:
        release_lock_db(album_id, child_id, username=None, force=True)
    except Exception:
        pass

    meta = load_meta(album_id) or {}
    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"
    child_path = _prefer_existing_child_dir(album_id, child_id, mode)

    lock_path = os.path.join(child_path, 'lock.json')
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
            flash('🔓 管理者がロックを強制解除しました', 'success')
        except Exception as e:
            flash(f'解除に失敗しました: {e}', 'danger')
    else:
        flash('ロックは存在しません', 'info')
    return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

# =============================================================================
# 親/子アルバム名の変更
# =============================================================================
@album_bp.route('/<album_id>/edit_name', methods=['GET'])
def edit_album_name_form(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    if not (user == 'admin' or user == meta.get('owner')):
        return '編集権限がありません', 403

    return render_template_string("""
<!doctype html>
<title>アルバム名の変更</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<div style="max-width:560px;margin:40px auto;font-family:sans-serif">
  <h1 style="font-size:20px;margin-bottom:12px;">アルバム名の変更</h1>
  <form method="post" action="{{ url_for('album.rename_album', album_id=album_id) }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="text" name="album_name" value="{{ current_name }}" required
           style="width:100%;padding:10px;margin:8px 0;border:1px solid #ccc;border-radius:6px;">
    <button type="submit" style="padding:10px 14px;border:0;border-radius:6px;background:#111;color:#fff;">保存</button>
    <a href="{{ url_for('album.album_home', album_id=album_id) }}" style="margin-left:8px">戻る</a>
  </form>
</div>
    """, album_id=album_id, current_name=meta.get("album_name", ""))

@album_bp.route('/<album_id>/rename', methods=['POST'])
def rename_album(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    if not (user == 'admin' or user == meta.get('owner')):
        return '編集権限がありません', 403

    new_name = (request.form.get('album_name') or '').strip()
    if not new_name:
        flash('アルバム名を入力してください', 'warning')
        return redirect(url_for('album.admin_create_album'))

    try:
        db_exec("UPDATE albums SET album_name=%s WHERE id=%s", (new_name, album_id))
        flash('アルバム名を更新しました', 'success')
    except Exception as e:
        flash(f'更新に失敗しました: {e}', 'danger')

    return redirect(url_for('album.admin_create_album'))

@album_bp.route('/<album_id>/rename_child/<child_id>', methods=['POST'])
def rename_child(album_id, child_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    user = session.get('user')
    if not (user == 'admin' or user == meta.get('owner')):
        return '編集権限がありません', 403

    new_name = (request.form.get('child_name') or '').strip()
    if not new_name:
        flash('子アルバム名は必須です', 'warning')
        return redirect(url_for('album.album_home', album_id=album_id))

    try:
        db_exec("UPDATE album_children SET name=%s WHERE album_id=%s AND folder=%s",
                (new_name, album_id, child_id))
        flash('子アルバム名を更新しました', 'success')
    except Exception as e:
        flash(f'更新に失敗しました: {e}', 'danger')

    return redirect(url_for('album.album_home', album_id=album_id))

# --- ここから追記: 加工開始(JSON) + 最新画像探索ヘルパ -------------------------
def find_latest_filename(album_id: str, child_id: str) -> str | None:
    """processモード用: latest.* があればそれを返し、無ければ child 内で最も新しい静止画を返す（両ルート探索）。"""
    meta = load_meta(album_id) or {}
    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"

    latest = None
    latest_mtime = -1
    for root in _get_roots_for_album(album_id):
        child_path = os.path.join(root, album_id, child_id)
        if not os.path.isdir(child_path):
            continue

        # latest.* を優先
        for f in os.listdir(child_path):
            if f.startswith("latest.") and allowed_file(f):
                p = os.path.join(child_path, f)
                mt = os.path.getmtime(p)
                if mt > latest_mtime:
                    latest, latest_mtime = f, mt

        # 無ければ通常画像の最新
        if not latest:
            pics = [f for f in os.listdir(child_path)
                    if os.path.isfile(os.path.join(child_path, f)) and allowed_file(f)]
            if pics:
                pics.sort(key=lambda n: os.path.getmtime(os.path.join(child_path, n)), reverse=True)
                if pics:
                    p = os.path.join(child_path, pics[0])
                    mt = os.path.getmtime(p)
                    if mt > latest_mtime:
                        latest, latest_mtime = pics[0], mt

    return latest

@album_bp.route('/<album_id>/<child_id>/begin_process', methods=['POST'])
def begin_process(album_id, child_id):
    """
    加工ロックを取得し、最新画像の絶対URLをJSONで返す。
    - 成功: { ok: true, image_url: "https://.../image/...", message: "..." }
    - ロック中: 409 + テキスト
    - 画像なし: 404
    """
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404
    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"
    if mode != "process":
        return "このアルバムは加工専用モードではありません", 400

    username = (request.form.get("username") or "").strip()
    if not username:
        ext_social_id = session.get("ext_user_social_id")
        if ext_social_id:
            ext_user = _get_ext_user_by_social(ext_social_id)
            if ext_user:
                username = (ext_user.get("nickname") or "").strip()
                if not username:
                    username = "（未設定）"
        if not username:
            username = (session.get("user") or "").strip()
    if not username:
        username = "不明"

    latest_filename = find_latest_filename(album_id, child_id)
    if not latest_filename:
        return "最新画像が存在しません", 404

    ok, msg = try_acquire_lock_db(album_id, child_id, username, ttl_sec=LOCK_TTL_SEC)
    if not ok:
        return msg, 409

    lock_path = os.path.join(_prefer_existing_child_dir(album_id, child_id, mode), 'lock.json')
    try:
        with open(lock_path, 'w') as f:
            json.dump({"user": username, "timestamp": int(time.time())}, f, ensure_ascii=False)
    except Exception:
        pass

    img_url = url_for(
        "album.image",
        album_id=album_id,
        child_id=child_id,
        filename=latest_filename,
        _external=True
    )
    return jsonify({
        "ok": True,
        "image_url": img_url,
        "message": f"{username} さんとして加工ロックを開始しました（30分）"
    })
# --- 追記 ここまで --------------------------------------------------------------

# =============================================================================
# 写真保存先の切替 UI / API（アルバム単位でSSD⇄HDDを物理移動：安全版）
# =============================================================================
def _require_admin() -> Response | None:
    if session.get("user") != "admin":
        return Response("管理者のみ利用可能です", 403)
    return None

def _count_files_and_bytes(root_dir: str) -> tuple[int, int]:
    files = 0
    total = 0
    for base, dirs, fnames in os.walk(root_dir):
        for name in fnames:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
                files += 1
                total += st.st_size
            except FileNotFoundError:
                pass
    return files, total

def _copy_tree(src: str, dst_tmp: str):
    for base, dirs, fnames in os.walk(src):
        rel = os.path.relpath(base, src)
        target_base = os.path.join(dst_tmp, rel) if rel != "." else dst_tmp
        os.makedirs(target_base, exist_ok=True)
        # 元ディレクトリのモードをおおまかに継承
        try:
            st = os.stat(base)
            os.chmod(target_base, st.st_mode & 0o777)
        except Exception:
            pass
        for name in fnames:
            sp = os.path.join(base, name)
            dp = os.path.join(target_base, name)
            shutil.copy2(sp, dp)

def _move_album_physical_photos_safe(album_id: str, dest_storage: str):
    """
    安全な写真移動：コピー→検証→原子的切替→旧データを backup_* に退避。DBは切替成功後に更新。
    """
    cur_storage, cur_root = _get_album_storage(album_id)
    dst_root = SSD_ROOT if dest_storage == "ssd" else HDD_ROOT
    if cur_root == dst_root:
        return False, "すでに目的地にあります"

    src_dir = os.path.join(cur_root, album_id)
    if not os.path.isdir(src_dir):
        return False, "移動元が存在しません"

    os.makedirs(dst_root, exist_ok=True)
    final_dst = os.path.join(dst_root, album_id)
    if os.path.exists(final_dst):
        return False, "移動先に同名ディレクトリが既に存在します"

    # 一時宛先
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst_tmp = os.path.join(dst_root, f"{album_id}.moving_{ts}")
    if os.path.exists(dst_tmp):
        shutil.rmtree(dst_tmp, ignore_errors=True)
    os.makedirs(dst_tmp, exist_ok=True)

    try:
        # コピー
        src_count, src_bytes = _count_files_and_bytes(src_dir)
        _copy_tree(src_dir, dst_tmp)
        dst_count, dst_bytes = _count_files_and_bytes(dst_tmp)

        # 検証
        if src_count != dst_count or src_bytes != dst_bytes:
            raise RuntimeError(f"検証NG: files {src_count}->{dst_count}, bytes {src_bytes}->{dst_bytes}")

        # 原子的切替（宛先側）
        os.rename(dst_tmp, final_dst)

        # 旧データは backup_* へ退避（元側）
        backup_name = f"{album_id}.backup_{ts}"
        backup_path = os.path.join(cur_root, backup_name)
        os.rename(src_dir, backup_path)

        # DB更新（ここで初めて切替）
        _set_album_storage(album_id, dest_storage)

        msg = f"切替完了。旧データは {backup_path} にバックアップとして残しました。確認後に手動削除してください。"
        return True, msg

    except Exception as e:
        try:
            if os.path.isdir(dst_tmp):
                shutil.rmtree(dst_tmp, ignore_errors=True)
        except Exception:
            pass
        return False, f"移動失敗: {e}"

@album_bp.route("/admin/storage", methods=["GET"])
def admin_storage_page():
    csrf_token = session.get('csrf_token') or secrets.token_urlsafe(32)
    session['csrf_token'] = csrf_token
    resp = _require_admin()
    if resp:
        return resp

    rows = []
    for r in list_albums_for_admin():
        aid = r["id"]
        meta = load_meta(aid)
        if not meta:
            continue
        storage, _root = _get_album_storage(aid)
        rows.append((aid, meta.get("album_name", "（無名）"), storage))
    rows.sort(key=lambda x: x[1])

    html = [
        f"<!doctype html><meta charset='utf-8'><title>アルバム保存先 管理</title><meta name='csrf-token' content='{csrf_token}'>",
        "<style>body{font-family:sans-serif} table{border-collapse:collapse} th,td{border:1px solid #ccc;padding:6px} code{background:#f6f6f6;padding:2px 4px;border-radius:4px} .row{margin:6px 0} .bar{width:260px}</style>",
        "<h1>アルバム保存先 管理（SSD ⇄ HDD）</h1>",
        "<p>「安全に移動（進行表示）」は <b>コピー→検証→原子的切替</b> を非同期で実行し、下のプログレスバーに進捗を表示します。完了後、元側に <code>*.backup_YYYYmmdd_HHMMSS</code> を残します。</p>",
        "<table><tr><th>アルバム名</th><th>album_id</th><th>現在</th><th>操作</th><th>進捗</th></tr>",
    ]
    for aid, name, storage in rows:
        # 従来の同期ボタン（残しておく）
        btn_to_hdd_sync = f"""
          <form method="post" action="{url_for('album.admin_move_storage')}" style="display:inline">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="album_id" value="{aid}">
            <input type="hidden" name="dest" value="hdd">
            <button type="submit">HDDへ移動（同期）</button>
          </form>"""
        btn_to_ssd_sync = f"""
          <form method="post" action="{url_for('album.admin_move_storage')}" style="display:inline">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="hidden" name="album_id" value="{aid}">
            <input type="hidden" name="dest" value="ssd">
            <button type="submit">SSDへ戻す（同期）</button>
          </form>"""

        # 非同期ボタン（進捗表示）
        btn_async = f"""
          <div class="row">
            <button onclick="startMove('{aid}','hdd')">HDDへ移動（安全に移動・進行表示）</button>
            <button onclick="startMove('{aid}','ssd')">SSDへ戻す（安全に移動・進行表示）</button>
          </div>
        """

        # 進捗UI（progress + メッセージ）
        prog_ui = f"""
          <div id="prog-{aid}">
            <progress class="bar" id="bar-{aid}" value="0" max="100"></progress>
            <span id="txt-{aid}">待機中</span>
          </div>
        """

        html.append(
            f"<tr><td>{name}</td><td><code>{aid}</code></td><td>{storage.upper()}</td>"
            f"<td>{btn_to_hdd_sync} {btn_to_ssd_sync}<br>{btn_async}</td>"
            f"<td>{prog_ui}</td></tr>"
        )
    html.append("</table>")

    # フロントJS：開始→ポーリング→完了
    html.append(f"""
<script>
async function startMove(album_id, dest){{
  const btns = document.querySelectorAll('button');
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  btns.forEach(b=>b.disabled=true);
  try {{
    const res = await fetch('{url_for('album.api_storage_move_async')}', {{
      method:'POST',
      headers:{{'Content-Type':'application/json','X-CSRFToken':csrfToken}},
      body: JSON.stringify({{album_id, dest}})
    }});
    const j = await res.json();
    if(!j.ok){{ alert('開始失敗: '+(j.error||res.status)); btns.forEach(b=>b.disabled=false); return; }}
    pollProgress(album_id);
  }} catch(e){{
    alert('開始エラー: '+e);
    btns.forEach(b=>b.disabled=false);
  }}
}}

async function pollProgress(album_id){{
  const bar = document.getElementById('bar-'+album_id);
  const txt = document.getElementById('txt-'+album_id);
  let timer = null;

  async function once(){{
    try {{
      const res = await fetch('{url_for('album.api_storage_progress')}?album_id='+encodeURIComponent(album_id), {{cache:'no-store'}});
      const j = await res.json();
      if(!j.ok){{ throw new Error(j.error||res.status); }}
      const p = j.progress||{{}};
      const pct = Math.max(0, Math.min(100, p.percent||0));
      bar.value = pct;
      txt.textContent = (p.status||'') + ' ' + pct + '% ' + (p.message?(' - '+p.message):'');

      if(p.status==='done' || p.status==='error' || pct>=100){{
        // 完了 or エラー
        clearInterval(timer);
        timer = null;
        // 完了ならリロードして現在の保存先を反映
        if(p.status==='done') setTimeout(()=>location.reload(), 800);
        else {{
          // エラーはボタンを戻す
          const btns = document.querySelectorAll('button');
          btns.forEach(b=>b.disabled=false);
        }}
        return;
      }}
    }} catch(e){{
      console.warn('progress error', e);
    }}
  }}

  await once();
  timer = setInterval(once, 1000);
}}
</script>
    """)
    return "\n".join(html)

@album_bp.route("/api/storage/move_async", methods=["POST"])
def api_storage_move_async():
    # 管理者チェック
    resp = _require_admin()
    if resp:
        return resp

    data = request.get_json(silent=True) or {}
    album_id = (data.get("album_id") or "").strip()
    dest = (data.get("dest") or "").strip().lower()
    if not album_id or dest not in ("ssd", "hdd"):
        return jsonify({"ok": False, "error": "invalid album_id/dest"}), 400

    # 既存スレッド実行中ならそのまま
    th = _move_threads.get(album_id)
    if th and th.is_alive():
        prog = _move_progress_read(album_id)
        return jsonify({"ok": True, "already_running": True, "progress": prog}), 200

    # 進捗初期化
    _move_progress_write(album_id, {
        "status": "starting",
        "percent": 0,
        "message": "移動準備中…",
        "started_ts": time.time(),
    })

    def _worker():
        try:
            _move_album_physical_photos_safe_with_progress(album_id, dest)
        finally:
            # 終了したスレッドは辞書から掃除
            try:
                _move_threads.pop(album_id, None)
            except Exception:
                pass

    t = threading.Thread(target=_worker, name=f"move-{album_id}", daemon=True)
    _move_threads[album_id] = t
    t.start()
    return jsonify({"ok": True, "started": True}), 202

@album_bp.route("/api/storage/progress", methods=["GET"])
def api_storage_progress():
    resp = _require_admin()
    if resp:
        return resp
    album_id = (request.args.get("album_id") or "").strip()
    if not album_id:
        return jsonify({"ok": False, "error": "missing album_id"}), 400
    prog = _move_progress_read(album_id)
    if not prog:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "progress": prog})

@album_bp.route("/admin/storage/move", methods=["POST"])
def admin_move_storage():
    resp = _require_admin()
    if resp:
        return resp
    album_id = (request.form.get("album_id") or "").strip()
    dest = (request.form.get("dest") or "hdd").strip().lower()
    if not album_id or dest not in ("ssd", "hdd"):
        return "album_id/dest が不正です", 400

    ok, msg = _move_album_physical_photos_safe(album_id, dest)
    if not ok:
        return f"移動失敗：{msg}", 400
    flash(msg, "success")
    return redirect(url_for("album.admin_storage_page"))

@album_bp.route("/api/storage/status", methods=["GET"])
def api_storage_status():
    if session.get("user") != "admin":
        return jsonify({"ok": False, "error": "forbidden"}), 403
    album_id = (request.args.get("album_id") or "").strip()
    if not album_id:
        return jsonify({"ok": False, "error": "missing album_id"}), 400
    storage, root = _get_album_storage(album_id)
    exists = os.path.isdir(os.path.join(root, album_id))
    return jsonify({"ok": True, "album_id": album_id, "storage": storage, "root": root, "exists": exists})

@album_bp.route("/api/storage/move", methods=["POST"])
def api_storage_move():
    if session.get("user") != "admin":
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    album_id = (data.get("album_id") or "").strip()
    dest = (data.get("dest") or "hdd").strip().lower()
    if not album_id or dest not in ("ssd", "hdd"):
        return jsonify({"ok": False, "error": "invalid album_id/dest"}), 400
    ok, msg = _move_album_physical_photos_safe(album_id, dest)
    return jsonify({"ok": ok, "message": msg})

# =============================================================================
# 既存: トークン禁止（イベントモード）
# =============================================================================
@album_bp.before_request
def _deny_token_when_event_album():
    """/albums/* に ?token= が付いていたら、アルバムが event モードのときは無効化して入口へ誘導"""
    if request.blueprint != "album":
        return

    token = request.args.get("token")
    if not token:
        return

    album_id = None
    if request.view_args and "album_id" in request.view_args:
        album_id = request.view_args.get("album_id")
    else:
        for seg in request.path.split("/"):
            if len(seg) == 36 and "-" in seg:
                album_id = seg
                break

    if not album_id:
        return

    meta = _fetch_album_meta(album_id)
    if not meta:
        return

    if (meta.get("access_mode") == "event"):
        return redirect(url_for("album.album_access", album_id=album_id))

# ===== ここから追記：アルバム全体ZIP（管理者専用・日本語名対応） ==========================
import os, re, unicodedata, threading, time, json
from zipfile import ZipFile, ZIP_DEFLATED
from tempfile import NamedTemporaryFile
from flask import after_this_request, send_file, abort, request, jsonify

# --- 日本語/絵文字対応のZIP内パス用サニタイズ ----------------------------
_CTRL = re.compile(r"[\x00-\x1f\x7f]")  # 制御文字除去

def _sanitize_arcname(name: str) -> str:
    """
    ZIPアーカイブ内のディレクトリ/ファイル名として安全化。
    ・日本語/絵文字は保持（UTF-8）
    ・スラッシュ/バックスラッシュは全角スラッシュに置換
    ・.. を ‥ に置換（相対パス上がり対策）
    ・制御文字を除去、前後空白除去、NFC正規化
    ・先頭の / や \\ を剥がす（絶対パス防止）
    """
    if not isinstance(name, str):
        name = str(name or "")
    name = name.replace("\\", "／").replace("/", "／")
    name = name.replace("..", "‥")
    name = _CTRL.sub("", name).strip()
    name = unicodedata.normalize("NFC", name)
    while name.startswith(("/", "\\")):
        name = name[1:]
    return name or "名称未設定"

def _sanitize_file_component(basename: str) -> str:
    """ファイル名の末端コンポーネントを日本語保持で安全化"""
    return _sanitize_arcname(os.path.basename(basename or ""))

# ====== 同期版：親アルバム全体ZIP（管理者のみ） ===========================
@album_bp.route('/<album_id>/admin_zip_all', methods=['GET'])
def admin_zip_all(album_id):
    """
    親アルバム全体をZIP化して返す（管理者のみ）。
    構成:
      子アルバム名/静止画ファイル...
      子アルバム名/動画ファイル...
    ・静止画は album_storage（SSD/HDD）側の実体から
    ・動画は MOVIE_ROOT 側から
    """
    if session.get('user') != 'admin':
        return abort(403)

    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404

    album_name = meta.get("album_name") or "album"
    children = meta.get("children", [])

    # 各子アルバムの実パスを決定
    try:
        storage, photos_root = _get_album_storage(album_id)  # ('ssd' or 'hdd', root_path)
    except Exception:
        photos_root = _photos_root(album_id)

    # 一時ファイルに作ってから send_file（送信後に削除）
    tmp = NamedTemporaryFile(prefix=f"zip_{album_id}_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()

    def iter_static_files(child_folder: str):
        """静止画（通常/加工）— 現在の保存先から取り出し。"""
        base = os.path.join(photos_root, album_id, child_folder)
        if not os.path.isdir(base):
            return
        for fname in os.listdir(base):
            fp = os.path.join(base, fname)
            if not os.path.isfile(fp):
                continue
            if fname.startswith("latest.") and not allowed_file(fname):
                # latest.* でも画像以外ならスキップ
                continue
            if allowed_file(fname):
                yield fname, fp
        # thumbs/ や history/ はZIPに含めない（運用データのため）

    def iter_movie_files(child_folder: str):
        """動画（元＋互換 .web.mp4 とポスターがあればそれも）。"""
        base = os.path.join(MOVIE_ROOT, album_id, child_folder)
        if not os.path.isdir(base):
            return
        for fname in os.listdir(base):
            fp = os.path.join(base, fname)
            if not os.path.isfile(fp):
                continue
            # 元動画・互換mp4・ポスターjpgを許容
            if allowed_movie(fname) or fname.endswith(".web.mp4") or fname.endswith(".poster.jpg"):
                yield fname, fp

    # ZIP 作成（日本語名そのまま格納）
    with ZipFile(tmp_path, mode='w', compression=ZIP_DEFLATED) as zf:
        for child in children:
            child_disp = child.get("name") or child.get("folder") or "child"
            child_name = _sanitize_arcname(child_disp)
            child_folder = child.get("folder")

            # 静止画
            for fname, abs_path in iter_static_files(child_folder):
                arcname = f"{child_name}/{_sanitize_file_component(fname)}"
                zf.write(abs_path, arcname)

            # 動画
            for fname, abs_path in iter_movie_files(child_folder):
                arcname = f"{child_name}/{_sanitize_file_component(fname)}"
                zf.write(abs_path, arcname)

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return response

    dl_name = f"{_sanitize_arcname(album_name)}.zip"
    return send_file(tmp_path, as_attachment=True, download_name=dl_name,
                     mimetype="application/zip", conditional=True)

# ===== 非同期版：全体ZIP（管理者・進捗付） ================================
# メモリ上の簡易ジョブ管理（プロセス再起動で消えます）
_ZIP_JOBS = {}  # key: (album_id, key)  value: dict(status, percent,..., tmp_path)
_ZIP_LOCK = threading.Lock()

def _gather_files_for_album(album_id: str):
    """ZIPに含めるファイルを列挙: [(arcname, abs_path), ...]（日本語対応）"""
    meta = load_meta(album_id)
    if not meta:
        return [], "アルバムが存在しません"
    album_name = meta.get("album_name") or "album"
    children = meta.get("children", [])

    # 写真の実体root（SSD/HDD）
    try:
        _, photos_root = _get_album_storage(album_id)
    except Exception:
        photos_root = _photos_root(album_id)

    entries = []
    for child in children:
        # 子ディレクトリ名は表示名優先。日本語保持で安全化
        child_disp = child.get("name") or child.get("folder") or "child"
        child_name = _sanitize_arcname(child_disp)
        child_folder = child.get("folder")

        # ---- 静止画（現在の保存先から）----
        base = os.path.join(photos_root, album_id, child_folder)
        if os.path.isdir(base):
            for fname in os.listdir(base):
                fp = os.path.join(base, fname)
                if not os.path.isfile(fp):
                    continue
                if fname.startswith("latest.") and not allowed_file(fname):
                    continue
                if allowed_file(fname):
                    safe_fname = _sanitize_file_component(fname)
                    arc = f"{child_name}/{safe_fname}"
                    entries.append((arc, fp))

        # ---- 動画（固定MOVIE_ROOTから）----
        movie_base = os.path.join(MOVIE_ROOT, album_id, child_folder)
        if os.path.isdir(movie_base):
            for fname in os.listdir(movie_base):
                fp = os.path.join(movie_base, fname)
                if not os.path.isfile(fp):
                    continue
                if allowed_movie(fname) or fname.endswith(".web.mp4") or fname.endswith(".poster.jpg"):
                    safe_fname = _sanitize_file_component(fname)
                    arc = f"{child_name}/{safe_fname}"
                    entries.append((arc, fp))

    return entries, album_name

def _zip_worker(album_id: str, job_key: str):
    job_id = (album_id, job_key)
    with _ZIP_LOCK:
        job = _ZIP_JOBS.get(job_id)
    if not job:
        return

    try:
        entries, album_name = _gather_files_for_album(album_id)
        if not entries:
            with _ZIP_LOCK:
                job.update(status='error', message='対象ファイルがありません', percent=0)
            return

        total_files = len(entries)
        total_bytes = 0
        for _, p in entries:
            try:
                total_bytes += os.path.getsize(p)
            except Exception:
                pass

        with _ZIP_LOCK:
            job.update(status='running', percent=0, total_files=total_files, processed_files=0,
                       total_bytes=total_bytes, processed_bytes=0, album_name=album_name, started=time.time())

        tmp = NamedTemporaryFile(prefix=f"zip_{album_id}_", suffix=".zip", delete=False)
        tmp_path = tmp.name
        tmp.close()

        # 進捗粗見積
        def _eta(proc_bytes, started):
            dt = max(0.001, time.time() - started)
            speed = proc_bytes / dt
            remain = max(0, total_bytes - proc_bytes)
            return int(remain / speed) if speed > 1 else None

        processed_files = 0
        processed_bytes = 0
        started = time.time()

        with ZipFile(tmp_path, mode='w', compression=ZIP_DEFLATED) as zf:
            for arcname, abs_path in entries:
                try:
                    zf.write(abs_path, arcname)
                    processed_files += 1
                    try:
                        processed_bytes += os.path.getsize(abs_path)
                    except Exception:
                        pass
                except Exception as e:
                    # 1ファイル失敗はスキップして続行
                    current_app.logger.warning("ZIP書込失敗: %s (%s)", abs_path, e)

                # 0.2秒間隔くらいで進捗更新（過負荷防止）
                if processed_files % 5 == 0:
                    percent = int(processed_bytes * 100 / total_bytes) if total_bytes else int(processed_files * 100 / total_files)
                    with _ZIP_LOCK:
                        job.update(processed_files=processed_files,
                                   processed_bytes=processed_bytes,
                                   percent=min(99, percent),
                                   eta_seconds=_eta(processed_bytes, started))

        # 完了
        with _ZIP_LOCK:
            job.update(status='done',
                       percent=100,
                       processed_files=processed_files,
                       processed_bytes=processed_bytes,
                       tmp_path=tmp_path,
                       eta_seconds=0)

    except Exception as e:
        with _ZIP_LOCK:
            job.update(status='error', message=str(e), percent=0)

@album_bp.route('/<album_id>/admin_zip_all_async', methods=['POST'])
def admin_zip_all_async(album_id):
    if session.get('user') != 'admin':
        return abort(403)
    # 既存ジョブ確認
    job_key = "default"  # アルバム単位で1ジョブに固定
    job_id = (album_id, job_key)
    with _ZIP_LOCK:
        job = _ZIP_JOBS.get(job_id)
        if job and job.get('status') in ('running', 'done'):
            return jsonify(ok=True, already_running=True, key=job_key, progress={
                k: job.get(k) for k in ('status','percent','total_files','processed_files','total_bytes','processed_bytes','eta_seconds')
            })
        # 新規作成
        _ZIP_JOBS[job_id] = {
            'status': 'queued', 'percent': 0,
            'total_files': 0, 'processed_files': 0,
            'total_bytes': 0, 'processed_bytes': 0,
            'eta_seconds': None, 'tmp_path': None
        }
    # スレッド開始
    t = threading.Thread(target=_zip_worker, args=(album_id, job_key), daemon=True)
    t.start()
    return jsonify(ok=True, key=job_key, already_running=False)

@album_bp.route('/<album_id>/admin_zip_all_progress', methods=['GET'])
def admin_zip_all_progress(album_id):
    if session.get('user') != 'admin':
        return abort(403)
    key = request.args.get('key') or 'default'
    job_id = (album_id, key)
    with _ZIP_LOCK:
        job = _ZIP_JOBS.get(job_id)
        if not job:
            return jsonify(ok=False, error='job not found')
        progress = {k: job.get(k) for k in ('status','percent','total_files','processed_files','total_bytes','processed_bytes','eta_seconds')}
        dl_ready = (job.get('status') == 'done' and bool(job.get('tmp_path')))
    return jsonify(ok=True, progress=progress, download_ready=dl_ready)

@album_bp.route('/<album_id>/admin_zip_all_download', methods=['GET'])
def admin_zip_all_download(album_id):
    if session.get('user') != 'admin':
        return abort(403)
    key = request.args.get('key') or 'default'
    job_id = (album_id, key)
    with _ZIP_LOCK:
        job = _ZIP_JOBS.get(job_id)
        if not job or job.get('status') != 'done' or not job.get('tmp_path'):
            return abort(404)
        tmp_path = job['tmp_path']
        album_name = job.get('album_name') or 'album'
        dl_name = f"{_sanitize_arcname(album_name)}.zip"

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        # ダウンロード後はジョブを消しておく
        with _ZIP_LOCK:
            _ZIP_JOBS.pop(job_id, None)
        return resp

    return send_file(tmp_path, as_attachment=True, download_name=dl_name,
                     mimetype="application/zip", conditional=True)
# ===== 追記ここまで =============================================================
