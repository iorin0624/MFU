# -*- coding: utf-8 -*-
from __future__ import annotations

# =========================
# 標準ライブラリ
# =========================
import os
import re
import io
import uuid
import time
import secrets
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from mimetypes import guess_extension
from urllib.parse import quote_plus, urlparse
from ipaddress import ip_address, IPv4Address, IPv6Address, ip_network
from app.utils.logs import write_line_login_log  # 追加
from jinja2 import TemplateNotFound
import math
from urllib.parse import urlparse, parse_qs, unquote


# =========================
# サードパーティ
# =========================
from PIL import Image
import requests
from itsdangerous import URLSafeSerializer, BadSignature

# =========================
# Flask / アプリ内部
# =========================
from flask import (
    request, session, redirect, url_for, render_template,
    abort, flash, current_app, send_from_directory, make_response
)
from werkzeug.utils import secure_filename

# Blueprint は必ず先に import
from . import bp, oauth

from app.utils.db import get_db
from app.utils.mail import send_mail
from .utils import (
    LINE_CLIENT_ID, LINE_CLIENT_SECRET, LINE_REDIRECT_URI,
    _require_ext_login, _is_mfu_logged_in, _uuid_bytes_to_str,
    _get_ext_user_by_social, _upsert_ext_user, _update_profile,
    _event_by_uuid_str, _membership_status,
    avatar_url_for,  # ← 追加
)
#from .auto_payment import load_default_card_summary


# ==== アバター保存設定 ====
AVATAR_ROOT = Path("/mnt/mfu/avatars")
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
AVATAR_ROOT.mkdir(parents=True, exist_ok=True)



# =========================
# 便利ヘルパ（このファイル内限定）
# =========================
def _normalize_email(s: str) -> str | None:
    s = (s or "").strip()
    if not s or "@" not in s or len(s) > 255:
        return None
    return s


def _issue_email_verify_token(user_id: int, email: str, *, ttl_hours: int = 24, redirect_url: str | None = None) -> str:
    """
    検証用トークンを発行して DB に保存し、URL に載せるプレーントークンを返す。
    DB 側には SHA256 ハッシュで保存。
    """
    raw = secrets.token_urlsafe(32)
    token_hex = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)

    db = get_db(); cur = db.cursor()
    try:
        # redirect_url カラムがある前提で進めます（なければ後ほど ALTER TABLE を検討）
        # 既存のコードから推測すると、このテーブルに動的にカラムを追加するのは難しいため、
        # もしカラムがない場合はエラーになりますが、まずは追加を試みます。
        try:
            cur.execute("""
                INSERT INTO mfu_email_verification (user_id, email, token, expires_at, redirect_url)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, email, token_hex, expires_at, redirect_url))
        except Exception:
            # カラムがない場合のフォールバック
            cur.execute("""
                INSERT INTO mfu_email_verification (user_id, email, token, expires_at)
                VALUES (%s, %s, %s, %s)
            """, (user_id, email, token_hex, expires_at))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return raw


def _send_verify_mail(to_email: str, token_raw: str):
    """メールアドレス確認メール（イベント非関連）→ send_mail に統一"""
    verify_url_get = url_for("external_login_user.email_verify", _external=True) + f"?t={token_raw}"
    subject = "イベント管理システムからメールアドレス確認のお願い"
    body = (
        "メールアドレスの確認をお願いします。\n"
        "下記の確認ページを開き、「確認する」ボタンを押してください（有効期限: 24時間）。\n\n"
        f"{verify_url_get}\n\n"
        "※このメールに心当たりがない場合は破棄してください。"
    )
    # イベント非関連なので event_uuid=None（From: noreply@mail.iori0624.jp）
    send_mail(
        to=to_email,
        subject=subject,
        body=body,
        event_uuid=None,
    )


def _get_admin_webhook_url() -> str | None:
    """usersテーブルからadminのDiscord Webhook URLを取得"""
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT webhook_url FROM users WHERE username=%s LIMIT 1", ("admin",))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row, tuple) else row.get("webhook_url")
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _notify_discord(content: str) -> None:
    """Discordへシンプル通知（失敗してもアプリ動作は阻害しない）"""
    url = _get_admin_webhook_url()
    if not url:
        return
    try:
        requests.post(url, json={"content": content}, timeout=5)
    except Exception:
        current_app.logger.exception("discord notify failed")


def _update_member_status_and_notify(event_id: int, user_id: int, new_status: str):
    """
    mfu_event_member.status を new_status に更新。
    変化があれば対象者へイベントメール（send_mail）で通知。
    戻り: (ok: bool, msg: str, applied_status: str)
    """
    if new_status not in ("approved", "rejected", "pending"):
        return False, "invalid status", new_status

    db = get_db(); cur = db.cursor()

    # イベント基本情報（件名/From生成）
    cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
    ev_row = cur.fetchone()
    if not ev_row:
        cur.close(); db.close()
        return False, "event not found", new_status
    ev_title  = ev_row[0] if isinstance(ev_row, tuple) else ev_row["title"]
    ev_uuid_b = ev_row[1] if isinstance(ev_row, tuple) else ev_row["event_uuid"]
    ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""
    view_url = f"https://mfu.iori0624.jp/external-login/events/view/{ev_uuid_str}"

    # 対象メンバーの現状
    cur.execute("""
        SELECT m.status, u.email, u.nickname
          FROM mfu_event_member m
          JOIN external_login_user u ON u.id = m.user_id
         WHERE m.event_id=%s AND m.user_id=%s
         LIMIT 1
    """, (event_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        return False, "member not found", new_status
    old_status = row[0] if isinstance(row, tuple) else row["status"]
    to_email   = row[1] if isinstance(row, tuple) else row["email"]
    nickname   = row[2] if isinstance(row, tuple) else row["nickname"]

    # 変化なしならそのままOK
    if old_status == new_status:
        cur.close(); db.close()
        return True, "no change", new_status

    # 更新
    cur.execute("""
        UPDATE mfu_event_member
           SET status=%s
         WHERE event_id=%s AND user_id=%s
         LIMIT 1
    """, (new_status, event_id, user_id))
    db.commit()

    # メール通知（宛先・UUIDがあれば）→ send_mail に統一
    if to_email and ev_uuid_str:
        try:
            subject = f"【{ev_title}】参加ステータスが更新されました"
            body = (
                f"{nickname or '参加者'} 様\n\n"
                f"イベント「{ev_title}」の参加ステータスが「{old_status}」→「{new_status}」に更新されました。\n"
                f"詳細は以下のページをご確認ください。\n{view_url}\n"
            )
            send_mail(
                to=to_email,
                subject=subject,
                body=body,
                event_uuid=ev_uuid_str,
            )
        except Exception as e:
            current_app.logger.exception("status notify mail failed to %s: %s", to_email, e)

    cur.close(); db.close()
    return True, "ok", new_status


def _get_acl_admin_emails(event_id: int) -> list[str]:
    """
    ACLの通知先メールを極力拾う：
      1) a.username -> users.username
      2) a.user_id  -> users.id
      3) a.email    (ACLに直接メールがある場合)
    役割は viewer/manager/owner/host/admin を対象。
    """
    emails: set[str] = set()
    roles = ("viewer", "manager", "owner", "host", "admin")

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # 1) username 結合
        try:
            cur.execute(f"""
                SELECT u.email
                  FROM mfu_event_admin_acl a
                  JOIN users u ON u.username = a.username
                 WHERE a.event_id = %s
                   AND COALESCE(a.role,'viewer') IN ({','.join(['%s']*len(roles))})
                   AND u.email IS NOT NULL AND u.email <> ''
            """, (event_id, *roles))
            for r in cur.fetchall() or []:
                e = (r.get("email") or "").strip()
                if e: emails.add(e)
        except Exception as e:
            current_app.logger.info("ACL email username-join failed: %s", e)

        # 2) user_id 結合
        try:
            cur.execute(f"""
                SELECT u.email
                  FROM mfu_event_admin_acl a
                  JOIN users u ON u.id = a.user_id
                 WHERE a.event_id = %s
                   AND COALESCE(a.role,'viewer') IN ({','.join(['%s']*len(roles))})
                   AND u.email IS NOT NULL AND u.email <> ''
            """, (event_id, *roles))
            for r in cur.fetchall() or []:
                e = (r.get("email") or "").strip()
                if e: emails.add(e)
        except Exception as e:
            current_app.logger.info("ACL email user_id-join failed: %s", e)

        # 3) ACLのemail列
        try:
            cur.execute("""
                SELECT a.email
                  FROM mfu_event_admin_acl a
                 WHERE a.event_id = %s
                   AND a.email IS NOT NULL AND a.email <> ''
            """, (event_id,))
            for r in cur.fetchall() or []:
                e = (r.get("email") or "").strip()
                if e: emails.add(e)
        except Exception as e:
            current_app.logger.info("ACL email direct-column failed: %s", e)

    finally:
        try: cur.close(); db.close()
        except Exception: pass

    out = sorted(emails)
    current_app.logger.info("ACL admin emails resolved (event_id=%s): %s", event_id, out)
    return out


def _fallback_site_admin_emails() -> list[str]:
    """最後の保険：サイト管理者（admin/root）のメール"""
    emails: set[str] = set()
    db = get_db(); cur = db.cursor()
    try:
        try:
            cur.execute("""
                SELECT email FROM users
                 WHERE username IN ('admin','root')
                   AND email IS NOT NULL AND email <> ''
            """)
            rows = cur.fetchall() or []
            for r in rows:
                e = (r[0] if isinstance(r, tuple) else r.get("email")) or ""
                e = e.strip()
                if e: emails.add(e)
        except Exception as e:
            current_app.logger.info("fallback site-admin emails failed: %s", e)
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return sorted(emails)

def _save_avatar(file_storage) -> str | None:
    """
    アバター画像を /mnt/mfu/avatars に保存してファイル名を返す。
    - 対応: PNG / JPEG / WEBP / GIF（アニメGIFはそのまま保存）
    - 画像は最大辺 1024px に縮小（GIF 以外）
    - 失敗時は None
    """
    if not file_storage:
        return None

    try:
        mime = (file_storage.mimetype or "").lower().strip()
    except Exception:
        mime = ""

    if mime not in ALLOWED_MIME:
        return None  # サポート外

    ext_map = {
        "image/jpeg": ".jpg",
        "image/png":  ".png",
        "image/webp": ".webp",
        "image/gif":  ".gif",
    }
    ext = ext_map.get(mime, "")
    if not ext:
        return None

    # 出力ファイル名（元名は使わず UUID のみ）
    name = f"{uuid.uuid4().hex}{ext}"
    out_path = os.path.join(AVATAR_ROOT, name)

    # GIF はアニメ等の都合でそのまま保存
    if mime == "image/gif":
        try:
            file_storage.save(out_path)
            return name
        except Exception:
            current_app.logger.exception("save avatar (gif) failed")
            return None

    # それ以外は PIL で軽く整形（最大辺 1024px / EXIF 無視）
    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as im:
            im = im.convert("RGB")  # JPEG/WEBP に備える
            # 画像縮小（サムネ相当・画質劣化を最小限に）
            im.thumbnail((1024, 1024))
            if mime == "image/png":
                im.save(out_path, format="PNG", optimize=True)
            elif mime == "image/webp":
                im.save(out_path, format="WEBP", quality=85, method=6)
            else:  # JPEG
                im.save(out_path, format="JPEG", quality=85, optimize=True, progressive=True)
        return name
    except Exception:
        current_app.logger.exception("save avatar failed")
        return None

# ==== 追加: URL/バイト列からアバター保存 ====

def _save_avatar_from_bytes(data: bytes, mime_hint: str | None = None) -> str | None:
    """
    バイト列を /mnt/mfu/avatars に保存してファイル名を返す。
    - 対応: PNG / JPEG / WEBP / GIF（GIFはそのまま保存）
    - 画像は最大辺 1024px に縮小（GIF以外）
    """
    try:
        mime = (mime_hint or "").lower().strip()
    except Exception:
        mime = ""

    # Content-Type が不明な場合は PIL で判定する
    ext_map = {
        "image/jpeg": ".jpg",
        "image/png":  ".png",
        "image/webp": ".webp",
        "image/gif":  ".gif",
    }
    if not mime or mime not in ALLOWED_MIME:
        # PILで開いてフォーマット判定（GIFならGIF、PNG/JPEG/WEBPに丸める）
        try:
            from io import BytesIO
            with Image.open(BytesIO(data)) as im_probe:
                fmt = (im_probe.format or "").upper()
                if fmt == "GIF":
                    mime = "image/gif"
                elif fmt == "PNG":
                    mime = "image/png"
                elif fmt in ("JPEG","JPG"):
                    mime = "image/jpeg"
                elif fmt == "WEBP":
                    mime = "image/webp"
        except Exception:
            return None
        if mime not in ALLOWED_MIME:
            return None

    ext = ext_map.get(mime)
    if not ext:
        return None

    name = f"{uuid.uuid4().hex}{ext}"
    out_path = os.path.join(AVATAR_ROOT, name)

    if mime == "image/gif":
        try:
            with open(out_path, "wb") as f:
                f.write(data)
            return name
        except Exception:
            current_app.logger.exception("save avatar (gif bytes) failed")
            return None

    # それ以外は縮小・圧縮して保存
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((1024, 1024))
            if mime == "image/png":
                im.save(out_path, format="PNG", optimize=True)
            elif mime == "image/webp":
                im.save(out_path, format="WEBP", quality=85, method=6)
            else:
                im.save(out_path, format="JPEG", quality=85, optimize=True, progressive=True)
        return name
    except Exception:
        current_app.logger.exception("save avatar (bytes) failed")
        return None


def _download_and_save_avatar(url: str, *, timeout: int = 8) -> str | None:
    """
    指定URLから画像を取得し、ローカルに保存してファイル名を返す。
    - 5MBまで
    - Content-Type と PIL の両方でざっくり判定
    """
    if not url or not isinstance(url, str):
        return None
    try:
        r = requests.get(url, timeout=timeout, stream=True, headers={"User-Agent": "MFU/1.0"})
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # 5MB上限
        max_bytes = 5 * 1024 * 1024
        data = b""
        for chunk in r.iter_content(256 * 1024):
            if not chunk:
                break
            data += chunk
            if len(data) > max_bytes:
                current_app.logger.warning("avatar download too large: %s", url)
                return None
        return _save_avatar_from_bytes(data, ctype if ctype in ALLOWED_MIME else None)
    except Exception:
        current_app.logger.exception("avatar download failed: %s", url)
        return None

def _normalize_role_for_db(db, role: str) -> tuple[str, bool]:
    """
    DBのENUM許容値を見て、roleが未許容なら安全値へフォールバックする。
    戻り値: (保存するrole, degradedフラグ)
    """
    role = (role or "none").strip().lower()
    try:
        cur = db.cursor()
        cur.execute("SHOW COLUMNS FROM mfu_event_member LIKE 'participant_role'")
        row = cur.fetchone()
        cur.close()
        if not row:
            return role, False
        # 例: "enum('none','camera','assistant','cosplayer')"
        type_str = (row[1] if isinstance(row, tuple) else row.get("Type", "")) or ""
        type_str = str(type_str).lower()
        allowed = []
        if type_str.startswith("enum(") and type_str.endswith(")"):
            inside = type_str[5:-1]
            allowed = [s.strip().strip("'").strip('"') for s in inside.split(",")]

        if role in allowed:
            return role, False

        # 'other'未許容 → できれば 'cosplayer'、なければ 'none'
        if role == "other":
            if "cosplayer" in allowed:
                return "cosplayer", True
            if "none" in allowed:
                return "none", True
            return role, True

        # その他未知値 → 'none' に退避（存在すれば）
        if "none" in allowed:
            return "none", True
        return role, True
    except Exception:
        return role, False

# --- トップ用：メール確認未了バナーをフラッシュ表示するヘルパ ----------------------------
from markupsafe import Markup, escape

def _maybe_flash_email_verify_banner_for_top():
    """
    ログイン済み & email登録あり & email_verified_atがNULL のユーザーに
    トップページで注意喚起バナーをフラッシュする。
    （テンプレに手を入れず既存のflash表示領域を利用）
    """
    try:
        uid = session.get("ext_user_id")
        if not uid:
            return

        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT email, email_verified_at
                  FROM external_login_user
                 WHERE id=%s
                 LIMIT 1
            """, (uid,))
            row = cur.fetchone()
        finally:
            try: cur.close(); db.close()
            except Exception: pass

        email = (row["email"] or "").strip() if row else ""
        unverified = bool(email and row and not row["email_verified_at"])
        if not unverified:
            return

        # メッセージ（改行→<br> 変換）
        msg = (
            "メールアドレスの確認が完了していません。\n"
            "イベント管理システムからお送りした『メールアドレス確認のお願い』内のリンクをタップして認証を完了してください。\n"
            "届いていない場合は迷惑メールをご確認のうえ、下のボタンで再送できます。"
        )
        body = Markup("<br>".join(escape(msg).split("\n")))

        btn = Markup(
            f'''<form method="post" action="{escape(url_for('external_login_user.resend_verify_email'))}" style="display:inline;margin-left:8px;">
                    <button type="submit" class="btn btn-sm btn-primary">確認メールを再送する</button>
                </form>'''
        )

        flash(Markup(
            '<div style="font-weight:600;">メール確認のお願い</div>'
            f'<div style="margin-top:4px;line-height:1.6;">{body} {btn}</div>'
        ), "warning")

    except Exception:
        current_app.logger.exception("top email verify banner flash failed")

