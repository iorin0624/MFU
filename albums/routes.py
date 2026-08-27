# -*- coding: utf-8 -*-
"""
アルバム機能 / 動画モード: 連番保存 + 変換(H.264/AAC) + ポスター生成 + 個別DL 対応フル版
+ 写真ストレージをSSD固定パスで管理

- 動画アップロード: {child_id}_YYYYMMDD_HHMM_NNNN.ext の連番で保存
- ffmpeg/ffprobe が見つかれば、*.web.mp4（H.264/AAC）と *.poster.jpg をバックグラウンド生成
- 一覧は .web.mp4 を優先再生（未生成なら converting=True をテンプレ側へ渡す）
- 画像(通常/加工)と動画のパスはルートで分離:
    画像:  /mnt/mfu/mfu_albums/<album>/<child>（固定）
    動画:  /mnt/mfu/mfu_album_movie/<album>/<child>（固定）
"""

from flask import (
    Blueprint, render_template, render_template_string, request, redirect,
    url_for, session, send_from_directory, send_file, abort, current_app, flash, jsonify
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
from app.utils.logs import log_request_raw
from app.utils.mail import send_mail
from app.utils.admin_passkey_stepup import require_admin_passkey

# 外部ユーティリティ（既存プロジェクトのモジュールを利用）
from app.albums.photo_namer import get_datetime_from_image
from app.utils.thumbs import enqueue_thumb_job, get_files_with_thumbs
from app.utils.push import send_push
from app.external_login_user.utils import _get_ext_user_by_social, is_withdrawn_ext_user

album_bp = Blueprint('album', __name__, template_folder='templates')
print("✅ album.routes (movie 連番 & 変換 & 個別DL + SSD固定保存) loaded")


ALBUM_AUTH_SESSION_KEY = "album_auth_ids"
ALBUM_AUTH_MAX_ITEMS = 120


def _grant_album_auth(album_id: str) -> None:
    _cleanup_legacy_album_auth_keys(current_album_id=album_id)
    allowed = session.get(ALBUM_AUTH_SESSION_KEY) or []
    if album_id in allowed:
        return
    allowed = (allowed + [album_id])[-ALBUM_AUTH_MAX_ITEMS:]
    session[ALBUM_AUTH_SESSION_KEY] = allowed


def _revoke_album_auth(album_id: str) -> None:
    allowed = session.get(ALBUM_AUTH_SESSION_KEY) or []
    if album_id in allowed:
        session[ALBUM_AUTH_SESSION_KEY] = [item for item in allowed if item != album_id]
    session.pop(f"auth_{album_id}", None)


def clear_event_album_auth() -> None:
    """外部ログアウト時にイベント連携分だけをセッションから除去する。"""
    allowed = [str(item) for item in (session.get(ALBUM_AUTH_SESSION_KEY) or []) if item]
    legacy_ids = [
        key[5:]
        for key in list(session.keys())
        if key.startswith("auth_") and key[5:]
    ]
    candidate_ids = list(dict.fromkeys(allowed + legacy_ids))
    if not candidate_ids:
        return

    placeholders = ",".join(["%s"] * len(candidate_ids))
    event_ids: set[str] = set()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id FROM albums WHERE access_mode='event' AND id IN ({placeholders})",
            tuple(candidate_ids),
        )
        event_ids = {
            str(row[0] if isinstance(row, tuple) else row.get("id"))
            for row in (cur.fetchall() or [])
        }
        cur.close()
    finally:
        conn.close()

    if event_ids:
        session[ALBUM_AUTH_SESSION_KEY] = [item for item in allowed if item not in event_ids]
        for album_id in event_ids:
            session.pop(f"auth_{album_id}", None)


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


def _access_log_username() -> str:
    ext_user_id = session.get("ext_user_id")
    nickname = (session.get("ext_user_nickname") or "").strip()

    if ext_user_id is None and session.get("ext_user_social_id"):
        try:
            ext_user = _get_ext_user_by_social(session["ext_user_social_id"]) or {}
            ext_user_id = ext_user.get("id")
            nickname = nickname or (ext_user.get("nickname") or "").strip()
        except Exception:
            pass

    if ext_user_id is not None:
        return f"LINE_{ext_user_id}_{nickname}"

    return (session.get("user") or "").strip()


