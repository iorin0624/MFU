# -*- coding: utf-8 -*-
"""
撮影交流会（併せ）向け 決済モジュール

- 参加者向け: /payment/e/<event_uuid>（フォーム + Web Payments SDK）
- 課金API   : /payment/api/charge/<event_uuid>
- 事前確認   : /payment/api/precheck/<event_uuid>
- サンクス  : /payment/e/<event_uuid>/thanks?pid=<payment_id>
- Webhooks  : /payment/webhooks
- 管理UI    : /payment/admin/events, /payment/admin/events/<id>, CSV出力, 返金

金額は毎回 mfu_event.fee_yen を優先し、payment.events.default_amount は
表示整合のために裏で同期（差がある場合のみ更新）。
"""

import os
import csv
import uuid
import base64
import logging
import hmac
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
from flask import (
    Blueprint, render_template, request, redirect, jsonify,
    session, abort, Response, url_for
)
from app.utils.mail import send_mail
from app.external_login_user.payments import _notify_payment_to_admin_and_acl
from app.external_login_user.utils import _uuid_bytes_to_str
from .bulk_refund_logic import (
    build_preview_hash,
    decide_bulk_refund_status,
    build_refund_note,
    append_note_if_missing,
    recalculate_paid_amount,
)
from .checkout_otp import (
    CheckoutOtpError,
    consume_checkout_otp,
    is_checkout_otp_verified,
    mask_email,
    require_checkout_otp,
    send_checkout_otp,
    verify_checkout_otp,
)
from .square_gateway import (
    SquareTransportError,
    request_square,
    square_error_info,
)
from .square_state import (
    PAYMENT_IN_PROGRESS_STATUSES,
    is_payment_completed,
    is_refund_completed,
    normalize_square_status,
    should_apply_square_update,
    square_datetime,
)


_BULK_REFUND_REASON_FIXED = "イベント参加費差額返金"

# ------------------------------------------------------------
# .env を読み込む（/mnt/mfu/app/payment/.env）
# ------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

bp = Blueprint(
    "payment", __name__,
    url_prefix="/payment",
    template_folder="template",
    static_folder="template",
    static_url_path="/template"
)

# ───────────────────────────────────────────────────────────
# MFU互換：DB接続とadmin保護
# ───────────────────────────────────────────────────────────
_MFU_GET_DB = None
try:
    from app import get_db as _MFU_GET_DB  # type: ignore
except Exception:
    pass

def _get_db():
    if _MFU_GET_DB:
        return _MFU_GET_DB()
    import pymysql
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "mfu"),
        charset="utf8mb4",
        autocommit=False,
    )

_ADMIN_REQUIRED = None
try:
    from app.auth import admin_required as _ADMIN_REQUIRED  # type: ignore
except Exception:
    try:
        from app import admin_required as _ADMIN_REQUIRED  # type: ignore
    except Exception:
        _ADMIN_REQUIRED = None

def admin_required(f):
    if _ADMIN_REQUIRED:
        return _ADMIN_REQUIRED(f)
    @wraps(f)
    def wrapper(*args, **kwargs):
        abort(503)
    return wrapper


# ───────────────────────────────────────────────────────────
# dict化（DictCursor 非依存）
# ───────────────────────────────────────────────────────────
def _fetchone_dict(cur):
    row = cur.fetchone()
    # mysql.connector(non-buffered) の場合、fetchone後に残件があると
    # 同一connection上の次クエリで "Unread result found" が起きるため明示的に破棄。
    _drain_cursor_results(cur)
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))

def _fetchall_dict(cur):
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _drain_cursor_results(cur) -> None:
    """mysql.connector の Unread result found 回避のため、残り結果を破棄する。"""
    try:
        cur.fetchall()
    except Exception:
        pass