# --- GoogleマップURL からイベント座標を抜く -------------------------------
def _parse_lat_lng_from_maps_url(maps_url: str):
    """
    GoogleマップURLから (lat, lng) を頑張って抜き出す。
    取れなければ (None, None) を返す。
    """
    if not maps_url:
        return None, None

    s = str(maps_url).strip()
    try:
        s = unquote(s)
    except Exception:
        pass

    # パターン1: ".../@35.1234567,139.9876543,"
    m = re.search(r"@(-?\d+\.\d+),\s*(-?\d+\.\d+)", s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except Exception:
            pass

    # パターン2: クエリに ll=lat,lng or q=lat,lng or query=lat,lng
    parsed = urlparse(s)
    q = parse_qs(parsed.query)
    for key in ("ll", "q", "query"):
        if key in q:
            for v in q[key]:
                m2 = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", v)
                if m2:
                    try:
                        return float(m2.group(1)), float(m2.group(2))
                    except Exception:
                        pass

    return None, None


# --- 2点間距離[m]（ハーサイン） -----------------------------------------
def _calc_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    2点の距離[m]をざっくり計算。
    イベント会場付近かどうか判定する用途なら十分な精度。
    """
    R = 6371000.0  # 地球半径[m]

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c



# =========================
# TOP（=マイページ）
# =========================
@bp.route("/")
def index():
    # ---- 未ログイン時は当該バナーのフラッシュを除去してから描画 --------------------
    try:
        if not session.get("ext_user_id"):
            fl = session.get("_flashes")
            if isinstance(fl, list) and fl:
                def _is_unverified_banner(t):
                    try:
                        cat, msg = t
                        cat_str = str(cat or "")
                        # 複数カテゴリ対応: "warning unverified_email" など
                        if "unverified_email" in cat_str:
                            return True
                        s = str(msg)
                        return ("warning" in cat_str) and (
                            "メール確認のお願い" in s
                            or "メールアドレスの確認が完了していません" in s
                        )
                    except Exception:
                        return False
                new_fl = [t for t in fl if not _is_unverified_banner(t)]
                if len(new_fl) != len(fl):
                    session["_flashes"] = new_fl
    except Exception:
        try:
            current_app.logger.exception("cleanup flashes for anonymous failed")
        except Exception:
            pass
    # -----------------------------------------------------------------------

    # ---- ログイン済みなら「未確認」バナーをここでだけ出す ------------------------
    try:
        uid = session.get("ext_user_id")
        if uid:
            db = get_db(); cur = db.cursor(dictionary=True)
            try:
                cur.execute("""
                    SELECT email, email_verified_at
                      FROM external_login_user
                     WHERE id=%s
                     LIMIT 1
                """, (uid,))
                row = cur.fetchone()
            finally:
                try: cur.close(); db.close()
                except Exception: pass

            email = (row.get("email") or "").strip() if row else ""
            unverified = bool(email and row and not row.get("email_verified_at"))
            if unverified:
                from markupsafe import Markup, escape
                msg = (
                    "メールアドレスの確認が完了していません。\n"
                    "イベント管理システムからお送りした『メールアドレス確認のお願い』内のリンクをタップして認証を完了してください。\n"
                    "届いていない場合は迷惑メールをご確認のうえ、下のボタンで再送できます。"
                )
                body = Markup("<br>".join(escape(msg).split("\n")))
                resend_url = url_for("external_login_user.resend_verify_email")
                btn = Markup(
                    f'''<form method="post" action="{escape(resend_url)}" style="display:inline;margin-left:8px;">
                            <button type="submit" class="btn btn-sm btn-primary">確認メールを再送する</button>
                        </form>'''
                )
                # ★ カテゴリ: "warning unverified_email"（黄色スタイル + 後段の特定しやすさ）
                flash(
                    Markup(
                        '<div style="font-weight:600;">メール確認のお願い</div>'
                        f'<div style="margin-top:4px;line-height:1.6;">{body} {btn}</div>'
                    ),
                    "warning unverified_email"
                )
    except Exception:
        try:
            current_app.logger.exception("top banner check failed")
        except Exception:
            pass
    # -----------------------------------------------------------------------

    social_id = session.get("ext_user_social_id")
    me = _get_ext_user_by_social(social_id) if social_id else None

    events_upcoming, events_past = [], []
    if me:
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("""
          SELECT
            e.id, e.event_uuid, e.title,
            e.starts_at, e.fee_yen, e.album_id,
            COALESCE(m.status,'pending')                        AS status,
            COALESCE(m.payment_status,'unpaid')                 AS payment_status,
            m.receipt_url,
            CAST(COALESCE(m.require_payment,1) AS UNSIGNED)     AS require_payment
          FROM mfu_event e
          JOIN (
              SELECT MAX(id) AS id, event_id
                FROM mfu_event_member
               WHERE user_id = %s
               GROUP BY event_id
          ) mm ON mm.event_id = e.id
          JOIN mfu_event_member m ON m.id = mm.id
          ORDER BY e.starts_at IS NULL, e.starts_at
          LIMIT 200
        """, (me["id"],))
        raws = cur.fetchall()
        cur.close(); db.close()

        from datetime import datetime as _dt
        now = _dt.now()

        def _to_dt(v):
            try:
                if hasattr(v, "year"): return v
                return _dt.fromisoformat(str(v).replace(" ", "T"))
            except Exception:
                return None

        for r in raws:
            euuid_str = _uuid_bytes_to_str(r["event_uuid"])
            item = {
                "id": r["id"],
                "event_uuid_str": euuid_str,
                "title": r["title"],
                "starts_at": r["starts_at"],
                "status": r["status"] or "pending",
                "payment_status": r["payment_status"] or "unpaid",
                "receipt_url": r["receipt_url"],
                "fee_yen": r["fee_yen"],
                "album_id": r["album_id"],
                "album_url": (url_for("album.album_access", album_id=r["album_id"]) if r["album_id"] else None),
                "pay_url": url_for("external_login_user.pay_start", event_uuid=euuid_str),
                "require_payment": int(r["require_payment"]),
                "my_payment_status": r["payment_status"] or "unpaid",
                "my_receipt_url": r["receipt_url"],
            }

            dt = _to_dt(r["starts_at"])
            if dt is None:
                events_past.append(item)
            else:
                (events_upcoming if dt.date() >= now.date() else events_past).append(item)

        def _key_asc(x):
            from datetime import datetime as _dt
            v = x.get("starts_at")
            try:
                return v if hasattr(v, "year") else _dt.fromisoformat(str(v).replace(" ", "T"))
            except Exception:
                return _dt.max

        def _key_desc(x):
            from datetime import datetime as _dt
            v = x.get("starts_at")
            try:
                return v if hasattr(v, "year") else _dt.fromisoformat(str(v).replace(" ", "T"))
            except Exception:
                return _dt.min

        events_upcoming.sort(key=_key_asc)
        events_past.sort(key=_key_desc, reverse=True)

    resp = make_response(render_template("ext_index.html",
                           login=bool(me), me=me,
                           events_upcoming=events_upcoming,
                           events_past=events_past))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/me")
def me():
    return redirect(url_for("external_login_user.index"))


# =========================
# LINEログイン（単一定義）
# =========================
def _state_signer():
    # Flask の secret_key を使って署名
    return URLSafeSerializer(current_app.secret_key, salt="line-state-v2")

def _ua_sha256(ua: str) -> str:
    return hashlib.sha256((ua or "").encode("utf-8")).hexdigest()

def _client_ip_prefix(addr: str) -> str:
    """
    端末の IP をプレフィックスに丸めて格納。
    - IPv6: /64 に丸める（モバイル環境での変動をある程度許容）
    - IPv4: /24 に丸める
    """
    try:
        ip = ip_address(addr)
        if isinstance(ip, IPv6Address):
            net = ip_network(str(ip) + "/64", strict=False)
        else:
            net = ip_network(str(ip) + "/24", strict=False)
        return str(net)
    except Exception:
        return ""

def _sanitize_next(candidate: str) -> str:
    # 絶対URLは禁止、/external-login 配下のみ許可
    if not isinstance(candidate, str):
        return url_for(".index")
    if candidate.startswith(("http://", "https://")):
        return url_for(".index")
    if not candidate.startswith("/external-login"):
        return url_for(".index")
    return candidate

def _now_ts() -> int:
    return int(time.time())

# ---（任意）リプレイ防止の簡易フック：使うならDB or メモリにJTIを記録して検出 ---
def _is_jti_used(jti: str) -> bool:
    # TODO: DBに oauth_used_jti(jti, used_at) を持てば確実
    return False

def _mark_jti_used(jti: str):
    # TODO: DBへ INSERT しておく
    pass


@bp.route("/line/login")
def line_login():
    # 1) next を安全化
    raw_next = (request.args.get("next") or session.get("ext_after_login_next") or request.referrer or "").strip()

    def _to_local_next(u: str) -> str | None:
        if not u:
            return None
        if u.startswith("/") and not u.startswith("//"):
            return u
        try:
            p = urlparse(u)
            if (p.path or "").startswith("/") and "/external-login/" in (p.path or ""):
                return p.path + (("?" + p.query) if p.query else "")
        except Exception:
            pass
        return None

    local_next = _to_local_next(raw_next) or "/external-login/"
    session["ext_after_login_next"] = local_next  # ← セッションにも保持

    # 2) 署名付き state を作って callback で検証できるようにする
    state_payload = {
        "n": local_next,                               # next（相対URL）
        "ip": _client_ip_prefix(request.remote_addr or ""),
        "ua": _ua_sha256(request.headers.get("User-Agent", "")),
        "t": int(time.time()),                         # 発行時刻
        "jti": secrets.token_urlsafe(12),              # リプレイ対策の一意ID（任意）
    }
    state_token = _state_signer().dumps(state_payload)

    # 3) LINE 認可ページへ（state を必ず付ける）
    redirect_uri = LINE_REDIRECT_URI() if callable(LINE_REDIRECT_URI) else LINE_REDIRECT_URI
    return oauth.line.authorize_redirect(redirect_uri=redirect_uri, state=state_token)  # type: ignore[arg-type]

@bp.route("/line/callback")
def line_callback():
    # ---- state 検証 ----
    state_token = request.args.get("state")
    if not state_token:
        flash("LINEログインに失敗しました。（stateなし）", "error")
        return redirect(url_for("external_login_user.index"))

    try:
        payload = _state_signer().loads(state_token)
    except BadSignature:
        flash("LINEログインに失敗しました。（state改ざん）", "error")
        return redirect(url_for("external_login_user.index"))

    next_path = _sanitize_next(payload.get("n"))
    ip_expected = payload.get("ip", "")
    ua_expected = payload.get("ua", "")
    issued_at = int(payload.get("t", 0) or 0)
    jti = payload.get("jti", "")

    if _now_ts() - issued_at > 15 * 60:
        next_path = url_for(".index")
    if jti and _is_jti_used(jti):
        next_path = url_for(".index")
    else:
        _mark_jti_used(jti or "")

    if ip_expected and _client_ip_prefix(request.remote_addr or "") != ip_expected:
        next_path = url_for(".index")
    if ua_expected and _ua_sha256(request.headers.get("User-Agent", "")) != ua_expected:
        next_path = url_for(".index")

    # ---- トークン交換 & プロフィール取得 ----
    code = request.args.get("code")
    if not code:
        flash("LINEログインに失敗しました。（codeなし）", "error")
        return redirect(url_for("external_login_user.index"))

    try:
        redirect_uri = LINE_REDIRECT_URI() if callable(LINE_REDIRECT_URI) else LINE_REDIRECT_URI
        token = oauth.line.fetch_access_token(grant_type="authorization_code", code=code, redirect_uri=redirect_uri)
        if isinstance(token, dict):
            token.pop("id_token", None)
        prof = oauth.line.get("https://api.line.me/v2/profile", token=token).json()
    except Exception:
        current_app.logger.exception("LINE token/profile error")
        flash("LINEログインに失敗しました。", "error")
        return redirect(url_for("external_login_user.index"))

    sub = (prof or {}).get("userId")
    if not sub:
        flash("LINEユーザーIDを取得できませんでした。", "error")
        return redirect(url_for("external_login_user.index"))

    session["ext_user_social_id"] = sub

    # ---- 画像ダウンロード→サーバー保存（5MBまで / GIFはそのまま、他は最大辺1024px）----
    def _download_and_save_avatar(url: str, *, timeout: int = 8) -> str | None:
        if not url or not isinstance(url, str):
            return None
        try:
            r = requests.get(url, timeout=timeout, stream=True, headers={"User-Agent": "MFU/1.0"})
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            # 5MB上限
            max_bytes = 5 * 1024 * 1024
            data = b""
            for chunk in r.iter_content(256 * 1024):
                if not chunk:
                    break
                data += chunk
                if len(data) > max_bytes:
                    current_app.logger.warning("avatar download too large: %s", url)
                    return None

            from io import BytesIO
            try:
                if not ctype or ctype == "application/octet-stream":
                    with Image.open(BytesIO(data)) as im:
                        fmt = (im.format or "").upper()
                    if fmt == "PNG":
                        ctype = "image/png"
                    elif fmt == "WEBP":
                        ctype = "image/webp"
                    elif fmt == "GIF":
                        ctype = "image/gif"
                    else:
                        ctype = "image/jpeg"
            except Exception:
                ctype = ctype or "image/jpeg"

            # 拡張子
            if ctype == "image/png":
                ext = ".png"
            elif ctype == "image/webp":
                ext = ".webp"
            elif ctype == "image/gif":
                ext = ".gif"
            else:
                ext = ".jpg"

            name = f"{uuid.uuid4().hex[:10]}{ext}"
            out_path = AVATAR_ROOT / name
            out_path.parent.mkdir(parents=True, exist_ok=True)

            if ctype == "image/gif":
                with open(out_path, "wb") as f:
                    f.write(data)
                return name

            with Image.open(BytesIO(data)) as im:
                im = im.convert("RGB")
                im.thumbnail((1024, 1024))
                if ctype == "image/png":
                    im.save(out_path, format="PNG", optimize=True)
                elif ctype == "image/webp":
                    im.save(out_path, format="WEBP", quality=85, method=6)
                else:  # JPEG
                    im.save(out_path, format="JPEG", quality=85, optimize=True, progressive=True)
            return name
        except Exception:
            current_app.logger.exception("avatar download failed: %s", url)
            return None

    # ---- ユーザ upsert + 初回判定 ----
    db = get_db(); cur = db.cursor()
    try:
        # email を含めて取得（既存未保存時に保存するため avatar_file / avatar_url も取る）
        cur.execute("""
            SELECT id, nickname, avatar_file, avatar_url, email
            FROM external_login_user
            WHERE social_id=%s
            LIMIT 1
        """, (sub,))
        row = cur.fetchone()
        onboarding = False
        needs_email = False

        picture_url = (prof.get("pictureUrl") or "")  # LINE側画像URL

        if not row:
            # 新規作成
            nick_new = (prof.get("displayName") or "（未設定）")
            cur.execute("""
                INSERT INTO external_login_user
                  (mfu_uuid, social_id, nickname, x_id, instagram_id, email, avatar_url, avatar_file)
                VALUES (UNHEX(REPLACE(UUID(),'-','')), %s, %s, NULL, NULL, NULL, %s, NULL)
            """, (sub, nick_new, picture_url))
            db.commit()
            onboarding = True          # ← 初回のみ True
            needs_email = True         # ← 新規はメール未登録扱い

            ext_user_id = cur.lastrowid
            nickname_for_log = nick_new

            # ここでLINE画像をサーバー保存
            if picture_url:
                saved = _download_and_save_avatar(picture_url)
                if saved:
                    try:
                        cur.execute(
                            "UPDATE external_login_user SET avatar_file=%s, updated_at=NOW() WHERE id=%s LIMIT 1",
                            (saved, ext_user_id)
                        )
                        db.commit()
                    except Exception:
                        try: db.rollback()
                        except Exception: pass
                        current_app.logger.exception("save avatar at signup failed")

        else:
            # 既存
            if isinstance(row, tuple):
                ext_user_id, nickname_existing, a_file_existing, a_url_existing, email_existing = \
                    row[0], row[1], row[2], row[3], (row[4] or "")
            else:
                ext_user_id = row["id"]
                nickname_existing = row["nickname"]
                a_file_existing = row.get("avatar_file")
                a_url_existing = row.get("avatar_url")
                email_existing = (row.get("email") or "")

            onboarding = not bool(nickname_existing and str(nickname_existing).strip())
            nickname_for_log = nickname_existing or "（未設定）"

            # メール未登録かどうか（空/NULL・@なしを未登録と判定）
            needs_email = not bool(email_existing and "@" in email_existing)

            # avatar_file未設定なら、LINEのURLか既存avatar_urlからダウンロードして保存
            if not a_file_existing:
                cand_url = picture_url or a_url_existing
                if cand_url:
                    saved = _download_and_save_avatar(cand_url)
                    if saved:
                        try:
                            cur.execute(
                                "UPDATE external_login_user SET avatar_file=%s, updated_at=NOW() WHERE id=%s LIMIT 1",
                                (saved, ext_user_id)
                            )
                            db.commit()
                        except Exception:
                            try: db.rollback()
                            except Exception: pass
                            current_app.logger.exception("save avatar for existing user failed")

        # ---- ここで [LINE_LOGIN] ログを1行追記（logsテーブルのみ） ----
        ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                      or request.remote_addr or "-")
        log_text = f"[LINE_LOGIN] ユーザー: #{ext_user_id}　{nickname_for_log}　がログインしました"
        try:
            cur.execute(
                "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
                (ip_for_log, log_text)
            )
            db.commit()
        except Exception:
            # ログ書き込み失敗は握りつぶし
            current_app.logger.warning("write [LINE_LOGIN] log failed", exc_info=True)

    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # セッションへ格納
    session["ext_user_id"] = ext_user_id
    session["ext_user_nickname"] = nickname_for_log

    # 追加：LINEログインは長期セッション扱いにする
    session.permanent = True

    # 既存の next を壊さない（上書きしない）
    session.setdefault("ext_after_login_next", next_path)

    # ---- リダイレクト方針 ----
    # ・初回: ext_user_onboarding=True（テンプレの「初回だけプロフィール作成…」を出したいケース）
    # ・メール未登録のみ: ext_user_need_email=True（初回メッセージは出さず、メール注意喚起のみ）
    if onboarding:
        session["ext_user_onboarding"] = True
    else:
        session.pop("ext_user_onboarding", None)

    if needs_email:
        session["ext_user_need_email"] = True
        # 改行を反映させたフラッシュ（<br>化）
        from markupsafe import Markup, escape
        email_notice = (
            "クレジットカード決済時のメールアドレスの入力について\n\n"
            "クレジットカード会社の規定変更により、クレジットカードでの決済時に、カード名義人のメールアドレスの入力が必要となります。\n"
            "入力されたメールアドレスは、カード決済時の本人認証処理にて、クレジットカードの不正利用の検知・防止のため、カード発行会社に提供いたします。\n\n"
            "ご理解、ご協力お願いいたします。\n\n"
            "登録したメールアドレスに、確認用のメールが送信されます。ご対応お願いいたします。\n"
        )
        # \n を <br> にして安全にマーク
        email_notice_markup = Markup("<br>".join(escape(email_notice).split("\n")))
        flash(email_notice_markup, "info")
        # プロフィール画面へ誘導（reason=email を付与しておくとテンプレ側で出し分けもしやすい）
        return redirect(url_for("external_login_user.profile", next=next_path, reason="email"))
    else:
        session.pop("ext_user_need_email", None)

    # メール登録済みなら通常遷移
    return redirect(next_path or session.pop("ext_after_login_next", None) or url_for("external_login_user.index"))

# =========================
# プロフィール（CSRF, 画像アップ対応・メール確認送信対応）
# =========================
@bp.route("/profile", methods=["GET", "POST"])
def profile():
    """
    外部参加者のプロフィール編集。
    - 画像アップロード: <input type="file" name="avatar_file">
    - CSRF: session["ext_csrf"] と hidden input csrf_token を比較
    - メールアドレス: 変更時は email_verified_at をクリアし確認メール送信
    - 保存後は ext_after_login_next（join等）に戻す
    - ★通知設定: notify_album_upload / notify_album_process をON/OFF保存
    - ★決済モード: payment_mode (manual / auto)
    """
    # ===== ログイン必須 =====
    social_id = session.get("ext_user_social_id")
    if not social_id:
        return redirect(url_for(
            "external_login_user.line_login",
            next=session.get("ext_after_login_next") or request.url
        ))

    # ===== CSRF 準備 =====
    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)
    csrf_token = session["ext_csrf"]

    # ===== ユーザー取得（編集に必要な項目を全部）=====
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id,
               nickname,
               x_id,
               instagram_id,
               email,
               avatar_url,
               avatar_file,
               email_verified_at,
               updated_at,
               COALESCE(payment_mode, 'manual') AS payment_mode,
               COALESCE(notify_album_upload, 1)  AS notify_album_upload,
               COALESCE(notify_album_process, 1) AS notify_album_process
          FROM external_login_user
         WHERE social_id=%s
         LIMIT 1
        """,
        (social_id,),
    )
    me = cur.fetchone()
    if not me:
        cur.close()
        db.close()
        return redirect(
            url_for(
                "external_login_user.line_login",
                next=session.get("ext_after_login_next") or request.url,
            )
        )

    # --- avatar_file → avatar_url 補完（テンプレは avatar_url を参照想定） ---
    try:
        a_file = (me.get("avatar_file") or "").strip()
        a_url = (me.get("avatar_url") or "").strip()
        if a_file and not a_url:
            me["avatar_url"] = url_for("external_login_user.avatar_file", name=a_file)
    except Exception:
        current_app.logger.exception("profile: avatar url complement failed")

    # --- デフォルトカード情報（あれば） ---
    card_summary: str | None = None
    has_card = False
    try:
        cur.execute(
            """
            SELECT card_brand, last4, exp_month, exp_year
              FROM external_login_user_card_data
             WHERE user_id=%s
               AND deleted_at IS NULL
             ORDER BY is_default DESC, id DESC
             LIMIT 1
            """,
            (me["id"],),
        )
        card = cur.fetchone()
        if card:
            brand = (card.get("card_brand") or "").upper()
            last4 = card.get("last4") or "****"
            mm = card.get("exp_month")
            yy = card.get("exp_year")
            if mm and yy:
                try:
                    card_summary = f"{brand} ****{last4} (有効期限: {int(mm):02d}/{yy})"
                except Exception:
                    card_summary = f"{brand} ****{last4}"
            else:
                card_summary = f"{brand} ****{last4}"
            has_card = True
    except Exception:
        current_app.logger.exception("profile: load card summary failed")
        card_summary = None
        has_card = False

    # --- 表示用の avatar_src を生成（updated_at でキャッシュバスター付与） ---
    def _build_avatar_src(m: dict) -> str | None:
        ver = None
        try:
            if m.get("updated_at") and hasattr(m.get("updated_at"), "timestamp"):
                ver = int(m["updated_at"].timestamp())  # type: ignore[attr-defined]
        except Exception:
            pass
        a_file_i = (m.get("avatar_file") or "").strip()
        a_url_i = (m.get("avatar_url") or "").strip()
        if a_file_i:
            base = url_for("external_login_user.avatar_file", name=a_file_i)
            return f"{base}?v={ver}" if ver else base
        if a_url_i:
            if ver:
                sep = "&" if "?" in a_url_i else "?"
                return f"{a_url_i}{sep}v={ver}"
            return a_url_i
        return None

    avatar_src = _build_avatar_src(me)

    # 初回判定（セッション or ニックネーム未設定）
    onboarding = bool(session.get("ext_user_onboarding")) \
        or not (me.get("nickname") and str(me.get("nickname")).strip()) \
        or me.get("nickname") == "（未設定）"

    # ===== GET: フォーム表示 =====
    if request.method == "GET":
        # ?next= が来たら join 戻し用に保存（安全化）
        raw_next = (request.args.get("next") or "").strip()
        from urllib.parse import urlparse

        def _to_local_next(u: str) -> str | None:
            if not u:
                return None
            if u.startswith("/") and not u.startswith("//"):
                return u
            try:
                p = urlparse(u)
                if (p.path or "").startswith("/external-login/"):
                    return p.path + (("?" + p.query) if p.query else "")
            except Exception:
                pass
            return None

        if raw_next:
            local_next = _to_local_next(raw_next)
            if local_next:
                # ガード：現在のセッションに「イベント参加URL」がある場合、
                # 新しい next が単なるマイページ（/external-login/）なら上書きしない
                current_next = session.get("ext_after_login_next") or ""
                is_current_join = "/events/join/" in current_next
                is_new_simple = local_next.strip() in ("/external-login/", "/external-login")

                if not (is_current_join and is_new_simple):
                    session["ext_after_login_next"] = local_next
                    # メール確認後の戻り先としても同期しておく
                    session["ext_after_verify_next"] = local_next

        # 旧テンプレ互換の form データ
        form = {
            "nickname": ("" if (onboarding and (me.get("nickname") == "（未設定）")) else (me.get("nickname") or "")),
            "x_id": me.get("x_id") or "",
            "instagram_id": me.get("instagram_id") or "",
            "email": me.get("email") or "",
            "avatar_url": me.get("avatar_url") or "",
            # ★ 通知設定（未設定はTrueとして表示）
            "notify_album_upload": bool(int(me.get("notify_album_upload", 1))),
            "notify_album_process": bool(int(me.get("notify_album_process", 1))),
            # ★ 決済モード（手動 / 自動）
            "payment_mode": (me.get("payment_mode") or "manual"),
        }
        cur.close()
        db.close()
        return render_template(
            "ext_profile.html",
            me=me,
            form=form,
            errors={},
            onboarding=onboarding,
            csrf_token=csrf_token,
            next_url=(session.get("ext_after_login_next") or ""),
            avatar_src=avatar_src,
            card_summary=card_summary,
        )

    # ===== POST: 保存 =====
    # CSRF
    token = (request.form.get("csrf_token") or "").strip()
    if not token or token != csrf_token:
        cur.close()
        db.close()
        flash("フォームの有効期限が切れました。もう一度お試しください。", "warning")
        session["ext_csrf"] = secrets.token_hex(16)
        return render_template(
            "ext_profile.html",
            me=me,
            form=dict(request.form),
            errors={"csrf_token": "無効なトークンです。"},
            onboarding=onboarding,
            csrf_token=session["ext_csrf"],
            next_url=(session.get("ext_after_login_next") or ""),
            avatar_src=avatar_src,
            card_summary=card_summary,
        ), 400

    nickname = (request.form.get("nickname") or "").strip()
    x_id_raw = (request.form.get("x_id") or "").strip()
    ig_id_raw = (request.form.get("instagram_id") or "").strip()
    email_in = (request.form.get("email") or "").strip() or None

    # ★ 通知設定チェックボックス
    notify_upload = 1 if str(request.form.get("notify_album_upload", "")).lower() in ("on", "1", "true", "yes") else 0
    notify_process = 1 if str(request.form.get("notify_album_process", "")).lower() in ("on", "1", "true", "yes") else 0

    # ★ 決済モード（手動 / 自動）
    payment_mode = (request.form.get("payment_mode") or "manual").strip().lower()
    if payment_mode not in ("manual", "auto"):
        payment_mode = "manual"

    # 正規化
    x_id = x_id_raw.lstrip("@") or None
    ig_id = ig_id_raw.lstrip("@") or None

    # 画像アップロード
    avatar_file_in = request.files.get("avatar_file")  # type: ignore
    saved_avatar_file = _save_avatar(avatar_file_in) if (avatar_file_in and getattr(avatar_file_in, "filename", "")) else None

    # バリデーション
    import re as _re

    errors: dict[str, str] = {}
    if not nickname:
        errors["nickname"] = "ニックネームは必須です。"
    if x_id and not _re.fullmatch(r"[A-Za-z0-9_]{1,15}", x_id):
        errors["x_id"] = "X IDは @なし、半角英数と_で1〜15文字。"
    if ig_id:
        ok = _re.fullmatch(r"[A-Za-z0-9._]{1,30}", ig_id) and not ig_id.startswith(".") and not ig_id.endswith(".") and ".." not in ig_id
        if not ok:
            errors["instagram_id"] = "Instagram IDは英数・.・_で1〜30文字。先頭/末尾の . と .. は不可。"
    if onboarding and not (x_id or ig_id):
        errors["x_or_ig"] = "初回登録では X ID か Instagram ID のどちらかを入力してください。"
    if avatar_file_in and getattr(avatar_file_in, "filename", "") and not saved_avatar_file:
        errors["avatar_file"] = "対応していない画像形式です。PNG/JPEG/WEBP/GIF をアップロードしてください。"

    # 自動決済 + カード未登録ならエラー
    if payment_mode == "auto" and not has_card:
        errors["payment_mode"] = "自動決済を利用するにはカード情報の登録が必要です。"

    if errors:
        cur.close()
        db.close()
        session["ext_csrf"] = secrets.token_hex(16)
        a_url_now = _build_avatar_src(me) or ""
        form_back = {
            "nickname": nickname,
            "x_id": x_id_raw,
            "instagram_id": ig_id_raw,
            "email": email_in or "",
            "avatar_url": me.get("avatar_url") or "",
            # ★ エラー戻しでも選択を保持
            "notify_album_upload": bool(notify_upload),
            "notify_album_process": bool(notify_process),
            "payment_mode": payment_mode,
        }
        return render_template(
            "ext_profile.html",
            me=me,
            form=form_back,
            errors=errors,
            onboarding=onboarding,
            csrf_token=session["ext_csrf"],
            next_url=(session.get("ext_after_login_next") or ""),
            avatar_src=a_url_now,
            card_summary=card_summary,
        ), 400

    # ===== DB 更新（nickname/x/ig/email/通知設定/決済モード）=====
    old_email = (me.get("email") or "") if isinstance(me, dict) else ""
    was_verified = bool(me.get("email_verified_at"))
    email_changed = bool(email_in and (old_email.strip().lower() != (email_in or "").strip().lower()))

    try:
        if email_changed:
            cur.execute(
                """
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       email_verified_at=NULL,
                       payment_mode=%s,
                       notify_album_upload=%s,
                       notify_album_process=%s,
                       updated_at=NOW()
                 WHERE id=%s
                 LIMIT 1
                """,
                (nickname, x_id, ig_id, email_in, payment_mode, notify_upload, notify_process, me["id"]),
            )
        else:
            cur.execute(
                """
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       payment_mode=%s,
                       notify_album_upload=%s,
                       notify_album_process=%s,
                       updated_at=NOW()
                 WHERE id=%s
                 LIMIT 1
                """,
                (nickname, x_id, ig_id, email_in, payment_mode, notify_upload, notify_process, me["id"]),
            )
        db.commit()

        if saved_avatar_file:
            cur.execute(
                "UPDATE external_login_user SET avatar_file=%s, updated_at=NOW() WHERE id=%s LIMIT 1",
                (saved_avatar_file, me["id"]),
            )
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        cur.close()
        db.close()
        flash("保存に失敗しました。時間をおいて再度お試しください。", "danger")
        return redirect(url_for("external_login_user.profile"))

    # 初回フラグを落とす
    if onboarding:
        session["ext_user_onboarding"] = False

    # ===== リダイレクト（join 等へ）=====
    next_url = (
        session.get("ext_after_login_next")
        or (request.args.get("next") or "").strip()
        or (request.form.get("next") or "").strip()
    )

    # ===== メール確認メール送信 =====
    needs_verify = False
    try:
        if email_in and (email_changed or (email_in and not was_verified)):
            # next_url を DB に保存するために渡す
            t_raw = _issue_email_verify_token(me["id"], email_in, redirect_url=next_url)  # 24h
            _send_verify_mail(email_in, t_raw)
            flash("確認メールを送信しました。受信ボックスをご確認ください。", "info")
            needs_verify = True
    except Exception:
        current_app.logger.exception("send verify mail failed")

    # メール確認が必要な場合、確認完了後に戻る先をセッションに保存しておく
    if needs_verify and next_url:
        session["ext_after_verify_next"] = next_url

    # イベント参加URL（/join/）が含まれている場合は、完了するまで保持し続けたいので pop しない
    current_next = session.get("ext_after_login_next") or ""
    is_join_url = "/events/join/" in current_next

    # メール確認が不要、かつイベント参加URLでもない場合のみ pop する
    if not needs_verify and not is_join_url:
        session.pop("ext_after_login_next", None)

    def _is_safe_local(url: str) -> bool:
        return bool(url) and url.startswith("/") and not url.startswith("//")

    if not _is_safe_local(next_url):
        next_url = url_for("external_login_user.index")

    cur.close()
    db.close()
    flash("プロフィールを保存しました。", "success")
    return redirect(next_url)


