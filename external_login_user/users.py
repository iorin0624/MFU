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
from datetime import datetime, timedelta, timezone
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
from weasyprint import HTML

# =========================
# Flask / アプリ内部
# =========================
from flask import (
    request, session, redirect, url_for, render_template,
    abort, flash, current_app, send_from_directory, make_response, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

# Blueprint は必ず先に import
from . import bp, oauth

from app.utils.db import get_db
from app.utils.mail import send_mail
from .utils import (
    LINE_CLIENT_ID, LINE_CLIENT_SECRET, LINE_REDIRECT_URI,
    _require_ext_login, _is_mfu_logged_in, _uuid_bytes_to_str,
    _get_ext_user_by_social, _upsert_ext_user, _update_profile,
    _event_by_uuid_str, _membership_status,
    update_event_member_status,
    avatar_url_for,  # ← 追加
    QR_TRADEMARK_NOTICE,
    remember_session_map_value,
    EXT_LOGIN_MODE_PWA,
    PWA_RESUME_LOCAL_STORAGE_ISSUED_AT_KEY,
    PWA_RESUME_LOCAL_STORAGE_TOKEN_KEY,
    normalize_ext_login_mode,
    create_external_login_resume_token,
    get_external_login_resume_token_summary,
    consume_external_login_resume_token,
    _get_current_commerce_law_config,
    _get_current_participant_terms_config,
    _get_current_privacy_policy_config,
    _is_participant_terms_effective,
    _is_privacy_policy_effective,
    _needs_privacy_policy_agreement,
    _agree_current_privacy_policy,
    _privacy_policy_date_label,
    _sanitize_ext_local_url,
    _is_disallowed_ext_redirect_path,
)
from .admin import _recalc_event_fee_if_auto
#from .auto_payment import load_default_card_summary


# ==== アバター保存設定 ====
AVATAR_ROOT = Path("/mnt/mfu/avatars")
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
AVATAR_ROOT.mkdir(parents=True, exist_ok=True)


JST = timezone(timedelta(hours=9))

# =========================
# 便利ヘルパ（このファイル内限定）
# =========================
def _normalize_email(s: str) -> str | None:
    s = (s or "").strip()
    if not s or "@" not in s or len(s) > 255:
        return None
    return s


def _normalize_external_url(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _is_chat_admin_alias_ext_user(ext_user_id: int) -> bool:
    if int(ext_user_id or 0) <= 0:
        return False
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT COALESCE(chat_admin_alias, 0) AS chat_admin_alias FROM external_login_user WHERE id=%s LIMIT 1",
            (int(ext_user_id),),
        )
        row = cur.fetchone() or {}
        return int(row.get("chat_admin_alias") or 0) == 1
    except Exception:
        return False
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass


def _load_event_chat_unread_counts(*, event_ids: list[int], ext_user_id: int, use_mfu_admin_scope: bool = False) -> dict[int, int]:
    if not event_ids:
        return {}

    counts: dict[int, int] = {}
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        placeholders = ",".join(["%s"] * len(event_ids))
        if use_mfu_admin_scope:
            current_app.logger.info("event chat unread counts scope=mfu_admin_alias ext_user_id=%s events=%s", ext_user_id, len(event_ids))
            cur.execute(
                f"""
                SELECT target_url
                  FROM mfu_notifications
                 WHERE user_kind='mfu'
                   AND recipient_key='admin'
                   AND kind='event_chat'
                   AND read_at IS NULL
                   AND target_url LIKE '/chat/events/%'
                """
            )
            for row in (cur.fetchall() or []):
                target_url = str((row or {}).get("target_url") or "")
                m = re.search(r"/chat/events/(\d+)", target_url)
                if not m:
                    continue
                event_id = int(m.group(1) or 0)
                if event_id > 0 and event_id in event_ids:
                    counts[event_id] = int(counts.get(event_id, 0)) + 1
            return counts

        cur.execute(
            f"""
            SELECT
              COALESCE(NULLIF(chat_event_id, 0), NULLIF(event_id, 0)) AS event_id,
              COUNT(*) AS unread_count
            FROM mfu_notifications
            WHERE user_kind='external'
              AND user_id=%s
              AND kind='chat_message'
              AND read_at IS NULL
              AND COALESCE(NULLIF(chat_event_id, 0), NULLIF(event_id, 0)) IN ({placeholders})
            GROUP BY COALESCE(NULLIF(chat_event_id, 0), NULLIF(event_id, 0))
            """,
            (int(ext_user_id), *event_ids),
        )
        for row in (cur.fetchall() or []):
            event_id = int(row.get("event_id") or 0)
            if event_id > 0:
                counts[event_id] = int(row.get("unread_count") or 0)
        current_app.logger.info("event chat unread counts scope=external ext_user_id=%s events=%s", ext_user_id, len(event_ids))
        return counts
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass




def _mfu_event_has_deleted_at_column() -> bool:
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM mfu_event LIKE 'deleted_at'")
        row = cur.fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass
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


def _to_jst_date(dt_value) -> datetime.date | None:
    if not dt_value:
        return None
    if isinstance(dt_value, str):
        try:
            dt_value = datetime.fromisoformat(dt_value.replace("Z", "+00:00"))
        except Exception:
            return None
    if not hasattr(dt_value, "tzinfo") or dt_value.tzinfo is None:
        return dt_value.date()
    return dt_value.astimezone(JST).date()


def _format_yen(amount_yen: int | None) -> str:
    if amount_yen is None:
        return ""
    return f"¥{int(amount_yen):,}"


def _build_privacy_policy_view_data(user_row: dict | None) -> dict:
    config = _get_current_privacy_policy_config()
    required = _needs_privacy_policy_agreement(user_row, config)
    agreed_revised = (user_row or {}).get("privacy_policy_agreed_revised_date")
    current_revised = config.get("privacy_policy_revised_date")
    return {
        "privacy_policy_required": required,
        "privacy_policy_url": config.get("privacy_policy_url") or "",
        "privacy_policy_revised_date": current_revised,
        "privacy_policy_revised_date_label": _privacy_policy_date_label(current_revised),
        "privacy_policy_mode": "reconsent" if (required and agreed_revised) else "initial",
        "privacy_policy_effective": _is_privacy_policy_effective(config),
    }


def _build_external_document_view_data() -> dict:
    privacy_config = _get_current_privacy_policy_config()
    commerce_law_config = _get_current_commerce_law_config()
    participant_terms_config = _get_current_participant_terms_config()
    return {
        "privacy_policy_link_url": privacy_config.get("privacy_policy_url") or "",
        "commerce_law_url": commerce_law_config.get("commerce_law_url") or "",
        "participant_terms_url": participant_terms_config.get("participant_terms_url") or "",
        "participant_terms_revised_date": participant_terms_config.get("participant_terms_revised_date"),
        "participant_terms_effective": _is_participant_terms_effective(participant_terms_config),
    }


def _resolve_privacy_policy_post_agree_next() -> str:
    next_url = _sanitize_ext_local_url(session.pop("ext_after_privacy_policy_next", None), default="/external-login/")
    if (
        next_url in {"/external-login/privacy-policy/agree", "/external-login/privacy-policy/agree?"}
        or _is_disallowed_ext_redirect_path(next_url)
    ):
        return url_for("external_login_user.index")
    return next_url or url_for("external_login_user.index")


def _fetchone_dict(cur):
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _get_admin_payer_profile(cur):
    cur.execute("SELECT * FROM payer_profiles WHERE issuer_user_id = %s", ("admin",))
    return _fetchone_dict(cur)


def _send_verify_mail(to_email: str, token_raw: str):
    """legacy / no longer used for new verification flow."""
    """メールアドレス確認コード（イベント非関連）→ send_mail に統一"""
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


def _generate_6digit_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _sender_ip() -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")[:64]


def _send_verify_pin_mail(email: str, pin: str) -> None:
    subject = "イベント管理システムのメールアドレス確認コード"
    body = (
        "メールアドレスの確認コードをお送りします。\n\n"
        "確認コード:\n"
        f"{pin}\n\n"
        "有効期限は10分です。\n"
        "このコードを未確認ページで入力してください。\n\n"
        "このメールに心当たりがない場合は破棄してください。"
    )
    send_mail(to=email, subject=subject, body=body, event_uuid=None)


def _issue_verify_pin(user_id: int, email: str, *, ttl_min: int = 10, cooldown_sec: int = 60) -> tuple[bool, str, str | None]:
    from .schema import ensure_email_verify_pin_schema
    ensure_email_verify_pin_schema()

    now = datetime.utcnow()
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT issued_at FROM mfu_email_verify_pin
             WHERE email=%s
             ORDER BY id DESC
             LIMIT 1
            """,
            (email,),
        )
        last = cur.fetchone()
        if last and last.get("issued_at") and (now - last["issued_at"]).total_seconds() < cooldown_sec:
            return False, "cooldown", None

        cur.execute(
            """
            SELECT COUNT(*) AS c FROM mfu_email_verify_pin
             WHERE email=%s AND issued_at >= (UTC_TIMESTAMP() - INTERVAL 1 HOUR)
            """,
            (email,),
        )
        cnt = int((cur.fetchone() or {}).get("c") or 0)
        if cnt >= 5:
            return False, "rate_limited", None

        pin = _generate_6digit_pin()
        cur.execute(
            """
            INSERT INTO mfu_email_verify_pin
              (user_id, email, pin_hash, issued_at, expires_at, sender_ip, purpose)
            VALUES
              (%s, %s, %s, UTC_TIMESTAMP(), DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE), %s, 'verify')
            """,
            (user_id, email, _hash_pin(pin), ttl_min, _sender_ip()),
        )
        db.commit()
        return True, "ok", pin
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _consume_verify_pin(user_id: int, email: str, pin: str) -> tuple[bool, str]:
    from .schema import ensure_email_verify_pin_schema
    ensure_email_verify_pin_schema()

    if not re.fullmatch(r"\d{6}", pin or ""):
        return False, "invalid_format"

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, pin_hash, expires_at, failed_attempts, locked_until
              FROM mfu_email_verify_pin
             WHERE user_id=%s
               AND email=%s
               AND purpose='verify'
               AND used_at IS NULL
             ORDER BY id DESC
             LIMIT 1
            """,
            (user_id, email),
        )
        rec = cur.fetchone()
        if not rec:
            return False, "not_found"

        now = datetime.utcnow()
        if rec.get("locked_until") and now < rec["locked_until"]:
            return False, "locked"
        if rec.get("expires_at") and now > rec["expires_at"]:
            return False, "expired"

        if _hash_pin(pin) != (rec.get("pin_hash") or ""):
            failed = int(rec.get("failed_attempts") or 0) + 1
            lock_until = None
            if failed >= 5:
                lock_until = now + timedelta(minutes=10)
            cur.execute(
                "UPDATE mfu_email_verify_pin SET failed_attempts=%s, locked_until=%s WHERE id=%s LIMIT 1",
                (failed, lock_until, rec["id"]),
            )
            db.commit()
            return False, "locked" if lock_until else "mismatch"

        cur.execute("UPDATE mfu_email_verify_pin SET used_at=UTC_TIMESTAMP() WHERE id=%s LIMIT 1", (rec["id"],))
        cur.execute(
            "UPDATE external_login_user SET email_verified_at=UTC_TIMESTAMP() WHERE id=%s AND email=%s LIMIT 1",
            (user_id, email),
        )
        db.commit()
        return True, "ok"
    finally:
        try: cur.close(); db.close()
        except Exception: pass


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