# ───────────────────────────────────────────────────────────
# Utils
# ───────────────────────────────────────────────────────────
def _env() -> str:
    db = None
    try:
        db = _get_db()
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key` = 'square_env_payment'")
        row = cur.fetchone()
        if row:
            if isinstance(row, dict):
                value = row.get("value")
            else:
                value = row[0]
            if value:
                return str(value).upper()
    except Exception:
        pass
    finally:
        if db:
            db.close()
    return os.environ.get("SQUARE_ENV", "SANDBOX").upper()

def _square_env_suffix(square_env: str) -> str:
    return "SANDBOX" if square_env == "SANDBOX" else "PRODUCTION"

def _square_env_value(name: str) -> str | None:
    square_env = _env()
    suffix = _square_env_suffix(square_env)
    return os.environ.get(f"SQUARE_{suffix}_{name}") or os.environ.get(f"SQUARE_{name}")

def _square_application_id() -> str | None:
    return _square_env_value("APPLICATION_ID")

def _square_location_id() -> str | None:
    return _square_env_value("LOCATION_ID")

def _square_access_token() -> str | None:
    return _square_env_value("ACCESS_TOKEN")

def _square_webhook_signature_key() -> str | None:
    return _square_env_value("PAYMENT_WEBHOOK_SIGNATURE_KEY") or _square_env_value("WEBHOOK_SIGNATURE_KEY")

def _square_api_base() -> str:
    return "https://connect.squareupsandbox.com" if _env() == "SANDBOX" else "https://connect.squareup.com"

def _square_js_url() -> str:
    return "https://sandbox.web.squarecdn.com/v1/square.js" if _env() == "SANDBOX" else "https://web.squarecdn.com/v1/square.js"

def _app_base_url() -> str:
    return os.environ.get("MFU_PUBLIC_BASE_URL", "https://mfu.iori0624.jp")

def _uuid_hex_to_str(value: str | bytes | None) -> str | None:
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except Exception:
            try:
                return _uuid_bytes_to_str(value)
            except Exception:
                return None
    value_str = str(value).strip()
    if not value_str:
        return None
    try:
        return str(uuid.UUID(hex=value_str))
    except Exception:
        return None

def _build_receipt_pdf_url(conn, *, event_id: int, user_id: int) -> str | None:
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT HEX(event_uuid) AS event_uuid_hex
              FROM mfu_event
             WHERE id=%s
             LIMIT 1
        """, (event_id,))
        row = cur.fetchone()
        if not row:
            return None
        event_uuid_hex = row.get("event_uuid_hex") if isinstance(row, dict) else row[0]
        event_uuid_str = _uuid_hex_to_str(event_uuid_hex)
        if not event_uuid_str:
            return None

        cur.execute("""
            SELECT id
              FROM mfu_event_member
             WHERE event_id=%s
               AND user_id=%s
             LIMIT 1
        """, (event_id, user_id))
        member_row = cur.fetchone()
        if not member_row:
            return None
        member_id = member_row.get("id") if isinstance(member_row, dict) else member_row[0]
        if not member_id:
            return None

        base_url = _app_base_url().rstrip("/")
        return f"{base_url}/external-login/events/{event_uuid_str}/members/{member_id}/receipt.pdf"
    except Exception:
        logging.exception("build_receipt_pdf_url failed")
        return None

def _sanitize_handle(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    s = s.strip()
    return s.lower() or None

def _normalize_handle(v: str | None) -> str:
    if not v:
        return ""
    v = v.strip()
    if v.startswith("@"):
        v = v[1:]
    # Python なので lower()
    return v.strip().lower()


def _is_allowed_return_url(url: str | None) -> bool:
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return False
    base = urlparse(_app_base_url().strip())
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        return False
    allowed_prefixes = (
        "/external-login/events/",
        "/external-login/pay/return/",
        "/external-login/lecture/return/",
    )
    return any((parsed.path or "").startswith(prefix) for prefix in allowed_prefixes)


def _sanitize_return_url(url: str | None) -> str | None:
    if not url:
        return None
    value = str(url).strip()
    if not value:
        return None
    return value if _is_allowed_return_url(value) else None


def _mask_payment_token(token: str | None, *, show: int = 4) -> str:
    if not token:
        return "(none)"
    token = str(token)
    if len(token) <= show:
        return "*" * len(token)
    return f"***{token[-show:]}"

# ───────────────────────────────────────────────────────────
# Square 顧客ヘルパ
# ───────────────────────────────────────────────────────────
def _ensure_customer_id_for_user(
    access_token: str,
    *,
    user_id: int,
    nickname: str,
    buyer_email: str,
) -> str:
    base = _square_api_base()

    ref = f"mfu_user:{int(user_id)}"

    # 検索
    try:
        sresp = request_square(
            "POST",
            f"{base}/v2/customers/search",
            access_token=access_token,
            json_body={"query": {"filter": {"reference_id": {"exact": ref}}}},
            timeout=15,
            retry_safe=True,
        )
        if sresp.status_code < 400:
            customers = (sresp.json() or {}).get("customers") or []
            if customers:
                customer = customers[0] or {}
                customer_id = customer.get("id")
                if not customer_id:
                    raise RuntimeError("customer id missing")
                if buyer_email and not (customer.get("email_address") or "").strip():
                    uresp = request_square(
                        "PUT",
                        f"{base}/v2/customers/{customer_id}",
                        access_token=access_token,
                        json_body={"email_address": buyer_email},
                        timeout=15,
                        retry_safe=True,
                    )
                    uresp.raise_for_status()
                return customer_id
    except Exception:
        logging.exception("search_customers failed")

    # 作成
    customer_idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"square-customer:{ref}"))
    customer_body = {
        "idempotency_key": customer_idempotency_key,
        "given_name": nickname,
        "reference_id": ref,
        "email_address": buyer_email,
    }
    cresp = request_square(
        "POST",
        f"{base}/v2/customers",
        access_token=access_token,
        json_body=customer_body,
        timeout=15,
        idempotency_key=customer_idempotency_key,
    )
    cresp.raise_for_status()
    return (cresp.json() or {}).get("customer", {}).get("id")


def _resolve_buyer_identity(conn, event_uuid: str, payment_token: str | None) -> tuple[int | None, str | None]:
    if not payment_token:
        return None, None
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT pr.user_id,
                   pr.buyer_email,
                   u.email AS ext_user_email
              FROM mfu_payment_request pr
              JOIN mfu_event me
                ON me.id = pr.event_id
              LEFT JOIN external_login_user u
                ON u.id = pr.user_id
             WHERE pr.token=%s
               AND me.payment_uuid=%s
             ORDER BY pr.id DESC
             LIMIT 1
        """, (payment_token, event_uuid))
        row = _fetchone_dict(cur)
        if not row:
            return None, None
        user_id = row.get("user_id")
        buyer_email = (row.get("buyer_email") or "").strip() or (row.get("ext_user_email") or "").strip() or None
        return int(user_id) if user_id is not None else None, buyer_email
    finally:
        try:
            cur.close()
        except Exception:
            pass

def _resolve_member_id_by_token(conn, event_uuid: str, payment_token: str | None) -> int | None:
    if not payment_token:
        return None
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id AS member_id
              FROM mfu_payment_request pr
              JOIN mfu_event me
                ON me.id = pr.event_id
              JOIN mfu_event_member m
                ON m.event_id = pr.event_id
               AND m.user_id = pr.user_id
             WHERE pr.token=%s
               AND me.payment_uuid=%s
             ORDER BY pr.id DESC
             LIMIT 1
        """, (payment_token, event_uuid))
        row = _fetchone_dict(cur)
        if not row:
            return None
        member_id = row.get("member_id")
        return int(member_id) if member_id is not None else None
    finally:
        try:
            cur.close()
        except Exception:
            pass

# ───────────────────────────────────────────────────────────
# Discord通知（必要時）
# ───────────────────────────────────────────────────────────
def _get_discord_webhook_url(conn, username: str = "admin") -> str | None:
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT webhook_url
              FROM users
             WHERE username = %s
               AND webhook_url IS NOT NULL
               AND webhook_url <> ''
             LIMIT 1
        """, (username,))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if not isinstance(row, dict) else row.get("webhook_url")
    except Exception:
        logging.exception("failed to load discord webhook url by username")
        return None

def _discord_notify(webhook_url, *, title, description, fields=(), color=0x2ECC71):
    try:
        embeds = [{
            "title": title,
            "description": description,
            "color": color,
            "fields": [{"name": n, "value": v, "inline": inh} for (n, v, inh) in fields]
        }]
        requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
    except Exception:
        logging.exception("discord notify failed")

def _notify_discord_payment_if_needed(conn, square_payment_id: str):
    try:
        cur = conn.cursor()
        cur.execute("""
          SELECT
                 p.id                  AS payment_row_id,
                 p.nickname,
                 p.amount_yen,
                 p.square_receipt_url,
                 p.square_status,
                 p.discord_notified,

                 e.title               AS event_title,
                 e.uuid                AS event_uuid,     -- ←決済管理はこれを使う

                 me.id                 AS mfu_event_id    -- ←管理画面はこれを使う
            FROM event_payments p
            JOIN events e
              ON e.id = p.event_id
            LEFT JOIN mfu_event me
              ON me.payment_uuid COLLATE utf8mb4_unicode_ci = e.uuid COLLATE utf8mb4_unicode_ci
           WHERE p.square_payment_id=%s
           LIMIT 1
        """, (square_payment_id,))
        row = cur.fetchone()
        if not row:
            return
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))

        if int(row.get("discord_notified") or 0) == 1:
            return

        status = (row.get("square_status") or "").upper()
        if status != "COMPLETED":
            return

        webhook = _get_discord_webhook_url(conn)
        if not webhook:
            return

        admin_base = _app_base_url().rstrip("/")
        mfu_event_id = row.get("mfu_event_id")
        event_uuid = row.get("event_uuid")

        title = "💳 決済が承認されました"
        fields = [
            ("タイトル", row.get("event_title") or "-", False),
            ("利用者名", row.get("nickname") or "-", True),
            ("決済金額", f"¥{int(row.get('amount_yen') or 0):,}", True),
            ("レシートリンク", row.get("square_receipt_url") or "-", False),
        ]

        # 管理画面（MFU本体のID）
        if mfu_event_id:
            fields.append(("管理画面", f"{admin_base}/external-login/admin/events/{mfu_event_id}", False))

        # 決済管理（UUIDルート）
        if event_uuid:
            fields.append(("決済管理", f"{admin_base}/payment/admin/events/uuid/{event_uuid}", False))

        _discord_notify(
            webhook,
            title=title,
            description="イベントのお支払いが承認/確定しました。",
            fields=fields
        )

        cur.execute("UPDATE event_payments SET discord_notified=1 WHERE id=%s", (row["payment_row_id"],))
        conn.commit()

    except Exception:
        logging.exception("notify_discord_payment_if_needed failed")

def _notify_tip_payment_completion(
    conn,
    *,
    event_id: int,
    user_id: int,
    amount_yen: int | None,
    payment_token: str | None,
) -> None:
    cur = conn.cursor()
    cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
    ev_row = cur.fetchone()
    if isinstance(ev_row, tuple):
        event_title = ev_row[0] or "(不明)"
        event_uuid_str = _uuid_bytes_to_str(ev_row[1]) if len(ev_row) > 1 else None
    elif isinstance(ev_row, dict):
        event_title = (ev_row.get("title") or "(不明)")
        event_uuid_str = _uuid_bytes_to_str(ev_row.get("event_uuid"))
    else:
        event_title = "(不明)"
        event_uuid_str = None

    cur.execute("SELECT id, nickname, email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
    user_row = cur.fetchone()
    if isinstance(user_row, tuple):
        disp_id = user_row[0]
        disp_name = user_row[1]
        disp_email = user_row[2] if len(user_row) > 2 else None
    elif isinstance(user_row, dict):
        disp_id = user_row.get("id")
        disp_name = user_row.get("nickname")
        disp_email = user_row.get("email")
    else:
        disp_name = "(不明)"
        disp_id = user_id
        disp_email = None

    amount_int = int(amount_yen) if isinstance(amount_yen, int) else 0

    webhook = _get_discord_webhook_url(conn)
    if webhook:
        amount_text = f"{amount_int:,}"
        msg = f"[投げ銭] {event_title} / {amount_text}円 / {disp_name or '(不明)'}({disp_id}) / token={payment_token or ''}"
        try:
            requests.post(webhook, json={"content": msg}, timeout=10)
        except Exception:
            logging.exception("tip discord notify failed")

    if disp_email:
        try:
            subject = "ご支援ありがとうございます💕"
            body = f"{disp_name or '参加者'}さん、投げ銭ありがとうございます！{amount_int}円のご支援、運営の力にさせていただきます。"
            send_mail(
                to=disp_email,
                subject=subject,
                body=body,
                event_uuid=event_uuid_str,
            )
        except Exception:
            logging.exception("tip thanks mail failed")


def _notify_mfu_payment_completion(
    conn,
    *,
    event_id: int,
    user_id: int,
    amount_yen: int | None,
    receipt_url: str | None,
    payment_status: str | None,
) -> None:
    status = (payment_status or "").upper()
    if status != "COMPLETED":
        return

    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, event_uuid, payment_uuid
          FROM mfu_event
         WHERE id=%s
         LIMIT 1
    """, (event_id,))
    ev_row = cur.fetchone()
    if not ev_row:
        return
    if isinstance(ev_row, dict):
        ev = dict(ev_row)
    else:
        ev = {
            "id": ev_row[0],
            "title": ev_row[1],
            "event_uuid": ev_row[2],
            "payment_uuid": ev_row[3],
        }
    ev_uuid_str = _uuid_bytes_to_str(ev.get("event_uuid"))
    if ev_uuid_str:
        ev["event_uuid_str"] = ev_uuid_str

    cur.execute("""
        SELECT id, nickname, email
          FROM external_login_user
         WHERE id=%s
         LIMIT 1
    """, (user_id,))
    user_row = cur.fetchone()
    user = {}
    if user_row:
        if isinstance(user_row, dict):
            user = dict(user_row)
        else:
            user = {"id": user_row[0], "nickname": user_row[1], "email": user_row[2]}

    receipt_pdf_url = _build_receipt_pdf_url(conn, event_id=event_id, user_id=user_id)

    admin_base = _app_base_url().rstrip("/")
    admin_link = f"{admin_base}/external-login/admin/events/{event_id}"
    payment_uuid = ev.get("payment_uuid")
    if payment_uuid:
        pay_admin_link = f"{admin_base}/payment/admin/events/uuid/{payment_uuid}"
    else:
        pay_admin_link = f"{admin_base}/payment/admin/events/{event_id}"
    event_view_link = f"{admin_base}/external-login/events/view/{ev_uuid_str}" if ev_uuid_str else ""
    amount_line = f"{amount_yen:,} 円" if isinstance(amount_yen, int) else "(未取得)"

    subject_admin = f"【{ev.get('title','イベント')}】クレジットカード決済が完了しました"
    lines = [
        f"イベント: {ev.get('title','(無題)')}",
        f"参加者: {user.get('nickname') or '(不明)'} (ID: {user.get('id')})",
        f"金額: {amount_line}",
        f"領収書PDF: {receipt_pdf_url or '(領収書発行準備中)'}",
        f"管理画面: {admin_link}",
        f"決済管理: {pay_admin_link}",
    ]
    _notify_payment_to_admin_and_acl(
        ev,
        event_uuid_str=ev_uuid_str,
        subject=subject_admin,
        mail_lines=lines,
        discord_lines=None,
    )

    user_email = (user.get("email") or "").strip()
    if user_email:
        subject_user = f"【{ev.get('title','イベント')}】お支払いありがとうございます！💕"
        body_user = (
            f"{user.get('nickname') or '参加者'} 様\n\n"
            "お忙しい中、お支払いいただきありがとうございます。\n"
            "このメールを持って決済完了とさせていただきます。\n"
            "領収書PDFは、下記のアドレスよりご確認よろしくお願いします。\n\n"
            "当日、お会いできるのを楽しみにしております！\n\n"
            f"イベント: {ev.get('title','(無題)')}\n"
            f"金額: {amount_line}\n"
            f"領収書PDF: {receipt_pdf_url or '(領収書発行準備中)'}\n"
            f"イベント詳細: {event_view_link or '(なし)'}\n\n"
        )
        send_mail(to=user_email, subject=subject_user, body=body_user, event_uuid=ev_uuid_str)

# ───────────────────────────────────────────────────────────
# スキーマ
# ───────────────────────────────────────────────────────────
DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
  id              BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  uuid            CHAR(22) NOT NULL UNIQUE,
  title           VARCHAR(200) NOT NULL,
  date            DATE NULL,
  default_amount  INT UNSIGNED NOT NULL DEFAULT 1000,
  is_active       TINYINT(1) NOT NULL DEFAULT 1,
  notes           TEXT NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_PAYMENTS = """
CREATE TABLE IF NOT EXISTS event_payments (
  id                   BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  event_id             BIGINT UNSIGNED NOT NULL,
  created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  nickname             VARCHAR(120) NOT NULL,
  x_id                 VARCHAR(120) NULL,
  instagram_id         VARCHAR(120) NULL,
  receipt_email        VARCHAR(255) NULL,
  amount_yen           INT UNSIGNED NOT NULL,
  memo                 VARCHAR(255) NULL,
  payment_token        CHAR(36) NULL,
  event_member_id      BIGINT UNSIGNED NULL,
  external_login_user_id BIGINT UNSIGNED NULL,

  idempotency_key      CHAR(36) NOT NULL,
  square_payment_id    VARCHAR(64) NULL UNIQUE,
  square_status        ENUM('PENDING','UNKNOWN','AUTHORIZED','APPROVED','COMPLETED','CANCELED','FAILED') NOT NULL DEFAULT 'PENDING',
  square_receipt_url   VARCHAR(512) NULL,
  card_brand           VARCHAR(32) NULL,
  card_last4           CHAR(4) NULL,
  card_exp_mm          TINYINT NULL,
  card_exp_yyyy        SMALLINT NULL,

  error_code           VARCHAR(64) NULL,
  error_detail         TEXT NULL,
  square_updated_at    DATETIME(6) NULL,
  last_synced_at       DATETIME NULL,
  sync_attempts        INT UNSIGNED NOT NULL DEFAULT 0,
  sync_error           TEXT NULL,

  discord_notified     TINYINT(1) NOT NULL DEFAULT 0,

  CONSTRAINT fk_event_payments_event
    FOREIGN KEY (event_id) REFERENCES events(id)
    ON DELETE CASCADE,

  KEY ix_event_created (event_id, created_at),
  KEY ix_status (square_status),
  KEY ix_payment_token (payment_token),
  KEY ix_event_member_id (event_member_id),
  KEY ix_external_login_user_id (external_login_user_id),
  KEY ix_event_identity (event_id, event_member_id, external_login_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_REFUNDS = """
CREATE TABLE IF NOT EXISTS event_refunds (
  id                BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  payment_row_id    BIGINT UNSIGNED NOT NULL,
  square_refund_id  VARCHAR(64) NULL UNIQUE,
  amount_yen        INT UNSIGNED NOT NULL,
  status            ENUM('PENDING','UNKNOWN','APPROVED','COMPLETED','REJECTED','FAILED','CANCELED') NOT NULL DEFAULT 'PENDING',
  reason            VARCHAR(255) NULL,
  bulk_refund_run_id CHAR(36) NULL,
  created_by_admin  VARCHAR(64) NULL,
  notified_at       DATETIME NULL,
  notify_to_email   VARCHAR(255) NULL,
  notify_error      TEXT NULL,
  error_code        VARCHAR(64) NULL,
  error_detail      TEXT NULL,
  square_updated_at DATETIME(6) NULL,
  last_synced_at    DATETIME NULL,
  sync_attempts     INT UNSIGNED NOT NULL DEFAULT 0,
  sync_error        TEXT NULL,
  accounting_applied_at DATETIME NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_event_refunds_payment
    FOREIGN KEY (payment_row_id) REFERENCES event_payments(id)
    ON DELETE CASCADE,
  KEY ix_payment_status (payment_row_id, status),
  KEY ix_bulk_refund_run (bulk_refund_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_SQUARE_WEBHOOK_EVENTS = """
CREATE TABLE IF NOT EXISTS square_webhook_events (
  id                BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  square_event_id   VARCHAR(96) NOT NULL UNIQUE,
  event_type        VARCHAR(96) NOT NULL,
  object_id         VARCHAR(96) NULL,
  payload_sha256    CHAR(64) NOT NULL,
  processing_status ENUM('RECEIVED','PROCESSED','FAILED','IGNORED') NOT NULL DEFAULT 'RECEIVED',
  error_detail      TEXT NULL,
  received_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at      DATETIME NULL,
  KEY ix_square_webhook_type_received (event_type, received_at),
  KEY ix_square_webhook_processing (processing_status, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_SQUARE_SYNC_CONTROL = """
CREATE TABLE IF NOT EXISTS square_sync_control (
  id           TINYINT UNSIGNED PRIMARY KEY,
  managed_from DATETIME(6) NOT NULL,
  last_alert_signature CHAR(64) NULL,
  last_alert_at DATETIME NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_PAYMENT_SCHEMA_READY = False
_PAYMENT_SCHEMA_LOCK = threading.Lock()


def _ensure_schema():
    global _PAYMENT_SCHEMA_READY
    if _PAYMENT_SCHEMA_READY:
        return
    with _PAYMENT_SCHEMA_LOCK:
        if _PAYMENT_SCHEMA_READY:
            return
        conn = _get_db()
        cur = conn.cursor()
        db_lock_acquired = False
        try:
            cur.execute("SELECT GET_LOCK('payment_schema_migrate', 30)")
            lock_row = cur.fetchone()
            lock_value = lock_row[0] if isinstance(lock_row, tuple) else (next(iter(lock_row.values())) if isinstance(lock_row, dict) and lock_row else 0)
            db_lock_acquired = int(lock_value or 0) == 1
            if not db_lock_acquired:
                raise RuntimeError("payment schema migration lock timeout")

            for ddl in (DDL_EVENTS, DDL_PAYMENTS, DDL_REFUNDS, DDL_SQUARE_WEBHOOK_EVENTS, DDL_SQUARE_SYNC_CONTROL):
                cur.execute(ddl)
            cur.execute("INSERT IGNORE INTO square_sync_control (id, managed_from) VALUES (1, NOW(6))")
            conn.commit()

            def try_ddl(sql: str) -> None:
                try:
                    cur.execute(sql)
                    conn.commit()
                except Exception:
                    conn.rollback()

            for alter_sql in (
                "ALTER TABLE square_sync_control ADD COLUMN last_alert_signature CHAR(64) NULL",
                "ALTER TABLE square_sync_control ADD COLUMN last_alert_at DATETIME NULL",
                "ALTER TABLE event_payments ADD COLUMN discord_notified TINYINT(1) NOT NULL DEFAULT 0",
                "ALTER TABLE event_payments ADD COLUMN payment_token CHAR(36) NULL",
                "ALTER TABLE event_payments ADD COLUMN event_member_id BIGINT UNSIGNED NULL",
                "ALTER TABLE event_payments ADD COLUMN external_login_user_id BIGINT UNSIGNED NULL",
                "ALTER TABLE event_payments ADD COLUMN square_updated_at DATETIME(6) NULL",
                "ALTER TABLE event_payments ADD COLUMN last_synced_at DATETIME NULL",
                "ALTER TABLE event_payments ADD COLUMN sync_attempts INT UNSIGNED NOT NULL DEFAULT 0",
                "ALTER TABLE event_payments ADD COLUMN sync_error TEXT NULL",
                "ALTER TABLE event_refunds ADD COLUMN bulk_refund_run_id CHAR(36) NULL",
                "ALTER TABLE event_refunds ADD COLUMN created_by_admin VARCHAR(64) NULL",
                "ALTER TABLE event_refunds ADD COLUMN notified_at DATETIME NULL",
                "ALTER TABLE event_refunds ADD COLUMN notify_to_email VARCHAR(255) NULL",
                "ALTER TABLE event_refunds ADD COLUMN notify_error TEXT NULL",
                "ALTER TABLE event_refunds ADD COLUMN square_updated_at DATETIME(6) NULL",
                "ALTER TABLE event_refunds ADD COLUMN last_synced_at DATETIME NULL",
                "ALTER TABLE event_refunds ADD COLUMN sync_attempts INT UNSIGNED NOT NULL DEFAULT 0",
                "ALTER TABLE event_refunds ADD COLUMN sync_error TEXT NULL",
                "ALTER TABLE event_refunds ADD COLUMN accounting_applied_at DATETIME NULL",
                "ALTER TABLE mfu_event_member ADD COLUMN receipt_note TEXT NULL",
            ):
                try_ddl(alter_sql)

            cur.execute("SHOW COLUMNS FROM event_payments LIKE 'square_status'")
            payment_status_column = cur.fetchone()
            payment_status_type = payment_status_column[1] if isinstance(payment_status_column, tuple) else ((payment_status_column or {}).get("Type") or "")
            if "'UNKNOWN'" not in str(payment_status_type):
                try_ddl("ALTER TABLE event_payments MODIFY square_status ENUM('PENDING','UNKNOWN','AUTHORIZED','APPROVED','COMPLETED','CANCELED','FAILED') NOT NULL DEFAULT 'PENDING'")

            cur.execute("SHOW COLUMNS FROM event_refunds LIKE 'status'")
            refund_status_column = cur.fetchone()
            refund_status_type = refund_status_column[1] if isinstance(refund_status_column, tuple) else ((refund_status_column or {}).get("Type") or "")
            if "'COMPLETED'" not in str(refund_status_type) or "'UNKNOWN'" not in str(refund_status_type):
                try_ddl("ALTER TABLE event_refunds MODIFY status ENUM('PENDING','UNKNOWN','APPROVED','COMPLETED','REJECTED','FAILED','CANCELED') NOT NULL DEFAULT 'PENDING'")

            for idx_sql in (
                "CREATE INDEX ix_event_member_id ON event_payments(event_member_id)",
                "CREATE INDEX ix_external_login_user_id ON event_payments(external_login_user_id)",
                "CREATE INDEX ix_event_identity ON event_payments(event_id, event_member_id, external_login_user_id)",
                "CREATE INDEX ix_payment_token ON event_payments(payment_token)",
                "CREATE INDEX ix_bulk_refund_run ON event_refunds(bulk_refund_run_id)",
            ):
                try_ddl(idx_sql)

            if os.environ.get("SQUARE_ALLOW_LEGACY_BACKFILL", "0").strip().lower() in {"1", "true", "yes", "on"}:
                _backfill_payment_identity(conn)
            _PAYMENT_SCHEMA_READY = True
        finally:
            if db_lock_acquired:
                try:
                    cur.execute("SELECT RELEASE_LOCK('payment_schema_migrate')")
                    cur.fetchone()
                except Exception:
                    logging.exception("payment schema migration lock release failed")
            try:
                cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

def _backfill_payment_identity(conn) -> None:
    """曖昧一致なしで埋められる識別子のみ backfill する。"""
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE event_payments p
            JOIN mfu_event_member m
              ON m.payment_row_id = p.id
             SET p.event_member_id = m.id,
                 p.external_login_user_id = COALESCE(p.external_login_user_id, m.user_id)
           WHERE p.event_member_id IS NULL
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cur.execute("""
            UPDATE event_payments p
            JOIN mfu_payment_request pr
              ON pr.token COLLATE utf8mb4_unicode_ci = p.payment_token COLLATE utf8mb4_unicode_ci
             SET p.external_login_user_id = pr.user_id
           WHERE p.external_login_user_id IS NULL
             AND p.payment_token IS NOT NULL
        """)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _square_managed_from(conn) -> datetime:
    """Old production rows stay read-only unless a separate migration is run."""

    cur = conn.cursor()
    try:
        cur.execute("SELECT managed_from FROM square_sync_control WHERE id=1 LIMIT 1")
        row = cur.fetchone()
        value = row[0] if isinstance(row, tuple) else (row.get("managed_from") if row else None)
        return value if isinstance(value, datetime) else datetime.max
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _is_square_managed_record(conn, created_at) -> bool:
    return isinstance(created_at, datetime) and created_at >= _square_managed_from(conn)

# ───────────────────────────────────────────────────────────
# MFUイベント連携
# ───────────────────────────────────────────────────────────
def _autoprovision_event_from_mfu(event_uuid: str) -> None:
    """
    初アクセス時、payment.events に行が無ければ
    MFUの mfu_event.payment_uuid に基づいて自動作成。
    """
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM events WHERE uuid=%s", (event_uuid,))
        if _fetchone_dict(cur):
            conn.close(); return

        cur.execute("""
            SELECT title, fee_yen
              FROM mfu_event
             WHERE payment_uuid=%s
             LIMIT 1
        """, (event_uuid,))
        me = _fetchone_dict(cur)
        if not me:
            conn.close(); return

        title = me.get("title") or "イベント"
        fee   = int(me.get("fee_yen") or 1000)
        cur.execute("""
            INSERT INTO events (uuid, title, default_amount, is_active)
            VALUES (%s,%s,%s,1)
        """, (event_uuid, title, fee))
        conn.commit()
        conn.close()
    except Exception:
        logging.exception("autoprovision from mfu_event failed")

def _get_live_amount_and_sync(conn, event_uuid: str) -> tuple[int, dict|None]:
    """
    毎回、金額は mfu_event.fee_yen を優先して取得。
    payment.events と乖離があれば default_amount を同期（更新）。
    戻り値: (amount, events_row or None)
    """
    cur = conn.cursor()

    cur.execute("SELECT * FROM events WHERE uuid=%s AND is_active=1", (event_uuid,))
    ev = _fetchone_dict(cur)

    # mfuの現在額（payment_uuid で引く）
    cur.execute("SELECT fee_yen, title FROM mfu_event WHERE payment_uuid=%s LIMIT 1", (event_uuid,))
    me = _fetchone_dict(cur)
    fee = int((me or {}).get("fee_yen") or 0)

    if fee <= 0:
        # mfu側に金額が無い場合はevents.default_amountを使う
        if not ev:
            return (0, None)
        return (int(ev.get("default_amount") or 0), ev)

    # eventsが無いケース（理論上ないが保険）
    if not ev:
        return (fee, None)

    # 差があれば同期
    try:
        if int(ev.get("default_amount") or 0) != fee:
            cur.execute("UPDATE events SET default_amount=%s WHERE id=%s", (fee, ev["id"]))
            conn.commit()
            ev["default_amount"] = fee
    except Exception:
        conn.rollback()
    return (fee, ev)

def _mfu_event_by_payment_uuid(event_uuid: str) -> dict | None:
    """
    mfu_event を payment_uuid で1件取得（必要カラムのみ）
    返り値: { id, title, fee_yen, event_uuid_str } or None
    """
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, fee_yen, event_uuid
              FROM mfu_event
             WHERE payment_uuid=%s
             LIMIT 1
        """, (event_uuid,))
        row = _fetchone_dict(cur)
        if not row:
            return None
        ev_uuid_str = _uuid_bytes_to_str(row.get("event_uuid"))
        return {
            "id": row["id"],
            "title": row["title"],
            "fee_yen": row["fee_yen"],
            "event_uuid_str": ev_uuid_str,
        }
    finally:
        try: conn.close()
        except Exception: pass

def _resolve_payment_token(event_uuid: str) -> str | None:
    try:
        token = (request.args.get("payment_token") or "").strip()
        if token:
            return token
    except Exception:
        pass
    try:
        ctx = session.get("pay_ctx") or {}
        token = (ctx.get("payment_token") or "").strip()
        return token or None
    except Exception:
        return None

def _fetch_payment_request(conn, event_uuid: str, token: str | None) -> dict | None:
    if not token:
        return None
    me = _mfu_event_by_payment_uuid(event_uuid) or {}
    event_id = me.get("id")
    event_uuid_str = me.get("event_uuid_str")
    if not event_id:
        return None
    cur = conn.cursor()
    try:
        if event_uuid_str:
            cur.execute("""
                SELECT id, event_id, user_id, nickname, x_id, instagram_id, amount_yen, status, kind, tip_event_id
                  FROM mfu_payment_request
                 WHERE token=%s AND event_id=%s AND event_uuid=%s AND status='pending'
                 LIMIT 1
            """, (token, event_id, event_uuid_str))
        else:
            cur.execute("""
                SELECT id, event_id, user_id, nickname, x_id, instagram_id, amount_yen, status, kind, tip_event_id
                  FROM mfu_payment_request
                 WHERE token=%s AND event_id=%s AND status='pending'
                 LIMIT 1
            """, (token, event_id))
        row = _fetchone_dict(cur)
        return row
    finally:
        try: cur.close()
        except Exception: pass

def _amount_for_payment(conn, event_uuid: str, token: str | None) -> int | None:
    if token:
        row = _fetch_payment_request(conn, event_uuid, token)
        if row:
            try:
                return int(row.get("amount_yen") or 0)
            except Exception:
                return None
    return None



def _payment_request_context(conn, event_uuid: str, token: str | None) -> dict:
    pr = _fetch_payment_request(conn, event_uuid, token) or {}
    kind = (pr.get("kind") or "event_fee").strip().lower() if pr else "event_fee"
    return {
        "payment_request": pr,
        "kind": kind,
        "is_tip": kind == "tip",
    }

def _infer_payment_token(
    conn,
    event_uuid: str,
    *,
    nickname: str,
    x_id: str | None,
    instagram_id: str | None,
) -> str | None:
    me = _mfu_event_by_payment_uuid(event_uuid) or {}
    event_id = me.get("id")
    if not event_id:
        return None
    cur = conn.cursor()
    try:
        if x_id:
            cur.execute("""
                SELECT token
                  FROM mfu_payment_request
                 WHERE event_id=%s AND status='pending'
                   AND REPLACE(LOWER(x_id), '@', '')=%s
                 ORDER BY id DESC
                 LIMIT 1
            """, (event_id, x_id))
        elif instagram_id:
            cur.execute("""
                SELECT token
                  FROM mfu_payment_request
                 WHERE event_id=%s AND status='pending'
                   AND REPLACE(LOWER(instagram_id), '@', '')=%s
                 ORDER BY id DESC
                 LIMIT 1
            """, (event_id, instagram_id))
        else:
            cur.execute("""
                SELECT token
                  FROM mfu_payment_request
                 WHERE event_id=%s AND status='pending'
                   AND LOWER(nickname)=LOWER(%s)
                 ORDER BY id DESC
                 LIMIT 1
            """, (event_id, nickname))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row, tuple) else row.get("token")
    finally:
        try: cur.close()
        except Exception: pass

def _mark_payment_token_used_and_apply_member_status(
    conn,
    *,
    payment_token: str | None,
    amount_yen: int | None,
    receipt_url: str | None,
    payment_row_id: int | None,
    payment_status: str | None,
) -> None:
    if not payment_token:
        return
    cur = conn.cursor()
    try:
        token_marked_used = False
        cur.execute("""
            UPDATE mfu_payment_request
               SET status='used', used_at=NOW()
             WHERE token=%s AND status='pending'
             LIMIT 1
        """, (payment_token,))
        token_marked_used = bool(cur.rowcount)
        cur.execute("""
            SELECT event_id, user_id, COALESCE(kind,'event_fee') AS kind, tip_event_id
              FROM mfu_payment_request
             WHERE token=%s
             ORDER BY id DESC
             LIMIT 1
        """, (payment_token,))
        pr = cur.fetchone()
        if not pr:
            return
        if isinstance(pr, tuple):
            event_id, user_id, req_kind, tip_event_id = pr[0], pr[1], pr[2], pr[3]
        else:
            event_id = pr.get("event_id")
            user_id = pr.get("user_id")
            req_kind = pr.get("kind")
            tip_event_id = pr.get("tip_event_id")
        req_kind = (req_kind or "event_fee").lower()
        if req_kind == "tip":
            if token_marked_used and event_id and user_id:
                _notify_tip_payment_completion(
                    conn,
                    event_id=int(tip_event_id or event_id),
                    user_id=int(user_id),
                    amount_yen=int(amount_yen) if amount_yen is not None else None,
                    payment_token=payment_token,
                )
            conn.commit()
            return
        cur.execute("""
            UPDATE mfu_event_member
               SET payment_status=CASE
                                    WHEN COALESCE(payment_status,'') IN ('', 'unpaid') THEN 'paid'
                                    ELSE payment_status
                                  END,
                   paid_at=COALESCE(paid_at, NOW()),
                   paid_amount_yen=CASE
                                     WHEN (paid_amount_yen IS NULL OR paid_amount_yen=0)
                                     THEN COALESCE(NULLIF(%s, 0), paid_amount_yen)
                                     ELSE paid_amount_yen
                                   END,
                   receipt_url=CASE
                                 WHEN (receipt_url IS NULL OR receipt_url='')
                                 THEN COALESCE(NULLIF(%s, ''), receipt_url)
                                 ELSE receipt_url
                               END,
                   payment_row_id=CASE
                                    WHEN (payment_row_id IS NULL OR payment_row_id=0)
                                    THEN COALESCE(NULLIF(%s, 0), payment_row_id)
                                    ELSE payment_row_id
                                  END
             WHERE event_id=%s AND user_id=%s
        """, (amount_yen, receipt_url, payment_row_id, event_id, user_id))
        member_updated = bool(cur.rowcount)
        conn.commit()
        if (token_marked_used or member_updated) and event_id and user_id:
            try:
                _notify_mfu_payment_completion(
                    conn,
                    event_id=int(event_id),
                    user_id=int(user_id),
                    amount_yen=int(amount_yen) if amount_yen is not None else None,
                    receipt_url=receipt_url,
                    payment_status=payment_status,
                )
            except Exception:
                logging.exception("mfu notify failed")
    finally:
        try:
            cur.close()
        except Exception:
            pass

# ───────────────────────────────────────────────────────────
# 事前チェック API（重複決済/最新金額の確認）
# ───────────────────────────────────────────────────────────
@bp.post("/api/precheck/<event_uuid>")
def api_precheck(event_uuid: str):
    """
    決済前の事前チェック:
      - 入力バリデーション（ニックネーム、X/Instagram のどちらか必須）
      - 現在の金額（fee_yen）をDBから取得して返却（デフォ値に依存しない）
      - ※ 重複決済チェックは payment 側では行わない
    レスポンス: { ok: bool, message?: str, amount_yen?: int }
    """
    data = (request.get_json(silent=True) or {})
    nickname = (data.get("nickname") or "").strip()
    x_id = _normalize_handle(data.get("x_id"))
    instagram_id = _normalize_handle(data.get("instagram_id"))
    token = (data.get("payment_token") or request.args.get("payment_token") or "").strip() or None

    me = _mfu_event_by_payment_uuid(event_uuid)
    if not me:
        return jsonify(ok=False, message="イベントが見つかりません。"), 404

    if not nickname:
        return jsonify(ok=False, message="ニックネームは必須です。"), 400
    if not x_id and not instagram_id:
        return jsonify(ok=False, message="X ID または Instagram ID を入力してください。"), 400

    conn = _get_db()
    try:
        token_amount = _amount_for_payment(conn, event_uuid, token)
    finally:
        try: conn.close()
        except Exception: pass

    amount = int(token_amount or me["fee_yen"] or 0)
    if amount <= 0:
        return jsonify(ok=False, message="このイベントには参加費が設定されていません。"), 400

    # ★ 重複決済チェックはイベント管理システム側で実施するため、ここでは常に金額だけ返す
    return jsonify(ok=True, amount_yen=amount)


def _event_checkout_otp_context(event_uuid: str, payment_token: str | None) -> tuple[str, str]:
    token = (payment_token or "").strip()
    if not token:
        raise CheckoutOtpError(
            "支払いトークンが見つかりません。イベントページから開き直してください。",
            400,
            "missing_payment_token",
        )
    conn = _get_db()
    try:
        token_amount = _amount_for_payment(conn, event_uuid, token)
        if token_amount is None:
            raise CheckoutOtpError("支払いトークンが無効です。", 400, "invalid_payment_token")
        _buyer_user_id, buyer_email = _resolve_buyer_identity(conn, event_uuid, token)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not buyer_email:
        raise CheckoutOtpError("登録メールアドレスを確認できません。", 400, "missing_email")
    return f"{event_uuid}:{token}:{int(token_amount)}", buyer_email


def _otp_json_error(exc: CheckoutOtpError):
    return jsonify(ok=False, error=exc.error_code, message=exc.message), exc.status_code


@bp.post("/api/otp/send/<event_uuid>")
def api_otp_send(event_uuid: str):
    data = request.get_json(silent=True) or {}
    try:
        checkout_key, buyer_email = _event_checkout_otp_context(event_uuid, data.get("payment_token"))
        result = send_checkout_otp(checkout_type="event", checkout_key=checkout_key, email=buyer_email)
        return jsonify(ok=True, **result)
    except CheckoutOtpError as exc:
        return _otp_json_error(exc)


@bp.post("/api/otp/verify/<event_uuid>")
def api_otp_verify(event_uuid: str):
    data = request.get_json(silent=True) or {}
    try:
        checkout_key, buyer_email = _event_checkout_otp_context(event_uuid, data.get("payment_token"))
        result = verify_checkout_otp(
            checkout_type="event",
            checkout_key=checkout_key,
            email=buyer_email,
            code=data.get("code"),
        )
        result["billing_contact"] = {"email": buyer_email, "countryCode": "JP"}
        return jsonify(ok=True, **result)
    except CheckoutOtpError as exc:
        return _otp_json_error(exc)

# ───────────────────────────────────────────────────────────
# 参加者向け：決済フォーム & サンクス
# ───────────────────────────────────────────────────────────
@bp.get("/e/<event_uuid>")
def pay_form(event_uuid: str):
    _ensure_schema()
    _autoprovision_event_from_mfu(event_uuid)

    payment_token = _resolve_payment_token(event_uuid)
    force_square_card = False
    is_tip_payment = False
    buyer_email = None
    conn = _get_db()
    try:
        amount, evrow = _get_live_amount_and_sync(conn, event_uuid)
        token_amount = _amount_for_payment(conn, event_uuid, payment_token)
        pr_ctx = _payment_request_context(conn, event_uuid, payment_token)
        if payment_token:
            _buyer_user_id, buyer_email = _resolve_buyer_identity(conn, event_uuid, payment_token)
        is_tip_payment = bool(pr_ctx.get("is_tip"))
        if token_amount is not None and token_amount > 0:
            amount = token_amount
    finally:
        try: conn.close()
        except Exception: pass

    if not evrow or amount <= 0:
        return "この決済リンクは無効です。", 404

    # 画面表示用イベント（タイトルは mfu_event に合わせる）
    me = _mfu_event_by_payment_uuid(event_uuid) or {}
    event = {
        "uuid": event_uuid,
        "title": me.get("title") or evrow.get("title") or "イベント",
        "default_amount": int(evrow.get("default_amount") or amount),
    }

    # ★ セッションからプリフィル（決済UUID一致 or 後方互換で mfu_event_uuid がある場合も許可）
    autofill = {}
    return_url = None
    try:
        ctx = session.get("pay_ctx") or {}
        if ctx and (ctx.get("payment_uuid") == event_uuid or ctx.get("mfu_event_uuid")):
            autofill = {
                "nickname": ctx.get("nickname") or "",
                "x_id": ctx.get("x_id") or "",
                "instagram_id": ctx.get("instagram_id") or "",
            }
            return_url = _sanitize_return_url(ctx.get("return_url"))
    except Exception:
        logging.exception("read pay_ctx failed")

    qs_return_url = (request.args.get("return_url") or "").strip()
    if qs_return_url:
        qs_return_url_sanitized = _sanitize_return_url(qs_return_url)
        if qs_return_url_sanitized:
            return_url = qs_return_url_sanitized

    try:
        pay_ctx = session.get("pay_ctx") or {}
        if not isinstance(pay_ctx, dict):
            pay_ctx = {}
        merged_pay_ctx = dict(pay_ctx)
        if return_url:
            merged_pay_ctx["return_url"] = return_url
        merged_pay_ctx["payment_uuid"] = event_uuid
        session["pay_ctx"] = merged_pay_ctx
    except Exception:
        logging.exception("write pay_ctx failed on pay_form")

    # クエリがあれば上書き
    qs_n = (request.args.get("nickname") or "").strip()
    qs_x = _sanitize_handle(request.args.get("x_id"))
    qs_i = _sanitize_handle(request.args.get("instagram_id"))
    if qs_n or qs_x or qs_i:
        autofill = {
            "nickname": qs_n or autofill.get("nickname", ""),
            "x_id": (qs_x or "") or autofill.get("x_id", ""),
            "instagram_id": (qs_i or "") or autofill.get("instagram_id", ""),
        }

    # payment_token があればDBの情報で補完（未入力のみ補う）
    if payment_token and (not autofill.get("nickname") or not autofill.get("x_id") or not autofill.get("instagram_id")):
        conn = _get_db()
        try:
            pr = _fetch_payment_request(conn, event_uuid, payment_token) or {}
        finally:
            try: conn.close()
            except Exception: pass
        if pr:
            if not autofill.get("nickname"):
                autofill["nickname"] = pr.get("nickname") or ""
            if not autofill.get("x_id"):
                autofill["x_id"] = _sanitize_handle(pr.get("x_id"))
            if not autofill.get("instagram_id"):
                autofill["instagram_id"] = _sanitize_handle(pr.get("instagram_id"))

    checkout_key = f"{event_uuid}:{payment_token}:{int(amount)}" if payment_token else ""
    otp_verified = bool(
        checkout_key
        and buyer_email
        and is_checkout_otp_verified(checkout_type="event", checkout_key=checkout_key, email=buyer_email)
    )

    return render_template(
        "pay.html",
        event=event,
        event_amount=amount,               # ← 互換のため“明示の金額”も渡す
        square_js_url=_square_js_url(),
        app_id=_square_application_id(),
        location_id=_square_location_id(),
        autofill=autofill,                 # ← テンプレはこの3項目を参照
        return_url=return_url,
        payment_token=payment_token,
        force_square_card=force_square_card,
        is_tip_payment=is_tip_payment,
        checkout_email_masked=mask_email(buyer_email),
        otp_verified=otp_verified,
        otp_send_url=url_for("payment.api_otp_send", event_uuid=event_uuid),
        otp_verify_url=url_for("payment.api_otp_verify", event_uuid=event_uuid),
    )

@bp.get("/e/<event_uuid>/thanks")
def pay_thanks(event_uuid: str):
    _ensure_schema()
    pid = request.args.get("pid")

    conn = _get_db()
    event, payment = None, None
    try:
        # 表示用にeventsを取得（amountはthanksでは使用しない）
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE uuid=%s LIMIT 1", (event_uuid,))
        event = _fetchone_dict(cur)

        if pid:
            cur.execute("SELECT * FROM event_payments WHERE square_payment_id=%s LIMIT 1", (pid,))
            payment = _fetchone_dict(cur)
            if payment:
                status_now = (payment.get("square_status") or "").upper()
                if not is_payment_completed(status_now):
                    access_token = _square_access_token()
                    if access_token:
                        try:
                            resp = request_square(
                                "GET",
                                f"{_square_api_base()}/v2/payments/{pid}",
                                access_token=access_token,
                                timeout=10
                            )
                            if resp.status_code < 400:
                                p = (resp.json() or {}).get("payment", {}) or {}
                                card = (p.get("card_details") or {}).get("card") or {}
                                cur.execute("""
                                    UPDATE event_payments
                                       SET square_status=%s,
                                           square_receipt_url=COALESCE(%s, square_receipt_url),
                                           card_brand=COALESCE(%s, card_brand),
                                           card_last4=COALESCE(%s, card_last4),
                                           card_exp_mm=COALESCE(%s, card_exp_mm),
                                            card_exp_yyyy=COALESCE(%s, card_exp_yyyy),
                                            square_updated_at=COALESCE(%s, square_updated_at),
                                            last_synced_at=NOW(), sync_attempts=sync_attempts+1,
                                            sync_error=NULL
                                     WHERE square_payment_id=%s
                                """, (
                                    p.get("status"), p.get("receipt_url"),
                                    card.get("card_brand"), card.get("last_4"),
                                    card.get("exp_month"), card.get("exp_year"),
                                    square_datetime(p.get("updated_at")),
                                    pid
                                ))
                                conn.commit()
                                cur.execute("SELECT * FROM event_payments WHERE square_payment_id=%s LIMIT 1", (pid,))
                                payment = _fetchone_dict(cur)
                        except Exception:
                            logging.exception("thanks: refresh payment status failed")

                # Discord通知（必要時）
                try:
                    _notify_discord_payment_if_needed(conn, pid)
                except Exception:
                    logging.exception("thanks: notify failed")

    finally:
        try: conn.close()
        except Exception: pass

    # 外部ログインへの自動戻り（セッションにreturn_urlがある場合）
    try:
        ctx = session.get("pay_ctx") or {}
        has_ctx = bool(ctx)
        ret = _sanitize_return_url(ctx.get("return_url")) if has_ctx else None
        ctx_payment_uuid = ctx.get("payment_uuid") if has_ctx else None
        ctx_mfu_event_uuid = ctx.get("mfu_event_uuid") if has_ctx else None
        payment_token = _resolve_payment_token(event_uuid)
        ok = bool(payment) and is_payment_completed(payment.get("square_status"))
        payment_row_id = None
        if payment:
            try:
                if isinstance(payment, dict):
                    raw_payment_row_id = payment.get("id") or payment.get("payment_row_id")
                elif isinstance(payment, (tuple, list)):
                    raw_payment_row_id = payment[0] if payment else None
                else:
                    raw_payment_row_id = getattr(payment, "id", None)
                if raw_payment_row_id is not None and str(raw_payment_row_id).strip() != "":
                    parsed_payment_row_id = int(raw_payment_row_id)
                    if parsed_payment_row_id > 0:
                        payment_row_id = parsed_payment_row_id
            except Exception:
                payment_row_id = None
        will_redirect = bool((ctx_payment_uuid == event_uuid or ctx_mfu_event_uuid) and ret)
        logging.info(
            "payment thanks redirect decision event_uuid=%s has_ctx=%s ctx_return_url=%s ctx_payment_uuid=%s ctx_mfu_event_uuid=%s payment_token=%s payment_row_id=%s will_redirect=%s",
            event_uuid,
            has_ctx,
            ret,
            ctx_payment_uuid,
            ctx_mfu_event_uuid,
            _mask_payment_token(payment_token),
            payment_row_id,
            will_redirect,
        )
        if will_redirect:
            q = {
                "status": "ok" if ok else "ng",
                "payment_id": pid or "",
                "receipt": (payment or {}).get("square_receipt_url") or "",
                "payment_token": payment_token or "",
            }
            if payment_row_id is not None:
                q["payment_row_id"] = str(payment_row_id)
            u = urlparse(ret)
            merged = dict(parse_qsl(u.query)); merged.update({k:v for k,v in q.items() if v})
            new_q = urlencode(merged)
            new_url = urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
            logging.info(
                "payment thanks redirect event_uuid=%s payment_id=%s payment_token=%s payment_row_id=%s return_url=%s",
                event_uuid,
                pid,
                _mask_payment_token(payment_token),
                payment_row_id,
                u.path or new_url,
            )
            return redirect(new_url)
    except Exception:
        logging.exception("thanks: external redirect failed")

    if not event:
        return "この決済リンクは無効です。", 404

    return render_template("thanks.html", event=event, payment=payment)

# ───────────────────────────────────────────────────────────
# 課金API
# ───────────────────────────────────────────────────────────
@bp.post("/api/charge/<event_uuid>")
def api_charge(event_uuid: str):
    _ensure_schema()
    data = request.get_json(force=True)

    nickname      = (data.get("nickname") or "").strip()
    x_id          = _sanitize_handle(data.get("x_id"))
    instagram_id  = _sanitize_handle(data.get("instagram_id"))
    source_id     = data.get("sourceId")
    payment_token = (data.get("payment_token") or request.args.get("payment_token") or "").strip() or None

    # ★ 追加：フロント（walletType）/ 後方互換（wallet_type）を受け取り、正規化
    _wt_in = data.get("walletType", data.get("wallet_type"))
    if isinstance(_wt_in, str):
        _wt_in = _wt_in.strip().upper()
    else:
        _wt_in = ""
    wallet_type = _wt_in if _wt_in in ("APPLE_PAY", "GOOGLE_PAY") else None

    if not nickname or not source_id or (not x_id and not instagram_id):
        return jsonify({"message": "ニックネームと、X IDまたはInstagram IDのいずれかは必須です。"}), 400

    conn = _get_db()
    try:
        if not payment_token:
            return jsonify({"message": "支払いトークンが見つかりません。イベントページから開き直してください。"}), 400
        token_amount = _amount_for_payment(conn, event_uuid, payment_token)
        if payment_token and token_amount is None:
            return jsonify({"message": "支払いトークンが無効です"}), 400
        _payment_request_context(conn, event_uuid, payment_token)
        buyer_user_id, buyer_email = _resolve_buyer_identity(conn, event_uuid, payment_token)
        if not buyer_user_id:
            return jsonify({"message": "支払いユーザー情報が取得できません。イベントページから開き直してください。"}), 400
        event_member_id = _resolve_member_id_by_token(conn, event_uuid, payment_token)
        if not buyer_email:
            return jsonify({"message": "メールアドレスが未登録のため決済を開始できません。"}), 400
        # ★ 常に最新金額を取得（トークンがあれば優先）
        amount, ev = _get_live_amount_and_sync(conn, event_uuid)
        if not ev or amount <= 0:
            return jsonify({"message": "イベントが見つからない/無効です"}), 404
        if token_amount is not None and token_amount > 0:
            amount = token_amount
        checkout_key = f"{event_uuid}:{payment_token}:{int(amount)}"
        try:
            require_checkout_otp(checkout_type="event", checkout_key=checkout_key, email=buyer_email)
        except CheckoutOtpError as exc:
            return jsonify({"message": exc.message, "error": exc.error_code}), exc.status_code

        cur = conn.cursor()
        lock_name = f"payment_charge:{ev['id']}:{payment_token}"
        lock_acquired = False
        cur.execute("SELECT GET_LOCK(%s, 0)", (lock_name,))
        lock_row = cur.fetchone()
        lock_val = lock_row[0] if isinstance(lock_row, tuple) else (next(iter(lock_row.values())) if isinstance(lock_row, dict) and lock_row else None)
        if int(lock_val or 0) != 1:
            return jsonify({"message": "同じ支払いが処理中です。しばらく待ってから再度お試しください。"}), 409
        lock_acquired = True
        cur.execute("""
            SELECT square_status
              FROM event_payments
             WHERE event_id=%s
               AND payment_token=%s
               AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED','COMPLETED')
             ORDER BY id DESC
             LIMIT 1
        """, (ev["id"], payment_token))
        exists = cur.fetchone()
        if exists:
            existing_status = (exists[0] if isinstance(exists, tuple) else exists.get("square_status") or "").upper()
            if existing_status in PAYMENT_IN_PROGRESS_STATUSES:
                return jsonify({"message": "この支払いは現在処理中です。時間をおいてご確認ください。"}), 409
            return jsonify({"message": "この支払いはすでに完了しています。"}), 409

        access_token = _square_access_token()
        location_id  = _square_location_id()
        if not access_token or not location_id:
            return jsonify({"message": "Square設定が未完了です"}), 500

        idemp = str(uuid.uuid4())

        # ★ 先にPENDINGで1行作成（wallet_type を保存）
        cur.execute("""
            INSERT INTO event_payments
              (event_id, nickname, x_id, instagram_id, receipt_email, amount_yen, memo,
               idempotency_key, square_status, wallet_type, payment_token, event_member_id, external_login_user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s)
        """, (ev["id"], nickname, x_id, instagram_id, buyer_email, int(amount), None, idemp, wallet_type, payment_token, event_member_id, buyer_user_id))
        pay_row_id = cur.lastrowid
        conn.commit()

        # 顧客ID
        try:
            customer_id = _ensure_customer_id_for_user(
                access_token,
                user_id=buyer_user_id,
                nickname=nickname,
                buyer_email=buyer_email,
            )
        except Exception:
            customer_id = None
            logging.exception("api_charge: Square customer creation failed; continuing without customer_id")

        # CreatePayment
        body = {
            "idempotency_key": idemp,
            "source_id": source_id,
            "amount_money": {"amount": int(amount), "currency": "JPY"},
            "location_id": location_id,
            "reference_id": f"event:{event_uuid}:pay:{pay_row_id}",
            "buyer_email_address": buyer_email,
            "customer_details": {
                "customer_initiated": True,
                "seller_keyed_in": False,
            },
        }
        if customer_id:
            body["customer_id"] = customer_id

        try:
            resp = request_square(
                "POST",
                f"{_square_api_base()}/v2/payments",
                access_token=access_token,
                json_body=body,
                timeout=25,
                idempotency_key=idemp,
            )
        except SquareTransportError as exc:
            cur.execute("""
                UPDATE event_payments
                   SET square_status='UNKNOWN', error_code='TRANSPORT_UNKNOWN',
                       error_detail=%s, sync_attempts=sync_attempts+1,
                       last_synced_at=NOW(), sync_error=%s
                 WHERE id=%s
            """, (str(exc), str(exc), pay_row_id))
            conn.commit()
            return jsonify({
                "message": "決済結果を確認中です。再度支払わず、管理者へお問い合わせください。",
                "error": "PAYMENT_RESULT_UNKNOWN",
                "payment_row_id": pay_row_id,
            }), 503
        ok = resp.status_code < 400
        try:
            payload = resp.json()
        except Exception:
            payload = {}

        if ok:
            p = payload.get("payment", {}) or {}
            status = normalize_square_status(p.get("status"))
            if status not in {"PENDING", "UNKNOWN", "AUTHORIZED", "APPROVED", "COMPLETED", "CANCELED", "FAILED"}:
                status = "UNKNOWN"
            details = p.get("card_details") or {}
            card    = details.get("card") or {}
            cur.execute("""
                UPDATE event_payments
                   SET square_payment_id=%s, square_status=%s,
                       card_brand=%s, card_last4=%s, card_exp_mm=%s, card_exp_yyyy=%s,
                       square_receipt_url=%s,
                       square_updated_at=%s, last_synced_at=NOW(),
                       sync_attempts=sync_attempts+1, sync_error=NULL,
                       error_code=NULL, error_detail=NULL
                 WHERE id=%s
            """, (p.get("id"), status, card.get("card_brand"), card.get("last_4"),
                  card.get("exp_month"), card.get("exp_year"), p.get("receipt_url"),
                  square_datetime(p.get("updated_at")), pay_row_id))
            conn.commit()
            if is_payment_completed(status):
                consume_checkout_otp(checkout_type="event", checkout_key=checkout_key, email=buyer_email)
            if payment_token and is_payment_completed(status):
                try:
                    _mark_payment_token_used_and_apply_member_status(
                        conn,
                        payment_token=payment_token,
                        amount_yen=amount,
                        receipt_url=p.get("receipt_url"),
                        payment_row_id=pay_row_id,
                        payment_status=status,
                    )
                except Exception:
                    conn.rollback()
            if is_payment_completed(status):
                try:
                    _notify_discord_payment_if_needed(conn, p.get("id"))
                except Exception:
                    logging.exception("api_charge: notify failed")
            return jsonify({
                "payment_id": p.get("id"),
                "status": p.get("status"),
                "amount": p.get("amount_money"),
                "receipt_url": p.get("receipt_url"),
                "payment_row_id": pay_row_id,
                "amount_yen": int(amount),
                "processing": not is_payment_completed(status),
            })
        else:
            errs = (payload.get("errors") or [])
            error_info = square_error_info(resp)
            code = error_info.code
            detail = error_info.detail
            uncertain = resp.status_code >= 500
            stored_status = "UNKNOWN" if uncertain else "FAILED"
            cur.execute("""
                UPDATE event_payments
                   SET square_status=%s, error_code=%s, error_detail=%s,
                       last_synced_at=NOW(), sync_attempts=sync_attempts+1, sync_error=%s
                 WHERE id=%s
            """, (stored_status, code, detail, detail if uncertain else None, pay_row_id))
            conn.commit()
            status_code = 503 if uncertain else 400
            message = "決済結果を確認中です。再度支払わず、管理者へお問い合わせください。" if uncertain else "Square API error"
            return jsonify({"message": message, "errors": errs, "payment_row_id": pay_row_id}), status_code

    finally:
        try:
            if 'cur' in locals() and 'lock_acquired' in locals() and lock_acquired:
                cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            logging.exception("api_charge: release lock failed")
        try: conn.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────
# Webhooks
# ───────────────────────────────────────────────────────────
def _register_square_webhook(conn, *, event: dict, raw_body: str, object_id: str | None) -> tuple[int | None, bool]:
    raw_event_id = str(event.get("event_id") or "").strip()
    event_id = f"payment:{raw_event_id}" if raw_event_id else ""
    event_type = str(event.get("type") or "").strip()
    if not event_id or not event_type:
        return None, False
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT IGNORE INTO square_webhook_events
              (square_event_id, event_type, object_id, payload_sha256, processing_status)
            VALUES (%s,%s,%s,%s,'RECEIVED')
        """, (event_id, event_type, object_id, hashlib.sha256(raw_body.encode("utf-8")).hexdigest()))
        inserted = bool(cur.rowcount)
        if inserted:
            webhook_row_id = int(cur.lastrowid)
            conn.commit()
            return webhook_row_id, True

        cur.execute("""
            SELECT id, processing_status
              FROM square_webhook_events
             WHERE square_event_id=%s
             LIMIT 1
        """, (event_id,))
        row = _fetchone_dict(cur)
        if row and row.get("processing_status") == "FAILED":
            cur.execute("""
                UPDATE square_webhook_events
                   SET processing_status='RECEIVED', error_detail=NULL, processed_at=NULL
                 WHERE id=%s AND processing_status='FAILED'
            """, (row["id"],))
            conn.commit()
            return int(row["id"]), bool(cur.rowcount)
        return (int(row["id"]) if row else None), False
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _finish_square_webhook(conn, webhook_row_id: int | None, *, status: str, error: str | None = None) -> None:
    if not webhook_row_id:
        return
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE square_webhook_events
               SET processing_status=%s, error_detail=%s, processed_at=NOW()
             WHERE id=%s
        """, (status, (error or None), webhook_row_id))
        conn.commit()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _payment_row_id_from_reference(reference_id: str | None) -> int | None:
    marker = ":pay:"
    if not reference_id or marker not in reference_id:
        return None
    try:
        value = int(reference_id.rsplit(marker, 1)[1])
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


@bp.post("/webhooks")
def webhooks():
    sig_key = _square_webhook_signature_key()
    if not sig_key:
        logging.critical("Square webhook signature key is missing env=%s", _env())
        return "webhook signature key is not configured", 503
    raw_body = request.get_data(as_text=True)
    try:
        from square.utilities.webhooks_helper import is_valid_webhook_event_signature
        sig_header = request.headers.get("x-square-hmacsha256-signature", "")
        url = f"{_app_base_url()}/payment/webhooks"
        if not is_valid_webhook_event_signature(raw_body, sig_header, sig_key, url):
            return "invalid signature", 403
    except Exception:
        logging.exception("webhook signature check failed")
        return "invalid signature", 403

    ev = request.get_json(silent=True) or {}
    etype = ev.get("type")
    object_data = (((ev.get("data") or {}).get("object") or {}))
    square_object = object_data.get("payment") if etype == "payment.updated" else object_data.get("refund")
    object_id = (square_object or {}).get("id") if isinstance(square_object, dict) else None
    _ensure_schema()
    conn = _get_db()
    webhook_row_id = None
    try:
        webhook_row_id, should_process = _register_square_webhook(
            conn,
            event=ev,
            raw_body=raw_body,
            object_id=object_id,
        )
        if not should_process:
            return "", 200

        if etype == "payment.updated" and isinstance(square_object, dict):
            p = square_object
            cur = conn.cursor()
            card = (p.get("card_details") or {}).get("card") or {}
            reference_row_id = _payment_row_id_from_reference(p.get("reference_id"))
            if reference_row_id:
                cur.execute("""
                    SELECT id, created_at, square_updated_at, payment_token, amount_yen, square_receipt_url
                      FROM event_payments
                     WHERE square_payment_id=%s
                        OR (id=%s AND square_payment_id IS NULL)
                     ORDER BY (square_payment_id=%s) DESC
                     LIMIT 1
                     FOR UPDATE
                """, (p.get("id"), reference_row_id, p.get("id")))
            else:
                cur.execute("""
                    SELECT id, created_at, square_updated_at, payment_token, amount_yen, square_receipt_url
                      FROM event_payments
                     WHERE square_payment_id=%s
                     LIMIT 1
                     FOR UPDATE
                """, (p.get("id"),))
            pay_row = _fetchone_dict(cur)
            if not pay_row or not _is_square_managed_record(conn, pay_row.get("created_at")):
                _finish_square_webhook(conn, webhook_row_id, status="IGNORED", error="legacy_or_unknown_payment")
                return "", 200
            if not should_apply_square_update(pay_row.get("square_updated_at"), p.get("updated_at")):
                _finish_square_webhook(conn, webhook_row_id, status="IGNORED", error="older_payment_update")
                return "", 200

            status = normalize_square_status(p.get("status"))
            if status not in {"PENDING", "UNKNOWN", "AUTHORIZED", "APPROVED", "COMPLETED", "CANCELED", "FAILED"}:
                status = "UNKNOWN"
            cur.execute("""
                UPDATE event_payments
                   SET square_payment_id=COALESCE(square_payment_id,%s),
                       square_status=%s,
                       square_receipt_url=COALESCE(%s, square_receipt_url),
                       card_brand=COALESCE(%s, card_brand),
                       card_last4=COALESCE(%s, card_last4),
                       card_exp_mm=COALESCE(%s, card_exp_mm),
                       card_exp_yyyy=COALESCE(%s, card_exp_yyyy),
                       square_updated_at=COALESCE(%s, square_updated_at),
                       last_synced_at=NOW(), sync_attempts=sync_attempts+1,
                       sync_error=NULL
                 WHERE id=%s
            """, (
                p.get("id"), status, p.get("receipt_url"),
                card.get("card_brand"), card.get("last_4"),
                card.get("exp_month"), card.get("exp_year"),
                square_datetime(p.get("updated_at")), pay_row["id"],
            ))
            conn.commit()
            if is_payment_completed(status) and pay_row.get("payment_token"):
                _mark_payment_token_used_and_apply_member_status(
                    conn,
                    payment_token=pay_row.get("payment_token"),
                    amount_yen=pay_row.get("amount_yen"),
                    receipt_url=p.get("receipt_url") or pay_row.get("square_receipt_url"),
                    payment_row_id=pay_row["id"],
                    payment_status=status,
                )
            if is_payment_completed(status):
                _notify_discord_payment_if_needed(conn, p.get("id"))
            _finish_square_webhook(conn, webhook_row_id, status="PROCESSED")

        elif etype == "refund.updated" and isinstance(square_object, dict):
            r = square_object
            cur = conn.cursor()
            cur.execute("""
                SELECT r.id, r.created_at, r.square_updated_at, e.title AS event_title
                  FROM event_refunds r
                  JOIN event_payments p ON p.id=r.payment_row_id
                  JOIN events e ON e.id=p.event_id
                 WHERE r.square_refund_id=%s
                 LIMIT 1
                 FOR UPDATE
            """, (r.get("id"),))
            refund_row = _fetchone_dict(cur)
            if not refund_row or not _is_square_managed_record(conn, refund_row.get("created_at")):
                _finish_square_webhook(conn, webhook_row_id, status="IGNORED", error="legacy_or_unknown_refund")
                return "", 200
            if not should_apply_square_update(refund_row.get("square_updated_at"), r.get("updated_at")):
                _finish_square_webhook(conn, webhook_row_id, status="IGNORED", error="older_refund_update")
                return "", 200
            status = normalize_square_status(r.get("status"))
            if status not in {"PENDING", "UNKNOWN", "APPROVED", "COMPLETED", "REJECTED", "FAILED", "CANCELED"}:
                status = "UNKNOWN"
            cur.execute("""
                UPDATE event_refunds
                   SET status=%s, square_updated_at=COALESCE(%s, square_updated_at),
                       last_synced_at=NOW(), sync_attempts=sync_attempts+1,
                       sync_error=NULL, error_code=NULL, error_detail=NULL
                 WHERE id=%s
            """, (status, square_datetime(r.get("updated_at")), refund_row["id"]))
            conn.commit()
            if is_refund_completed(status):
                _finalize_completed_refund(
                    conn,
                    refund_id=int(refund_row["id"]),
                    event_title=refund_row.get("event_title") or "イベント",
                )
            _finish_square_webhook(conn, webhook_row_id, status="PROCESSED")
        else:
            _finish_square_webhook(conn, webhook_row_id, status="IGNORED", error="unsupported_event_type")
    except Exception as exc:
        logging.exception("Square webhook processing failed type=%s", etype)
        try:
            conn.rollback()
            _finish_square_webhook(conn, webhook_row_id, status="FAILED", error=str(exc)[:1000])
        except Exception:
            logging.exception("Square webhook failure status update failed")
        return "webhook processing failed", 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return "", 200

# ───────────────────────────────────────────────────────────
# 管理UI
# ───────────────────────────────────────────────────────────
def _square_health_summary(conn) -> dict:
    cur = conn.cursor()
    try:
        managed_from = _square_managed_from(conn)
        summary = {"managed_from": managed_from}
        queries = {
            "managed_event_in_progress": "SELECT COUNT(*) AS n FROM event_payments WHERE created_at >= %s AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')",
            "managed_event_unknown_no_id": "SELECT COUNT(*) AS n FROM event_payments WHERE created_at >= %s AND square_status='UNKNOWN' AND square_payment_id IS NULL",
            "protected_event_in_progress": "SELECT COUNT(*) AS n FROM event_payments WHERE created_at < %s AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')",
            "managed_refund_in_progress": "SELECT COUNT(*) AS n FROM event_refunds WHERE created_at >= %s AND status IN ('PENDING','UNKNOWN','APPROVED')",
            "managed_refund_failed": "SELECT COUNT(*) AS n FROM event_refunds WHERE created_at >= %s AND status IN ('FAILED','REJECTED')",
            "protected_refund_in_progress": "SELECT COUNT(*) AS n FROM event_refunds WHERE created_at < %s AND status IN ('PENDING','UNKNOWN','APPROVED')",
        }
        for name, sql in queries.items():
            cur.execute(sql, (managed_from,))
            row = _fetchone_dict(cur) or {}
            summary[name] = int(row.get("n") or 0)
        cur.execute("""
            SELECT event_type, processing_status, received_at, processed_at, error_detail
              FROM square_webhook_events
             ORDER BY received_at DESC
             LIMIT 1
        """)
        summary["last_webhook"] = _fetchone_dict(cur)
        try:
            cur.execute("SELECT COUNT(*) AS n FROM invoice_card_payments WHERE created_at >= %s AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')", (managed_from,))
            summary["managed_invoice_in_progress"] = int((_fetchone_dict(cur) or {}).get("n") or 0)
            cur.execute("SELECT COUNT(*) AS n FROM invoice_card_payments WHERE created_at < %s AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')", (managed_from,))
            summary["protected_invoice_in_progress"] = int((_fetchone_dict(cur) or {}).get("n") or 0)
        except Exception:
            conn.rollback()
            summary["managed_invoice_in_progress"] = 0
            summary["protected_invoice_in_progress"] = 0
        return summary
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _record_sync_error(conn, *, table: str, row_id: int, detail: str) -> None:
    if table not in {"event_payments", "event_refunds", "invoice_card_payments"}:
        raise ValueError("unsupported Square sync table")
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE {table} SET last_synced_at=NOW(), sync_attempts=sync_attempts+1, sync_error=%s WHERE id=%s",
            ((detail or "Square sync error")[:2000], row_id),
        )
        conn.commit()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _notify_square_sync_risk_if_needed(conn, result: dict) -> None:
    risk = {
        "errors": int(result.get("errors") or 0),
        "unknown_without_id": int(result.get("unknown_without_id") or 0),
    }
    if not any(risk.values()):
        return
    signature = hashlib.sha256(repr(sorted(risk.items())).encode("utf-8")).hexdigest()
    cur = conn.cursor()
    try:
        cur.execute("SELECT last_alert_signature, last_alert_at FROM square_sync_control WHERE id=1 LIMIT 1")
        control = _fetchone_dict(cur) or {}
        last_at = control.get("last_alert_at")
        if control.get("last_alert_signature") == signature and isinstance(last_at, datetime) and last_at >= datetime.now() - timedelta(hours=1):
            return
        webhook = _get_discord_webhook_url(conn)
        if not webhook:
            return
        _discord_notify(
            webhook,
            title="⚠️ Square同期で確認が必要です",
            description="新しい安全処理の対象で同期異常を検出しました。既存の決済・返金は更新していません。",
            fields=(
                ("同期エラー", str(risk["errors"]), True),
                ("Square ID未取得", str(risk["unknown_without_id"]), True),
                ("確認画面", f"{_app_base_url().rstrip('/')}/payment/", False),
            ),
            color=0xE67E22,
        )
        cur.execute("UPDATE square_sync_control SET last_alert_signature=%s, last_alert_at=NOW() WHERE id=1", (signature,))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception("Square sync risk notification failed")
    finally:
        try:
            cur.close()
        except Exception:
            pass


def reconcile_square_managed_records(*, limit: int = 25) -> dict:
    """Synchronize only records created after this safety rollout started."""

    _ensure_schema()
    try:
        from app.invoice.services import (
            ensure_invoice_schema,
            mark_invoice_paid_by_card,
            notify_invoice_card_payment_if_needed,
        )
        from app.invoice.routes import _send_invoice_receipt_mail_if_needed
        ensure_invoice_schema()
    except Exception:
        ensure_invoice_schema = None
        mark_invoice_paid_by_card = None
        notify_invoice_card_payment_if_needed = None
        _send_invoice_receipt_mail_if_needed = None
        logging.exception("Square reconcile: invoice schema preparation failed")

    access_token = _square_access_token()
    if not access_token:
        raise RuntimeError("Square設定が未完了です")
    conn = _get_db()
    result = {"event_checked": 0, "refund_checked": 0, "invoice_checked": 0, "updated": 0, "errors": 0, "unknown_without_id": 0}
    lock_acquired = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK('square_reconcile_managed', 0)")
        lock_row = cur.fetchone()
        lock_value = lock_row[0] if isinstance(lock_row, tuple) else (next(iter(lock_row.values())) if isinstance(lock_row, dict) and lock_row else 0)
        lock_acquired = int(lock_value or 0) == 1
        if not lock_acquired:
            raise RuntimeError("別のSquare同期処理が実行中です")
        managed_from = _square_managed_from(conn)

        cur.execute("""
            SELECT id, square_payment_id, payment_token, amount_yen, square_updated_at
              FROM event_payments
             WHERE created_at >= %s
               AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')
             ORDER BY created_at
             LIMIT %s
        """, (managed_from, int(limit)))
        payment_rows = _fetchall_dict(cur)
        result["unknown_without_id"] += sum(1 for row in payment_rows if not row.get("square_payment_id"))
        for row in payment_rows:
            payment_id = row.get("square_payment_id")
            if not payment_id:
                continue
            result["event_checked"] += 1
            try:
                resp = request_square("GET", f"{_square_api_base()}/v2/payments/{payment_id}", access_token=access_token, timeout=15)
                if resp.status_code >= 400:
                    raise RuntimeError(square_error_info(resp).detail)
                payment = (resp.json() or {}).get("payment") or {}
                if not should_apply_square_update(row.get("square_updated_at"), payment.get("updated_at")):
                    continue
                status = normalize_square_status(payment.get("status"))
                card = ((payment.get("card_details") or {}).get("card") or {})
                cur.execute("""
                    UPDATE event_payments
                       SET square_status=%s, square_receipt_url=COALESCE(%s,square_receipt_url),
                           card_brand=COALESCE(%s,card_brand), card_last4=COALESCE(%s,card_last4),
                           card_exp_mm=COALESCE(%s,card_exp_mm), card_exp_yyyy=COALESCE(%s,card_exp_yyyy),
                           square_updated_at=COALESCE(%s,square_updated_at), last_synced_at=NOW(),
                           sync_attempts=sync_attempts+1, sync_error=NULL
                     WHERE id=%s
                """, (status, payment.get("receipt_url"), card.get("card_brand"), card.get("last_4"),
                      card.get("exp_month"), card.get("exp_year"), square_datetime(payment.get("updated_at")), row["id"]))
                conn.commit()
                result["updated"] += 1
                if is_payment_completed(status) and row.get("payment_token"):
                    _mark_payment_token_used_and_apply_member_status(
                        conn,
                        payment_token=row.get("payment_token"),
                        amount_yen=row.get("amount_yen"),
                        receipt_url=payment.get("receipt_url"),
                        payment_row_id=row["id"],
                        payment_status=status,
                    )
                if is_payment_completed(status):
                    _notify_discord_payment_if_needed(conn, payment_id)
            except Exception as exc:
                result["errors"] += 1
                _record_sync_error(conn, table="event_payments", row_id=int(row["id"]), detail=str(exc))

        cur.execute("""
            SELECT r.id, r.square_refund_id, r.square_updated_at, e.title AS event_title
              FROM event_refunds r
              JOIN event_payments p ON p.id=r.payment_row_id
              JOIN events e ON e.id=p.event_id
             WHERE r.created_at >= %s
               AND r.status IN ('PENDING','UNKNOWN','APPROVED')
             ORDER BY r.created_at
             LIMIT %s
        """, (managed_from, int(limit)))
        refund_rows = _fetchall_dict(cur)
        result["unknown_without_id"] += sum(1 for row in refund_rows if not row.get("square_refund_id"))
        for row in refund_rows:
            refund_id = row.get("square_refund_id")
            if not refund_id:
                continue
            result["refund_checked"] += 1
            try:
                resp = request_square("GET", f"{_square_api_base()}/v2/refunds/{refund_id}", access_token=access_token, timeout=15)
                if resp.status_code >= 400:
                    raise RuntimeError(square_error_info(resp).detail)
                refund = (resp.json() or {}).get("refund") or {}
                if not should_apply_square_update(row.get("square_updated_at"), refund.get("updated_at")):
                    continue
                status = normalize_square_status(refund.get("status"))
                cur.execute("""
                    UPDATE event_refunds
                       SET status=%s, square_updated_at=COALESCE(%s,square_updated_at),
                           last_synced_at=NOW(), sync_attempts=sync_attempts+1,
                           sync_error=NULL, error_code=NULL, error_detail=NULL
                     WHERE id=%s
                """, (status, square_datetime(refund.get("updated_at")), row["id"]))
                conn.commit()
                result["updated"] += 1
                if is_refund_completed(status):
                    _finalize_completed_refund(conn, refund_id=int(row["id"]), event_title=row.get("event_title") or "イベント")
            except Exception as exc:
                result["errors"] += 1
                _record_sync_error(conn, table="event_refunds", row_id=int(row["id"]), detail=str(exc))

        try:
            cur.execute("""
                SELECT id, invoice_id, square_payment_id, square_updated_at
                  FROM invoice_card_payments
                 WHERE created_at >= %s
                   AND square_status IN ('PENDING','UNKNOWN','AUTHORIZED','APPROVED')
                 ORDER BY created_at
                 LIMIT %s
            """, (managed_from, int(limit)))
            invoice_rows = _fetchall_dict(cur)
        except Exception:
            conn.rollback()
            invoice_rows = []
        result["unknown_without_id"] += sum(1 for row in invoice_rows if not row.get("square_payment_id"))
        for row in invoice_rows:
            payment_id = row.get("square_payment_id")
            if not payment_id:
                continue
            result["invoice_checked"] += 1
            try:
                resp = request_square("GET", f"{_square_api_base()}/v2/payments/{payment_id}", access_token=access_token, timeout=15)
                if resp.status_code >= 400:
                    raise RuntimeError(square_error_info(resp).detail)
                payment = (resp.json() or {}).get("payment") or {}
                if not should_apply_square_update(row.get("square_updated_at"), payment.get("updated_at")):
                    continue
                status = normalize_square_status(payment.get("status"))
                card = ((payment.get("card_details") or {}).get("card") or {})
                paid_at = datetime.now() if is_payment_completed(status) else None
                cur.execute("""
                    UPDATE invoice_card_payments
                       SET square_status=%s, square_receipt_url=COALESCE(%s,square_receipt_url),
                           card_brand=COALESCE(%s,card_brand), card_last4=COALESCE(%s,card_last4),
                           card_exp_mm=COALESCE(%s,card_exp_mm), card_exp_yyyy=COALESCE(%s,card_exp_yyyy),
                           paid_at=COALESCE(%s,paid_at), square_updated_at=COALESCE(%s,square_updated_at),
                           last_synced_at=NOW(), sync_attempts=sync_attempts+1, sync_error=NULL
                     WHERE id=%s
                """, (status, payment.get("receipt_url"), card.get("card_brand"), card.get("last_4"),
                      card.get("exp_month"), card.get("exp_year"), paid_at, square_datetime(payment.get("updated_at")), row["id"]))
                conn.commit()
                result["updated"] += 1
                if is_payment_completed(status) and mark_invoice_paid_by_card:
                    mark_invoice_paid_by_card(int(row["invoice_id"]), paid_at=paid_at)
                    if notify_invoice_card_payment_if_needed:
                        notify_invoice_card_payment_if_needed(int(row["id"]))
                    if _send_invoice_receipt_mail_if_needed:
                        _send_invoice_receipt_mail_if_needed(
                            int(row["invoice_id"]),
                            paid_at=paid_at,
                            payment_method="クレジットカード決済",
                        )
            except Exception as exc:
                result["errors"] += 1
                _record_sync_error(conn, table="invoice_card_payments", row_id=int(row["id"]), detail=str(exc))
        _notify_square_sync_risk_if_needed(conn, result)
        return result
    finally:
        try:
            if lock_acquired:
                cur.execute("SELECT RELEASE_LOCK('square_reconcile_managed')")
                cur.fetchone()
        except Exception:
            logging.exception("Square reconcile lock release failed")
        try:
            conn.close()
        except Exception:
            pass


@bp.get("/")
@admin_required
def payment_home():
    _ensure_schema()
    conn = _get_db()
    try:
        health = _square_health_summary(conn)
    finally:
        conn.close()
    sync_result = session.pop("payment_sync_result", None)
    return render_template("admin_home.html", health=health, sync_result=sync_result)


@bp.post("/admin/sync")
@admin_required
def admin_square_sync():
    try:
        session["payment_sync_result"] = reconcile_square_managed_records(limit=25)
    except Exception as exc:
        logging.exception("manual Square reconciliation failed")
        session["payment_sync_result"] = {"error": str(exc)}
    return redirect("/payment/")


def _new_uuid() -> str:
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=\n")[:22]

def _preview_secret_key() -> str:
    return os.environ.get("BULK_REFUND_PREVIEW_SECRET") or os.environ.get("SECRET_KEY") or "mfu-bulk-refund-preview"


def _normalize_preview_rows(rows: list[dict]) -> list[dict]:
    stable = []
    for row in rows:
        stable.append({
            "payment_row_id": int(row.get("payment_row_id") or 0),
            "paid": int(row.get("paid") or 0),
            "current_fee": int(row.get("current_fee") or 0),
            "refunded_sum": int(row.get("refunded_sum") or 0),
            "reserved_refund_sum": int(row.get("reserved_refund_sum") or 0),
            "refunded_diff_total": int(row.get("refunded_diff_total") or 0),
            "reserved_diff_total": int(row.get("reserved_diff_total") or 0),
            "remaining_refundable": int(row.get("remaining_refundable") or 0),
            "diff": int(row.get("diff") or 0),
            "remaining_diff": int(row.get("remaining_diff") or 0),
            "status": row.get("status") or "",
            "reason_code": row.get("reason_code") or "",
        })
    stable.sort(key=lambda x: x["payment_row_id"])
    return stable


def _build_preview_hash(*, payment_event_id: int, payment_event_uuid: str, external_event_id: int | None, rows: list[dict]) -> str:
    return build_preview_hash(
        secret=_preview_secret_key(),
        payment_event_id=payment_event_id,
        payment_event_uuid=payment_event_uuid,
        external_event_id=external_event_id,
        rows=rows,
    )


def _reason_message(code: str) -> str:
    table = {
        "eligible": "返金実行可能です。",
        "member_fee_override_present": "会員個別料金が設定されているため手動返金してください。",
        "missing_identity": "会員識別子が不足しているため対象外です。先に手動紐付けしてください。",
        "non_success_status": "決済ステータスが返金対象外です。",
        "diff_non_positive": "差額が0以下のため返金不要です。",
        "already_refunded": "すでに差額返金が完了しています。",
    }
    return table.get(code, "対象外です。")


def _fetch_payment_event_context(conn, payment_event_id: int) -> dict | None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, uuid, title FROM events WHERE id=%s LIMIT 1", (payment_event_id,))
        ev = _fetchone_dict(cur)
        if not ev:
            return None
        cur.execute("SELECT id, fee_yen, title FROM mfu_event WHERE payment_uuid=%s LIMIT 1", (ev["uuid"],))
        mfu_ev = _fetchone_dict(cur)
        return {
            "payment_event": ev,
            "mfu_event": mfu_ev,
            "current_fee": int((mfu_ev or {}).get("fee_yen") or 0),
        }
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _member_override_value(member_row: dict) -> int:
    if "fee_yen" in member_row:
        try:
            return int(member_row.get("fee_yen") or 0)
        except Exception:
            return 0
    try:
        return int(member_row.get("custom_fee_yen") or 0)
    except Exception:
        return 0


def _bulk_refund_preview_rows(conn, payment_event_id: int) -> tuple[dict | None, list[dict]]:
    context = _fetch_payment_event_context(conn, payment_event_id)
    if not context:
        return None, []

    payment_event = context["payment_event"]
    mfu_event = context["mfu_event"] or {}
    current_fee = int(context["current_fee"] or 0)

    cur = conn.cursor()
    try:
        cur.execute("""
          SELECT p.id AS payment_row_id,
                 p.nickname,
                 p.amount_yen,
                 p.square_status,
                 p.event_member_id,
                 p.external_login_user_id,
                 p.receipt_email,
                 p.x_id,
                 p.instagram_id,
                 COALESCE(SUM(CASE WHEN r.status='COMPLETED' THEN r.amount_yen ELSE 0 END),0) AS refunded_sum,
                 COALESCE(SUM(CASE WHEN r.status IN ('PENDING','UNKNOWN','APPROVED','COMPLETED') THEN r.amount_yen ELSE 0 END),0) AS reserved_refund_sum,
                 COALESCE(SUM(CASE WHEN r.status='COMPLETED' AND r.reason=%s THEN r.amount_yen ELSE 0 END),0) AS refunded_diff_total,
                 COALESCE(SUM(CASE WHEN r.status IN ('PENDING','UNKNOWN','APPROVED','COMPLETED') AND r.reason=%s THEN r.amount_yen ELSE 0 END),0) AS reserved_diff_total
            FROM event_payments p
            LEFT JOIN event_refunds r
              ON r.payment_row_id = p.id
           WHERE p.event_id=%s
           GROUP BY p.id
           ORDER BY p.created_at DESC
        """, (_BULK_REFUND_REASON_FIXED, _BULK_REFUND_REASON_FIXED, payment_event_id))
        payments = _fetchall_dict(cur)

        has_member_fee_yen = False
        try:
            cur.execute("SHOW COLUMNS FROM mfu_event_member LIKE 'fee_yen'")
            has_member_fee_yen = bool(cur.fetchone())
            _drain_cursor_results(cur)
        except Exception:
            has_member_fee_yen = False

        rows = []
        for pay in payments:
            square_status = (pay.get("square_status") or "").upper()
            if square_status == "FAILED":
                continue

            paid = int(pay.get("amount_yen") or 0)
            refunded_sum = int(pay.get("refunded_sum") or 0)
            reserved_refund_sum = int(pay.get("reserved_refund_sum") or 0)
            refunded_diff_total = int(pay.get("refunded_diff_total") or 0)
            reserved_diff_total = int(pay.get("reserved_diff_total") or 0)
            remaining = max(paid - reserved_refund_sum, 0)
            diff = max(0, paid - current_fee)
            remaining_diff = max(0, diff - reserved_diff_total)

            member = None
            event_member_id = pay.get("event_member_id")
            if event_member_id:
                if has_member_fee_yen:
                    cur.execute("""
                        SELECT m.id, m.user_id, m.event_id, m.fee_yen, m.custom_fee_yen, u.nickname AS member_nickname
                          FROM mfu_event_member m
                          LEFT JOIN external_login_user u ON u.id = m.user_id
                         WHERE m.id=%s
                         LIMIT 1
                    """, (event_member_id,))
                else:
                    cur.execute("""
                        SELECT m.id, m.user_id, m.event_id, NULL AS fee_yen, m.custom_fee_yen, u.nickname AS member_nickname
                          FROM mfu_event_member m
                          LEFT JOIN external_login_user u ON u.id = m.user_id
                         WHERE m.id=%s
                         LIMIT 1
                    """, (event_member_id,))
                member = _fetchone_dict(cur)

            override_fee = _member_override_value(member or {})
            status, reason_code = decide_bulk_refund_status(
                has_member=bool(member),
                member_event_match=(bool(member) and (not mfu_event or int(member.get("event_id") or 0) == int(mfu_event.get("id") or 0))),
                square_status=square_status,
                override_fee=override_fee,
                diff=diff,
                remaining=remaining_diff,
            )

            participant = (member or {}).get("member_nickname") or pay.get("nickname") or "-"
            rows.append({
                "payment_row_id": int(pay["payment_row_id"]),
                "participant_name": participant,
                "paid": paid,
                "current_fee": current_fee,
                "refunded_sum": refunded_sum,
                "reserved_refund_sum": reserved_refund_sum,
                "refunded_diff_total": refunded_diff_total,
                "reserved_diff_total": reserved_diff_total,
                "remaining_refundable": remaining_diff,
                "diff": diff,
                "remaining_diff": remaining_diff,
                "status": status,
                "reason_code": reason_code,
                "reason_text": _reason_message(reason_code),
                "event_member_id": int(member.get("id")) if member and member.get("id") else None,
                "external_login_user_id": int(member.get("user_id")) if member and member.get("user_id") else pay.get("external_login_user_id"),
                "square_status": square_status,
            })

        return context, rows
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _acquire_bulk_event_lock(conn, payment_event_id: int) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT GET_LOCK(%s, 0)", (f"payment_bulk_refund:{payment_event_id}",))
        row = cur.fetchone()
        _drain_cursor_results(cur)
        val = row[0] if isinstance(row, tuple) else (row.get("GET_LOCK(%s, 0)") if isinstance(row, dict) else None)
        return int(val or 0) == 1
    except Exception:
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _release_bulk_event_lock(conn, payment_event_id: int) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT RELEASE_LOCK(%s)", (f"payment_bulk_refund:{payment_event_id}",))
        cur.fetchone()
        _drain_cursor_results(cur)
    except Exception:
        pass
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _create_refund_record(cur, *, payment_row_id: int, amount_yen: int, reason: str | None, payload: dict, ok: bool, resp_text: str, run_id: str, admin_name: str | None) -> int:
    if ok:
        refund_obj = payload.get("refund") or {}
        refund_status = normalize_square_status(refund_obj.get("status"), default="PENDING")
        if refund_status not in {"PENDING", "UNKNOWN", "APPROVED", "COMPLETED", "REJECTED", "FAILED", "CANCELED"}:
            refund_status = "UNKNOWN"
        cur.execute("""
          INSERT INTO event_refunds
          (payment_row_id, square_refund_id, amount_yen, status, reason, bulk_refund_run_id, created_by_admin,
           square_updated_at, last_synced_at, sync_attempts, error_code, error_detail, sync_error)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),1,NULL,NULL,NULL)
        """, (payment_row_id, refund_obj.get("id"), amount_yen, refund_status, reason, run_id, admin_name,
              square_datetime(refund_obj.get("updated_at"))))
    else:
        err = (payload.get("errors") or [{}])[0]
        failed_status = "UNKNOWN" if err.get("code") == "TRANSPORT_UNKNOWN" else "FAILED"
        cur.execute("""
          INSERT INTO event_refunds
          (payment_row_id, square_refund_id, amount_yen, status, reason, bulk_refund_run_id, created_by_admin,
           last_synced_at, sync_attempts, error_code, error_detail, sync_error)
          VALUES (%s,NULL,%s,%s,%s,%s,%s,NOW(),1,%s,%s,%s)
        """, (payment_row_id, amount_yen, failed_status, reason, run_id, admin_name,
              err.get("code"), err.get("detail") or resp_text,
              (err.get("detail") or resp_text) if failed_status == "UNKNOWN" else None))
    return cur.lastrowid


def _summarize_mail_error(err: Exception) -> str:
    msg = f"{type(err).__name__}: {err}".strip()
    return msg[:250] if msg else type(err).__name__


def _format_yyyymd(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _mark_refund_notify_error(conn, *, refund_id: int, reason: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE event_refunds SET notify_error=%s WHERE id=%s",
            ((reason or "notify failed")[:500], refund_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _send_bulk_refund_mail(conn, *, refund_id: int, event_title: str) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT r.id, r.payment_row_id, r.amount_yen, r.notified_at,
                   p.event_member_id
              FROM event_refunds r
              JOIN event_payments p ON p.id = r.payment_row_id
             WHERE r.id=%s
             LIMIT 1
            """,
            (refund_id,),
        )
        refund = _fetchone_dict(cur)
        if not refund:
            return {"status": "send_blocked", "reason": "refund_not_found"}
        if refund.get("notified_at"):
            return {"status": "already_notified", "reason": "already_notified"}

        member_id = refund.get("event_member_id")
        if not member_id:
            reason = "event_member_id_missing"
            _mark_refund_notify_error(conn, refund_id=int(refund_id), reason=reason)
            return {"status": "send_blocked", "reason": reason}

        cur.execute(
            "SELECT user_id FROM mfu_event_member WHERE id=%s LIMIT 1",
            (member_id,),
        )
        member = _fetchone_dict(cur)
        user_id = int(member.get("user_id")) if member and member.get("user_id") else None
        if not user_id:
            reason = "member_user_id_missing"
            _mark_refund_notify_error(conn, refund_id=int(refund_id), reason=reason)
            return {"status": "send_blocked", "reason": reason}

        cur.execute(
            "SELECT email FROM external_login_user WHERE id=%s LIMIT 1",
            (user_id,),
        )
        user = _fetchone_dict(cur)
        to_email = (user.get("email") or "").strip() if user else ""
        if not to_email:
            reason = "user_email_missing"
            _mark_refund_notify_error(conn, refund_id=int(refund_id), reason=reason)
            return {"status": "send_blocked", "reason": reason}

        refund_yen = int(refund.get("amount_yen") or 0)
        exec_date = _format_yyyymd(datetime.now())
        safe_title = (event_title or "イベント").strip() or "イベント"
        subject = f"【{safe_title}】差額返金が完了しました。"
        body = (
            "差額返金の手続きが完了しました。\n\n"
            f"イベント名: {safe_title}\n"
            f"返金額: {refund_yen:,}円\n"
            f"返金実行日: {exec_date}\n\n"
            "カード明細への反映、返金はカード会社により数日～２か月ほどかかる場合があります。\n"
            "\n"
        )
        try:
            send_mail(
                to=to_email,
                subject=subject,
                body=body,
                event_name=safe_title,
                external_login_user_id=user_id,
                mail_kind="bulk_refund_completed",
            )
        except Exception as e:
            _mark_refund_notify_error(conn, refund_id=int(refund_id), reason=_summarize_mail_error(e))
            return {"status": "send_failed", "reason": "send_mail_error"}

        cur.execute(
            """
            UPDATE event_refunds
               SET notified_at=NOW(),
                   notify_to_email=%s,
                   notify_error=NULL
             WHERE id=%s
               AND notified_at IS NULL
            """,
            (to_email, refund_id),
        )
        conn.commit()
        return {"status": "sent", "reason": "sent"}
    except Exception as e:
        logging.exception("send_bulk_refund_mail failed: refund_id=%s", refund_id)
        _mark_refund_notify_error(conn, refund_id=int(refund_id), reason=_summarize_mail_error(e))
        return {"status": "send_failed", "reason": "internal_error"}
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _update_member_after_refund_success(conn, *, payment_row_id: int, refund_yen: int) -> None:
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.amount_yen, p.event_member_id
              FROM event_payments p
             WHERE p.id=%s
             LIMIT 1
        """, (payment_row_id,))
        pay = _fetchone_dict(cur)
        if not pay:
            return
        member_id = pay.get("event_member_id")
        if not member_id:
            return

        cur.execute("""
            SELECT COALESCE(SUM(amount_yen),0) AS refunded_total
              FROM event_refunds
             WHERE payment_row_id=%s
               AND status='COMPLETED'
        """, (payment_row_id,))
        rsum = _fetchone_dict(cur) or {}
        refunded_total = int(rsum.get("refunded_total") or 0)
        paid_total = int(pay.get("amount_yen") or 0)
        new_paid = recalculate_paid_amount(original_paid=paid_total, refunded_total=refunded_total)

        cur.execute("""
            SELECT admin_note, receipt_note
              FROM mfu_event_member
             WHERE id=%s
             LIMIT 1
        """, (member_id,))
        member = _fetchone_dict(cur)
        if not member:
            return

        note = build_refund_note(dt=datetime.now(), refund_yen=int(refund_yen))
        admin_note = append_note_if_missing(member.get("admin_note"), note)
        receipt_note = append_note_if_missing(member.get("receipt_note"), note)

        cur.execute("""
            UPDATE mfu_event_member
               SET paid_amount_yen=%s,
                   admin_note=%s,
                   receipt_note=%s
             WHERE id=%s
        """, (new_paid, admin_note or None, receipt_note or None, member_id))
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _finalize_completed_refund(conn, *, refund_id: int, event_title: str) -> dict:
    """Apply accounting and notification once, and only after COMPLETED."""

    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, payment_row_id, amount_yen, status, accounting_applied_at, created_at
              FROM event_refunds
             WHERE id=%s
             LIMIT 1
             FOR UPDATE
        """, (refund_id,))
        refund = _fetchone_dict(cur)
        if not refund:
            return {"status": "missing"}
        if not _is_square_managed_record(conn, refund.get("created_at")):
            return {"status": "legacy_read_only"}
        if not is_refund_completed(refund.get("status")):
            return {"status": "not_completed"}

        if not refund.get("accounting_applied_at"):
            _update_member_after_refund_success(
                conn,
                payment_row_id=int(refund["payment_row_id"]),
                refund_yen=int(refund.get("amount_yen") or 0),
            )
            cur.execute("""
                UPDATE event_refunds
                   SET accounting_applied_at=NOW()
                 WHERE id=%s AND accounting_applied_at IS NULL
            """, (refund_id,))
            conn.commit()

        mail_result = _send_bulk_refund_mail(
            conn,
            refund_id=int(refund_id),
            event_title=event_title or "イベント",
        )
        return {"status": "completed", "mail": mail_result}
    finally:
        try:
            cur.close()
        except Exception:
            pass


@bp.get("/admin/events")
@admin_required
def admin_events():
    _ensure_schema()
    search_query = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "all").strip().lower()
    if status_filter not in ("all", "active", "inactive"):
        status_filter = "all"
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
          SELECT e.*,
                 (SELECT COUNT(*)
                    FROM event_payments p
                   WHERE p.event_id=e.id
                     AND COALESCE((
                        SELECT pr.kind
                          FROM mfu_payment_request pr
                         WHERE pr.token COLLATE utf8mb4_unicode_ci = p.payment_token COLLATE utf8mb4_unicode_ci
                         ORDER BY pr.id DESC
                         LIMIT 1
                     ),'event_fee')='event_fee'
                     AND p.square_status='COMPLETED') AS cnt,
                 (SELECT COALESCE(SUM(amount_yen),0) FROM event_payments p
                    WHERE p.event_id=e.id
                      AND COALESCE((
                        SELECT pr.kind
                          FROM mfu_payment_request pr
                         WHERE pr.token COLLATE utf8mb4_unicode_ci = p.payment_token COLLATE utf8mb4_unicode_ci
                         ORDER BY pr.id DESC
                         LIMIT 1
                      ),'event_fee')='event_fee'
                      AND p.square_status='COMPLETED') AS sum_amount,
                 (SELECT COUNT(*)
                    FROM event_payments p
                   WHERE p.event_id=e.id
                     AND COALESCE((
                        SELECT pr.kind
                          FROM mfu_payment_request pr
                         WHERE pr.token COLLATE utf8mb4_unicode_ci = p.payment_token COLLATE utf8mb4_unicode_ci
                         ORDER BY pr.id DESC
                         LIMIT 1
                     ),'event_fee')='tip'
                     AND p.square_status='COMPLETED') AS tip_cnt,
                 (SELECT COALESCE(SUM(amount_yen),0) FROM event_payments p
                    WHERE p.event_id=e.id
                      AND COALESCE((
                        SELECT pr.kind
                          FROM mfu_payment_request pr
                         WHERE pr.token COLLATE utf8mb4_unicode_ci = p.payment_token COLLATE utf8mb4_unicode_ci
                         ORDER BY pr.id DESC
                         LIMIT 1
                      ),'event_fee')='tip'
                      AND p.square_status='COMPLETED') AS tip_sum_amount
          FROM events e ORDER BY created_at DESC
        """)
        all_events = _fetchall_dict(cur)
        cur.execute("""
          SELECT COALESCE(SUM(r.amount_yen),0) AS refunded
            FROM event_refunds r
           WHERE r.status='COMPLETED'
        """)
        refund_row = _fetchone_dict(cur) or {}
    finally:
        try: conn.close()
        except Exception: pass

    gross_yen = sum(int(e.get("sum_amount") or 0) + int(e.get("tip_sum_amount") or 0) for e in all_events)
    refunded_yen = int(refund_row.get("refunded") or 0)
    summary = {
        "event_count": len(all_events),
        "active_count": sum(1 for e in all_events if int(e.get("is_active") or 0) == 1),
        "payment_count": sum(int(e.get("cnt") or 0) + int(e.get("tip_cnt") or 0) for e in all_events),
        "gross_yen": gross_yen,
        "refunded_yen": refunded_yen,
        "net_yen": max(gross_yen - refunded_yen, 0),
    }
    normalized_query = search_query.casefold()
    events = []
    for event in all_events:
        is_active = int(event.get("is_active") or 0) == 1
        if status_filter == "active" and not is_active:
            continue
        if status_filter == "inactive" and is_active:
            continue
        if normalized_query and normalized_query not in str(event.get("title") or "").casefold():
            continue
        event["total_count"] = int(event.get("cnt") or 0) + int(event.get("tip_cnt") or 0)
        event["total_amount"] = int(event.get("sum_amount") or 0) + int(event.get("tip_sum_amount") or 0)
        events.append(event)

    return render_template(
        "admin_events.html",
        events=events,
        summary=summary,
        search_query=search_query,
        status_filter=status_filter,
    )

@bp.get("/admin/events/new")
@admin_required
def admin_events_new():
    return render_template("admin_events_new.html")

@bp.post("/admin/events/new")
@admin_required
def admin_events_new_post():
    title = (request.form.get("title") or "").strip()
    date  = (request.form.get("date") or "").strip() or None
    amount= int(request.form.get("default_amount") or 1000)
    notes = (request.form.get("notes") or "").strip() or None
    if not title:
        return "タイトル必須", 400

    uid = _new_uuid()
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
          INSERT INTO events (uuid,title,date,default_amount,notes)
          VALUES (%s,%s,%s,%s,%s)
        """, (uid, title, date, amount, notes))
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass
    return redirect("/payment/admin/events")

@bp.get("/admin/events/<int:event_id>")
@admin_required
def admin_event_detail(event_id: int):
    _ensure_schema()
    kind_filter = (request.args.get("kind") or "event_fee").strip().lower()
    if kind_filter not in ("event_fee", "tip", "all"):
        kind_filter = "event_fee"
    status_filter = (request.args.get("status") or "all").strip().lower()
    if status_filter not in ("all", "completed", "pending", "failed", "refunded"):
        status_filter = "all"
    method_filter = (request.args.get("method") or "all").strip().lower()
    if method_filter not in ("all", "card", "apple_pay", "google_pay"):
        method_filter = "all"
    search_query = (request.args.get("q") or "").strip()
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except Exception:
        page = 1
    per_page = 50

    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE id=%s", (event_id,))
        event = _fetchone_dict(cur)
        if not event:
            return "イベントが見つかりません", 404

        cur.execute("""
          SELECT event_payments.*,
                 COALESCE((
                   SELECT pr.kind
                     FROM mfu_payment_request pr
                    WHERE pr.token COLLATE utf8mb4_unicode_ci = event_payments.payment_token COLLATE utf8mb4_unicode_ci
                    ORDER BY pr.id DESC
                    LIMIT 1
                 ),'event_fee') AS payment_kind
            FROM event_payments
           WHERE event_id=%s
           ORDER BY created_at DESC
        """, (event_id,))
        all_payments = _fetchall_dict(cur)

        # 返金集計
        refunds_map = {}
        if all_payments:
            ids = [p["id"] for p in all_payments]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"""
              SELECT payment_row_id,
                     COALESCE(SUM(CASE WHEN status='COMPLETED'
                           THEN amount_yen ELSE 0 END),0) AS refunded
              FROM event_refunds
              WHERE payment_row_id IN ({placeholders})
              GROUP BY payment_row_id
            """, ids)
            for r in _fetchall_dict(cur):
                refunds_map[r["payment_row_id"]] = int(r["refunded"] or 0)

        for p in all_payments:
            refunded = refunds_map.get(p["id"], 0)
            p["refunded_yen"] = refunded
            p["remaining_yen"] = max(int(p["amount_yen"]) - refunded, 0)
    finally:
        try: conn.close()
        except Exception: pass

    success_statuses = {"COMPLETED"}
    kind_counts = {
        "event_fee": sum(1 for p in all_payments if p.get("payment_kind") == "event_fee"),
        "tip": sum(1 for p in all_payments if p.get("payment_kind") == "tip"),
        "all": len(all_payments),
    }
    successful = [p for p in all_payments if str(p.get("square_status") or "").upper() in success_statuses]
    gross_yen = sum(int(p.get("amount_yen") or 0) for p in successful)
    refunded_yen = sum(int(p.get("refunded_yen") or 0) for p in all_payments)
    summary = {
        "payment_count": len(successful),
        "gross_yen": gross_yen,
        "refunded_yen": refunded_yen,
        "net_yen": max(gross_yen - refunded_yen, 0),
        "pending_count": sum(1 for p in all_payments if str(p.get("square_status") or "").upper() in PAYMENT_IN_PROGRESS_STATUSES),
        "failed_count": sum(1 for p in all_payments if str(p.get("square_status") or "").upper() in {"FAILED", "CANCELED"}),
    }

    normalized_query = search_query.casefold()
    filtered_payments = []
    for payment in all_payments:
        square_status = str(payment.get("square_status") or "").upper()
        refunded = int(payment.get("refunded_yen") or 0)
        remaining = int(payment.get("remaining_yen") or 0)
        wallet_type = str(payment.get("wallet_type") or "").upper()

        if kind_filter != "all" and payment.get("payment_kind") != kind_filter:
            continue
        if status_filter == "completed" and square_status not in success_statuses:
            continue
        if status_filter == "pending" and square_status not in PAYMENT_IN_PROGRESS_STATUSES:
            continue
        if status_filter == "failed" and square_status not in {"FAILED", "CANCELED"}:
            continue
        if status_filter == "refunded" and refunded <= 0:
            continue
        if method_filter == "apple_pay" and wallet_type != "APPLE_PAY":
            continue
        if method_filter == "google_pay" and wallet_type != "GOOGLE_PAY":
            continue
        if method_filter == "card" and wallet_type in {"APPLE_PAY", "GOOGLE_PAY"}:
            continue
        if normalized_query:
            haystack = " ".join(
                str(payment.get(key) or "")
                for key in ("nickname", "x_id", "instagram_id", "receipt_email")
            ).casefold()
            if normalized_query not in haystack:
                continue

        if refunded > 0 and remaining <= 0:
            payment["status_label"] = "返金済み"
            payment["status_class"] = "secondary"
        elif refunded > 0:
            payment["status_label"] = "一部返金"
            payment["status_class"] = "warning"
        elif square_status in success_statuses:
            payment["status_label"] = "完了"
            payment["status_class"] = "success"
        elif square_status == "UNKNOWN":
            payment["status_label"] = "要確認"
            payment["status_class"] = "danger"
        elif square_status in PAYMENT_IN_PROGRESS_STATUSES:
            payment["status_label"] = "処理中"
            payment["status_class"] = "warning"
        elif square_status == "CANCELED":
            payment["status_label"] = "キャンセル"
            payment["status_class"] = "secondary"
        else:
            payment["status_label"] = "失敗"
            payment["status_class"] = "danger"

        if wallet_type == "APPLE_PAY":
            payment["method_label"] = "Apple Pay"
        elif wallet_type == "GOOGLE_PAY":
            payment["method_label"] = "Google Pay"
        else:
            payment["method_label"] = "カード"
        payment["can_refund"] = square_status == "COMPLETED" and remaining > 0
        filtered_payments.append(payment)

    total_items = len(filtered_payments)
    total_pages = max((total_items + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    payments = filtered_payments[start:start + per_page]

    pay_url = f"{_app_base_url()}/payment/e/{event['uuid']}"
    bulk_refund_url = f"/payment/admin/events/{event_id}/bulk-refund"
    return render_template(
        "admin_event_detail.html",
        event=event,
        payments=payments,
        pay_url=pay_url,
        bulk_refund_url=bulk_refund_url,
        kind_filter=kind_filter,
        kind_counts=kind_counts,
        status_filter=status_filter,
        method_filter=method_filter,
        search_query=search_query,
        summary=summary,
        page=page,
        total_pages=total_pages,
        total_items=total_items,
    )

def _bulk_refund_summary(rows: list[dict]) -> dict:
    return {
        "payment_count": len(rows),
        "eligible_count": sum(1 for r in rows if r.get("status") == "eligible"),
        "total_refund_yen": sum(int(r.get("remaining_diff") or 0) for r in rows if r.get("status") == "eligible"),
    }


@bp.get("/admin/events/<int:event_id>/bulk-refund")
@admin_required
def admin_bulk_refund_preview_page(event_id: int):
    _ensure_schema()
    conn = _get_db()
    try:
        context, rows = _bulk_refund_preview_rows(conn, event_id)
        if not context:
            return "イベントが見つかりません", 404
        payment_event = context["payment_event"]
        mfu_event = context.get("mfu_event")
        preview_hash = _build_preview_hash(
            payment_event_id=payment_event["id"],
            payment_event_uuid=payment_event.get("uuid") or "",
            external_event_id=(mfu_event or {}).get("id"),
            rows=rows,
        )
        return render_template(
            "admin_bulk_refund_preview.html",
            event=payment_event,
            mfu_event=mfu_event,
            rows=rows,
            summary=_bulk_refund_summary(rows),
            preview_hash=preview_hash,
            refund_reason_default=_BULK_REFUND_REASON_FIXED,
            result=None,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.post("/admin/events/<int:event_id>/bulk-refund/preview")
@admin_required
def admin_bulk_refund_preview_recalc(event_id: int):
    _ensure_schema()
    conn = _get_db()
    try:
        context, rows = _bulk_refund_preview_rows(conn, event_id)
        if not context:
            return "イベントが見つかりません", 404
        payment_event = context["payment_event"]
        mfu_event = context.get("mfu_event")
        preview_hash = _build_preview_hash(
            payment_event_id=payment_event["id"],
            payment_event_uuid=payment_event.get("uuid") or "",
            external_event_id=(mfu_event or {}).get("id"),
            rows=rows,
        )
        return render_template(
            "admin_bulk_refund_preview.html",
            event=payment_event,
            mfu_event=mfu_event,
            rows=rows,
            summary=_bulk_refund_summary(rows),
            preview_hash=preview_hash,
            refund_reason_default=(request.form.get("refund_reason") or _BULK_REFUND_REASON_FIXED),
            result=None,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.post("/admin/events/<int:event_id>/bulk-refund/execute")
@admin_required
def admin_bulk_refund_execute(event_id: int):
    _ensure_schema()
    submitted_hash = (request.form.get("preview_hash") or "").strip()
    selected_ids = [x for x in (request.form.get("selected_csv") or "").split(",") if x.strip()]
    refund_reason = (request.form.get("refund_reason") or "").strip() or _BULK_REFUND_REASON_FIXED

    conn = _get_db()
    try:
        context, rows = _bulk_refund_preview_rows(conn, event_id)
        if not context:
            return "イベントが見つかりません", 404

        payment_event = context["payment_event"]
        mfu_event = context.get("mfu_event")
        computed_hash = _build_preview_hash(
            payment_event_id=payment_event["id"],
            payment_event_uuid=payment_event.get("uuid") or "",
            external_event_id=(mfu_event or {}).get("id"),
            rows=rows,
        )

        if not submitted_hash or not hmac.compare_digest(submitted_hash, computed_hash):
            preview_hash = computed_hash
            return render_template(
                "admin_bulk_refund_preview.html",
                event=payment_event,
                mfu_event=mfu_event,
                rows=rows,
                summary=_bulk_refund_summary(rows),
                preview_hash=preview_hash,
                refund_reason_default=refund_reason,
                result={"error": "内容が変更されたため、再プレビューが必要です。"},
            ), 409

        selected_set = set()
        for sid in selected_ids:
            try:
                selected_set.add(int(sid))
            except Exception:
                continue

        if not _acquire_bulk_event_lock(conn, event_id):
            preview_hash = computed_hash
            return render_template(
                "admin_bulk_refund_preview.html",
                event=payment_event,
                mfu_event=mfu_event,
                rows=rows,
                summary=_bulk_refund_summary(rows),
                preview_hash=preview_hash,
                refund_reason_default=refund_reason,
                result={"error": "別の一括返金処理が実行中です。時間をおいて再試行してください。"},
            ), 409

        run_id = str(uuid.uuid4())
        admin_name = session.get("user") if isinstance(session.get("user"), str) else "admin"
        access_token = _square_access_token()
        if not access_token:
            return "Square設定が未完了です", 500

        cur = conn.cursor()
        results = []
        try:
            for row in rows:
                payment_row_id = int(row["payment_row_id"])
                eligible = row.get("status") == "eligible"
                if payment_row_id not in selected_set:
                    results.append({"payment_row_id": payment_row_id, "result": "skipped", "reason": "not_selected"})
                    continue
                if not eligible:
                    results.append({"payment_row_id": payment_row_id, "result": "skipped", "reason": row.get("reason_code")})
                    continue

                amount = int(row.get("remaining_diff") or 0)
                if amount <= 0:
                    results.append({"payment_row_id": payment_row_id, "result": "skipped", "reason": row.get("reason_code") or "already_refunded"})
                    continue
                cur.execute("SELECT square_payment_id FROM event_payments WHERE id=%s LIMIT 1", (payment_row_id,))
                pay_row = cur.fetchone()
                _drain_cursor_results(cur)
                square_payment_id = pay_row[0] if isinstance(pay_row, tuple) else (pay_row.get("square_payment_id") if pay_row else None)
                if not square_payment_id:
                    results.append({"payment_row_id": payment_row_id, "result": "failed", "reason": "missing_square_payment_id"})
                    continue

                body = {
                    "idempotency_key": f"bulk:{run_id}:{payment_row_id}",
                    "payment_id": square_payment_id,
                    "amount_money": {"amount": amount, "currency": "JPY"},
                    "reason": refund_reason,
                }

                transport_error = None
                try:
                    resp = request_square(
                        "POST",
                        f"{_square_api_base()}/v2/refunds",
                        access_token=access_token,
                        json_body=body,
                        timeout=25,
                        idempotency_key=body["idempotency_key"],
                    )
                    ok = resp.status_code < 400
                    try:
                        payload = resp.json()
                    except Exception:
                        payload = {}
                    resp_text = resp.text
                except SquareTransportError as exc:
                    transport_error = exc
                    ok = False
                    payload = {"errors": [{"code": "TRANSPORT_UNKNOWN", "detail": str(exc)}]}
                    resp_text = str(exc)

                refund_id = _create_refund_record(
                    cur,
                    payment_row_id=payment_row_id,
                    amount_yen=amount,
                    reason=refund_reason,
                    payload=payload,
                    ok=ok,
                    resp_text=resp_text,
                    run_id=run_id,
                    admin_name=admin_name,
                )
                conn.commit()

                mail_status = None
                refund_status = normalize_square_status(((payload.get("refund") or {}).get("status")), default="UNKNOWN") if ok else "UNKNOWN"
                if ok and is_refund_completed(refund_status):
                    finalize_result = _finalize_completed_refund(
                        conn,
                        refund_id=int(refund_id),
                        event_title=payment_event.get("title") or "イベント",
                    )
                    mail_status = finalize_result.get("mail")
                results.append({
                    "payment_row_id": payment_row_id,
                    "result": ("success" if is_refund_completed(refund_status) else "pending") if ok else ("unknown" if transport_error else "failed"),
                    "reason": None if ok else ((payload.get("errors") or [{}])[0].get("code")),
                    "mail_status": mail_status,
                })
        finally:
            try:
                cur.close()
            except Exception:
                pass
            _release_bulk_event_lock(conn, event_id)

        # mysql.connector の unread result を完全回避するため、
        # 実行後の再プレビュー計算は別connectionで行う。
        conn2 = _get_db()
        try:
            context2, rows2 = _bulk_refund_preview_rows(conn2, event_id)
        finally:
            try:
                conn2.close()
            except Exception:
                pass

        preview_hash2 = _build_preview_hash(
            payment_event_id=payment_event["id"],
            payment_event_uuid=payment_event.get("uuid") or "",
            external_event_id=(mfu_event or {}).get("id"),
            rows=rows2,
        )
        return render_template(
            "admin_bulk_refund_preview.html",
            event=payment_event,
            mfu_event=mfu_event,
            rows=rows2,
            summary=_bulk_refund_summary(rows2),
            preview_hash=preview_hash2,
            refund_reason_default=refund_reason,
            result={
                "run_id": run_id,
                "rows": results,
                "success": sum(1 for r in results if r.get("result") == "success"),
                "failed": sum(1 for r in results if r.get("result") == "failed"),
                "skipped": sum(1 for r in results if r.get("result") == "skipped"),
                "mail_sent": sum(1 for r in results if (r.get("mail_status") or {}).get("status") == "sent"),
                "mail_failed": sum(1 for r in results if (r.get("mail_status") or {}).get("status") == "send_failed"),
                "mail_blocked": sum(1 for r in results if (r.get("mail_status") or {}).get("status") == "send_blocked"),
            },
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.post("/admin/event-payments/<int:payment_row_id>/bind-member")
@admin_required
def admin_bind_payment_member(payment_row_id: int):
    _ensure_schema()
    member_id_raw = (request.form.get("event_member_id") or "").strip()
    user_id_raw = (request.form.get("external_login_user_id") or "").strip()

    try:
        member_id = int(member_id_raw)
    except Exception:
        return "event_member_id は整数必須です", 400

    user_id = None
    if user_id_raw:
        try:
            user_id = int(user_id_raw)
        except Exception:
            return "external_login_user_id は整数で指定してください", 400

    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, event_id FROM event_payments WHERE id=%s LIMIT 1", (payment_row_id,))
        pay = _fetchone_dict(cur)
        if not pay:
            return "対象の決済が見つかりません", 404

        payment_event_id = int(pay["event_id"])
        context = _fetch_payment_event_context(conn, payment_event_id)
        mfu_event = (context or {}).get("mfu_event") or {}
        mfu_event_id = mfu_event.get("id")
        if not mfu_event_id:
            return "連携先イベントが見つかりません", 400

        cur.execute("SELECT id, user_id, event_id FROM mfu_event_member WHERE id=%s LIMIT 1", (member_id,))
        member = _fetchone_dict(cur)
        if not member:
            return "member が存在しません", 400
        if int(member.get("event_id") or 0) != int(mfu_event_id):
            return "member が連携先イベントに属していません", 400

        cur.execute("SELECT id FROM event_payments WHERE event_member_id=%s AND id<>%s LIMIT 1", (member_id, payment_row_id))
        duplicated = _fetchone_dict(cur)
        if duplicated:
            return "指定memberは他の決済に紐付いています（上書き不可）", 409

        resolved_user_id = user_id if user_id is not None else int(member.get("user_id") or 0)
        admin_name = session.get("user") if isinstance(session.get("user"), str) else "admin"
        memo = f"bind-member by {admin_name}: member_id={member_id}"

        cur.execute(
            """
            UPDATE event_payments
               SET event_member_id=%s,
                   external_login_user_id=%s,
                   memo=CASE
                         WHEN memo IS NULL OR memo='' THEN %s
                         ELSE CONCAT(memo, '\n', %s)
                       END
             WHERE id=%s
            """,
            (member_id, resolved_user_id, memo, memo, payment_row_id),
        )
        conn.commit()
        return redirect(f"/payment/admin/events/{payment_event_id}/bulk-refund")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@bp.get("/admin/events/<int:event_id>/export.csv")
@admin_required
def admin_event_export(event_id: int):
    _ensure_schema()
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
          SELECT created_at, nickname, x_id, instagram_id, receipt_email,
                 amount_yen, square_status, card_brand, card_last4, square_receipt_url
          FROM event_payments WHERE event_id=%s ORDER BY created_at DESC
        """, (event_id,))
        rows = _fetchall_dict(cur)
    finally:
        try: conn.close()
        except Exception: pass

    class Echo:
        def write(self, x): return x

    def generate():
        yield "\ufeff"
        writer = csv.writer(Echo())
        header = ["日時","ニックネーム","X ID","Instagram ID","レシートメール",
                  "金額(円)","ステータス","カードブランド","下4桁","レシートURL"]
        yield writer.writerow(header)
        for r in rows:
            yield writer.writerow([
                r["created_at"], r["nickname"], r["x_id"], r["instagram_id"],
                r["receipt_email"], r["amount_yen"], r["square_status"],
                r["card_brand"], r["card_last4"], r["square_receipt_url"]
            ])

    filename = f"event_{event_id}_payments.csv"
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})

@bp.post("/admin/events/<int:event_id>/toggle")
@admin_required
def admin_event_toggle(event_id: int):
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE events SET is_active=1-is_active WHERE id=%s", (event_id,))
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass
    return redirect(f"/payment/admin/events/{event_id}")

@bp.post("/admin/refund/<int:payment_row_id>")
@admin_required
def admin_refund(payment_row_id: int):
    amount_form = (request.form.get("amount_yen") or "").strip()
    reason = (request.form.get("reason") or "").strip() or None

    access_token = _square_access_token()
    if not access_token:
        return "Square設定が未完了です", 500

    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM event_payments WHERE id=%s", (payment_row_id,))
        pay = _fetchone_dict(cur)
        if not pay:
            return "対象の決済が見つかりません", 404

        event_id = int(pay["event_id"])
        cur.execute("""
          SELECT COALESCE(SUM(CASE WHEN status IN ('PENDING','UNKNOWN','APPROVED','COMPLETED')
                 THEN amount_yen ELSE 0 END),0) AS total_refunded
          FROM event_refunds WHERE payment_row_id=%s
        """, (payment_row_id,))
        totals = _fetchone_dict(cur) or {}
        already = int(totals.get("total_refunded", 0))
        total = int(pay["amount_yen"])
        remaining = max(total - already, 0)
        if remaining <= 0:
            return redirect(f"/payment/admin/events/{event_id}")

        try:
            req_amount = int(amount_form) if amount_form else remaining
        except Exception:
            return "金額の形式が正しくありません", 400
        if req_amount <= 0 or req_amount > remaining:
            return f"返金金額は 1〜{remaining} の範囲で指定してください", 400

        body = {
            "idempotency_key": str(uuid.uuid4()),
            "payment_id": pay["square_payment_id"],
            "amount_money": {"amount": req_amount, "currency": "JPY"}
        }
        if reason: body["reason"] = reason

        transport_error = None
        try:
            resp = request_square(
                "POST",
                f"{_square_api_base()}/v2/refunds",
                access_token=access_token,
                json_body=body,
                timeout=20,
                idempotency_key=body["idempotency_key"],
            )
            ok = resp.status_code < 400
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            resp_text = resp.text
        except SquareTransportError as exc:
            transport_error = exc
            ok = False
            payload = {"errors": [{"code": "TRANSPORT_UNKNOWN", "detail": str(exc)}]}
            resp_text = str(exc)

        refund_id = _create_refund_record(
            cur,
            payment_row_id=payment_row_id,
            amount_yen=req_amount,
            reason=reason,
            payload=payload,
            ok=ok,
            resp_text=resp_text,
            run_id=str(uuid.uuid4()),
            admin_name=session.get("user") if isinstance(session.get("user"), str) else "admin",
        )
        conn.commit()
        refund_status = normalize_square_status(((payload.get("refund") or {}).get("status")), default="UNKNOWN") if ok else "UNKNOWN"
        if ok and is_refund_completed(refund_status):
            cur.execute("SELECT title FROM events WHERE id=%s LIMIT 1", (event_id,))
            event_row = cur.fetchone()
            event_title = event_row[0] if isinstance(event_row, tuple) else ((event_row or {}).get("title") if event_row else None)
            _finalize_completed_refund(conn, refund_id=int(refund_id), event_title=event_title or "イベント")
        elif transport_error:
            logging.warning("admin_refund: Square result unknown payment_row_id=%s", payment_row_id)
        return redirect(f"/payment/admin/events/{event_id}#p{payment_row_id}")
    finally:
        try: conn.close()
        except Exception: pass

@bp.get("/admin/events/uuid/<event_uuid>")
@admin_required
def admin_event_detail_by_uuid(event_uuid: str):
    _ensure_schema()
    conn = _get_db()
    try:
        cur = conn.cursor()
        # payment.events の uuid は CHAR(22)（Base64 URL-safe短縮）
        cur.execute("SELECT id FROM events WHERE uuid=%s LIMIT 1", (event_uuid,))
        row = _fetchone_dict(cur)
    finally:
        try: conn.close()
        except Exception: pass

    if not row:
        return "イベントが見つかりません（UUID）", 404

    # 既存のIDルートへリダイレクトして再利用
    return redirect(f"/payment/admin/events/{row['id']}")