def _request_client_ip() -> str:
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "-"


def _write_event_album_view_granted(album_id: str, event_id, auth_kind: str) -> None:
    """Write a separate audit row only after the event album page rendered."""
    try:
        log_request_raw(
            ip=_request_client_ip(),
            method="AUDIT",
            path=request.path or f"/album/{album_id}/",
            status=200,
            ua=request.headers.get("User-Agent", "-"),
            referer=request.headers.get("Referer", ""),
            endpoint="album.album_view_granted",
            username=_access_log_username(),
            latency_ms=0,
            marker=(
                f"[ALBUM_VIEW_GRANTED] album_id={album_id} "
                f"event_id={event_id} auth={auth_kind}"
            ),
        )
    except Exception:
        current_app.logger.warning(
            "event album view audit failed album_id=%s event_id=%s",
            album_id,
            event_id,
            exc_info=True,
        )


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
              u.email,
              COALESCE(u.is_deleted, 0) AS is_deleted
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
    return [r for r in (rows or []) if not is_withdrawn_ext_user(r)]

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
        """
        SELECT u.email,
               COALESCE(u.is_deleted, 0) AS is_deleted,
               u.nickname
          FROM mfu_event_member m
          JOIN external_login_user u ON u.id = m.user_id
         WHERE m.event_id=%s
           AND m.user_id=%s
           AND m.status='approved'
           AND COALESCE(m.is_canceled, 0)=0
         LIMIT 1
        """,
        (int(event_meta["event_id"]), int(request_by_id)),
    )
    if is_withdrawn_ext_user(requester_row):
        return
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
    send_mail(requester_email, subject, body)

def _fetch_event_notification_contacts(event_id: int, user_ids: list[int]) -> list[dict]:
    if not user_ids:
        return []
    placeholders = ",".join(["%s"] * len(user_ids))
    sql = (
        "SELECT m.user_id, u.nickname, u.email, COALESCE(u.is_deleted, 0) AS is_deleted, "
        "       COALESCE(u.notify_album_process, 1) AS notify_album_process "
        "  FROM mfu_event_member m "
        "  JOIN external_login_user u ON u.id = m.user_id "
        f" WHERE m.event_id=%s AND m.user_id IN ({placeholders}) "
        "   AND m.status='approved' "
        "   AND COALESCE(m.is_canceled, 0)=0 "
        " ORDER BY u.nickname ASC, m.user_id ASC"
    )
    rows = [
        r for r in (db_get_all(sql, (event_id, *user_ids)) or [])
        if not is_withdrawn_ext_user(r)
    ]
    current_app.logger.info(
        "album process notify contacts loaded event_id=%s requested_user_ids=%s contacts_after_cancel_filter=%s condition=%s",
        event_id,
        len(user_ids),
        len(rows),
        "status='approved' AND COALESCE(is_canceled,0)=0",
    )
    return rows


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
# --- 写真（静止画／加工）の保存先 ---
ALBUM_ROOT = '/mnt/mfu/mfu_albums'

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

def _prefer_existing_child_dir(album_id: str, child_id: str, mode: str):
    """メディア種別に応じた子アルバムの保存先を返す。"""
    return storage_child_dir(album_id, child_id, mode)

def _open_media_path(album_id: str, child_id: str, filename: str, mode: str = "normal") -> str | None:
    """メディアの絶対パスを安全に解決する（send_file 用）。"""
    if (mode or "").lower() == "movie":
        path = os.path.join(MOVIE_ROOT, album_id, child_id, filename)
        return path if os.path.isfile(path) else None
    path = os.path.join(ALBUM_ROOT, album_id, child_id, filename)
    return path if os.path.isfile(path) else None

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

    child_path = storage_child_dir(album_id, child_id, mode=normalized_mode)
    if not os.path.isdir(child_path):
        return 0

    count = 0
    for fname in os.listdir(child_path):
        full = os.path.join(child_path, fname)
        if not os.path.isfile(full) or not allowed_file(fname):
            continue

        if normalized_mode == "process":
            if not fname.startswith("latest."):
                continue
        elif fname.startswith("latest."):
            continue

        count += 1
    return count