def _format_checkin_notice_time(dt_obj: datetime) -> str:
    return f"{dt_obj.year}年{dt_obj.month}月{dt_obj.day}日　{dt_obj.strftime('%H:%M:%S')}"


def _build_checkin_notice_body(*, nickname: str, checked_at: datetime, event_title: str, participant_role: str | None, costume_label: str | None, method_label: str, admin_url: str) -> str:
    role_val = (participant_role or "-").strip() or "-"
    costume_val = (costume_label or "-").strip() or "-"
    lines = [
        "以下の方がチェックイン完了しました",
        "",
        f"【名　　前】{nickname}",
        f"【受付時間】{_format_checkin_notice_time(checked_at)}",
        f"【イベント】{event_title}",
        f"【役　　割】{role_val}",
        f"【衣　　装】{costume_val}",
        f"【受付方法】{method_label}",
        "",
        "管理ページ",
        admin_url,
    ]
    if "QR" in method_label:
        lines.extend(["", f"※{QR_TRADEMARK_NOTICE}"])
    return "\n".join(lines)

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

    # 更新（承認遷移時の System 自動投稿は共通関数で処理）
    update_event_member_status(event_id=event_id, user_id=user_id, new_status=new_status)

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
                    <button type="submit" class="btn btn-sm btn-primary">確認コードを再送する</button>
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
    row = None
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
                    SELECT email, email_verified_at,
                           privacy_policy_agreed_at,
                           privacy_policy_agreed_revised_date
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
                            <button type="submit" class="btn btn-sm btn-primary">確認コードを再送する</button>
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
    privacy_view = _build_privacy_policy_view_data(row or me)
    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)

    if request.args.get("tip") == "done":
        if request.args.get("status") == "ok":
            flash("投げ銭ありがとうございます！", "success")
        elif request.args.get("status") == "ng":
            flash("投げ銭の決済が完了しませんでした。", "warning")

    events_upcoming, events_past = [], []
    if me:
        db = get_db(); cur = db.cursor(dictionary=True)
        has_deleted_at = _mfu_event_has_deleted_at_column()
        where_deleted = "WHERE e.deleted_at IS NULL" if has_deleted_at else ""
        cur.execute(f"""
          SELECT
            e.id, e.event_uuid, e.title,
            e.starts_at, e.fee_yen, e.album_id, e.line_openchat_url,
            COALESCE(e.tip_enabled,0) AS tip_enabled,
            m.id AS member_id,
            COALESCE(m.status,'pending')                        AS status,
            COALESCE(m.is_canceled,0)                          AS is_canceled,
            COALESCE(m.payment_status,'unpaid')                 AS payment_status,
            m.receipt_url,
            COALESCE(m.bank_transfer,0) AS bank_transfer,
            COALESCE(m.paypay_transfer,0) AS paypay_transfer,
            CAST(COALESCE(m.require_payment,1) AS UNSIGNED)     AS require_payment
          FROM mfu_event e
          JOIN (
              SELECT MAX(id) AS id, event_id
                FROM mfu_event_member
               WHERE user_id = %s
               GROUP BY event_id
          ) mm ON mm.event_id = e.id
          JOIN mfu_event_member m ON m.id = mm.id
          {where_deleted}
          ORDER BY e.starts_at IS NULL, e.starts_at
          LIMIT 200
        """, (me["id"],))
        raws = cur.fetchall()
        cur.close(); db.close()

        event_unread_counts: dict[int, int] = {}
        if raws:
            event_ids = [int(r["id"]) for r in raws if r.get("id")]
            use_mfu_admin_scope = _is_chat_admin_alias_ext_user(int(me.get("id") or 0))
            if event_ids:
                event_unread_counts = _load_event_chat_unread_counts(
                    event_ids=event_ids,
                    ext_user_id=int(me.get("id") or 0),
                    use_mfu_admin_scope=use_mfu_admin_scope,
                )

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
                "is_canceled": int(r.get("is_canceled") or 0),
                "payment_status": r["payment_status"] or "unpaid",
                "receipt_url": r["receipt_url"],
                "member_id": r.get("member_id"),
                "bank_transfer": int(r.get("bank_transfer") or 0),
                "paypay_transfer": int(r.get("paypay_transfer") or 0),
                "fee_yen": r["fee_yen"],
                "album_id": r["album_id"],
                "album_url": (url_for("album.album_access", album_id=r["album_id"]) if r["album_id"] else None),
                "pay_url": url_for("external_login_user.pay_start", event_uuid=euuid_str),
                "require_payment": int(r["require_payment"]),
                "tip_enabled": int(r.get("tip_enabled") or 0),
                "my_payment_status": r["payment_status"] or "unpaid",
                "my_receipt_url": r["receipt_url"],
                "line_openchat_url": _normalize_external_url(r.get("line_openchat_url")),
                "chat_unread_count": int(event_unread_counts.get(int(r["id"]), 0)),
            }
            receipt_pdf_url = None
            if (
                item["my_payment_status"] == "paid"
                and item["member_id"]
                and (
                    item["bank_transfer"] == 1
                    or item["paypay_transfer"] == 1
                    or item["receipt_url"]
                )
            ):
                receipt_pdf_url = url_for(
                    "external_login_user.member_receipt_pdf",
                    event_uuid=euuid_str,
                    member_id=item["member_id"],
                )
            item["receipt_pdf_url"] = receipt_pdf_url

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

    document_view = _build_external_document_view_data()

    resp = make_response(render_template(
        "ext_index.html",
        login=bool(me),
        me=me,
        events_upcoming=events_upcoming,
        events_past=events_past,
        ext_csrf=session.get("ext_csrf"),
        **privacy_view,
        **document_view,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@bp.post("/privacy-policy/agree")
def privacy_policy_agree():
    guard = _require_ext_login()
    if guard:
        return guard

    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)
    token = (request.form.get("csrf_token") or "").strip()
    if not token or token != session.get("ext_csrf"):
        flash("フォームの有効期限が切れました。もう一度お試しください。", "warning")
        return redirect(url_for("external_login_user.index"))

    ext_user_id = int(session.get("ext_user_id") or 0)
    if ext_user_id <= 0:
        return redirect(url_for("external_login_user.index"))

    config = _get_current_privacy_policy_config()
    if _is_privacy_policy_effective(config):
        if not _agree_current_privacy_policy(ext_user_id, source="top"):
            flash("プライバシーポリシーへの同意保存に失敗しました。時間をおいて再度お試しください。", "danger")
            return redirect(url_for("external_login_user.index"))
        flash("プライバシーポリシーに同意しました。", "success")

    return redirect(_resolve_privacy_policy_post_agree_next())




@bp.get("/api/events/chat-unread-counts")
def api_event_chat_unread_counts():
    social_id = session.get("ext_user_social_id")
    me = _get_ext_user_by_social(social_id) if social_id else None
    if not me:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    ids_arg = (request.args.get("event_ids") or "").strip()
    event_ids: list[int] = []
    if ids_arg:
        for token in ids_arg.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                event_id = int(token)
            except Exception:
                continue
            if event_id > 0:
                event_ids.append(event_id)
    # 重複除去 + 件数上限
    event_ids = list(dict.fromkeys(event_ids))[:200]

    # event_ids が未指定なら、参加中イベントで限定して返す（既存UI用途）
    if not event_ids:
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT DISTINCT event_id
                  FROM mfu_event_member
                 WHERE user_id=%s
                """,
                (int(me["id"]),),
            )
            event_ids = [int((r or {}).get("event_id") or 0) for r in (cur.fetchall() or []) if int((r or {}).get("event_id") or 0) > 0]
        finally:
            cur.close(); db.close()

    use_mfu_admin_scope = _is_chat_admin_alias_ext_user(int(me.get("id") or 0))
    raw_counts = _load_event_chat_unread_counts(
        event_ids=event_ids,
        ext_user_id=int(me.get("id") or 0),
        use_mfu_admin_scope=use_mfu_admin_scope,
    )
    counts = {str(k): int(v) for k, v in raw_counts.items()}

    return jsonify({"ok": True, "counts": counts})

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


def _trim_line_oauth_state_session(max_items: int = 2) -> None:
    """Authlib が session に保持する LINE OAuth state を少数に制限して Cookie肥大化を防ぐ。"""
    state_keys = [k for k in session.keys() if isinstance(k, str) and k.startswith("_state_line_")]
    if len(state_keys) <= max_items:
        return
    for key in sorted(state_keys)[:-max_items]:
        session.pop(key, None)


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

    local_next = (_to_local_next(raw_next) or "/external-login/")[:512]
    session["ext_after_login_next"] = local_next  # ← セッションにも保持
    login_mode = normalize_ext_login_mode(request.args.get("pwa") or session.get("ext_login_mode"))
    session["ext_login_mode"] = login_mode
    pwa_client_id = (request.args.get("pwa_client_id") or session.get("ext_pwa_client_id") or "").strip()[:128]
    if login_mode == EXT_LOGIN_MODE_PWA and pwa_client_id:
        session["ext_pwa_client_id"] = pwa_client_id

    # 2) 署名付き state を作って callback で検証できるようにする
    state_payload = {
        "n": local_next,                               # next（相対URL）
        "mode": login_mode,
        "pcid": pwa_client_id if login_mode == EXT_LOGIN_MODE_PWA else "",
        "ip": _client_ip_prefix(request.remote_addr or ""),
        "ua": _ua_sha256(request.headers.get("User-Agent", "")),
        "t": int(time.time()),                         # 発行時刻
        "jti": secrets.token_urlsafe(12),              # リプレイ対策の一意ID（任意）
    }
    state_token = _state_signer().dumps(state_payload)

    # 3) LINE 認可ページへ（state を必ず付ける）
    redirect_uri = LINE_REDIRECT_URI() if callable(LINE_REDIRECT_URI) else LINE_REDIRECT_URI
    _trim_line_oauth_state_session(max_items=1)
    return oauth.line.authorize_redirect(redirect_uri=redirect_uri, state=state_token)  # type: ignore[arg-type]

@bp.route("/line/callback")
def line_callback():
    _trim_line_oauth_state_session(max_items=1)
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
    login_mode = normalize_ext_login_mode(payload.get("mode"))
    pwa_client_id = (payload.get("pcid") or "").strip()[:128]
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
        _trim_line_oauth_state_session(max_items=0)
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
            SELECT id, nickname, avatar_file, avatar_url, email,
                   privacy_policy_agreed_revised_date
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
    session["ext_login_mode"] = login_mode
    if pwa_client_id:
        session["ext_pwa_client_id"] = pwa_client_id

    # 既存の next を壊さない（上書きしない）
    session.setdefault("ext_after_login_next", next_path)
    privacy_user_row = {}
    if row:
        if isinstance(row, dict):
            privacy_user_row["privacy_policy_agreed_revised_date"] = row.get("privacy_policy_agreed_revised_date")
        elif isinstance(row, tuple) and len(row) >= 6:
            privacy_user_row["privacy_policy_agreed_revised_date"] = row[5]
    privacy_required = _needs_privacy_policy_agreement(
        privacy_user_row,
        _get_current_privacy_policy_config(),
    )

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
        if login_mode != EXT_LOGIN_MODE_PWA:
            # プロフィール画面へ誘導（reason=email を付与しておくとテンプレ側で出し分けもしやすい）
            profile_next_url = url_for("external_login_user.profile", next=next_path, reason="email")
            if privacy_required:
                session["ext_after_privacy_policy_next"] = profile_next_url
                return redirect(url_for("external_login_user.index"))
            return redirect(profile_next_url)
    else:
        session.pop("ext_user_need_email", None)

    if login_mode == EXT_LOGIN_MODE_PWA:
        pwa_next_path = url_for("external_login_user.index") if privacy_required else next_path
        if privacy_required:
            session["ext_after_privacy_policy_next"] = _sanitize_ext_local_url(next_path, default="/external-login/")
        resume_token = create_external_login_resume_token(
            ext_user_id=int(ext_user_id),
            social_id=sub,
            next_path=pwa_next_path,
            mode=login_mode,
            pwa_client_id=pwa_client_id or None,
        )
        return redirect(url_for("external_login_user.pwa_resume_page", rt=resume_token))

    if privacy_required:
        session["ext_after_privacy_policy_next"] = _sanitize_ext_local_url(next_path, default="/external-login/")
        return redirect(url_for("external_login_user.index"))

    # メール登録済みなら通常遷移
    return redirect(next_path or session.pop("ext_after_login_next", None) or url_for("external_login_user.index"))


@bp.get("/pwa-resume")
def pwa_resume_page():
    resume_token = (request.args.get("rt") or "").strip()
    summary = get_external_login_resume_token_summary(resume_token)
    next_path = _sanitize_next((summary or {}).get("next_path") or url_for("external_login_user.index"))
    is_valid = bool(summary)
    return render_template(
        "pwa_resume.html",
        resume_token=resume_token if is_valid else "",
        next_path=next_path,
        is_valid=is_valid,
        resume_token_key=PWA_RESUME_LOCAL_STORAGE_TOKEN_KEY,
        resume_token_at_key=PWA_RESUME_LOCAL_STORAGE_ISSUED_AT_KEY,
    )


@bp.post("/api/pwa-resume/consume")
def pwa_resume_consume_api():
    payload = request.get_json(silent=True) or {}
    token = (payload.get("token") or request.form.get("token") or "").strip()
    pwa_client_id = (payload.get("pwa_client_id") or request.form.get("pwa_client_id") or "").strip()[:128]

    row = consume_external_login_resume_token(token=token or None, pwa_client_id=pwa_client_id or None)
    if not row:
        return jsonify({
            "ok": False,
            "message": "復帰トークンの有効期限が切れたか、すでに使用済みです。再度ログインしてください。",
        }), 400

    next_path = _sanitize_next((row.get("next_path") or "").strip() or url_for("external_login_user.index"))
    session["ext_user_id"] = int(row["ext_user_id"])
    session["ext_user_social_id"] = (row.get("social_id") or "").strip()
    session["ext_after_login_next"] = next_path
    session["ext_login_mode"] = normalize_ext_login_mode(row.get("mode"))
    session.permanent = True

    me = _get_ext_user_by_social(session["ext_user_social_id"])
    if me:
        session["ext_user_nickname"] = me.get("nickname") or "（未設定）"
        email = (me.get("email") or "").strip()
        if email and "@" in email:
            session.pop("ext_user_need_email", None)
        else:
            session["ext_user_need_email"] = True

    return jsonify({"ok": True, "next": next_path})

# =========================
# プロフィール（CSRF, 画像アップ対応・メール確認送信対応）
# =========================
@bp.route("/profile", methods=["GET", "POST"])
def profile():
    """
    外部参加者のプロフィール編集。
    - 画像アップロード: <input type="file" name="avatar_file">
    - CSRF: session["ext_csrf"] と hidden input csrf_token を比較
    - メールアドレス: 変更時は email_verified_at をクリアし確認コード送信
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

    # access log 用の表示名をセッションへ同期（DB更新成功時のみ）
    session["ext_user_nickname"] = nickname
    session.modified = True

    # 初回フラグを落とす
    if onboarding:
        session["ext_user_onboarding"] = False

    # ===== リダイレクト（join 等へ）=====
    next_url = (
        session.get("ext_after_login_next")
        or (request.args.get("next") or "").strip()
        or (request.form.get("next") or "").strip()
    )

    # ===== メール確認コード送信 =====
    needs_verify = False
    try:
        if email_in and (email_changed or (email_in and not was_verified)):
            # next_url を DB に保存するために渡す
            ok_pin, pin_reason, pin_raw = _issue_verify_pin(me["id"], email_in)
            if ok_pin and pin_raw:
                _send_verify_pin_mail(email_in, pin_raw)
                flash("確認コードを送信しました。メールをご確認ください。", "info")
            elif pin_reason == "cooldown":
                flash("送信間隔が短すぎます。しばらく待ってから再度お試しください。", "warning")
            elif pin_reason == "rate_limited":
                flash("送信回数が上限に達しました。時間をおいて再度お試しください。", "warning")
            else:
                flash("確認コードの送信に失敗しました。時間をおいて再度お試しください。", "danger")
            needs_verify = True
    except Exception:
        current_app.logger.exception("send verify pin failed")
        flash("確認コードの送信に失敗しました。時間をおいて再度お試しください。", "danger")

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
    if needs_verify:
        return redirect(url_for("external_login_user.unverified", next=next_url))
    return redirect(next_url)


@bp.route("/profile/setup", endpoint="profile_setup", methods=["GET"])
def profile_setup_alias():
    """互換 endpoint: 旧導線 /profile/setup を /profile に寄せる。"""
    return redirect(url_for("external_login_user.profile"))


# =========================
# 参加（承認制）・閲覧
# =========================
@bp.route("/line-login")
def line_login_shortcut():
    return redirect(url_for("external_login_user.line_login", **request.args), code=302)

def _is_lecture_event_from_event(ev: dict) -> bool:
    title = (ev.get("title") or "").strip()
    return "【講座】" in title

def _mask_iv_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    tail = token[-4:] if len(token) > 4 else token
    return f"***{tail}"

def _mark_lecture_auto_approve_by_iv(
    *,
    event_id: int,
    event_uuid: str,
    user_id: int,
    iv_token: str,
) -> bool:
    if not iv_token:
        return False
    try:
        remember_session_map_value("lecture_auto_approve_by_iv", event_uuid, True)
    except Exception:
        pass

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, COALESCE(lecture_auto_approve,0) AS lecture_auto_approve
              FROM mfu_payment_request
             WHERE event_id=%s AND user_id=%s
             ORDER BY id DESC
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        if not row:
            current_app.logger.info(
                "join: lecture iv auto-approve pending (event_id=%s user_id=%s iv=%s)",
                event_id,
                user_id,
                _mask_iv_token(iv_token),
            )
            return False
        if int(row.get("lecture_auto_approve") or 0) != 1:
            cur.execute("""
                UPDATE mfu_payment_request
                   SET lecture_auto_approve=1
                 WHERE id=%s AND event_id=%s AND user_id=%s
                 LIMIT 1
            """, (row["id"], event_id, user_id))
            db.commit()
        current_app.logger.info(
            "join: lecture iv auto-approve set (event_id=%s user_id=%s iv=%s payment_request_id=%s)",
            event_id,
            user_id,
            _mask_iv_token(iv_token),
            row["id"],
        )
        return True
    except Exception:
        current_app.logger.exception(
            "join: lecture iv auto-approve update failed (event_id=%s user_id=%s)",
            event_id,
            user_id,
        )
        return False
    finally:
        try: cur.close(); db.close()
        except Exception: pass

def _lecture_auto_approve_from_payment_request(
    *,
    event_id: int,
    user_id: int,
    payment_row_id: int | None,
) -> bool:
    db = get_db(); cur = db.cursor()
    try:
        if payment_row_id:
            cur.execute("""
                SELECT COALESCE(lecture_auto_approve,0)
                  FROM mfu_payment_request
                 WHERE id=%s AND event_id=%s AND user_id=%s
                 LIMIT 1
            """, (payment_row_id, event_id, user_id))
            row = cur.fetchone()
            if row:
                val = row[0] if isinstance(row, tuple) else row.get("lecture_auto_approve") or 0
                return int(val or 0) == 1
        cur.execute("""
            SELECT COALESCE(lecture_auto_approve,0)
              FROM mfu_payment_request
             WHERE event_id=%s
               AND user_id=%s
               AND status IN ('used','pending')
               AND lecture_auto_approve=1
             ORDER BY id DESC
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        if not row:
            return False
        val = row[0] if isinstance(row, tuple) else row.get("lecture_auto_approve") or 0
        return int(val or 0) == 1
    except Exception:
        return False
    finally:
        try: cur.close(); db.close()
        except Exception: pass


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
        SELECT id, email, nickname, x_id, instagram_id,
               privacy_policy_agreed_revised_date,
               privacy_policy_agreed_at
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

    iv_param = (request.args.get("iv") or request.args.get("vi") or "").strip()
    is_lecture = _is_lecture_event_from_event(ev)
    if iv_param and is_lecture:
        remember_session_map_value("lecture_invite_tokens", event_uuid, iv_param)
        _mark_lecture_auto_approve_by_iv(
            event_id=ev["id"],
            event_uuid=event_uuid,
            user_id=ext_uid,
            iv_token=iv_param,
        )

    # 表示用 UUID 文字列 / 管理URL
    ev_uuid_str = _uuid_bytes_to_str(ev["event_uuid"])
    ev["event_uuid_str"] = ev_uuid_str
    admin_url = f"https://mfu.iori0624.jp/external-login/admin/events/{ev['id']}"
    privacy_config = _get_current_privacy_policy_config()
    privacy_effective = _is_privacy_policy_effective(privacy_config)
    privacy_required_for_user = _needs_privacy_policy_agreement(u, privacy_config)
    participant_terms_config = _get_current_participant_terms_config()
    participant_terms_effective = _is_participant_terms_effective(participant_terms_config)
    privacy_error = ""
    participant_terms_error = ""

    # 既存メンバー状況
    cur.execute("""
        SELECT id, COALESCE(status,'pending') AS status,
               COALESCE(participant_role,'none') AS participant_role,
               costume_label,
               COALESCE(process, 0) AS process,
               COALESCE(payment_status,'unpaid') AS payment_status,
               payment_row_id,
               COALESCE(require_payment,1) AS require_payment,
               COALESCE(is_canceled,0) AS is_canceled,
               participant_terms_agreed_revised_date,
               privacy_policy_join_agreed_revised_date
          FROM mfu_event_member
         WHERE event_id=%s AND user_id=%s
         LIMIT 1
    """, (ev["id"], ext_uid))
    m = cur.fetchone()

    if is_lecture:
        require_payment = int(m.get("require_payment") or 1) if m else 1
        payment_status = (m.get("payment_status") or "unpaid").strip() if m else "unpaid"
        if require_payment != 0 and payment_status != "paid":
            iv_redirect = (session.get("lecture_invite_tokens") or {}).get(event_uuid) or iv_param
            if iv_redirect:
                return redirect(url_for("external_login_user.lecture_start", event_uuid=event_uuid, iv=iv_redirect))
            return redirect(url_for("external_login_user.lecture_start", event_uuid=event_uuid))

    # 招待トークン一致（GET/POSTどちらでも query の iv を見て判定）
    iv = (request.args.get("iv") or request.args.get("vi") or "").strip()
    if not iv:
        iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid) or ""
    auto_hit = bool(int(ev["auto_on"] or 0) == 1 and ev.get("invite_token") and iv and iv == ev["invite_token"])
    auto_hit_by_lecture = False
    if m and m.get("payment_status") == "paid" and _is_lecture_event_from_event(ev):
        auto_hit_by_lecture = _lecture_auto_approve_from_payment_request(
            event_id=ev["id"],
            user_id=ext_uid,
            payment_row_id=m.get("payment_row_id"),
        )
        auto_hit = auto_hit or auto_hit_by_lecture

    # =========================
    # POST: フォーム送信（申請）
    # =========================
    if request.method == "POST":
        # CSRF
        token = request.form.get("csrf_token", "")
        if not token or token != csrf_token:
            cur.close(); db.close()
            abort(400, "invalid csrf token")

        privacy_agree_checked = request.form.get("privacy_policy_agree") in ("1", "on", "true", "yes")
        participant_terms_checked = request.form.get("participant_terms_agree") in ("1", "on", "true", "yes")
        if privacy_effective and not privacy_agree_checked:
            privacy_error = "参加申請にはプライバシーポリシーへの同意が必要です。"
        if participant_terms_effective and not participant_terms_checked:
            participant_terms_error = "参加申請には参加条件・支払・キャンセル規定・返金規定への同意が必要です。"

        role = (request.form.get("participant_role") or "cosplayer").strip().lower()
        # ★ 'other' を許可
        if role not in ("camera", "assistant", "cosplayer", "other"):
            role = "cosplayer"

        costume = (request.form.get("costume_label") or "").strip() or None
        # ★ 「衣装／その他」のときだけ保持
        if role not in ("cosplayer", "other"):
            costume = None  # サーバ側でも空に
        process_flag = 1 if request.form.get("process") in ("1", "on", "true") else 0

        if privacy_error or participant_terms_error:
            status = (m and m.get("status")) or None
            cur.close(); db.close()
            return render_template(
                "event_join.html",
                ev=ev,
                status=status,
                form_role=role,
                form_costume=costume or "",
                form_process=bool(process_flag),
                csrf_token=csrf_token,
                privacy_policy_effective=privacy_effective,
                privacy_policy_url=privacy_config.get("privacy_policy_url") or "",
                privacy_policy_error=privacy_error,
                privacy_policy_checked=privacy_agree_checked,
                privacy_policy_agreed_latest=not privacy_required_for_user,
                participant_terms_effective=participant_terms_effective,
                participant_terms_url=participant_terms_config.get("participant_terms_url") or "",
                participant_terms_error=participant_terms_error,
                participant_terms_checked=participant_terms_checked,
            ), 400

        # ステータス決定（自動承認 or 手動承認待ち）
        already_approved = bool(m and (m.get("status") or "").strip().lower() == "approved")
        new_status = "approved" if (auto_hit or already_approved) else "pending"
        should_notify = not (already_approved and auto_hit_by_lecture)

        if privacy_effective and privacy_required_for_user:
            if not _agree_current_privacy_policy(ext_uid, source="join"):
                cur.close(); db.close()
                return render_template(
                    "event_join.html",
                    ev=ev,
                    status=None,
                    form_role=role,
                    form_costume=costume or "",
                    form_process=bool(process_flag),
                    csrf_token=csrf_token,
                    privacy_policy_effective=privacy_effective,
                    privacy_policy_url=privacy_config.get("privacy_policy_url") or "",
                    privacy_policy_error="プライバシーポリシーへの同意保存に失敗しました。時間をおいて再度お試しください。",
                    privacy_policy_checked=privacy_agree_checked,
                    privacy_policy_agreed_latest=False,
                    participant_terms_effective=participant_terms_effective,
                    participant_terms_url=participant_terms_config.get("participant_terms_url") or "",
                    participant_terms_error=participant_terms_error,
                    participant_terms_checked=participant_terms_checked,
                ), 500

        update_event_member_status(
            ev["id"],
            ext_uid,
            new_status,
            extra_update_fields={
                "participant_role": role,
                "costume_label": costume,
                "process": process_flag,
                "is_canceled": 0,
                "canceled_at": None,
                "canceled_by": None,
            },
            extra_insert_fields={
                "participant_role": role,
                "costume_label": costume,
                "process": process_flag,
            },
        )
        join_update_parts = ["joined_at=COALESCE(joined_at, NOW())"]
        join_update_values: list[object] = []
        if participant_terms_effective:
            join_update_parts.extend([
                "participant_terms_agreed_at=%s",
                "participant_terms_agreed_revised_date=%s",
            ])
            join_update_values.extend([
                datetime.now(JST).replace(tzinfo=None),
                participant_terms_config.get("participant_terms_revised_date"),
            ])
        if privacy_effective and privacy_agree_checked:
            join_update_parts.extend([
                "privacy_policy_join_agreed_at=%s",
                "privacy_policy_join_agreed_revised_date=%s",
            ])
            join_update_values.extend([
                datetime.now(JST).replace(tzinfo=None),
                privacy_config.get("privacy_policy_revised_date"),
            ])
        join_update_values.extend([ev["id"], ext_uid])
        cur.execute(
            f"""
            UPDATE mfu_event_member
               SET {', '.join(join_update_parts)}
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
            """,
            tuple(join_update_values),
        )
        db.commit()
        _recalc_event_fee_if_auto(ev["id"])
        if auto_hit_by_lecture and new_status == "approved" and not already_approved:
            current_app.logger.info(
                "join: lecture auto-approved member (event_id=%s user_id=%s)",
                ev["id"],
                ext_uid,
            )

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
        if should_notify:
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
        if should_notify:
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
    if m and int(m.get("is_canceled") or 0) == 1:
        status = "canceled"
    if status == "pending" and m and m.get("payment_status") == "paid" and _is_lecture_event_from_event(ev):
        status = None
    form_role = (m and (m.get("participant_role") or "cosplayer")) or "cosplayer"
    form_costume = (m and (m.get("costume_label") or "")) or ""
    form_process = bool(m and int(m.get("process") or 0) == 1)

    cur.close(); db.close()
    return render_template(
        "event_join.html",
        ev=ev,
        status=status,
        form_role=form_role,
        form_costume=form_costume,
        form_process=form_process,
        csrf_token=csrf_token,
        privacy_policy_effective=privacy_effective,
        privacy_policy_url=privacy_config.get("privacy_policy_url") or "",
        privacy_policy_error=privacy_error,
        privacy_policy_checked=False,
        privacy_policy_agreed_latest=not privacy_required_for_user,
        participant_terms_effective=participant_terms_effective,
        participant_terms_url=participant_terms_config.get("participant_terms_url") or "",
        participant_terms_error=participant_terms_error,
        participant_terms_checked=False,
    )


@bp.route("/events/<event_uuid>/members/<int:member_id>/receipt.pdf")
def member_receipt_pdf(event_uuid: str, member_id: int):
    guard = _require_ext_login()
    if guard:
        return guard

    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))
    if not me and not _is_mfu_logged_in():
        abort(401)

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              m.id AS member_id,
              m.user_id,
              m.event_id,
              m.paid_amount_yen,
              m.paid_at,
              m.receipt_url,
              COALESCE(m.bank_transfer, 0) AS bank_transfer,
              COALESCE(m.paypay_transfer, 0) AS paypay_transfer,
              m.receipt_note,
              u.nickname
            FROM mfu_event_member m
            JOIN external_login_user u ON u.id = m.user_id
            WHERE m.id = %s AND m.event_id = %s
            LIMIT 1
        """, (member_id, ev["id"]))
        row = cur.fetchone()
        if not row:
            abort(404, "参加者が見つかりません")

        if not _is_mfu_logged_in():
            if not me or row.get("user_id") != me.get("id"):
                abort(403, "権限がありません")

        paid_amount = row.get("paid_amount_yen")
        pay_date = _to_jst_date(row.get("paid_at"))
        if paid_amount is None or pay_date is None:
            raise ValueError("支払情報が不足しています")

        bank_transfer = int(row.get("bank_transfer") or 0)
        paypay_transfer = int(row.get("paypay_transfer") or 0)
        receipt_url = (row.get("receipt_url") or "").strip()

        if bank_transfer == 1 and paypay_transfer == 0:
            payment_method = "銀行振込"
        elif bank_transfer == 0 and paypay_transfer == 1:
            payment_method = "PayPay友達送金"
        elif bank_transfer == 0 and paypay_transfer == 0 and receipt_url:
            payment_method = "クレジットカード"
        else:
            raise ValueError("支払種別の判定に失敗しました")

        payer = _get_admin_payer_profile(cur)
        if not payer:
            raise ValueError("発行者情報が見つかりません")

        issue_date = datetime.now(JST).date()
        event_date = _to_jst_date(ev.get("starts_at"))
        if event_date:
            event_date_label = event_date.strftime("%Y年%-m月%-d日")
        else:
            event_date_label = ""
        event_title = (ev.get("title") or "").strip()
        if event_date_label and event_title:
            description = f"{event_date_label}　{event_title}　のイベント参加費のため"
        elif event_date_label:
            description = f"{event_date_label}　のイベント参加費のため"
        elif event_title:
            description = f"{event_title}　のイベント参加費のため"
        else:
            description = "イベント参加費のため"
        receipt_data = {
            "recipient_name": f"{row.get('nickname') or ''} 様",
            "issue_date": issue_date,
            "issue_date_label": f"{issue_date.year}年{issue_date.month}月{issue_date.day}日" if issue_date else "",
            "pay_date": pay_date,
            "pay_date_label": f"{pay_date.year}年{pay_date.month}月{pay_date.day}日" if pay_date else "",
            "amount": _format_yen(int(paid_amount)),
            "description": description,
            "payment_method": payment_method,
            "payer_name": payer.get("payer_name"),
            "payer_address": payer.get("payer_address"),
            "payer_phone": payer.get("payer_phone"),
            "payer_email": payer.get("payer_email"),
            "receipt_note": (row.get("receipt_note") or "").strip(),
        }

        html = render_template("receipt_pdf.html", receipt=receipt_data, event=ev)
        pdf_bytes = HTML(string=html, base_url=request.url_root).write_pdf()

        filename = f"receipt_{event_uuid}_{member_id}.pdf"
        resp = make_response(pdf_bytes)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        return resp
    except HTTPException:
        raise
    except ValueError:
        current_app.logger.exception(
            "receipt pdf validation failed: event_uuid=%s member_id=%s",
            event_uuid,
            member_id,
        )
        abort(400, "領収書の情報が不足しています")
    except Exception:
        current_app.logger.exception(
            "receipt pdf generation failed: event_uuid=%s member_id=%s",
            event_uuid,
            member_id,
        )
        abort(500, "領収書の生成に失敗しました")
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass


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
              m.id AS member_id,
              COALESCE(m.status,'pending')                        AS status,
              CAST(COALESCE(m.require_payment,1) AS UNSIGNED)     AS require_payment,
              COALESCE(m.payment_status,'unpaid')                 AS payment_status,
              m.paid_amount_yen, m.paid_at, m.receipt_url,
              COALESCE(m.bank_transfer,0) AS bank_transfer,
              COALESCE(m.paypay_transfer,0) AS paypay_transfer,
              COALESCE(m.process,0) AS process,
              COALESCE(m.is_host,0) AS is_host,
              COALESCE(m.is_subhost,0) AS is_subhost,
              COALESCE(m.participant_role,'none') AS participant_role,
              COALESCE(m.costume_label,'')        AS costume_label,
              m.custom_fee_yen,
              COALESCE(m.is_canceled,0)           AS is_canceled,
              m.canceled_at
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
    my_member_id           = row.get("member_id") if row else None
    my_bank_transfer       = int(row.get("bank_transfer") or 0) if row else 0
    my_paypay_transfer     = int(row.get("paypay_transfer") or 0) if row else 0
    my_process             = int(row.get("process")) if row else 0
    # ★ ここを追加：現在の役割/衣装（未設定時の既定値も整える）
    my_participant_role    = (row.get("participant_role") if row else "none") or "none"
    my_costume_label       = (row.get("costume_label")  if row else "") or ""
    my_custom_fee_yen      = row.get("custom_fee_yen") if row else None
    my_is_canceled         = int(row.get("is_canceled") or 0) if row else 0
    my_canceled_at         = row.get("canceled_at") if row else None

    my_receipt_pdf_url = None
    if (
        my_payment_status == "paid"
        and my_member_id
        and (my_bank_transfer == 1 or my_paypay_transfer == 1 or my_receipt_url)
    ):
        my_receipt_pdf_url = url_for(
            "external_login_user.member_receipt_pdf",
            event_uuid=event_uuid,
            member_id=my_member_id,
        )

    # 表示モード
    if _is_mfu_logged_in():
        view_mode = "admin"
    else:
        if my_is_canceled == 1:
            view_mode = "member_limited"
        elif str(my_status).lower() == "approved":
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

    raw_openchat_url = _pick_str("openchat_url", "line_openchat_url", "open_chat_url", "line_oc_url")
    openchat_url = _normalize_external_url(raw_openchat_url)
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
        my_receipt_pdf_url=my_receipt_pdf_url,
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
        my_custom_fee_yen=my_custom_fee_yen,
        # 互換エイリアス（テンプレが form_* を参照していても動くように）
        form_role=my_participant_role,
        form_costume=my_costume_label,
        my_is_canceled=my_is_canceled,
        my_canceled_at=my_canceled_at,
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

    db_chk = get_db(); cur_chk = db_chk.cursor()
    try:
        cur_chk.execute(
            "SELECT COALESCE(is_canceled,0) FROM mfu_event_member WHERE event_id=%s AND user_id=%s LIMIT 1",
            (ev["id"], me["id"]),  # type: ignore
        )
        c_row = cur_chk.fetchone()
    finally:
        try: cur_chk.close(); db_chk.close()
        except Exception: pass
    if c_row and int(c_row[0] if isinstance(c_row, tuple) else (c_row.get("is_canceled") or 0)) == 1:
        flash("キャンセル済みのため、この操作はできません。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

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

    db_chk = get_db(); cur_chk = db_chk.cursor()
    try:
        cur_chk.execute(
            "SELECT COALESCE(is_canceled,0) FROM mfu_event_member WHERE event_id=%s AND user_id=%s LIMIT 1",
            (ev["id"], me["id"]),  # type: ignore
        )
        c_row = cur_chk.fetchone()
    finally:
        try: cur_chk.close(); db_chk.close()
        except Exception: pass
    if c_row and int(c_row[0] if isinstance(c_row, tuple) else (c_row.get("is_canceled") or 0)) == 1:
        flash("キャンセル済みのため、この操作はできません。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

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
              "ext_user_need_email", "ext_user_email_unverified",
              "ext_login_mode", "ext_pwa_client_id"):
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
              AND COALESCE(m.is_canceled,0)=0
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
              AND COALESCE(m.is_canceled,0)=0
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
            if costume:
                return f"主催＆{costume}"
            return "主催"
        if is_subhost:
            if costume:
                return f"副主催＆{costume}"
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
              em.checkin_at,
              em.checkin_method,
              COALESCE(em.is_canceled,0) AS is_canceled
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
    if row and int(row.get("is_canceled") or 0) == 1:
        flash("キャンセル済みのため参加証は利用できません。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))
    if isinstance(raw_checkin_at, _dt):
        checkin_at_str = raw_checkin_at.strftime("%Y-%m-%d %H:%M:%S")
    else:
        # 文字列などの場合はそのまま or None
        checkin_at_str = str(raw_checkin_at) if raw_checkin_at else None

    checked_in = bool(raw_checkin_at)
    checkin_method = (row.get("checkin_method") if row else None)

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
    checkin_qr_enabled = bool(ev.get("checkin_qr_enabled"))
    gps_checkin_enabled = ((ev_lat is not None and ev_lng is not None) or bool(maps_url)) and (not checkin_qr_enabled)

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
        checkin_method=checkin_method,
        gps_checkin_enabled=gps_checkin_enabled,
        checkin_qr_enabled=checkin_qr_enabled,
        qr_trademark_notice=QR_TRADEMARK_NOTICE,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

def _update_checkin_member_row(*, member_id: int, checked_at, lat, lng, method: str) -> int:
    """checkin_method列のENUM差異に備え、失敗時はmethod更新なしへフォールバック。"""
    db = get_db(); cur = db.cursor()
    try:
        try:
            cur.execute(
                """
                UPDATE mfu_event_member
                   SET checkin_at = %s,
                       checkin_lat = %s,
                       checkin_lng = %s,
                       checkin_method = %s
                 WHERE id = %s
                   AND checkin_at IS NULL
                """,
                (checked_at, lat, lng, method, member_id),
            )
            db.commit()
            return cur.rowcount
        except Exception as e:
            msg = str(e)
            if ("checkin_method" not in msg) and ("1265" not in msg):
                raise
            try:
                db.rollback()
            except Exception:
                pass
            current_app.logger.warning("checkin_method update skipped due to schema mismatch: %s", msg)
            cur.execute(
                """
                UPDATE mfu_event_member
                   SET checkin_at = %s,
                       checkin_lat = %s,
                       checkin_lng = %s
                 WHERE id = %s
                   AND checkin_at IS NULL
                """,
                (checked_at, lat, lng, member_id),
            )
            db.commit()
            return cur.rowcount
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass


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
    if bool(ev.get("checkin_qr_enabled")):
        return jsonify(ok=False, error="このイベントは会場QRコード受付のみ有効です。"), 400
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
                m.checkin_lng,
                COALESCE(m.participant_role,'') AS participant_role,
                COALESCE(m.costume_label,'') AS costume_label
            FROM mfu_event_member AS m
            WHERE m.event_id = %s
              AND m.user_id = %s
              AND m.status = 'approved'
              AND COALESCE(m.is_canceled,0)=0
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

    updated = _update_checkin_member_row(
        member_id=member["id"],
        checked_at=now,
        lat=user_lat,
        lng=user_lng,
        method="gps",
    )

    # UPDATE に失敗（他プロセスが先にチェックイン済みにした等）の場合はここで終了
    if not updated:
        return jsonify(
            ok=True,
            already=True,
            message="すでに受付処理が完了しています。",
        )

    # --- 通知処理（初回チェックインのみ） -------------------------
    nickname = me.get("nickname") or "参加者"
    admin_url = url_for("external_login_user.admin_event_view", event_id=event_id, _external=True)
    notice_body = _build_checkin_notice_body(
        nickname=nickname,
        checked_at=now,
        event_title=ev_title,
        participant_role=member.get("participant_role"),
        costume_label=member.get("costume_label"),
        method_label="GPS",
        admin_url=admin_url,
    )

    # 1) Discord（adminユーザー向け）
    try:
        _notify_discord(notice_body)
    except Exception:
        current_app.logger.exception("Discord 受付通知でエラーが発生しました")

    # 2) ACL メンバーにメール通知
    try:
        acl_emails = _get_acl_admin_emails(event_id)
        if acl_emails:
            subject = f"[MFU] 受付完了: {ev_title} / {nickname} さん"
            for addr in acl_emails:
                send_mail(
                    to=addr,
                    subject=subject,
                    body=notice_body,
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


@bp.route("/checkin/<token>")
def event_qr_checkin(token: str):
    """会場掲示トークン経由のQR受付（ログイン後に自動チェックイン）。"""
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))
    if not me:
        abort(401)

    token = (token or "").strip().lower()
    if len(token) != 64:
        return make_response("チェックイン用トークンが不正です。", 400)

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, title, event_uuid,
                   COALESCE(checkin_qr_enabled, 0) AS checkin_qr_enabled,
                   checkin_qr_token,
                   checkin_qr_expires_at
              FROM mfu_event
             WHERE checkin_qr_token=%s
             LIMIT 1
        """, (token,))
        ev = cur.fetchone()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    if not ev:
        return make_response("チェックイン用トークンが無効です。", 404)
    if int(ev.get("checkin_qr_enabled") or 0) != 1:
        return make_response("このチェックインは現在無効です。", 400)

    expires_at = ev.get("checkin_qr_expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace(" ", "T"))
        except Exception:
            expires_at = None
    if expires_at and expires_at < datetime.now():
        return make_response("このチェックイン用トークンは期限切れです。", 400)

    event_id = ev["id"]
    ev_title = ev.get("title") or "イベント"
    ev_uuid_str = _uuid_bytes_to_str(ev.get("event_uuid")) or ""

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, checkin_at,
                   COALESCE(participant_role,'') AS participant_role,
                   COALESCE(costume_label,'') AS costume_label
              FROM mfu_event_member
             WHERE event_id=%s
               AND user_id=%s
               AND status='approved'
               AND COALESCE(is_canceled,0)=0
             ORDER BY id DESC
             LIMIT 1
        """, (event_id, me["id"]))
        member = cur.fetchone()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    if not member:
        return make_response("このイベントの参加情報が見つかりませんでした。", 404)

    if member.get("checkin_at"):
        return redirect(url_for("external_login_user.event_pass", event_uuid=ev_uuid_str))

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    updated = _update_checkin_member_row(
        member_id=member["id"],
        checked_at=now,
        lat=None,
        lng=None,
        method="qr",
    )

    if not updated:
        return redirect(url_for("external_login_user.event_pass", event_uuid=ev_uuid_str))

    nickname = me.get("nickname") or "参加者"
    admin_url = url_for("external_login_user.admin_event_view", event_id=event_id, _external=True)
    notice_body = _build_checkin_notice_body(
        nickname=nickname,
        checked_at=now,
        event_title=ev_title,
        participant_role=member.get("participant_role"),
        costume_label=member.get("costume_label"),
        method_label="QR",
        admin_url=admin_url,
    )

    try:
        _notify_discord(notice_body)
    except Exception:
        current_app.logger.exception("Discord QR受付通知でエラーが発生しました")

    try:
        acl_emails = _get_acl_admin_emails(event_id)
        if acl_emails:
            subject = f"[MFU] 受付完了: {ev_title} / {nickname} さん (QR)"
            for addr in acl_emails:
                send_mail(
                    to=addr,
                    subject=subject,
                    body=notice_body,
                    event_uuid=ev_uuid_str or None,
                )
    except Exception:
        current_app.logger.exception("ACL向けQR受付メール通知でエラーが発生しました")

    return redirect(url_for("external_login_user.event_pass", event_uuid=ev_uuid_str))


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
      <button type="submit" style="padding:10px 16px">確認コードを送信</button>
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

    ok_pin, reason, pin_raw = _issue_verify_pin(me["id"], email)
    if ok_pin and pin_raw:
        _send_verify_pin_mail(email, pin_raw)
        flash("確認コードを送信しました。メールをご確認ください。", "success")
    elif reason == "cooldown":
        flash("送信間隔が短すぎます。しばらく待ってから再度お試しください。", "warning")
    elif reason == "rate_limited":
        flash("送信回数が上限に達しました。時間をおいて再度お試しください。", "warning")
    else:
        flash("確認コードの送信に失敗しました。時間をおいて再度お試しください。", "danger")
    return redirect(url_for("external_login_user.email_start"))


@bp.route("/email/verify", methods=["GET", "POST"])
def email_verify():
    """legacy / no longer used for new verification flow."""
    flash("この認証方式は終了しました。確認コードを入力してください。", "warning")
    return redirect(url_for("external_login_user.unverified"))

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
    """未認証ユーザー向け確認コード再送。"""
    social_id = session.get("ext_user_social_id")
    if not social_id:
        flash("ログイン状態が無効です。もう一度お試しください。", "warning")
        return redirect(url_for("external_login_user.index"))

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

    try:
        ok_pin, reason, pin_raw = _issue_verify_pin(me["id"], email)
        if ok_pin and pin_raw:
            _send_verify_pin_mail(email, pin_raw)
            flash("確認コードを再送しました。メールをご確認ください。", "info")
        elif reason == "cooldown":
            flash("送信間隔が短すぎます。しばらく待ってから再度お試しください。", "warning")
        elif reason == "rate_limited":
            flash("送信回数が上限に達しました。時間をおいて再度お試しください。", "warning")
        else:
            flash("確認コードの再送に失敗しました。時間をおいて再度お試しください。", "danger")
    except Exception:
        current_app.logger.exception("resend verify pin failed")
        flash("確認コードの再送に失敗しました。時間をおいて再度お試しください。", "danger")

    return redirect(url_for("external_login_user.unverified"))

@bp.route("/unverified")
def unverified():
    """
    メール未確認ユーザー専用ページ。
    - ここからプロフィール編集へ
    - 確認コードの再送へ
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


@bp.post("/email/verify-pin", endpoint="verify_email_pin")
def verify_email_pin():
    uid = session.get("ext_user_id")
    if not uid:
        return redirect(url_for("external_login_user.index"))

    pin = (request.form.get("pin") or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        flash("6桁の確認コードを入力してください。", "warning")
        return redirect(url_for("external_login_user.unverified", next=request.form.get("next") or ""))

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT email, email_verified_at FROM external_login_user WHERE id=%s LIMIT 1", (uid,))
        row = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    email = (row.get("email") or "").strip() if row else ""
    if not email:
        flash("メールアドレスが未登録です。プロフィール編集からご登録ください。", "warning")
        return redirect(url_for("external_login_user.profile"))

    ok, reason = _consume_verify_pin(uid, email, pin)
    if ok:
        flash("メールアドレスの確認が完了しました。", "success")
        next_url = (request.form.get("next") or "").strip() or session.pop("ext_after_verify_next", None) or session.pop("ext_after_login_next", None) or url_for("external_login_user.index")
        if not (next_url.startswith("/") and not next_url.startswith("//")):
            next_url = url_for("external_login_user.index")
        return redirect(next_url)

    msg = {
        "invalid_format": "6桁の確認コードを入力してください。",
        "locked": "試行回数が上限に達しました。しばらく待ってから再度お試しください。",
        "expired": "確認コードが一致しないか、有効期限が切れています。",
        "mismatch": "確認コードが一致しないか、有効期限が切れています。",
        "not_found": "確認コードが一致しないか、有効期限が切れています。",
    }.get(reason, "確認コードが一致しないか、有効期限が切れています。")
    flash(msg, "warning")
    return redirect(url_for("external_login_user.unverified", next=request.form.get("next") or ""))


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

    child_id = (request.args.get("child_id") or "").strip()
    try:
        from app.albums.routes import _grant_album_auth  # ローカル import で循環参照を回避
        _grant_album_auth(str(album_id))
    except Exception:
        current_app.logger.warning(
            "event_album_direct grant auth failed event_uuid=%s album_id=%s child_id=%s",
            event_uuid,
            album_id,
            child_id,
            exc_info=True,
        )

    if child_id:
        return redirect(url_for("album.view_child", album_id=album_id, child_id=child_id))
    return redirect(url_for("album.album_home", album_id=album_id))


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
