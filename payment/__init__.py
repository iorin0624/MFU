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
from pathlib import Path
from datetime import datetime
from functools import wraps
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
from flask import (
    Blueprint, render_template, request, redirect, jsonify,
    session, abort, Response
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
    pass

def admin_required(f):
    if _ADMIN_REQUIRED:
        return _ADMIN_REQUIRED(f)
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user") != "admin":
            abort(403)
        return f(*args, **kwargs)
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
    return _square_env_value("WEBHOOK_SIGNATURE_KEY")

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
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"}

    ref = f"mfu_user:{int(user_id)}"

    # 検索
    try:
        sresp = requests.post(f"{base}/v2/customers/search", headers=headers,
                              json={"query": {"filter": {"reference_id": {"exact": ref}}}}, timeout=15)
        if sresp.status_code < 400:
            customers = (sresp.json() or {}).get("customers") or []
            if customers:
                customer = customers[0] or {}
                customer_id = customer.get("id")
                if not customer_id:
                    raise RuntimeError("customer id missing")
                if buyer_email and not (customer.get("email_address") or "").strip():
                    uresp = requests.put(
                        f"{base}/v2/customers/{customer_id}",
                        headers=headers,
                        json={"email_address": buyer_email},
                        timeout=15,
                    )
                    uresp.raise_for_status()
                return customer_id
    except Exception:
        logging.exception("search_customers failed")

    # 作成
    cresp = requests.post(f"{base}/v2/customers", headers=headers,
                          json={"given_name": nickname, "reference_id": ref, "email_address": buyer_email}, timeout=15)
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
        if status not in ("APPROVED", "COMPLETED"):
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
    if status not in ("AUTHORIZED", "APPROVED", "COMPLETED"):
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
  square_status        ENUM('PENDING','AUTHORIZED','APPROVED','COMPLETED','CANCELED','FAILED') NOT NULL DEFAULT 'PENDING',
  square_receipt_url   VARCHAR(512) NULL,
  card_brand           VARCHAR(32) NULL,
  card_last4           CHAR(4) NULL,
  card_exp_mm          TINYINT NULL,
  card_exp_yyyy        SMALLINT NULL,

  error_code           VARCHAR(64) NULL,
  error_detail         TEXT NULL,

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
  status            ENUM('PENDING','APPROVED','REJECTED','FAILED','CANCELED') NOT NULL DEFAULT 'PENDING',
  reason            VARCHAR(255) NULL,
  bulk_refund_run_id CHAR(36) NULL,
  created_by_admin  VARCHAR(64) NULL,
  error_code        VARCHAR(64) NULL,
  error_detail      TEXT NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_event_refunds_payment
    FOREIGN KEY (payment_row_id) REFERENCES event_payments(id)
    ON DELETE CASCADE,
  KEY ix_payment_status (payment_row_id, status),
  KEY ix_bulk_refund_run (bulk_refund_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def _ensure_schema():
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(DDL_EVENTS)
        cur.execute(DDL_PAYMENTS)
        cur.execute(DDL_REFUNDS)
        # 既存環境向けの微調整（存在すれば失敗→ロールバック）
        try:
            cur.execute("ALTER TABLE event_payments ADD COLUMN discord_notified TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur.execute("ALTER TABLE event_payments ADD COLUMN payment_token CHAR(36) NULL")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur.execute("ALTER TABLE event_payments ADD COLUMN event_member_id BIGINT UNSIGNED NULL")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur.execute("ALTER TABLE event_payments ADD COLUMN external_login_user_id BIGINT UNSIGNED NULL")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur.execute("ALTER TABLE event_payments MODIFY square_status ENUM('PENDING','AUTHORIZED','APPROVED','COMPLETED','CANCELED','FAILED') NOT NULL DEFAULT 'PENDING'")
            conn.commit()
        except Exception:
            conn.rollback()
        for idx_sql in (
            "CREATE INDEX ix_event_member_id ON event_payments(event_member_id)",
            "CREATE INDEX ix_external_login_user_id ON event_payments(external_login_user_id)",
            "CREATE INDEX ix_event_identity ON event_payments(event_id, event_member_id, external_login_user_id)",
            "CREATE INDEX ix_payment_token ON event_payments(payment_token)",
            "CREATE INDEX ix_bulk_refund_run ON event_refunds(bulk_refund_run_id)",
        ):
            try:
                cur.execute(idx_sql)
                conn.commit()
            except Exception:
                conn.rollback()
        for alter_sql in (
            "ALTER TABLE event_refunds ADD COLUMN bulk_refund_run_id CHAR(36) NULL",
            "ALTER TABLE event_refunds ADD COLUMN created_by_admin VARCHAR(64) NULL",
            "ALTER TABLE mfu_event_member ADD COLUMN receipt_note TEXT NULL",
        ):
            try:
                cur.execute(alter_sql)
                conn.commit()
            except Exception:
                conn.rollback()
        _backfill_payment_identity(conn)
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass

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
              ON pr.token = p.payment_token
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
                SELECT id, event_id, user_id, nickname, x_id, instagram_id, amount_yen, status
                  FROM mfu_payment_request
                 WHERE token=%s AND event_id=%s AND event_uuid=%s AND status='pending'
                 LIMIT 1
            """, (token, event_id, event_uuid_str))
        else:
            cur.execute("""
                SELECT id, event_id, user_id, nickname, x_id, instagram_id, amount_yen, status
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
        cur.execute("""
            UPDATE mfu_payment_request
               SET status='used', used_at=NOW()
             WHERE token=%s AND status='pending'
             LIMIT 1
        """, (payment_token,))
        cur.execute("""
            SELECT event_id, user_id
              FROM mfu_payment_request
             WHERE token=%s
             ORDER BY id DESC
             LIMIT 1
        """, (payment_token,))
        pr = cur.fetchone()
        if not pr:
            return
        event_id = pr[0] if isinstance(pr, tuple) else pr.get("event_id")
        user_id = pr[1] if isinstance(pr, tuple) else pr.get("user_id")
        cur.execute("""
            UPDATE mfu_event_member
               SET payment_status='paid',
                   paid_at=NOW(),
                   paid_amount_yen=%s,
                   receipt_url=COALESCE(%s, receipt_url),
                   payment_row_id=%s
             WHERE event_id=%s AND user_id=%s
               AND COALESCE(payment_status,'unpaid') <> 'paid'
        """, (amount_yen, receipt_url, payment_row_id, event_id, user_id))
        conn.commit()
        if event_id and user_id:
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

# ───────────────────────────────────────────────────────────
# 参加者向け：決済フォーム & サンクス
# ───────────────────────────────────────────────────────────
@bp.get("/e/<event_uuid>")
def pay_form(event_uuid: str):
    _ensure_schema()
    _autoprovision_event_from_mfu(event_uuid)

    payment_token = _resolve_payment_token(event_uuid)
    conn = _get_db()
    try:
        amount, evrow = _get_live_amount_and_sync(conn, event_uuid)
        token_amount = _amount_for_payment(conn, event_uuid, payment_token)
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
            return_url = ctx.get("return_url")
    except Exception:
        logging.exception("read pay_ctx failed")

    qs_return_url = (request.args.get("return_url") or "").strip()
    if qs_return_url:
        return_url = qs_return_url

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
                if status_now not in ("APPROVED", "COMPLETED", "AUTHORIZED"):
                    access_token = _square_access_token()
                    if access_token:
                        try:
                            resp = requests.get(
                                f"{_square_api_base()}/v2/payments/{pid}",
                                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
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
                                           card_exp_yyyy=COALESCE(%s, card_exp_yyyy)
                                     WHERE square_payment_id=%s
                                """, (
                                    p.get("status"), p.get("receipt_url"),
                                    card.get("card_brand"), card.get("last_4"),
                                    card.get("exp_month"), card.get("exp_year"),
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
        if (ctx.get("payment_uuid") == event_uuid or ctx.get("mfu_event_uuid")) and ctx.get("return_url"):
            ret = ctx.get("return_url")
            payment_token = _resolve_payment_token(event_uuid)
            ok = bool(payment) and ((payment.get("square_status") or "").upper() in ("AUTHORIZED","APPROVED","COMPLETED"))
            q = {
                "status": "ok" if ok else "ng",
                "payment_id": pid or "",
                "receipt": (payment or {}).get("square_receipt_url") or "",
                "payment_token": payment_token or "",
            }
            u = urlparse(ret)
            merged = dict(parse_qsl(u.query)); merged.update({k:v for k,v in q.items() if v})
            new_q = urlencode(merged)
            new_url = urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
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

        # 二重決済ブロック（event_payments に対して）—削除
        cur = conn.cursor()

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
            return jsonify({"message": "顧客の作成に失敗しました"}), 500

        # CreatePayment
        body = {
            "idempotency_key": idemp,
            "source_id": source_id,
            "amount_money": {"amount": int(amount), "currency": "JPY"},
            "location_id": location_id,
            "reference_id": f"event:{event_uuid}:pay:{pay_row_id}",
            "customer_id": customer_id,
            "buyer_email_address": buyer_email,
        }

        resp = requests.post(
            f"{_square_api_base()}/v2/payments",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type":"application/json", "Accept":"application/json"},
            json=body, timeout=25
        )
        ok = resp.status_code < 400
        try:
            payload = resp.json()
        except Exception:
            payload = {}

        if ok:
            p = payload.get("payment", {}) or {}
            status  = p.get("status") or "AUTHORIZED"
            details = p.get("card_details") or {}
            card    = details.get("card") or {}
            cur.execute("""
                UPDATE event_payments
                   SET square_payment_id=%s, square_status=%s,
                       card_brand=%s, card_last4=%s, card_exp_mm=%s, card_exp_yyyy=%s,
                       square_receipt_url=%s,
                       error_code=NULL, error_detail=NULL
                 WHERE id=%s
            """, (p.get("id"), status, card.get("card_brand"), card.get("last_4"),
                  card.get("exp_month"), card.get("exp_year"), p.get("receipt_url"), pay_row_id))
            conn.commit()
            if payment_token:
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
            })
        else:
            errs = (payload.get("errors") or [])
            code = errs[0].get("code") if errs else None
            detail = errs[0].get("detail") if errs else resp.text
            cur.execute("""
                UPDATE event_payments
                   SET square_status='FAILED', error_code=%s, error_detail=%s
                 WHERE id=%s
            """, (code, detail, pay_row_id))
            conn.commit()
            return jsonify({"message": "Square API error", "errors": errs}), 400

    finally:
        try: conn.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────
# Webhooks
# ───────────────────────────────────────────────────────────
@bp.post("/webhooks")
def webhooks():
    sig_key = _square_webhook_signature_key()
    if sig_key:
        try:
            from square.utilities.webhooks_helper import is_valid_webhook_event_signature
            sig_header = request.headers.get("x-square-hmacsha256-signature", "")
            raw_body = request.get_data(as_text=True)
            url = f"{_app_base_url()}/payment/webhooks"
            if not is_valid_webhook_event_signature(raw_body, sig_header, sig_key, url):
                return "invalid signature", 403
        except Exception:
            logging.exception("webhook signature check failed")

    ev = request.get_json(silent=True) or {}
    etype = ev.get("type")

    if etype == "payment.updated":
        p = ev["data"]["object"]["payment"]
        conn = _get_db()
        try:
            cur = conn.cursor()
            card = (p.get("card_details") or {}).get("card") or {}
            cur.execute("""
                UPDATE event_payments
                SET square_status=%s,
                    square_receipt_url=COALESCE(%s, square_receipt_url),
                    card_brand=COALESCE(%s, card_brand),
                    card_last4=COALESCE(%s, card_last4),
                    card_exp_mm=COALESCE(%s, card_exp_mm),
                    card_exp_yyyy=COALESCE(%s, card_exp_yyyy)
                WHERE square_payment_id=%s
            """, (
                p.get("status"), p.get("receipt_url"),
                card.get("card_brand"), card.get("last_4"),
                card.get("exp_month"), card.get("exp_year"),
                p.get("id")
            ))
            conn.commit()
            if (p.get("status") or "").upper() in ("AUTHORIZED", "APPROVED", "COMPLETED"):
                cur.execute("""
                    SELECT id, payment_token, amount_yen, square_receipt_url
                      FROM event_payments
                     WHERE square_payment_id=%s
                     LIMIT 1
                """, (p.get("id"),))
                pay_row = cur.fetchone()
                if pay_row:
                    pay_row_id = pay_row[0] if isinstance(pay_row, tuple) else pay_row.get("id")
                    payment_token = pay_row[1] if isinstance(pay_row, tuple) else pay_row.get("payment_token")
                    amount_yen = pay_row[2] if isinstance(pay_row, tuple) else pay_row.get("amount_yen")
                    receipt_url = pay_row[3] if isinstance(pay_row, tuple) else pay_row.get("square_receipt_url")
                    if payment_token:
                        _mark_payment_token_used_and_apply_member_status(
                            conn,
                            payment_token=payment_token,
                            amount_yen=amount_yen,
                            receipt_url=receipt_url,
                            payment_row_id=pay_row_id,
                            payment_status=p.get("status"),
                        )
            _notify_discord_payment_if_needed(conn, p.get("id"))
        finally:
            try: conn.close()
            except Exception: pass

    elif etype == "refund.updated":
        r = ev["data"]["object"]["refund"]
        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute("""
              UPDATE event_refunds
              SET status=%s, error_code=NULL, error_detail=NULL
              WHERE square_refund_id=%s
            """, (r.get("status"), r.get("id")))
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass

    return "", 200

# ───────────────────────────────────────────────────────────
# 管理UI
# ───────────────────────────────────────────────────────────
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
            "remaining_refundable": int(row.get("remaining_refundable") or 0),
            "diff": int(row.get("diff") or 0),
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
        "diff_exceeds_remaining": "差額が返金可能残額を超えるため対象外です。",
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
                 COALESCE(SUM(CASE WHEN r.status IN ('PENDING','APPROVED') THEN r.amount_yen ELSE 0 END),0) AS refunded_sum
            FROM event_payments p
            LEFT JOIN event_refunds r
              ON r.payment_row_id = p.id
           WHERE p.event_id=%s
           GROUP BY p.id
           ORDER BY p.created_at DESC
        """, (payment_event_id,))
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
            paid = int(pay.get("amount_yen") or 0)
            refunded_sum = int(pay.get("refunded_sum") or 0)
            remaining = max(paid - refunded_sum, 0)
            diff = paid - current_fee

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

            square_status = (pay.get("square_status") or "").upper()
            override_fee = _member_override_value(member or {})
            status, reason_code = decide_bulk_refund_status(
                has_member=bool(member),
                member_event_match=(bool(member) and (not mfu_event or int(member.get("event_id") or 0) == int(mfu_event.get("id") or 0))),
                square_status=square_status,
                override_fee=override_fee,
                diff=diff,
                remaining=remaining,
            )

            participant = (member or {}).get("member_nickname") or pay.get("nickname") or "-"
            rows.append({
                "payment_row_id": int(pay["payment_row_id"]),
                "participant_name": participant,
                "paid": paid,
                "current_fee": current_fee,
                "refunded_sum": refunded_sum,
                "remaining_refundable": remaining,
                "diff": diff,
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


def _create_refund_record(cur, *, payment_row_id: int, amount_yen: int, reason: str | None, payload: dict, ok: bool, resp_text: str, run_id: str, admin_name: str | None) -> None:
    if ok:
        refund_obj = payload.get("refund") or {}
        cur.execute("""
          INSERT INTO event_refunds
          (payment_row_id, square_refund_id, amount_yen, status, reason, bulk_refund_run_id, created_by_admin, error_code, error_detail)
          VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,NULL)
        """, (payment_row_id, refund_obj.get("id"), amount_yen, refund_obj.get("status") or "PENDING", reason, run_id, admin_name))
    else:
        err = (payload.get("errors") or [{}])[0]
        cur.execute("""
          INSERT INTO event_refunds
          (payment_row_id, square_refund_id, amount_yen, status, reason, bulk_refund_run_id, created_by_admin, error_code, error_detail)
          VALUES (%s,NULL,%s,'FAILED',%s,%s,%s,%s,%s)
        """, (payment_row_id, amount_yen, reason, run_id, admin_name, err.get("code"), err.get("detail") or resp_text))


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


@bp.get("/admin/events")
@admin_required
def admin_events():
    _ensure_schema()
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
          SELECT e.*,
                 (SELECT COUNT(*) FROM event_payments p WHERE p.event_id=e.id) AS cnt,
                 (SELECT COALESCE(SUM(amount_yen),0) FROM event_payments p
                    WHERE p.event_id=e.id AND p.square_status IN ('AUTHORIZED','APPROVED','COMPLETED')) AS sum_amount
          FROM events e ORDER BY created_at DESC
        """)
        events = _fetchall_dict(cur)
    finally:
        try: conn.close()
        except Exception: pass
    return render_template("admin_events.html", events=events)

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
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE id=%s", (event_id,))
        event = _fetchone_dict(cur)
        if not event:
            return "イベントが見つかりません", 404

        cur.execute("""
          SELECT * FROM event_payments
          WHERE event_id=%s
          ORDER BY created_at DESC
        """, (event_id,))
        payments = _fetchall_dict(cur)

        # 返金集計
        refunds_map = {}
        if payments:
            ids = [p["id"] for p in payments]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"""
              SELECT payment_row_id,
                     COALESCE(SUM(CASE WHEN status IN ('PENDING','APPROVED')
                           THEN amount_yen ELSE 0 END),0) AS refunded
              FROM event_refunds
              WHERE payment_row_id IN ({placeholders})
              GROUP BY payment_row_id
            """, ids)
            for r in _fetchall_dict(cur):
                refunds_map[r["payment_row_id"]] = int(r["refunded"] or 0)

        for p in payments:
            refunded = refunds_map.get(p["id"], 0)
            p["refunded_yen"] = refunded
            p["remaining_yen"] = max(int(p["amount_yen"]) - refunded, 0)
    finally:
        try: conn.close()
        except Exception: pass

    pay_url = f"{_app_base_url()}/payment/e/{event['uuid']}"
    bulk_refund_url = f"/payment/admin/events/{event_id}/bulk-refund"
    return render_template("admin_event_detail.html", event=event, payments=payments, pay_url=pay_url, bulk_refund_url=bulk_refund_url)

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
            preview_hash=preview_hash,
            refund_reason_default="イベント参加費差額の一括返金",
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
            preview_hash=preview_hash,
            refund_reason_default=(request.form.get("refund_reason") or "イベント参加費差額の一括返金"),
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
    refund_reason = (request.form.get("refund_reason") or "").strip() or "イベント参加費差額の一括返金"

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

                amount = int(row.get("diff") or 0)
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

                resp = requests.post(
                    f"{_square_api_base()}/v2/refunds",
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"},
                    json=body,
                    timeout=25,
                )
                ok = resp.status_code < 400
                try:
                    payload = resp.json()
                except Exception:
                    payload = {}

                _create_refund_record(
                    cur,
                    payment_row_id=payment_row_id,
                    amount_yen=amount,
                    reason=refund_reason,
                    payload=payload,
                    ok=ok,
                    resp_text=resp.text,
                    run_id=run_id,
                    admin_name=admin_name,
                )
                if ok:
                    _update_member_after_refund_success(conn, payment_row_id=payment_row_id, refund_yen=amount)
                conn.commit()
                results.append({"payment_row_id": payment_row_id, "result": "success" if ok else "failed", "reason": None if ok else ((payload.get("errors") or [{}])[0].get("code"))})
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
            preview_hash=preview_hash2,
            refund_reason_default=refund_reason,
            result={
                "run_id": run_id,
                "rows": results,
                "success": sum(1 for r in results if r.get("result") == "success"),
                "failed": sum(1 for r in results if r.get("result") == "failed"),
                "skipped": sum(1 for r in results if r.get("result") == "skipped"),
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
          SELECT COALESCE(SUM(CASE WHEN status IN ('PENDING','APPROVED')
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

        resp = requests.post(
            f"{_square_api_base()}/v2/refunds",
            headers={"Authorization": f"Bearer {access_token}","Content-Type": "application/json","Accept": "application/json"},
            json=body, timeout=20
        )
        ok = resp.status_code < 400
        payload = {}
        try: payload = resp.json()
        except Exception: pass

        if ok:
            r = payload.get("refund") or {}
            cur.execute("""
              INSERT INTO event_refunds
              (payment_row_id, square_refund_id, amount_yen, status, reason, error_code, error_detail)
              VALUES (%s,%s,%s,%s,%s,NULL,NULL)
            """, (payment_row_id, r.get("id"), req_amount, r.get("status") or "PENDING", reason))
        else:
            errs = (payload.get("errors") or [{}])
            cur.execute("""
              INSERT INTO event_refunds
              (payment_row_id, square_refund_id, amount_yen, status, reason, error_code, error_detail)
              VALUES (%s,NULL,%s,'FAILED',%s,%s,%s)
            """, (payment_row_id, req_amount, reason, errs[0].get("code"), errs[0].get("detail") or resp.text))
        conn.commit()
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