# =========================
# 参加（承認制）・閲覧
# =========================
@bp.route("/line-login")
def line_login_shortcut():
    return redirect(url_for("external_login_user.line_login", **request.args), code=302)


@bp.route("/events/join/<event_uuid>", methods=["GET", "POST"])
def join_event(event_uuid: str):
    """
    仕様:
      - 未ログイン時は LINE ログインへ。next に現在URL(含iv)を渡す。
      - ログイン後/初回プロフィール作成後も常にこのフォームへ戻す。
      - GET: 既存メンバーは状態表示、未申請は「役割/衣装」入力フォームを表示。
      - POST: 自動承認トークン(iv)が一致すれば 'approved'、なければ 'pending' で upsert。
              送信後はイベントページ（/events/view/<uuid>）へ遷移。
    """
    import secrets

    # --- ログインチェック（next でこのページに戻す。iv も保持される） ---
    social_id = session.get("ext_user_social_id")
    if not social_id:
        session["ext_after_login_next"] = request.url
        login_url = url_for("external_login_user.line_login", next=request.url, _external=False)
        return redirect(login_url)

    # --- CSRF トークン ---
    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)
    csrf_token = session["ext_csrf"]

    db = get_db(); cur = db.cursor(dictionary=True)

    # --- 外部ユーザーを取得（social_id → external_login_user.id） ---
    cur.execute("""
        SELECT id, email, nickname, x_id, instagram_id
          FROM external_login_user
         WHERE social_id=%s
         LIMIT 1
    """, (social_id,))
    u = cur.fetchone()
    if not u:
        login_url = url_for("external_login_user.line_login", next=request.url, _external=False)
        return redirect(login_url)

    ext_uid  = u["id"]
    to_email = u.get("email")
    nick     = u.get("nickname")
    xid      = u.get("x_id") or ""
    igid     = u.get("instagram_id") or ""

    # --- プロフィール未設定チェック ---
    # ニックネームが未設定、または「（未設定）」の場合はプロフィール編集へ誘導
    is_profile_incomplete = not (nick and str(nick).strip()) or nick == "（未設定）"
    if is_profile_incomplete:
        session["ext_after_login_next"] = request.url
        flash("イベントに参加する前に、プロフィールの作成をお願いします。", "info")
        return redirect(url_for("external_login_user.profile", next=request.url))

    # --- イベント本体（テンプレが参照する列は必ず選ぶ） ---
    cur.execute("""
      SELECT
        id, title, event_uuid,
        starts_at, fee_yen, place_name, address,
        COALESCE(auto_approve_by_invite,0) AS auto_on,
        invite_token
      FROM mfu_event
      WHERE event_uuid = UNHEX(REPLACE(%s,'-',''))
      LIMIT 1
    """, (event_uuid,))
    ev = cur.fetchone()
    if not ev:
        cur.close(); db.close()
        abort(404, "event not found")

    # 表示用 UUID 文字列 / 管理URL
    ev_uuid_str = _uuid_bytes_to_str(ev["event_uuid"])
    ev["event_uuid_str"] = ev_uuid_str
    admin_url = f"https://mfu.iori0624.jp/external-login/admin/events/{ev['id']}"

    # 既存メンバー状況
    cur.execute("""
        SELECT id, COALESCE(status,'pending') AS status,
               COALESCE(participant_role,'none') AS participant_role,
               costume_label
          FROM mfu_event_member
         WHERE event_id=%s AND user_id=%s
         LIMIT 1
    """, (ev["id"], ext_uid))
    m = cur.fetchone()

    # 招待トークン一致（GET/POSTどちらでも query の iv を見て判定）
    iv = (request.args.get("iv") or "").strip()
    auto_hit = bool(int(ev["auto_on"] or 0) == 1 and ev.get("invite_token") and iv and iv == ev["invite_token"])

    # =========================
    # POST: フォーム送信（申請）
    # =========================
    if request.method == "POST":
        # CSRF
        token = request.form.get("csrf_token", "")
        if not token or token != csrf_token:
            cur.close(); db.close()
            abort(400, "invalid csrf token")

        role = (request.form.get("participant_role") or "cosplayer").strip().lower()
        # ★ 'other' を許可
        if role not in ("camera", "assistant", "cosplayer", "other"):
            role = "cosplayer"

        costume = (request.form.get("costume_label") or "").strip() or None
        # ★ 「衣装／その他」のときだけ保持
        if role not in ("cosplayer", "other"):
            costume = None  # サーバ側でも空に

        # ステータス決定（自動承認 or 手動承認待ち）
        new_status = "approved" if auto_hit else "pending"

        # upsert
        if m:
            cur.execute("""
                UPDATE mfu_event_member
                   SET status=%s,
                       participant_role=%s,
                       costume_label=%s,
                       joined_at=COALESCE(joined_at, NOW())
                 WHERE id=%s AND event_id=%s
                 LIMIT 1
            """, (new_status, role, costume, m["id"], ev["id"]))
        else:
            cur.execute("""
                INSERT INTO mfu_event_member
                  (event_id, user_id, status, participant_role, costume_label, joined_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (ev["id"], ext_uid, new_status, role, costume))
        db.commit()

        # ===== 管理者メール収集（ACLメンバー） =====
        try:
            cur.execute("""
              SELECT u.email
                FROM mfu_event_admin_acl a
                JOIN users u ON u.username=a.username
               WHERE a.event_id=%s
                 AND COALESCE(a.role,'viewer') IN ('viewer','manager')
                 AND u.email IS NOT NULL
            """, (ev["id"],))
            admin_emails = [r["email"] for r in (cur.fetchall() or []) if r.get("email")]
        except Exception:
            admin_emails = []
        finally:
            try:
                cur.close(); db.close()
            except Exception:
                pass

        # ===== 参加者メール（件名はすでに【イベント名】統一済み） =====
        try:
            if to_email:
                if new_status == "approved":
                    send_mail(
                        to=to_email,
                        subject=f"【{ev['title']}】参加が承認されました",
                        body=(f"{nick or '参加者'} 様\n\n"
                              f"イベント「{ev['title']}」への参加が承認されました。\n"
                              f"当日のご参加をお待ちしております！\n"
                              f"https://mfu.iori0624.jp/external-login/events/view/{ev_uuid_str}\n"),
                        event_uuid=ev_uuid_str,
                    )
                else:
                    send_mail(
                        to=to_email,
                        subject=f"【{ev['title']}】参加申請を受け付けました（承認待ち）",
                        body=(f"{nick or '参加者'} 様\n\n"
                              f"イベント「{ev['title']}」への参加申請を受け付けました。\n"
                              f"主催の承認後にご案内いたします。\n"
                              f"申請状況は以下のページで確認できます。\n"
                              f"https://mfu.iori0624.jp/external-login/events/view/{ev_uuid_str}\n"),
                        event_uuid=ev_uuid_str,
                    )
        except Exception:
            current_app.logger.exception("join: user mail failed")

        # ===== 管理者メール（ACLメンバーのみ）＋ Discord =====
        try:
            # 役割の日本語化（'other' を追加）
            role_jp = {"camera": "カメラマン", "assistant": "アシスタント", "cosplayer": "衣装", "other": "その他"}.get(role, "衣装")
            costume_line = (costume or "")
            kind_label = "自動承認" if new_status == "approved" and auto_hit else "承認待ち"

            # 件名（種別を併記）
            subject_admin = f"【{ev['title']}】新しい参加申請があります（{kind_label}）"

            # 本文（ご指定の体裁＋申請種別を追記）
            body_text = (
                "新しい参加申請があります。\n"
                "\n"
                f"イベント名：{ev['title']}\n"
                f"申請者：{nick or '(名前未設定)'}\n"
                f"X ID：{xid}\n"
                f"IG ID：{igid}\n"
                "\n"
                f"役割：{role_jp}\n"
                f"衣装：{costume_line}\n"
                "\n"
                "詳細は管理画面をご確認ください。\n"
                f"管理URL: {admin_url}\n"
                "\n"
                f"申請種別：{kind_label}\n"
            )

            # 管理者メール送信
            for em in {e for e in admin_emails if e}:
                send_mail(
                    to=em,
                    subject=subject_admin,
                    body=body_text,
                    event_uuid=ev_uuid_str,
                )

            # Discord も同文面
            wh = _get_admin_webhook_url()
            if wh:
                r = requests.post(wh, json={"content": body_text}, timeout=7)
                if r.status_code >= 300:
                    current_app.logger.error("join: discord webhook non-2xx %s, body=%s", r.status_code, r.text[:500])
            else:
                current_app.logger.warning("join: admin webhook_url not set")

        except Exception:
            current_app.logger.exception("join: admin mail/discord failed")

        # 送信後はイベントページへ
        if new_status == "pending":
            flash("参加申請を受け付けました。承認までお待ちください。", "success")
        else:
            flash("参加が承認されました。", "success")
        return redirect(url_for("external_login_user.view_event", event_uuid=ev_uuid_str))

    # =========================
    # GET: 表示
    # =========================
    for k in ("starts_at", "fee_yen", "place_name", "address"):
        ev.setdefault(k, None)

    status = (m and m.get("status")) or None
    form_role = (m and (m.get("participant_role") or "cosplayer")) or "cosplayer"
    form_costume = (m and (m.get("costume_label") or "")) or ""

    cur.close(); db.close()
    return render_template(
        "event_join.html",
        ev=ev,
        status=status,
        form_role=form_role,
        form_costume=form_costume,
        csrf_token=csrf_token,
    )


@bp.route("/events/view/<event_uuid>")
def view_event(event_uuid: str):
    guard = _require_ext_login()
    if guard:
        return guard

    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))
    if not me:
        abort(401)

    # 参加状況（最新行） ← 役割/衣装を追加で取得
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              COALESCE(m.status,'pending')                        AS status,
              CAST(COALESCE(m.require_payment,1) AS UNSIGNED)     AS require_payment,
              COALESCE(m.payment_status,'unpaid')                 AS payment_status,
              m.paid_amount_yen, m.paid_at, m.receipt_url,
              COALESCE(m.process,0) AS process,
              COALESCE(m.is_host,0) AS is_host,
              COALESCE(m.is_subhost,0) AS is_subhost,
              COALESCE(m.participant_role,'none') AS participant_role,
              COALESCE(m.costume_label,'')        AS costume_label
            FROM mfu_event_member m
            WHERE m.event_id=%s AND m.user_id=%s
            ORDER BY m.id DESC
            LIMIT 1
        """, (ev["id"], me["id"]))
        row = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    my_status              = (row["status"] if row else "pending") or "pending"
    my_require_payment     = int(row["require_payment"]) if row else 1
    my_payment_status      = (row["payment_status"] if row else "unpaid") or "unpaid"
    my_paid_amount_yen     = row.get("paid_amount_yen") if row else None
    my_paid_at             = row.get("paid_at") if row else None
    my_receipt_url         = row.get("receipt_url") if row else None
    my_process             = int(row.get("process")) if row else 0
    # ★ ここを追加：現在の役割/衣装（未設定時の既定値も整える）
    my_participant_role    = (row.get("participant_role") if row else "none") or "none"
    my_costume_label       = (row.get("costume_label")  if row else "") or ""

    # 表示モード
    if _is_mfu_logged_in():
        view_mode = "admin"
    else:
        if str(my_status).lower() == "approved":
            view_mode = "member"
        elif str(my_status).lower() in ("pending", "rejected"):
            view_mode = "member_limited"
        else:
            view_mode = "guest"

    # 各種リンク生成（略：元コードそのまま）
    album_url = url_for("album.album_access", album_id=ev["album_id"]) if ev.get("album_id") else None

    def _pick_http_url(*keys) -> str | None:
        for k in keys:
            v = ev.get(k)
            if isinstance(v, str):
                u = v.strip()
                if u.startswith(("http://", "https://")):
                    return u
        return None

    def _pick_str(*keys) -> str | None:
        for k in keys:
            v = ev.get(k)
            if isinstance(v, str):
                u = v.strip()
                if u:
                    return u
        return None

    openchat_url    = _pick_http_url("openchat_url", "line_openchat_url", "open_chat_url", "line_oc_url")
    openchat_pass   = _pick_str("openchat_password", "openchat_pass", "line_openchat_password", "line_openchat_pass", "oc_pass", "oc_password")

    # ===== アンケートURL =====
    # 指定があればそれを使う / なければデフォルトGoogleフォームに「日付＋全角スペース＋イベント名」を注入
    google_form_url = _pick_http_url("google_form_url", "survey_url", "form_url", "questionnaire_url", "googleform_url")

    if not google_form_url:
        DEFAULT_FORM_BASE = (
            "https://docs.google.com/forms/d/e/1FAIpQLSc9s9OjBr00GrOWcX8wFrd8DoqEB_-inVzWBfxhcMYxYZJyxg/"
            "viewform?usp=pp_url&entry.1842334381="
        )

        def _pick_event_ymd() -> str:
            for k in (
                "event_date", "date", "held_on", "held_date",
                "start_date", "start_at", "starts_at",
                "event_day", "day",
            ):
                v = ev.get(k)
                if not v:
                    continue

                if hasattr(v, "strftime"):
                    try:
                        return v.strftime("%Y/%m/%d")
                    except Exception:
                        pass

                s = str(v).strip()
                m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
                if m:
                    y, mo, d = m.groups()
                    return f"{int(y):04d}/{int(mo):02d}/{int(d):02d}"

            return "0000/00/00"

        def _pick_event_title() -> str:
            for k in ("title", "event_title", "name", "event_name"):
                v = ev.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        ymd = _pick_event_ymd()
        title = _pick_event_title()

        # yyyy/mm/dd（全角スペース）イベント名
        raw_value = f"{ymd}　{title}".rstrip()  # 全角スペース固定、末尾だけ整える

        # URLに載せるのでエンコード（スラッシュや全角スペースも安全に）
        google_form_url = DEFAULT_FORM_BASE + quote_plus(raw_value)


    maps_link = _pick_http_url("maps_url", "map_url", "google_maps_url", "gmap_url", "maplink", "mapslink")

    if not maps_link:
        def _num(v):
            try:
                return float(str(v).strip())
            except Exception:
                return None
        lat = ev.get("lat") or ev.get("latitude") or ev.get("geo_lat") or ev.get("lat_deg")
        lng = ev.get("lng") or ev.get("lon") or ev.get("longitude") or ev.get("geo_lng") or ev.get("geo_lon") or ev.get("lng_deg")
        lat = _num(lat); lng = _num(lng)
        if (lat is not None) and (lng is not None):
            maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"

    if not maps_link:
        q = _pick_str("maps_query", "map_query")
        if not q:
            parts = []
            name = ev.get("place_name") or ""
            addr = ev.get("address") or ""
            if isinstance(name, str) and name.strip():
                parts.append(name.strip())
            if isinstance(addr, str) and addr.strip():
                parts.append(addr.strip())
            q = " ".join(parts) if parts else None
        if q:
            maps_link = f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"

    resp = make_response(render_template(
        "event_view.html",
        ev=ev, me=me,
        view_mode=view_mode,
        my_status=my_status,
        my_require_payment=my_require_payment,
        my_payment_status=my_payment_status,
        my_paid_amount_yen=my_paid_amount_yen,
        my_paid_at=my_paid_at,
        my_receipt_url=my_receipt_url,
        my_process=my_process,
        album_url=album_url,
        maps_link=maps_link,
        openchat_url=openchat_url,
        openchat_pass=openchat_pass,
        google_form_url=google_form_url,
        line_openchat_url=openchat_url,
        survey_url=google_form_url,
        # ★ 追加でテンプレに渡す（フォーム初期値用）
        my_participant_role=my_participant_role,
        my_costume_label=my_costume_label,
        # 互換エイリアス（テンプレが form_* を参照していても動くように）
        form_role=my_participant_role,
        form_costume=my_costume_label,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# =========================
# 役割（参加者自身が編集可）
# =========================
@bp.post("/events/<event_uuid>/my-role")
def update_my_role(event_uuid: str):
    guard = _require_ext_login()
    if guard: return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    status = _membership_status(ev["id"], me["id"])  # type: ignore
    if status is None:
        abort(403, "このイベントの参加者ではありません")

    ui_role = (request.form.get("participant_role") or "none").strip().lower()
    # ★ UIとして許容（otherを追加）
    if ui_role not in ("none", "camera", "assistant", "cosplayer", "other"):
        ui_role = "none"

    costume = (request.form.get("costume_label") or "").strip() or None
    # ★ 「衣装 or その他」のときだけメモ保持
    keep_costume = ui_role in ("cosplayer", "other")
    if not keep_costume:
        costume = None

    if costume and len(costume) > 120:
        flash("衣装名は120文字以内で入力してください。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    db = get_db(); cur = db.cursor()
    # ★ DBのENUMに合わせて安全な値に正規化
    save_role, degraded = _normalize_role_for_db(db, ui_role)
    try:
        cur.execute("""
            UPDATE mfu_event_member
               SET participant_role=%s,
                   costume_label=%s
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (save_role, costume, ev["id"], me["id"]))  # type: ignore
        db.commit()
        if degraded:
            flash("注意：DBスキーマが 'other' 未対応のため、近い役割にフォールバックして保存しました。後日ENUMに 'other' を追加してください。", "warning")
        else:
            flash("役割を更新しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("役割の更新に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))


# =========================
# 加工回し（参加者自身が編集可）
# =========================
@bp.post("/events/<event_uuid>/process")
def update_my_process(event_uuid: str):
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    status = _membership_status(ev["id"], me["id"])  # type: ignore
    if status is None:
        abort(403, "このイベントの参加者ではありません")

    process_flag = 1 if request.form.get("process") in ("1", "on", "true") else 0

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            UPDATE mfu_event_member
               SET process=%s
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (process_flag, ev["id"], me["id"]))  # type: ignore
        db.commit()
        flash("加工回し設定を更新しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("加工回し設定の更新に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))


# =========================
# ログアウト
# =========================
@bp.route("/logout")
def logout():
    # 主要キーを削除
    for k in ("ext_user_id", "ext_user_social_id", "ext_user_nickname",
              "ext_after_login_next", "ext_user_onboarding",
              "ext_user_need_email", "ext_user_email_unverified"):
        session.pop(k, None)
    # フラッシュ全消し
    session.pop("_flashes", None)

    # 任意：ログアウト完了メッセージ（info）
    # flash("ログアウトしました。", "info")

    # トップへ
    return redirect(url_for("external_login_user.index"))


# =========================
# 参加者一覧（主催→副主催→他、各グループはニックネーム昇順）
# =========================
@bp.route("/events/<event_uuid>/members")
def member_list(event_uuid: str):
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    # 管理者はOK / 参加者は承認済みのみ
    if _is_mfu_logged_in():
        allow = True
    else:
        sid = session.get("ext_user_social_id")
        if not sid:
            flash("参加者または管理者のみ閲覧できます。まずは参加申請してください。", "warning")
            return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))
        me = _get_ext_user_by_social(sid)  # type: ignore
        allow = (_membership_status(ev["id"], me["id"]) == "approved")  # type: ignore
        if not allow:
            flash("承認後に閲覧できます。", "info")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT
              u.id,
              u.nickname,
              u.x_id,
              u.instagram_id,
              u.avatar_file,
              u.avatar_url,
              u.updated_at,
              m.participant_role,
              m.costume_label,
              m.is_host,
              m.is_subhost,
              m.checkin_at
            FROM mfu_event_member m
            JOIN external_login_user u ON u.id = m.user_id
            WHERE m.event_id = %s
              AND m.status = 'approved'
            ORDER BY
              m.is_host DESC,
              m.is_subhost DESC,
              u.nickname ASC
        """, (ev["id"],))
        rows = cur.fetchall()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    members: list[dict] = []
    for r in rows:
        if isinstance(r, tuple):
            (uid, nickname, x_id, ig_id, a_file, a_url, upd_at,
             role, costume, is_host, is_subhost, checkin_at) = r
        else:
            uid        = r["id"]
            nickname   = r["nickname"]
            x_id       = r["x_id"]
            ig_id      = r["instagram_id"]
            a_file     = r["avatar_file"]
            a_url      = r["avatar_url"]
            upd_at     = r["updated_at"]
            role       = r["participant_role"]
            costume    = r["costume_label"]
            is_host    = r["is_host"]
            is_subhost = r["is_subhost"]
            checkin_at = r["checkin_at"]

        # キャッシュバスター付きアイコンURL
        try:
            ver = int(upd_at.timestamp()) if upd_at else None  # type: ignore[attr-defined]
        except Exception:
            from datetime import datetime as _dt
            ver = int(_dt.fromisoformat(str(upd_at).replace(" ", "T")).timestamp()) if upd_at else None

        avatar_src = None
        if a_file:
            base = url_for("external_login_user.avatar_file", name=a_file)
            avatar_src = f"{base}?v={ver}" if ver else base
        elif a_url:
            if ver:
                sep = "&" if "?" in a_url else "?"
                avatar_src = f"{a_url}{sep}v={ver}"
            else:
                avatar_src = a_url

        members.append({
            "user_id": uid,
            "nickname": nickname or "（無名）",
            "x_id": x_id,
            "instagram_id": ig_id,
            "participant_role": (role or "none"),
            "costume_label": costume,
            "is_host": bool(is_host),
            "is_subhost": bool(is_subhost),
            "avatar_src": avatar_src,
            "checkin_at": checkin_at,
            "checked_in": bool(checkin_at),
        })

    # --- 並び順：主催 → 副主催 → カメラ → アシ → それ以外（各グループ内はニックネーム昇順） ---
    def _members_sort_group(m: dict) -> int:
        if m["is_host"]:
            return 0
        if m["is_subhost"]:
            return 1
        if m["participant_role"] == "camera":
            return 2
        if m["participant_role"] == "assistant":
            return 3
        return 4  # cosplayer / other / none など

    members_sorted = sorted(members, key=lambda m: (_members_sort_group(m), m["nickname"]))

    return render_template("event_members.html", ev=ev, members=members_sorted)

@bp.route("/events/view/<event_uuid>/sns_clip")
def member_sns_clip(event_uuid: str):
    """X / Instagram 用のコピペテキスト生成画面"""

    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    # --- 閲覧権限チェック（member_list と同じロジック） ---
    if _is_mfu_logged_in():
        allow = True
    else:
        sid = session.get("ext_user_social_id")
        if not sid:
            flash("参加者または管理者のみ閲覧できます。まずは参加申請してください。", "warning")
            return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))
        me = _get_ext_user_by_social(sid)  # type: ignore
        allow = (_membership_status(ev["id"], me["id"]) == "approved")  # type: ignore
        if not allow:
            flash("承認後に閲覧できます。", "info")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # --- 参加者一覧取得（member_list と同じ SQL ベース） ---
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT
              u.id,
              u.nickname,
              u.x_id,
              u.instagram_id,
              u.avatar_file,
              u.avatar_url,
              u.updated_at,
              m.participant_role,
              m.costume_label,
              m.is_host,
              m.is_subhost,
              m.checkin_at
            FROM mfu_event_member m
            JOIN external_login_user u ON u.id = m.user_id
            WHERE m.event_id = %s
              AND m.status = 'approved'
            ORDER BY
              m.is_host DESC,
              m.is_subhost DESC,
              u.nickname ASC
        """, (ev["id"],))
        rows = cur.fetchall()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    members: list[dict] = []
    for r in rows:
        if isinstance(r, tuple):
            (uid, nickname, x_id, ig_id, a_file, a_url, upd_at,
             role, costume, is_host, is_subhost, checkin_at) = r
        else:
            uid        = r["id"]
            nickname   = r["nickname"]
            x_id       = r["x_id"]
            ig_id      = r["instagram_id"]
            a_file     = r["avatar_file"]
            a_url      = r["avatar_url"]
            upd_at     = r["updated_at"]
            role       = r["participant_role"]
            costume    = r["costume_label"]
            is_host    = r["is_host"]
            is_subhost = r["is_subhost"]
            checkin_at = r["checkin_at"]

        members.append({
            "user_id": uid,
            "nickname": nickname or "（無名）",
            "x_id": x_id,
            "instagram_id": ig_id,
            "participant_role": (role or "none"),
            "costume_label": costume,
            "is_host": bool(is_host),
            "is_subhost": bool(is_subhost),
            "checkin_at": checkin_at,
            "checked_in": bool(checkin_at),
        })

    # --- 並び順：主催 → 副主催 → カメラ → アシ → それ以外（各グループ内はニックネーム昇順） ---
    def _sns_sort_group(m: dict) -> int:
        if m["is_host"]:
            return 0
        if m["is_subhost"]:
            return 1
        if m["participant_role"] == "camera":
            return 2
        if m["participant_role"] == "assistant":
            return 3
        return 4  # cosplayer / other / none など

    members_sorted = sorted(members, key=lambda m: (_sns_sort_group(m), m["nickname"]))

    # --- 表示用ラベル生成 ---
    ZS = "　"  # 全角スペース

    def _role_label(m: dict) -> str:
        role = (m["participant_role"] or "none").strip()
        is_host = m["is_host"]
        is_subhost = m["is_subhost"]
        costume = (m["costume_label"] or "").strip()

        # 結合パターン
        if is_host and role == "camera":
            return "主催＆カメラマン"
        if is_host and role == "assistant":
            return "主催＆アシスタント"
        if is_subhost and role == "assistant":
            return "副主催＆アシスタント"
        if is_subhost and role == "camera":
            return "副主催＆カメラマン"

        # 単独役割
        if is_host:
            return "主催"
        if is_subhost:
            return "副主催"
        if role == "camera":
            return "カメラマン"
        if role == "assistant":
            return "アシスタント"

        # コス・その他 → 衣装名を先頭に
        if costume:
            return costume

        # 何もなければとりあえず「衣装」
        return "衣装"

    def _sns_handle(member: dict, platform: str) -> str:
        """プラットフォームごとの @ID 選択
        - Instagram用: instagram_id があればそれを使う（なければ空）
        - X用: x_id があればそれを使う（なければ空）
        """
        ig = (member.get("instagram_id") or "").strip()
        x_  = (member.get("x_id") or "").strip()

        if platform == "instagram":
            handle = ig
        else:  # "x"
            handle = x_

        return f"@{handle}" if handle else ""

    def _build_line(member: dict, platform: str) -> str:
        role = _role_label(member)
        name = f'{member["nickname"]}さん'
        handle = _sns_handle(member, platform)
        if handle:
            return f"{role}{ZS}{name}{ZS}{handle}"
        else:
            return f"{role}{ZS}{name}{ZS}"

    ig_lines: list[str] = []
    x_lines: list[str] = []

    for m in members_sorted:
        ig_lines.append(_build_line(m, "instagram"))
        x_lines.append(_build_line(m, "x"))

    sns_tag_raw = (ev.get("sns_hashtag") or "").strip()
    sns_tag = f"#{sns_tag_raw.lstrip('#')}" if sns_tag_raw else ""
    if sns_tag:
        ig_lines = [sns_tag, ""] + ig_lines
        x_lines = [sns_tag, ""] + x_lines

    return render_template(
        "event_members_sns.html",
        ev=ev,
        ig_lines=ig_lines,
        x_lines=x_lines,
        event_uuid=event_uuid,
    )


# =========================
# 参加証（PASS）
# =========================
@bp.route("/events/pass/<event_uuid>")
def event_pass(event_uuid: str):
    from datetime import datetime as _dt

    # 外部ログイン必須
    guard = _require_ext_login()
    if guard:
        return guard

    # イベント取得
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    # ログイン中ユーザー
    me = _get_ext_user_by_social(session.get("ext_user_social_id"))
    if not me:
        abort(401)

    # 参加状況（最新行）取得（★ checkin_at も取得するように拡張）
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              COALESCE(em.status,'pending')                    AS status,
              CAST(COALESCE(em.require_payment,1) AS UNSIGNED) AS require_payment,
              COALESCE(em.payment_status,'unpaid')             AS payment_status,
              em.paid_amount_yen,
              em.paid_at,
              em.receipt_url,
              em.checkin_at
            FROM mfu_event_member em
            WHERE em.event_id=%s AND em.user_id=%s
            ORDER BY em.id DESC
            LIMIT 1
        """, (ev["id"], me["id"]))
        row = cur.fetchone()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    # 正規化（従来の支払系）
    my_status          = (row["status"] if row else "pending") or "pending"
    my_require_payment = int(row["require_payment"]) if row else 1
    my_payment_status  = (row["payment_status"] if row else "unpaid") or "unpaid"
    my_paid_amount_yen = row.get("paid_amount_yen") if row else None
    my_paid_at         = row.get("paid_at") if row else None
    my_receipt_url     = row.get("receipt_url") if row else None

    # ★ チェックイン情報
    raw_checkin_at = row.get("checkin_at") if row else None
    if isinstance(raw_checkin_at, _dt):
        checkin_at_str = raw_checkin_at.strftime("%Y-%m-%d %H:%M:%S")
    else:
        # 文字列などの場合はそのまま or None
        checkin_at_str = str(raw_checkin_at) if raw_checkin_at else None

    checked_in = bool(raw_checkin_at)

    # 支払表示キーワード（テンプレ側ではもう使ってなくても互換のため残す）
    fee = int(ev.get("fee_yen") or 0)
    needs_pay = (my_require_payment == 1) and (fee > 0)
    if needs_pay:
        if my_payment_status == "paid":
            payment_key, payment_label = "paid", "支払済"
        else:
            payment_key, payment_label = "unpaid", "未支払"
    else:
        payment_key, payment_label = "free", "支払不要"

    # アバターURL算出
    def _pick(*keys):
        for k in keys:
            v = me.get(k) if isinstance(me, dict) else getattr(me, k, None)
            if isinstance(v, (str, bytes)) and str(v).strip():
                return str(v).strip()
        return None

    file_name = _pick("avatar_file", "avatar_filename", "icon_file", "profile_image_file")
    url_raw   = _pick("avatar_url", "icon_url", "picture_url",
                      "profile_image_url", "image_url", "photo_url")

    avatar_src = None
    if file_name:
        if "external_login_user.avatar_file" in current_app.view_functions:
            try:
                avatar_src = url_for("external_login_user.avatar_file", name=file_name)
            except TypeError:
                pass
            if not avatar_src:
                try:
                    avatar_src = url_for("external_login_user.avatar_file", filename=file_name)
                except TypeError:
                    pass
        if not avatar_src and "external_login_user.avatar" in current_app.view_functions:
            try:
                avatar_src = url_for("external_login_user.avatar", name=file_name)
            except TypeError:
                try:
                    avatar_src = url_for("external_login_user.avatar", filename=file_name)
                except TypeError:
                    pass
        if not avatar_src:
            avatar_src = f"/external-login/avatar/{quote_plus(file_name)}"

    if not avatar_src and url_raw:
        if url_raw.startswith(("http://", "https://", "data:")):
            avatar_src = url_raw

    _ver = me.get("updated_at") if isinstance(me, dict) else getattr(me, "updated_at", None)
    if hasattr(_ver, "strftime"):
        cache_bust = _ver.strftime("%Y%m%d%H%M%S")
    else:
        cache_bust = str(_ver or _dt.now().strftime("%Y%m%d%H%M%S"))

    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    # ★ イベントに座標 or maps_url があれば「GPS受付有効」とみなす
    ev_lat  = ev.get("event_lat")
    ev_lng  = ev.get("event_lng")
    maps_url = (ev.get("maps_url") or "").strip()
    gps_checkin_enabled = (ev_lat is not None and ev_lng is not None) or bool(maps_url)

    resp = make_response(render_template(
        "event_pass.html",
        ev=ev,
        me=me,
        now_str=now_str,
        payment_key=payment_key,
        payment_label=payment_label,
        my_payment_status=my_payment_status,
        my_paid_amount_yen=my_paid_amount_yen,
        my_paid_at=my_paid_at,
        my_receipt_url=my_receipt_url,
        avatar_src=avatar_src,
        cache_bust=cache_bust,
        # ★ ここから GPS 受付用
        checked_in=checked_in,
        checkin_at_str=checkin_at_str,
        gps_checkin_enabled=gps_checkin_enabled,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@bp.route("/events/pass/<event_uuid>/checkin", methods=["POST"])
def event_pass_checkin(event_uuid: str):
    """
    参加証画面からの GPS チェックインAPI。

    - URLの event_uuid からイベント特定
    - ログイン中ユーザー + イベントID から mfu_event_member を特定
    - イベントの座標（event_lat/event_lng or maps_url）と現在位置の距離を計算
    - 半径内なら mfu_event_member.checkin_* を更新
    - 初回のみ Discord + ACL メール通知
    """
    # 外部ログイン必須
    guard = _require_ext_login()
    if guard:
        return guard

    # ログイン中ユーザー
    me = _get_ext_user_by_social(session.get("ext_user_social_id"))
    if not me:
        abort(401)

    # イベント取得
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        return jsonify(ok=False, error="イベントが見つかりませんでした。"), 404

    event_id = ev["id"]
    ev_title = ev.get("title") or "イベント"
    ev_uuid_str = ev.get("event_uuid_str") or ""
    if not ev_uuid_str and ev.get("event_uuid") is not None:
        # 念のため bytes から変換する fallback
        try:
            ev_uuid_str = _uuid_bytes_to_str(ev["event_uuid"]) or ""
        except Exception:
            ev_uuid_str = ""

    # 参加メンバー（自分）を特定（最新1件）
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
                m.id,
                m.checkin_at,
                m.checkin_lat,
                m.checkin_lng
            FROM mfu_event_member AS m
            WHERE m.event_id = %s
              AND m.user_id = %s
              AND m.status = 'approved'
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (event_id, me["id"]),
        )
        member = cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass

    if not member:
        return jsonify(ok=False, error="このイベントの参加情報が見つかりませんでした。"), 404

    # すでに受付済みなら何もしない（通知も出さない）
    if member.get("checkin_at"):
        return jsonify(
            ok=True,
            already=True,
            message="すでに受付済みです。",
        )

    # --- 会場側の座標決定 --------------------------------------
    # 優先: event_lat / event_lng カラム（あれば）
    ev_lat = ev.get("event_lat")
    ev_lng = ev.get("event_lng")

    # なければ maps_url からパース
    if ev_lat is None or ev_lng is None:
        maps_url = ev.get("maps_url") or ""
        from_lat, from_lng = _parse_lat_lng_from_maps_url(maps_url)
        ev_lat = ev_lat if ev_lat is not None else from_lat
        ev_lng = ev_lng if ev_lng is not None else from_lng

    if ev_lat is None or ev_lng is None:
        return jsonify(ok=False, error="このイベントではGPS受付が有効化されていません。"), 400

    # 受付許容半径[m]（イベントにカラムがあれば利用、なければ300m）
    try:
        radius_m = int(ev.get("checkin_radius_m") or 300)
        if radius_m <= 0:
            radius_m = 300
    except Exception:
        radius_m = 300

    # --- 端末からの位置情報を取得（JSON / form 両対応） ---------
    lat_raw = None
    lng_raw = None

    if request.is_json:
        data = request.get_json(silent=True) or {}
        lat_raw = data.get("lat")
        lng_raw = data.get("lng")
    else:
        lat_raw = request.form.get("lat")
        lng_raw = request.form.get("lng")

    try:
        user_lat = float(lat_raw)
        user_lng = float(lng_raw)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="位置情報の形式が不正です。"), 400

    # 距離計算
    distance_m = _calc_distance_m(float(ev_lat), float(ev_lng), user_lat, user_lng)

    if distance_m > radius_m:
        return jsonify(
            ok=False,
            error=f"会場から少し離れすぎています（約 {distance_m:.0f} m）。受付は会場付近で行ってください。",
            distance_m=round(distance_m),
            radius_m=radius_m,
        ), 400

    # --- 会場付近 → チェックイン登録 ------------------------------
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    db = get_db(); cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_event_member
               SET checkin_at = %s,
                   checkin_lat = %s,
                   checkin_lng = %s
             WHERE id = %s
               AND checkin_at IS NULL
            """,
            (now, user_lat, user_lng, member["id"]),
        )
        db.commit()
        updated = cur.rowcount
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    # UPDATE に失敗（他プロセスが先にチェックイン済みにした等）の場合はここで終了
    if not updated:
        return jsonify(
            ok=True,
            already=True,
            message="すでに受付処理が完了しています。",
        )

    # --- 通知処理（初回チェックインのみ） -------------------------
    nickname = me.get("nickname") or "参加者"
    pass_url = url_for("external_login_user.event_pass", event_uuid=event_uuid, _external=True)

    # 1) Discord（adminユーザー向け）
    try:
        msg = (
            f"【受付完了】{ev_title}\n"
            f"{nickname} さんが会場付近から受付しました。\n"
            f"受付時刻: {now_str}\n"
            f"距離: 約 {distance_m:.0f} m / 許容 {radius_m} m\n"
            f"参加証: {pass_url}"
        )
        _notify_discord(msg)
    except Exception:
        current_app.logger.exception("Discord 受付通知でエラーが発生しました")

    # 2) ACL メンバーにメール通知
    try:
        acl_emails = _get_acl_admin_emails(event_id)
        if acl_emails:
            subject = f"[MFU] 受付完了: {ev_title} / {nickname} さん"
            body = (
                f"{nickname} 様がイベント「{ev_title}」の受付をGPSで完了しました。\n\n"
                f"受付時刻: {now_str}\n"
                f"位置: lat={user_lat:.6f}, lng={user_lng:.6f}\n"
                f"距離: 約 {distance_m:.0f} m（許容 {radius_m} m）\n\n"
                f"参加証URL:\n{pass_url}\n"
            )
            for addr in acl_emails:
                send_mail(
                    to=addr,
                    subject=subject,
                    body=body,
                    event_uuid=ev_uuid_str or None,
                )
    except Exception:
        current_app.logger.exception("ACL向け受付メール通知でエラーが発生しました")

    return jsonify(
        ok=True,
        message="受付が完了しました。",
        distance_m=round(distance_m),
        radius_m=radius_m,
        checkin_at=now_str,
    )


# =========================
# アバター配信（Apache直配信なら不要）
# =========================
@bp.route("/avatars/<path:name>")
def avatar_file(name: str):
    resp = send_from_directory(str(AVATAR_ROOT), name, as_attachment=False, max_age=0)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# =========================
# メール登録/確認フロー
# =========================
@bp.route("/email", methods=["GET", "POST"])
def email_start():
    if not session.get("ext_user_social_id"):
        return redirect(url_for("external_login_user.index"))

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    if not me:
        return redirect(url_for("external_login_user.profile"))

    if request.method == "GET":
        from flask import render_template_string
        return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>メールアドレスの登録・確認</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <h1 style="font-size:20px;margin:0 0 12px">メールアドレスの登録・確認</h1>
  <form method="post">
    <label>メールアドレス<br>
      <input type="email" name="email" required style="padding:8px;width:320px;max-width:100%">
    </label>
    <div style="margin-top:12px">
      <button type="submit" style="padding:10px 16px">確認メールを送信</button>
    </div>
  </form>
</div>
        """)

    email = _normalize_email(request.form.get("email"))
    if not email:
        flash("正しいメールアドレスを入力してください。", "warning")
        return redirect(url_for("external_login_user.email_start"))

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT email, email_verified_at FROM external_login_user WHERE id=%s LIMIT 1", (me["id"],))
        row = cur.fetchone()
        cur_email = (row[0] if isinstance(row, tuple) else row and row.get("email"))
        verified_at = (row[1] if isinstance(row, tuple) else row and row.get("email_verified_at"))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if cur_email and email.lower() == (cur_email or "").lower() and verified_at:
        flash("このメールアドレスは既に確認済みです。", "info")
        return redirect(url_for("external_login_user.email_start"))

    t_raw = _issue_email_verify_token(me["id"], email)  # type: ignore
    _send_verify_mail(email, t_raw)
    flash("確認メールを送信しました。受信ボックスをご確認ください。", "success")
    return redirect(url_for("external_login_user.email_start"))