def resolve_thumb_url(album_id: str, child_id: str, original_filename: str) -> str:
    base, _ = os.path.splitext(original_filename)
    for cand in (f"{base}.webp", f"{base}.jpg", f"{base}.jpeg"):
        p = os.path.join(ALBUM_ROOT, album_id, child_id, 'thumbs', cand)
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

def _movie_dir(album_id: str, child_id: str) -> str:
    return os.path.join(MOVIE_ROOT, album_id, child_id)

def _movie_subdir(album_id: str, child_id: str, sub: str) -> str:
    p = os.path.join(_movie_dir(album_id, child_id), sub)
    os.makedirs(p, exist_ok=True)
    return p

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
        "SELECT id, album_name AS name, owner, access_token, event_id, access_mode "
        "FROM albums WHERE owner=%s ORDER BY album_name ASC",
        (user,)
    )

def list_albums_for_admin():
    return db_get_all(
        "SELECT id, album_name AS name, owner, access_token, event_id, access_mode "
        "FROM albums ORDER BY album_name ASC"
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

# ------------------ メディア保存先 ------------------
def storage_child_dir(album_id: str, child_id: str, mode: str | None = None) -> str:
    """
    子アルバムのフルパスを返す。
    - movie: MOVIE_ROOT（固定）
    - その他: ALBUM_ROOT（固定）
    """
    if (mode or '').lower() == 'movie':
        base = MOVIE_ROOT
    else:
        base = ALBUM_ROOT
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
    ext_user_id = session.get("ext_user_id")
    if ext_user_id is None:
        u = db_get_one(
            """
            SELECT id, nickname
              FROM external_login_user
             WHERE social_id=%s
               AND COALESCE(is_deleted, 0)=0
             LIMIT 1
            """,
            (sid,),
        )
        if not u:
            session["ext_user_onboarding"] = True
            session["after_login_redirect"] = url_for('album.album_access', album_id=session.get("_gate_album_id"), _external=True)
            return False
        ext_user_id = int(u["id"])
        session["ext_user_id"] = ext_user_id
        session["ext_user_nickname"] = (u.get("nickname") or "").strip()
    else:
        try:
            ext_user_id = int(ext_user_id)
        except (TypeError, ValueError):
            session.pop("ext_user_id", None)
            return False
    mem = db_get_one(
        """
        SELECT m.status, COALESCE(m.is_canceled,0) AS is_canceled
          FROM mfu_event_member AS m
          JOIN external_login_user AS u ON u.id=m.user_id
         WHERE m.event_id=%s
           AND m.user_id=%s
           AND COALESCE(u.is_deleted,0)=0
         LIMIT 1
        """,
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
    event_detail_url = None
    event_detail_label = "イベント詳細へ戻る"
    event_summary = None
    if album_meta and album_meta.get("access_mode") == "event" and album_meta.get("event_id"):
        try:
            event_summary = db_get_one(
                """
                SELECT event_uuid, title, starts_at, place_name
                  FROM mfu_event
                 WHERE id=%s
                 LIMIT 1
                """,
                (album_meta["event_id"],),
            )
            event_uuid_str = _uuid_bytes_to_str((event_summary or {}).get("event_uuid"))
            if is_admin:
                event_detail_url = url_for(
                    "external_login_user.admin_event_view",
                    event_id=album_meta["event_id"],
                )
                event_detail_label = "イベント管理へ戻る"
            elif event_uuid_str:
                event_detail_url = url_for(
                    "external_login_user.view_event",
                    event_uuid=event_uuid_str,
                )
        except Exception as exc:
            current_app.logger.warning(
                "event album header context failed album_id=%s: %s",
                album_id,
                exc,
            )
            event_summary = None

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

    rendered = render_template(
        'album_home.html',
        album_id=album_id, meta=meta,
        is_admin=is_admin, is_owner=is_owner,
        processing_list=processing_list,   # ★追加
        ext_user_nickname=ext_user_nickname,
        completed_process_children=completed_process_children,
        show_extlogin_nav=show_extlogin_nav,
        event_detail_url=event_detail_url,
        event_detail_label=event_detail_label,
        event_summary=event_summary,
        is_event_album=bool((album_meta or {}).get("access_mode") == "event"),
    )
    if album_meta and album_meta.get("access_mode") == "event" and album_meta.get("event_id"):
        auth_kind = "admin" if is_admin else ("owner" if is_owner else "event_session")
        _write_event_album_view_granted(album_id, album_meta["event_id"], auth_kind)
    return rendered

@album_bp.route('/<album_id>/create_child', methods=['POST'])
def create_child(album_id):
    meta = load_meta(album_id)
    if not meta:
        return redirect(url_for('album.album_access', album_id=album_id))

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
                "       COALESCE(u.is_deleted, 0) AS is_deleted,"
                "       COALESCE(u.notify_album_upload, 1)  AS notify_album_upload,"
                "       COALESCE(u.notify_album_process, 1) AS notify_album_process "
                "  FROM mfu_event_member m "
                "  JOIN external_login_user u ON u.id = m.user_id "
                " WHERE m.event_id=%s    AND m.status='approved'    AND COALESCE(m.is_canceled, 0)=0 "
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

            rows = [r for r in (rows or []) if not is_withdrawn_ext_user(r)]
            recipients_total = len(rows)
            current_app.logger.info(
                "album notify recipients loaded kind=%s album_id=%s child_id=%s event_id=%s recipients_after_cancel_filter=%s condition=%s",
                kind,
                album_id,
                child_id,
                event_id,
                recipients_total,
                "status='approved' AND COALESCE(is_canceled,0)=0",
            )

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

            # 送信
            sent_ok = False
            mail_failed_count = 0
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
    - それ以外は静止画/加工を固定保存先から表示
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
            file_path = _open_media_path(album_id, child_id, filename, mode='normal')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted += 1
                except Exception as e:
                    current_app.logger.warning("削除失敗 %s: %s", file_path, e)
        flash(f"{deleted} 件のファイルを削除しました", "success")
        return redirect(request.url)

    def _thumb_url(_album_id: str, _child_id: str, original_filename: str) -> str:
        base, _ = os.path.splitext(original_filename)
        for cand in (f"{base}.webp", f"{base}.jpg", f"{base}.jpeg"):
            p = os.path.join(ALBUM_ROOT, _album_id, _child_id, 'thumbs', cand)
            if os.path.isfile(p):
                return url_for('album.album_thumb', album_id=_album_id, child_id=_child_id, filename=cand)
        # サムネが無いときは元画像
        return url_for('album.image', album_id=_album_id, child_id=_child_id, filename=original_filename)

    videos = []
    files = []

    if mode == "process":
        # 加工専用: latest.* の一番新しい1枚を表示
        candidates = []
        p = os.path.join(ALBUM_ROOT, album_id, child_id)
        if os.path.isdir(p):
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
            show_extlogin_nav=show_extlogin_nav,
        )

    else:
        # 通常モード（静止画）
        merged = []
        p = os.path.join(ALBUM_ROOT, album_id, child_id)
        if os.path.isdir(p):
            for f in os.listdir(p):
                full = os.path.join(p, f)
                if not os.path.isfile(full):
                    continue
                if not allowed_file(f) or f.startswith("latest."):
                    continue
                merged.append({"name": f, "thumb": _thumb_url(album_id, child_id, f)})
        files = sorted(merged, key=lambda x: x["name"])

    zip_token = session.pop('zip_token', None)

    # 加工履歴（静止画用）
    history_list = []
    history_path = os.path.join(child_path, "history.json")
    if os.path.exists(history_path):
        try:
            with open(history_path, 'r') as f:
                history_list = json.load(f)
        except Exception as e:
            current_app.logger.warning("加工履歴読み込みエラー: %s", e)
    history_list = [
        r for r in (history_list or [])
        if isinstance(r, dict) and not is_withdrawn_ext_user(r)
    ]

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
    p = os.path.join(ALBUM_ROOT, album_id, child_id, 'thumbs', filename)
    if os.path.isfile(p):
        return send_file(p, conditional=True)
    abort(404)

@album_bp.route('/<album_id>/<child_id>/process_status', methods=['POST'])
def update_process_status(album_id, child_id, data_override=None):
    meta = load_meta(album_id)
    if not meta:
        return jsonify({"ok": False, "error": "album_not_found"}), 404

    is_authed = _has_album_auth(album_id)
    if not (is_authed or session.get('user') == 'admin' or _is_ext_logged_in()):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = data_override if isinstance(data_override, dict) else (request.get_json(silent=True) or {})
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
    abs_path = _open_media_path(album_id, child_id, filename, mode='normal')
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

    guard = require_admin_passkey(f"album_delete:{album_id}")
    if guard:
        return guard

    for root in (ALBUM_ROOT, MOVIE_ROOT):
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

        os.makedirs(os.path.join(ALBUM_ROOT, album_id), exist_ok=True)

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

    event_album_list = []
    normal_album_list = []
    for r in rows:
        album = {
            "id": r["id"],
            "name": r["name"],
            "owner": r["owner"],
            "password": "(未設定)",
            "access_token": r.get("access_token", ""),
            "event_id": r.get("event_id"),
            "access_mode": r.get("access_mode") or "token",
        }
        is_event_album = album["event_id"] is not None and album["access_mode"] == "event"
        (event_album_list if is_event_album else normal_album_list).append(album)

    event_album_list.sort(key=lambda x: x['name'])
    normal_album_list.sort(key=lambda x: x['name'])
    return render_template(
        'admin_create_album.html',
        event_album_list=event_album_list,
        normal_album_list=normal_album_list,
    )

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

    guard = require_admin_passkey(f"album_child_delete:{album_id}:{child_id}")
    if guard:
        return guard

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

    # 固定保存先から最新画像を探索
    latest = None
    latest_mtime = -1
    latest_path = None
    p = os.path.join(ALBUM_ROOT, album_id, child_id)
    if os.path.isdir(p):
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

    event_meta = _fetch_album_meta(album_id)
    if event_meta and event_meta.get('access_mode') == 'event':
        flash('イベント連携アルバムの名前はイベント管理画面から変更してください', 'warning')
        return redirect(url_for('album.admin_create_album'))

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

    event_meta = _fetch_album_meta(album_id)
    if event_meta and event_meta.get('access_mode') == 'event':
        flash('イベント連携アルバムの名前はイベント管理画面から変更してください', 'warning')
        return redirect(url_for('album.admin_create_album'))

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
    """processモード用: latest.* があれば返し、無ければ最新の静止画を返す。"""
    meta = load_meta(album_id) or {}
    child_meta = next((c for c in meta.get("children", []) if c.get("folder") == child_id), None)
    mode = child_meta.get("mode", "normal") if child_meta else "normal"

    latest = None
    latest_mtime = -1
    child_path = os.path.join(ALBUM_ROOT, album_id, child_id)
    if os.path.isdir(child_path):

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
# 既存: トークン禁止（イベントモード）
# =============================================================================
_EVENT_ALBUM_JSON_ENDPOINTS = {
    "album.begin_process",
    "album.request_process",
    "album.update_process_status",
}


@album_bp.before_request
def _enforce_event_album_access():
    """イベント連携アルバムの全ルートで現在の参加資格を再確認する。"""
    if request.blueprint != "album":
        return

    album_id = (request.view_args or {}).get("album_id")
    if not album_id:
        return

    meta = _fetch_album_meta(str(album_id))
    if not meta or meta.get("access_mode") != "event":
        return

    # 入口は event_gate 側でログイン・参加承認を判定する。
    if request.endpoint in {"album.album_access", "album.api_album_authenticate"}:
        return

    user = session.get("user")
    if user == "admin" or (user and user == meta.get("owner")):
        return

    session["_gate_album_id"] = str(album_id)
    event_id = meta.get("event_id")
    if event_id and _is_event_member_approved(int(event_id)):
        _grant_album_auth(str(album_id))
        return

    # 以前の閲覧許可だけでは通さず、ログアウト・取消後は直ちに無効化する。
    _revoke_album_auth(str(album_id))
    if (
        request.endpoint in _EVENT_ALBUM_JSON_ENDPOINTS
        or request.is_json
        or request.path.startswith("/album/api/")
    ):
        return jsonify({"ok": False, "error": "event_album_auth_required"}), 403
    return redirect(url_for("album.album_access", album_id=album_id))


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

# ===== アルバム全体ZIP（管理者専用・共通ZIP基盤） ==========================
import unicodedata
from flask import after_this_request, send_file, abort, request, jsonify
from app.utils.zip_stream import make_zip_entries, read_zip_progress, start_zip_entries_job

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
    ・静止画は ALBUM_ROOT 側から
    ・動画は MOVIE_ROOT 側から
    """
    if session.get('user') != 'admin':
        return abort(403)

    entries, album_name = _gather_files_for_album(album_id)
    if not entries:
        return '対象ファイルがありません', 404

    key = f"album-{uuid.uuid4().hex}"
    tmp_path = make_zip_entries(
        entries,
        key,
        download_name=f"{_sanitize_arcname(album_name)}.zip",
        access={"type": "admin", "album_id": album_id},
    )
    if not tmp_path:
        return 'ZIP生成に失敗しました', 500

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

def _gather_files_for_album(album_id: str):
    """ZIPに含めるファイルを列挙: [(arcname, abs_path), ...]（日本語対応）"""
    meta = load_meta(album_id)
    if not meta:
        return [], "アルバムが存在しません"
    album_name = meta.get("album_name") or "album"
    children = meta.get("children", [])

    photos_root = ALBUM_ROOT

    entries = []
    for child in children:
        # 子ディレクトリ名は表示名優先。日本語保持で安全化
        child_disp = child.get("name") or child.get("folder") or "child"
        child_name = _sanitize_arcname(child_disp)
        child_folder = child.get("folder")

        # ---- 静止画 ----
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

@album_bp.route('/<album_id>/admin_zip_all_async', methods=['POST'])
def admin_zip_all_async(album_id):
    """管理画面の互換入口。生成・進捗・配信は共通ZIP基盤へ委譲する。"""
    if session.get('user') != 'admin':
        return abort(403)

    entries, album_name = _gather_files_for_album(album_id)
    if not entries:
        return jsonify(ok=False, error='対象ファイルがありません'), 404

    key = f"album-{uuid.uuid4().hex}"
    try:
        start_zip_entries_job(
            entries,
            key=key,
            download_name=f"{_sanitize_arcname(album_name)}.zip",
            access={"type": "admin", "album_id": album_id},
        )
    except FileExistsError:
        return jsonify(ok=False, error='already_in_progress', key=key), 409

    return jsonify(
        ok=True,
        key=key,
        already_running=False,
        progress_url=f"/api/zip-progress?key={key}",
        download_url=f"/api/zip-download/{key}",
    )


@album_bp.route('/<album_id>/admin_zip_all_progress', methods=['GET'])
def admin_zip_all_progress(album_id):
    """旧管理画面との互換レスポンス。進捗データは共通ストアから読む。"""
    if session.get('user') != 'admin':
        return abort(403)
    key = request.args.get('key') or ''
    progress = read_zip_progress(key) if key else None
    if not progress:
        return jsonify(ok=False, error='job not found'), 404
    access = progress.get('access') or {}
    if access.get('type') != 'admin' or access.get('album_id') != album_id:
        return jsonify(ok=False, error='job not found'), 404
    return jsonify(
        ok=True,
        progress=progress,
        download_ready=progress.get('status') == 'done',
        download_url=f"/api/zip-download/{key}",
    )


@album_bp.route('/<album_id>/admin_zip_all_download', methods=['GET'])
def admin_zip_all_download(album_id):
    """旧ダウンロードURLを共通配信URLへ転送する。"""
    if session.get('user') != 'admin':
        return abort(403)
    key = request.args.get('key') or ''
    progress = read_zip_progress(key) if key else None
    access = (progress or {}).get('access') or {}
    if access.get('type') != 'admin' or access.get('album_id') != album_id:
        return abort(404)
    return redirect(f"/api/zip-download/{key}")
# ===== 追記ここまで =============================================================