@bp.route("/email/verify", methods=["GET", "POST"])
def email_verify():
    token_raw = (request.values.get("t") or "").strip()
    if not token_raw:
        abort(400, "missing token")
    token_hex = hashlib.sha256(token_raw.encode("utf-8")).hexdigest()

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # redirect_url カラムがない場合を考慮して、まずは全カラム取得を試みる
        try:
            cur.execute("""
                SELECT id, user_id, email, expires_at, used_at, redirect_url
                  FROM mfu_email_verification
                 WHERE token=%s
                 LIMIT 1
            """, (token_hex,))
        except Exception:
            # カラムがない場合は redirect_url を除いて取得
            cur.execute("""
                SELECT id, user_id, email, expires_at, used_at
                  FROM mfu_email_verification
                 WHERE token=%s
                 LIMIT 1
            """, (token_hex,))

        row = cur.fetchone()
        if not row:
            abort(400, "invalid token")

        if isinstance(row, tuple):
            # tuple の場合はインデックスで取得
            rec_id = row[0]
            user_id = row[1]
            email = row[2]
            expires_at = row[3]
            used_at = row[4]
            db_redirect_url = row[5] if len(row) > 5 else None
        else:
            rec_id = row["id"]
            user_id = row["user_id"]
            email = row["email"]
            expires_at = row["expires_at"]
            used_at = row["used_at"]
            db_redirect_url = row.get("redirect_url")  # カラムがなければ None になる

        now = datetime.utcnow()
        if used_at or (expires_at and now > expires_at):
            abort(400, "token expired or used")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if request.method == "GET":
        from flask import render_template_string
        return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>メールアドレス確認</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <h1 style="font-size:20px;margin:0 0 12px">このメールアドレスを登録・確認しますか？</h1>
  <p style="margin:0 0 16px"><strong>{{ email }}</strong></p>
  <form method="post" style="display:inline-block;margin-right:8px">
    <input type="hidden" name="t" value="{{ token_raw }}">
    <button type="submit" style="padding:10px 16px">確認する</button>
  </form>
</div>
        """, email=email, token_raw=token_raw)

    # POST: 確定
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("UPDATE external_login_user SET email=%s, email_verified_at=NOW() WHERE id=%s LIMIT 1",
                    (email, user_id))
        cur.execute("UPDATE mfu_email_verification SET used_at=NOW() WHERE id=%s LIMIT 1", (rec_id,))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 完了後の戻り先を決定（DB保存値を優先し、なければセッション、それもなければマイページ）
    next_url = db_redirect_url or session.pop("ext_after_verify_next", None) or session.pop("ext_after_login_next", None) or "https://mfu.iori0624.jp/e/"
    
    from flask import render_template_string
    return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>確認完了</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <p style="margin:0 0 8px">メールアドレスの確認が完了しました。</p>
  <p style="margin:0 0 16px"><strong>{{ email }}</strong></p>
  <a href="{{ next_url }}" 
     style="padding:10px 16px;display:inline-block;
            text-decoration:none;border:1px solid #ccc;
            border-radius:6px;background:#f8f9fa">
    【元のページに戻って参加を完了する】
  </a>
</div>
        """, email=email, next_url=next_url)

@bp.route("/admin/avatars/backfill", methods=["GET", "POST"])
def backfill_avatars():
    # 管理者のみ
    if not _is_mfu_logged_in():
        abort(403)

    limit = 200
    try:
        q = int(request.args.get("limit", "") or request.form.get("limit", "") or "200")
        if 1 <= q <= 2000:
            limit = q
    except Exception:
        pass

    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT id, avatar_url, avatar_file
          FROM external_login_user
         WHERE (avatar_file IS NULL OR avatar_file = '')
           AND avatar_url IS NOT NULL AND avatar_url <> ''
         ORDER BY id ASC
         LIMIT %s
    """, (limit,))
    rows = cur.fetchall() or []
    ok = 0; ng = 0; logs = []
    for r in rows:
        url = r.get("avatar_url")
        uid = r.get("id")
        saved = _download_and_save_avatar(url)
        if saved:
            try:
                cur.execute("UPDATE external_login_user SET avatar_file=%s, updated_at=NOW() WHERE id=%s LIMIT 1",
                            (saved, uid))
                db.commit()
                ok += 1
                logs.append(f"OK  id={uid}  -> {saved}")
            except Exception as e:
                try: db.rollback()
                except Exception: pass
                ng += 1
                logs.append(f"NG  id={uid}  db-update-failed: {e}")
        else:
            ng += 1
            logs.append(f"NG  id={uid}  download-failed")
    try: cur.close(); db.close()
    except Exception: pass

    # シンプルなテキスト応答
    from flask import Response
    body = "Backfill avatars\n" + \
           f"limit={limit}\n" + \
           f"done: ok={ok}, ng={ng}\n\n" + \
           "\n".join(logs[:500])
    return Response(body, mimetype="text/plain; charset=utf-8")

@bp.post("/email/resend-verify", endpoint="resend_verify_email")
def resend_verify_email():
    """
    トップの注意喚起バナーから叩かれる『確認メールの再送』。
    - ログイン必須
    - email が未設定なら警告
    - トークン発行 → 確認メール送信 → フラッシュ表示
    """
    # ログインチェック（未ログインならトップへ）
    social_id = session.get("ext_user_social_id")
    if not social_id:
        flash("ログイン状態が無効です。もう一度お試しください。", "warning")
        return redirect(url_for("external_login_user.index"))

    # 対象ユーザー取得
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, email
              FROM external_login_user
             WHERE social_id=%s
             LIMIT 1
        """, (social_id,))
        me = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if not me:
        flash("ユーザー情報を取得できませんでした。", "danger")
        return redirect(url_for("external_login_user.index"))

    email = (me.get("email") or "").strip()
    if not email or "@" not in email:
        flash("メールアドレスが未登録です。プロフィール編集からご登録ください。", "warning")
        return redirect(url_for("external_login_user.profile", reason="email"))

    # トークン発行して送信
    try:
        # セッションから戻り先を取得（イベント参加URLを最優先する）
        n1 = session.get("ext_after_login_next") or ""
        n2 = session.get("ext_after_verify_next") or ""
        
        if "/events/join/" in n1:
            next_url = n1
        elif "/events/join/" in n2:
            next_url = n2
        else:
            next_url = n1 or n2 or None

        token_raw = _issue_email_verify_token(me["id"], email, redirect_url=next_url)  # 24h 有効
        _send_verify_mail(email, token_raw)
        flash("確認メールを再送しました。受信ボックスをご確認ください。", "info")
    except Exception:
        current_app.logger.exception("resend verify mail failed")
        flash("確認メールの再送に失敗しました。時間をおいて再度お試しください。", "danger")

    # トップへ戻る（バナーは email_verified_at が NULL の間だけ表示されます）
    return redirect(url_for("external_login_user.index"))

@bp.route("/unverified")
def unverified():
    """
    メール未確認ユーザー専用ページ。
    - ここからプロフィール編集へ
    - 確認メールの再送へ
    - ログアウトへ
    以外は before_request でブロックされる想定。
    """
    # 未ログイン or 既に確認済みならトップへ
    uid = session.get("ext_user_id")
    if not uid:
        return redirect(url_for("external_login_user.index"))
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT email, email_verified_at FROM external_login_user WHERE id=%s LIMIT 1", (uid,))
        row = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    email = (row.get("email") or "").strip() if row else ""
    verified = bool(email and row and row.get("email_verified_at"))
    if verified:
        # もう確認済みなら next があれば戻す／無ければトップ
        dest = request.args.get("next")
        return redirect(dest or url_for("external_login_user.index"))

    return render_template(
        "ext_unverified.html",
        email=email,
        next=request.args.get("next") or "",
    )


# ==== PINコードログイン（LINEが使えない人向け：メール→PIN→ログイン） ====
# 画面テンプレートは増やしません。トップ/プロフィールへリダイレクト＋flashで案内します。
import hashlib, secrets
from datetime import datetime, timedelta
from flask import request, flash

def _ensure_email_pin_schema() -> None:
    """PIN保管テーブルを作る（なければ作成・何度呼んでもOK）"""
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mfu_email_login_pin (
              id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              email       VARCHAR(255)    NOT NULL,
              pin_hash    CHAR(64)        NOT NULL,        -- sha256(pin)
              issued_at   DATETIME        NOT NULL,
              expires_at  DATETIME        NOT NULL,
              used_at     DATETIME        NULL,
              sender_ip   VARCHAR(64)     NULL,
              KEY idx_email_expires (email, expires_at),
              KEY idx_expires (expires_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def _issue_pin(email: str, *, ttl_min: int = 10, cooldown_sec: int = 60) -> tuple[bool, str]:
    """
    PINを発行してメール送信。クールダウン（同一メールへ短時間の連投を抑制）
    戻り: (ok, message)
    """
    email = (email or "").strip()
    if not email or "@" not in email or len(email) > 255:
        return False, "メールアドレスを正しく入力してください。"

    _ensure_email_pin_schema()

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # クールダウン：直近 cooldown_sec 以内に発行済みなら弾く
        cur.execute("""
            SELECT issued_at FROM mfu_email_login_pin
             WHERE email=%s
             ORDER BY id DESC LIMIT 1
        """, (email,))
        row = cur.fetchone()
        if row:
            try:
                last = row["issued_at"] if hasattr(row["issued_at"], "timestamp") \
                      else datetime.fromisoformat(str(row["issued_at"]).replace(" ", "T"))
                if (datetime.utcnow() - last).total_seconds() < cooldown_sec:
                    return False, "送信間隔が短すぎます。しばらく経ってから再度お試しください。"
            except Exception:
                pass

        # 6桁 PIN 生成（先頭ゼロを許容）
        pin = f"{secrets.randbelow(1_000_000):06d}"
        pin_hash = _hash_pin(pin)
        now = datetime.utcnow()
        exp = now + timedelta(minutes=ttl_min)
        cur.execute("""
            INSERT INTO mfu_email_login_pin (email, pin_hash, issued_at, expires_at, sender_ip)
            VALUES (%s,%s,%s,%s,%s)
        """, (email, pin_hash, now, exp, (request.headers.get("X-Forwarded-For","").split(",")[0].strip()
                                           or request.remote_addr or "")))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # メール送信（既存の send_mail を使用）
    subject = "【MFU】PINコードログイン用コードのお知らせ（有効10分）"
    body = (
        "MFU イベント管理システムです。\n"
        "以下のPINコードをログイン画面で入力してください。\n\n"
        f"PINコード：{pin}\n"
        "有効期限：10分\n\n"
        "※このメールに心当たりが無い場合は破棄してください。"
    )
    try:
        # event_uuid なし → noreply 送信（既存実装に合わせる）
        send_mail(to=email, subject=subject, body=body, event_uuid=None)
    except Exception:
        return False, "メール送信に失敗しました。時間を置いて再試行してください。"

    return True, "PINコードを送信しました。メールをご確認ください。（有効10分）"


def _resolve_user_by_email(email: str) -> dict | None:
    """
    external_login_user を email で1件返す。
    同一メールが複数ある場合は updated_at の新しいもの優先 → 次に id 若いもの。
    """
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, social_id, nickname, updated_at
              FROM external_login_user
             WHERE email=%s
             ORDER BY COALESCE(updated_at, '1970-01-01 00:00:00') DESC, id ASC
             LIMIT 1
        """, (email,))
        row = cur.fetchone()
        return row or None
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _write_login_log(user_id: int, nickname: str, tag: str = "PIN_LOGIN") -> None:
    """logs テーブルに1行書く（既存の LINE ログと同じ流儀）"""
    db = get_db(); cur = db.cursor()
    try:
        ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                      or request.remote_addr or "-")
        txt = f"[{tag}] ユーザー: #{user_id}　{nickname or '（未設定）'}　がログインしました"
        cur.execute("INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)", (ip_for_log, txt))
        db.commit()
    except Exception:
        try: db.rollback()
        except Exception: pass
    finally:
        try: cur.close(); db.close()
        except Exception: pass


@bp.post("/pin/request")
def pin_request():
    """
    入力: form/json { email }
    既存テンプレは変更せず、flashで案内 → トップへ戻す。
    """
    email = (request.form.get("email") or (request.json or {}).get("email") or "").strip()
    ok, msg = _issue_pin(email)

    # ★ ここを追加：成功時はセッションに保存して次画面で省力化
    if ok:
        try:
            session["pin_email"] = email
        except Exception:
            pass

    flash(msg, "success" if ok else "warning")
    return redirect(url_for("external_login_user.index"))


@bp.post("/pin/login")
def pin_login():
    """
    入力: form/json { email, pin }
    検証OKなら external_login_user を email から特定し、
    既存の ext セッションキーをセットしてログイン完了。
    """
    email = (request.form.get("email") or (request.json or {}).get("email") or "").strip()
    pin   = (request.form.get("pin")   or (request.json or {}).get("pin")   or "").strip()

    # ★ ここを追加：メール未入力ならセッションの保存値を使う
    if not email:
        try:
            email = (session.get("pin_email") or "").strip()
        except Exception:
            email = ""

    if not email or "@" not in email or not pin or not pin.isdigit() or len(pin) != 6:
        flash("メールアドレスと6桁のPINコードを入力してください。", "warning")
        return redirect(url_for("external_login_user.index"))

    _ensure_email_pin_schema()

    # 最新の未使用レコードと照合（期限内のみ）
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, pin_hash, expires_at, used_at
              FROM mfu_email_login_pin
             WHERE email=%s
             ORDER BY id DESC
             LIMIT 5
        """, (email,))
        rows = cur.fetchall() or []
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    now = datetime.utcnow()
    pin_ok = False
    chosen_id = None
    for r in rows:
        if r.get("used_at"):
            continue
        try:
            exp = r["expires_at"] if hasattr(r["expires_at"], "timestamp") \
                 else datetime.fromisoformat(str(r["expires_at"]).replace(" ", "T"))
        except Exception:
            continue
        if now > exp:
            continue
        if _hash_pin(pin) == (r.get("pin_hash") or ""):
            pin_ok = True
            chosen_id = r.get("id")
            break

    if not pin_ok:
        flash("PINコードが一致しないか、有効期限が切れています。", "danger")
        return redirect(url_for("external_login_user.index"))

    # 使用済みにマーク
    db2 = get_db(); cur2 = db2.cursor()
    try:
        cur2.execute("UPDATE mfu_email_login_pin SET used_at=NOW() WHERE id=%s LIMIT 1", (chosen_id,))
        db2.commit()
    finally:
        try: cur2.close(); db2.close()
        except Exception: pass

    # ユーザーを email から特定 → ext セッションに反映
    target = _resolve_user_by_email(email)
    if not target or not target.get("social_id"):
        flash("このメールアドレスに対応するユーザーが見つかりません。プロフィールから登録してください。", "warning")
        return redirect(url_for("external_login_user.profile"))

    session["ext_user_social_id"] = target["social_id"]
    session["ext_user_id"] = target["id"]
    session["ext_user_nickname"] = target.get("nickname") or "（未設定）"

    # ★ ここを追加：ログイン後にセッション中の pin_email を掃除
    try:
        session.pop("pin_email", None)
    except Exception:
        pass

    _write_login_log(target["id"], target.get("nickname") or "", "PIN_LOGIN")
    flash("PINコードでログインしました。", "success")
    return redirect(session.pop("ext_after_login_next", None) or url_for("external_login_user.index"))

# === 直リンク：イベント → アルバム ================================
@bp.route("/events/<event_uuid>/album")
def event_album_direct(event_uuid: str):
    """
    直リンク例:
      /external-login/events/<event_uuid>/album
    未ログインならLINEログインへ飛ばし、ログイン後にこのURLへ戻す。
    承認済みメンバー（または管理者）のみアルバムへリダイレクト。
    """
    # --- 未ログインなら next を保持してログインへ ---
    sid = session.get("ext_user_social_id")
    if not sid:
        session["ext_after_login_next"] = request.url
        return redirect(url_for("external_login_user.line_login", next=request.url, _external=False))

    # --- イベント取得 ---
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    # --- 自分の外部ユーザー行 ---
    me = _get_ext_user_by_social(sid)  # type: ignore
    if not me:
        # 理論上ここには来にくいが、安全側
        session["ext_after_login_next"] = request.url
        return redirect(url_for("external_login_user.line_login", next=request.url, _external=False))

    # --- アクセス可否（管理者は素通し／一般は承認済みのみ） ---
    if not _is_mfu_logged_in():
        if _membership_status(ev["id"], me["id"]) != "approved":  # type: ignore
            flash("アルバムは承認後に閲覧できます。", "info")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # --- アルバムへ ---
    album_id = ev.get("album_id")
    if not album_id:
        flash("このイベントのアルバムはまだ準備中です。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # 既存のアルバム表示エンドポイントへ橋渡し
    return redirect(url_for("album.album_access", album_id=album_id))


# ===== アップデート情報（テキスト→SHA1で既読管理） =========================
# 保存: /mnt/mfu/app/external_login_user/template/update_file.txt
# エンドポイント:
#  - GET  /updates/check : {show, text, hash, seen} を返す（ログイン必須）
#  - GET  /updates/text  : 本文のみ text/plain（未ログイン可）
#  - POST /updates/ack   : 既読化
# 備考:
#  - 既読キー: external_login_user.update_seen_hash（なければ session['ext_update_seen_hash']）
# ==========================================================================

from flask import current_app, session, jsonify, abort, Response
from app.utils.db import get_db
import hashlib
import os

UPDATE_FILE = "/mnt/mfu/app/external_login_user/template/update_file.txt"


def _read_update_text() -> tuple[str, str | None]:
    """update_file.txt の内容と SHA1 を返す。無ければ ('', None)。"""
    try:
        if not os.path.exists(UPDATE_FILE):
            return ("", None)
        with open(UPDATE_FILE, "r", encoding="utf-8") as f:
            txt = (f.read() or "").strip()
    except Exception:
        current_app.logger.exception("read update_file.txt failed")
        return ("", None)

    if not txt:
        return ("", None)

    h = hashlib.sha1(txt.encode("utf-8", "ignore")).hexdigest()
    return (txt, h)


def _get_seen_hash(social_id: str) -> str | None:
    """DBから既読ハッシュを取得。失敗時は session から取得。"""
    try:
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT update_seen_hash FROM external_login_user WHERE social_id=%s LIMIT 1",
            (social_id,)
        )
        row = cur.fetchone() or {}
        try:
            cur.close(); db.close()
        except Exception:
            pass
        seen = (row.get("update_seen_hash") or "").strip() or None
        if seen:
            return seen
    except Exception as e:
        current_app.logger.info("get_seen_hash: fallback to session (%s)", e)

    return session.get("ext_update_seen_hash")  # ないなら None


def _set_seen_hash(social_id: str, cur_hash: str) -> None:
    """既読ハッシュをDBに保存。失敗時は session に保存。"""
    try:
        db = get_db(); cur = db.cursor()
        try:
            cur.execute(
                "UPDATE external_login_user "
                "   SET update_seen_hash=%s, updated_at=NOW() "
                " WHERE social_id=%s LIMIT 1",
                (cur_hash, social_id)
            )
            db.commit()
        finally:
            try:
                cur.close(); db.close()
            except Exception:
                pass
    except Exception as e:
        current_app.logger.info("set_seen_hash: fallback to session (%s)", e)
        session["ext_update_seen_hash"] = cur_hash


def _no_cache(resp: Response) -> Response:
    """ブラウザキャッシュ抑止。"""
    try:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Vary"] = "Cookie"
    except Exception:
        pass
    return resp


@bp.get("/updates/check")
def updates_check():
    """
    ログイン後トップで呼ぶAJAX。
    - show: True ならモーダル表示
    - text: 本文
    - hash: 現在の内容SHA1
    - seen: 既読かどうか（= DB/セッションのハッシュが一致）
    """
    social_id = session.get("ext_user_social_id")
    if not social_id:
        return jsonify({"show": False})

    txt, cur_hash = _read_update_text()
    if not cur_hash:
        return jsonify({"show": False})

    seen_hash = _get_seen_hash(social_id)
    is_seen = (seen_hash == cur_hash)
    show = (not is_seen)

    return _no_cache(jsonify({
        "show": bool(show),
        "text": txt,
        "hash": cur_hash,
        "seen": bool(is_seen),
    }))


@bp.get("/updates/text")
def updates_text():
    """再確認リンク用：本文のみ（未ログインでも可）。"""
    txt, _ = _read_update_text()
    if not txt:
        txt = "（現在、アップデート情報はありません）"
    resp = Response(txt, mimetype="text/plain; charset=utf-8")
    return _no_cache(resp)


@bp.post("/updates/ack")
def updates_ack():
    """
    「次回以降表示しない」押下時に既読化。
    CSRFは省略（同一オリジン・要ログインの軽量操作）。必要なら拡張してください。
    """
    social_id = session.get("ext_user_social_id")
    if not social_id:
        abort(401)

    _, cur_hash = _read_update_text()
    if not cur_hash:
        return _no_cache(jsonify({"ok": True, "message": "no update"})), 200

    _set_seen_hash(social_id, cur_hash)
    return _no_cache(jsonify({"ok": True})), 200
# ======================================================================
