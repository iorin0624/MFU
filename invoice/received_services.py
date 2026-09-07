from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from flask import current_app, render_template

from app.utils.db import get_db
from app.utils.mail import send_mail

RECEIVED_STATUSES = {
    "draft": "下書き",
    "sent": "依頼送信済み",
    "editing": "相手方が編集中",
    "submitted": "提出済み",
    "revision_requested": "修正依頼中",
    "accepted": "承認済み・支払待ち",
    "paid": "支払済み",
    "cancelled": "取消",
    "expired": "期限切れ",
}
TAX_RATES = {"tax10": Decimal("0.10"), "tax8": Decimal("0.08"), "nontax": Decimal("0")}
TAX_MODE_LABELS = {"internal": "内税", "external": "外税"}
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_REQUEST_HOURLY_LIMIT = 10
OTP_IP_HOURLY_LIMIT = 5
GRANT_SECONDS = 2 * 60 * 60
INVITE_DAYS = 14
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif"}


class ReceivedInvoiceError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> datetime:
    return datetime.now()


_schema_lock = threading.Lock()
_schema_ready = False


def _create_received_invoice_schema() -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_requests (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              public_id CHAR(36) NOT NULL UNIQUE,
              request_no VARCHAR(40) NOT NULL UNIQUE,
              status VARCHAR(32) NOT NULL DEFAULT 'draft',
              auth_version BIGINT NOT NULL DEFAULT 1,
              content_version BIGINT NOT NULL DEFAULT 1,
              counterparty_name VARCHAR(191) NOT NULL,
              counterparty_email VARCHAR(320) NOT NULL,
              bill_to_name VARCHAR(191) NOT NULL,
              bill_to_postal_code VARCHAR(32) NULL,
              bill_to_address1 VARCHAR(255) NULL,
              bill_to_address2 VARCHAR(255) NULL,
              bill_to_phone VARCHAR(64) NULL,
              bill_to_email VARCHAR(320) NULL,
              subject VARCHAR(255) NOT NULL,
              service_date DATE NULL,
              due_date DATE NULL,
              admin_note TEXT NULL,
              tax_mode VARCHAR(16) NOT NULL DEFAULT 'internal',
              issuer_name VARCHAR(191) NULL,
              issuer_postal_code VARCHAR(32) NULL,
              issuer_address1 VARCHAR(255) NULL,
              issuer_address2 VARCHAR(255) NULL,
              issuer_phone VARCHAR(64) NULL,
              issuer_email VARCHAR(320) NULL,
              issuer_registration_no VARCHAR(32) NULL,
              invoice_no VARCHAR(64) NULL,
              issue_date DATE NULL,
              bank_info TEXT NULL,
              recipient_note TEXT NULL,
              subtotal_yen INT NOT NULL DEFAULT 0,
              tax_10_yen INT NOT NULL DEFAULT 0,
              tax_8_yen INT NOT NULL DEFAULT 0,
              tax_yen INT NOT NULL DEFAULT 0,
              total_yen INT NOT NULL DEFAULT 0,
              pdf_storage_path VARCHAR(512) NULL,
              sent_at DATETIME NULL,
              submitted_at DATETIME NULL,
              accepted_at DATETIME NULL,
              paid_at DATETIME NULL,
              revision_requested_at DATETIME NULL,
              expires_at DATETIME NULL,
              created_at DATETIME NOT NULL,
              updated_at DATETIME NOT NULL,
              INDEX idx_received_invoice_status (status, updated_at),
              INDEX idx_received_invoice_email (counterparty_email)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM received_invoice_requests LIKE 'content_version'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_requests ADD COLUMN content_version BIGINT NOT NULL DEFAULT 1 AFTER auth_version")
        cur.execute("SHOW COLUMNS FROM received_invoice_requests LIKE 'tax_mode'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_requests ADD COLUMN tax_mode VARCHAR(16) NOT NULL DEFAULT 'internal' AFTER admin_note")
            # Rows created before tax-mode support were always calculated as
            # external tax. Preserve their amounts while making new rows internal.
            cur.execute("UPDATE received_invoice_requests SET tax_mode='external'")
        split_tax_columns_added = False
        cur.execute("SHOW COLUMNS FROM received_invoice_requests LIKE 'tax_10_yen'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_requests ADD COLUMN tax_10_yen INT NOT NULL DEFAULT 0 AFTER subtotal_yen")
            split_tax_columns_added = True
        cur.execute("SHOW COLUMNS FROM received_invoice_requests LIKE 'tax_8_yen'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_requests ADD COLUMN tax_8_yen INT NOT NULL DEFAULT 0 AFTER tax_10_yen")
            split_tax_columns_added = True
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_items (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              request_id BIGINT NOT NULL,
              source VARCHAR(16) NOT NULL DEFAULT 'admin',
              locked_by_admin TINYINT(1) NOT NULL DEFAULT 1,
              sort_order INT NOT NULL DEFAULT 0,
              row_type VARCHAR(16) NOT NULL DEFAULT 'normal',
              item_name VARCHAR(255) NOT NULL,
              memo_text TEXT NULL,
              quantity DECIMAL(12,2) NOT NULL DEFAULT 1.00,
              unit_name VARCHAR(32) NULL,
              unit_price_yen INT NOT NULL DEFAULT 0,
              tax_category VARCHAR(16) NOT NULL DEFAULT 'tax10',
              usage_date DATE NULL,
              detail VARCHAR(255) NULL,
              line_subtotal_yen INT NOT NULL DEFAULT 0,
              line_tax_yen INT NOT NULL DEFAULT 0,
              line_total_yen INT NOT NULL DEFAULT 0,
              created_at DATETIME NOT NULL,
              updated_at DATETIME NOT NULL,
              INDEX idx_received_items_request (request_id, sort_order, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM received_invoice_items LIKE 'row_type'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_items ADD COLUMN row_type VARCHAR(16) NOT NULL DEFAULT 'normal' AFTER sort_order")
        cur.execute("SHOW COLUMNS FROM received_invoice_items LIKE 'memo_text'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE received_invoice_items ADD COLUMN memo_text TEXT NULL AFTER item_name")
        if split_tax_columns_added:
            cur.execute(
                """
                UPDATE received_invoice_requests r SET
                  tax_10_yen=COALESCE((SELECT SUM(i.line_tax_yen) FROM received_invoice_items i WHERE i.request_id=r.id AND i.tax_category='tax10'),0),
                  tax_8_yen=COALESCE((SELECT SUM(i.line_tax_yen) FROM received_invoice_items i WHERE i.request_id=r.id AND i.tax_category='tax8'),0)
                """
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_otps (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              request_id BIGINT NOT NULL,
              email VARCHAR(320) NOT NULL,
              code_hash CHAR(64) NOT NULL,
              request_ip VARCHAR(64) NOT NULL,
              attempts INT NOT NULL DEFAULT 0,
              expires_at DATETIME NOT NULL,
              used_at DATETIME NULL,
              created_at DATETIME NOT NULL,
              INDEX idx_received_otp_request (request_id, created_at),
              INDEX idx_received_otp_ip (request_ip, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_attachments (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              request_id BIGINT NOT NULL,
              original_name VARCHAR(255) NOT NULL,
              storage_path VARCHAR(512) NOT NULL,
              content_type VARCHAR(128) NULL,
              size_bytes BIGINT NOT NULL,
              uploaded_at DATETIME NOT NULL,
              INDEX idx_received_attachment_request (request_id, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_events (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              request_id BIGINT NOT NULL,
              event_type VARCHAR(64) NOT NULL,
              actor_type VARCHAR(16) NOT NULL,
              actor_ip VARCHAR(64) NULL,
              detail_json TEXT NULL,
              created_at DATETIME NOT NULL,
              INDEX idx_received_event_request (request_id, created_at, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_versions (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              request_id BIGINT NOT NULL,
              version_no INT NOT NULL,
              snapshot_json LONGTEXT NOT NULL,
              pdf_storage_path VARCHAR(512) NOT NULL,
              submitted_at DATETIME NOT NULL,
              UNIQUE KEY uniq_received_version (request_id, version_no),
              INDEX idx_received_version_request (request_id, version_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS received_invoice_guidance_templates (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              template_name VARCHAR(191) NOT NULL,
              body TEXT NOT NULL,
              sort_order INT NOT NULL DEFAULT 0,
              is_default TINYINT(1) NOT NULL DEFAULT 0,
              created_at DATETIME NOT NULL,
              updated_at DATETIME NOT NULL,
              UNIQUE KEY uniq_received_guidance_name (template_name),
              INDEX idx_received_guidance_order (sort_order, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def ensure_received_invoice_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _create_received_invoice_schema()
        _schema_ready = True


def _text(value: Any, limit: int = 0) -> str:
    result = str(value or "").strip()
    return result[:limit] if limit else result


def _date(value: Any) -> date | None:
    try:
        return datetime.strptime(_text(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(_text(value).replace(",", "") or default).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _yen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _tax_mode(value: Any) -> str:
    mode = _text(value)
    return mode if mode in TAX_MODE_LABELS else "internal"


def calculate_item(item: dict, tax_mode: str = "internal") -> dict:
    row_type = "memo" if _text(item.get("row_type")) == "memo" else "normal"
    if row_type == "memo":
        return {
            **item,
            "row_type": "memo",
            "item_name": "",
            "memo_text": _text(item.get("memo_text")),
            "quantity": Decimal("0.00"),
            "unit_name": "",
            "unit_price_yen": 0,
            "tax_category": "nontax",
            "line_subtotal_yen": 0,
            "line_tax_yen": 0,
            "line_total_yen": 0,
        }
    quantity = max(Decimal("0"), _decimal(item.get("quantity"), "1"))
    price = max(0, int(_decimal(item.get("unit_price_yen"))))
    category = _text(item.get("tax_category"))
    if category not in TAX_RATES:
        category = "tax10"
    entered_amount = _yen(quantity * Decimal(price))
    rate = TAX_RATES[category]
    if _tax_mode(tax_mode) == "internal":
        total = entered_amount
        tax = 0 if rate == 0 else _yen(Decimal(entered_amount) - (Decimal(entered_amount) / (Decimal("1") + rate)))
        subtotal = total - tax
    else:
        subtotal = entered_amount
        tax = _yen(Decimal(subtotal) * rate)
        total = subtotal + tax
    return {
        **item,
        "row_type": "normal",
        "memo_text": "",
        "quantity": quantity,
        "unit_price_yen": price,
        "tax_category": category,
        "line_subtotal_yen": subtotal,
        "line_tax_yen": tax,
        "line_total_yen": total,
    }


def _request_no() -> str:
    return f"RI{now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def _admin_items(form) -> list[dict]:
    tax_mode = _tax_mode(form.get("tax_mode"))
    row_types = form.getlist("row_type[]")
    names = form.getlist("item_name[]")
    memos = form.getlist("memo_text[]")
    quantities = form.getlist("quantity[]")
    units = form.getlist("unit_name[]")
    prices = form.getlist("unit_price_yen[]")
    taxes = form.getlist("tax_category[]")
    locked = set(form.getlist("locked_index[]"))
    result = []
    row_count = max(len(row_types), len(names), len(memos))
    for index in range(row_count):
        row_type = "memo" if index < len(row_types) and row_types[index] == "memo" else "normal"
        name = names[index] if index < len(names) else ""
        memo = memos[index] if index < len(memos) else ""
        if row_type == "memo" and not _text(memo):
            continue
        if row_type == "normal" and not _text(name):
            continue
        result.append(calculate_item({
            "source": "admin",
            "locked_by_admin": 1 if str(index) in locked else 0,
            "sort_order": len(result),
            "row_type": row_type,
            "item_name": _text(name, 255),
            "memo_text": _text(memo),
            "quantity": quantities[index] if index < len(quantities) else "1",
            "unit_name": _text(units[index] if index < len(units) else "式", 32),
            "unit_price_yen": prices[index] if index < len(prices) else "0",
            "tax_category": taxes[index] if index < len(taxes) else "tax10",
            "usage_date": None,
            "detail": "",
        }, tax_mode))
    if not any(item["row_type"] == "normal" for item in result):
        raise ReceivedInvoiceError("報酬明細を1件以上入力してください。")
    return result


def _default_bill_to() -> dict:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_issuer_templates ORDER BY is_default DESC, sort_order, id LIMIT 1")
        row = cur.fetchone() or {}
        cur.execute("SELECT email FROM users WHERE username='admin' LIMIT 1")
        admin = cur.fetchone() or {}
    finally:
        cur.close()
        db.close()
    return {
        "bill_to_name": row.get("issuer_name") or "MFU",
        "bill_to_postal_code": row.get("issuer_postal_code") or "",
        "bill_to_address1": row.get("issuer_address1") or "",
        "bill_to_address2": row.get("issuer_address2") or "",
        "bill_to_phone": row.get("issuer_phone") or "",
        "bill_to_email": row.get("issuer_email") or admin.get("email") or "admin@mail.iori0624.jp",
    }


def default_received_form() -> dict:
    base = _default_bill_to()
    guidance = get_default_guidance_template()
    return {
        **base,
        "counterparty_name": "",
        "counterparty_email": "",
        "subject": "",
        "service_date": "",
        "due_date": (now().date() + timedelta(days=30)).isoformat(),
        "admin_note": (guidance or {}).get("body") or "",
        "tax_mode": "internal",
        "items": [{"row_type": "normal", "item_name": "報酬", "memo_text": "", "quantity": "1", "unit_name": "式", "unit_price_yen": 0, "tax_category": "tax10", "locked_by_admin": 1}],
    }


def list_guidance_templates() -> list[dict]:
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM received_invoice_guidance_templates ORDER BY sort_order,id")
        return cur.fetchall()
    finally:
        cur.close(); db.close()


def get_guidance_template(template_id: int) -> dict | None:
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM received_invoice_guidance_templates WHERE id=%s", (template_id,))
        return cur.fetchone()
    finally:
        cur.close(); db.close()


def get_default_guidance_template() -> dict | None:
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM received_invoice_guidance_templates WHERE is_default=1 ORDER BY sort_order,id LIMIT 1")
        return cur.fetchone()
    finally:
        cur.close(); db.close()


def _guidance_values(form) -> tuple[str, str, int, bool]:
    name = _text(form.get("template_name"), 191)
    body = _text(form.get("body"))
    try:
        sort_order = int(form.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    if not name or not body:
        raise ReceivedInvoiceError("テンプレート名と案内文は必須です。")
    return name, body, sort_order, bool(form.get("is_default"))


def create_guidance_template(form) -> int:
    name, body, sort_order, is_default = _guidance_values(form)
    db = get_db(); cur = db.cursor()
    try:
        if is_default:
            cur.execute("UPDATE received_invoice_guidance_templates SET is_default=0,updated_at=%s", (now(),))
        cur.execute(
            "INSERT INTO received_invoice_guidance_templates (template_name,body,sort_order,is_default,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (name, body, sort_order, 1 if is_default else 0, now(), now()),
        )
        template_id = int(cur.lastrowid)
        db.commit()
        return template_id
    except Exception as exc:
        db.rollback()
        if "Duplicate" in str(exc):
            raise ReceivedInvoiceError("同じ名前の案内テンプレートがあります。") from exc
        raise
    finally:
        cur.close(); db.close()


def update_guidance_template(template_id: int, form) -> None:
    name, body, sort_order, is_default = _guidance_values(form)
    db = get_db(); cur = db.cursor()
    try:
        if is_default:
            cur.execute("UPDATE received_invoice_guidance_templates SET is_default=0,updated_at=%s", (now(),))
        cur.execute(
            "UPDATE received_invoice_guidance_templates SET template_name=%s,body=%s,sort_order=%s,is_default=%s,updated_at=%s WHERE id=%s",
            (name, body, sort_order, 1 if is_default else 0, now(), template_id),
        )
        if cur.rowcount != 1:
            raise ReceivedInvoiceError("案内テンプレートが見つかりません。", status=404)
        db.commit()
    except Exception as exc:
        db.rollback()
        if "Duplicate" in str(exc):
            raise ReceivedInvoiceError("同じ名前の案内テンプレートがあります。") from exc
        raise
    finally:
        cur.close(); db.close()


def delete_guidance_template(template_id: int) -> None:
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("DELETE FROM received_invoice_guidance_templates WHERE id=%s", (template_id,))
        db.commit()
    finally:
        cur.close(); db.close()


def set_default_guidance_template(template_id: int) -> None:
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT id FROM received_invoice_guidance_templates WHERE id=%s FOR UPDATE", (template_id,))
        if not cur.fetchone():
            raise ReceivedInvoiceError("案内テンプレートが見つかりません。", status=404)
        cur.execute("UPDATE received_invoice_guidance_templates SET is_default=0,updated_at=%s", (now(),))
        cur.execute("UPDATE received_invoice_guidance_templates SET is_default=1,updated_at=%s WHERE id=%s", (now(), template_id))
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close(); db.close()


def _write_items(cur, request_id: int, items: list[dict]) -> None:
    cur.execute("DELETE FROM received_invoice_items WHERE request_id=%s", (request_id,))
    for item in items:
        cur.execute(
            """
            INSERT INTO received_invoice_items
              (request_id,source,locked_by_admin,sort_order,row_type,item_name,memo_text,quantity,unit_name,
               unit_price_yen,tax_category,usage_date,detail,line_subtotal_yen,line_tax_yen,
               line_total_yen,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                request_id, item["source"], item["locked_by_admin"], item["sort_order"], item["row_type"],
                item["item_name"], item.get("memo_text"), item["quantity"], item["unit_name"], item["unit_price_yen"],
                item["tax_category"], item.get("usage_date"), item.get("detail"),
                item["line_subtotal_yen"], item["line_tax_yen"], item["line_total_yen"], now(), now(),
            ),
        )


def _totals(items: list[dict]) -> dict[str, int]:
    subtotal = sum(int(i["line_subtotal_yen"]) for i in items)
    tax_10 = sum(int(i["line_tax_yen"]) for i in items if i["tax_category"] == "tax10")
    tax_8 = sum(int(i["line_tax_yen"]) for i in items if i["tax_category"] == "tax8")
    tax = tax_10 + tax_8
    total = sum(int(i["line_total_yen"]) for i in items)
    return {"subtotal_yen": subtotal, "tax_10_yen": tax_10, "tax_8_yen": tax_8, "tax_yen": tax, "total_yen": total}


def create_received_request(form, actor_ip: str) -> int:
    email = _text(form.get("counterparty_email"), 320).lower()
    name = _text(form.get("counterparty_name"), 191)
    subject = _text(form.get("subject"), 255)
    bill_to = _text(form.get("bill_to_name"), 191)
    if not name or "@" not in email or not subject or not bill_to:
        raise ReceivedInvoiceError("相手方名・メールアドレス・件名・請求先名は必須です。")
    tax_mode = _tax_mode(form.get("tax_mode"))
    items = _admin_items(form)
    totals = _totals(items)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO received_invoice_requests
              (public_id,request_no,status,auth_version,counterparty_name,counterparty_email,
               bill_to_name,bill_to_postal_code,bill_to_address1,bill_to_address2,bill_to_phone,
               bill_to_email,subject,service_date,due_date,admin_note,tax_mode,subtotal_yen,
               tax_10_yen,tax_8_yen,tax_yen,total_yen,
               created_at,updated_at)
            VALUES (%s,%s,'draft',1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(uuid.uuid4()), _request_no(), name, email, bill_to,
                _text(form.get("bill_to_postal_code"), 32), _text(form.get("bill_to_address1"), 255),
                _text(form.get("bill_to_address2"), 255), _text(form.get("bill_to_phone"), 64),
                _text(form.get("bill_to_email"), 320), subject, _date(form.get("service_date")),
                _date(form.get("due_date")), _text(form.get("admin_note")), tax_mode,
                totals["subtotal_yen"], totals["tax_10_yen"], totals["tax_8_yen"], totals["tax_yen"], totals["total_yen"],
                now(), now(),
            ),
        )
        request_id = int(cur.lastrowid)
        _write_items(cur, request_id, items)
        _event(cur, request_id, "created", "admin", actor_ip, {"total_yen": totals["total_yen"], "tax_mode": tax_mode})
        db.commit()
        return request_id
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def admin_form_from_request(form) -> dict:
    data = dict(form)
    try:
        data["items"] = _admin_items(form)
    except ReceivedInvoiceError:
        data["items"] = []
    return data


def update_received_request_admin(row: dict, form, actor_ip: str) -> None:
    if row["status"] != "draft":
        raise ReceivedInvoiceError("依頼送信後は素案を直接編集できません。修正依頼機能をご利用ください。", status=409)
    email = _text(form.get("counterparty_email"), 320).lower()
    name = _text(form.get("counterparty_name"), 191)
    subject = _text(form.get("subject"), 255)
    bill_to = _text(form.get("bill_to_name"), 191)
    if not name or "@" not in email or not subject or not bill_to:
        raise ReceivedInvoiceError("相手方名・メールアドレス・件名・請求先名は必須です。")
    tax_mode = _tax_mode(form.get("tax_mode"))
    items = _admin_items(form)
    totals = _totals(items)
    db = get_db(); cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE received_invoice_requests SET
              counterparty_name=%s,counterparty_email=%s,bill_to_name=%s,bill_to_postal_code=%s,
              bill_to_address1=%s,bill_to_address2=%s,bill_to_phone=%s,bill_to_email=%s,
              subject=%s,service_date=%s,due_date=%s,admin_note=%s,tax_mode=%s,
              subtotal_yen=%s,tax_10_yen=%s,tax_8_yen=%s,tax_yen=%s,total_yen=%s,
              content_version=content_version+1,updated_at=%s
            WHERE id=%s AND status='draft'
            """,
            (
                name,email,bill_to,_text(form.get("bill_to_postal_code"),32),
                _text(form.get("bill_to_address1"),255),_text(form.get("bill_to_address2"),255),
                _text(form.get("bill_to_phone"),64),_text(form.get("bill_to_email"),320),
                subject,_date(form.get("service_date")),_date(form.get("due_date")),
                _text(form.get("admin_note")),tax_mode,totals["subtotal_yen"],totals["tax_10_yen"],
                totals["tax_8_yen"],totals["tax_yen"],totals["total_yen"],now(),row["id"],
            ),
        )
        if cur.rowcount != 1:
            raise ReceivedInvoiceError("状態が変更されたため保存できません。", status=409)
        _write_items(cur, row["id"], items)
        _event(cur, row["id"], "admin_draft_updated", "admin", actor_ip, {"total_yen": totals["total_yen"], "tax_mode": tax_mode})
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close(); db.close()


def get_received_request(*, request_id: int | None = None, public_id: str | None = None) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if request_id is not None:
            cur.execute("SELECT * FROM received_invoice_requests WHERE id=%s", (request_id,))
        else:
            cur.execute("SELECT * FROM received_invoice_requests WHERE public_id=%s", (_text(public_id, 36),))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT * FROM received_invoice_items WHERE request_id=%s ORDER BY sort_order,id", (row["id"],))
        row["items"] = cur.fetchall()
        cur.execute("SELECT * FROM received_invoice_attachments WHERE request_id=%s ORDER BY id", (row["id"],))
        row["attachments"] = cur.fetchall()
        cur.execute("SELECT * FROM received_invoice_events WHERE request_id=%s ORDER BY created_at DESC,id DESC", (row["id"],))
        row["events"] = cur.fetchall()
        cur.execute("SELECT * FROM received_invoice_versions WHERE request_id=%s ORDER BY version_no DESC", (row["id"],))
        row["versions"] = cur.fetchall()
        return row
    finally:
        cur.close()
        db.close()


def list_received_requests(status: str = "", q: str = "") -> list[dict]:
    where, params = [], []
    if status in RECEIVED_STATUSES:
        where.append("status=%s")
        params.append(status)
    if _text(q):
        where.append("(request_no LIKE %s OR counterparty_name LIKE %s OR subject LIKE %s OR invoice_no LIKE %s)")
        pattern = f"%{_text(q)}%"
        params.extend([pattern] * 4)
    sql = "SELECT * FROM received_invoice_requests"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC,id DESC"
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(params))
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def _event(cur, request_id: int, event_type: str, actor_type: str, actor_ip: str, detail: dict | None = None) -> None:
    cur.execute(
        "INSERT INTO received_invoice_events (request_id,event_type,actor_type,actor_ip,detail_json,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (request_id, event_type, actor_type, _text(actor_ip, 64), json.dumps(detail or {}, ensure_ascii=False), now()),
    )


def record_event(request_id: int, event_type: str, actor_type: str, actor_ip: str, detail: dict | None = None) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        _event(cur, request_id, event_type, actor_type, actor_ip, detail)
        db.commit()
    finally:
        cur.close()
        db.close()


def _admin_email(row: dict) -> str:
    return _text(row.get("bill_to_email"), 320) or "admin@mail.iori0624.jp"


def send_request_invitation(row: dict, edit_url: str, actor_ip: str) -> None:
    body = (
        f"{row['counterparty_name']} 様\n\n"
        "請求書作成のお願いです。下記ページを開き、メール認証後に不足項目と交通費等をご記入ください。\n\n"
        f"件名: {row['subject']}\n"
        f"作成ページ: {edit_url}\n"
        f"回答期限: {row.get('due_date') or '指定なし'}\n\n"
        "固定表示されている報酬項目・金額は変更できません。"
    )
    if _text(row.get("admin_note")):
        body += f"\n\nご案内:\n{_text(row.get('admin_note'))}"
    send_mail(
        row["counterparty_email"], "【MFU】請求書作成のお願い", body,
        from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_invitation",
    )
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE received_invoice_requests SET status='sent',sent_at=%s,expires_at=%s,auth_version=auth_version+1,updated_at=%s WHERE id=%s",
            (now(), now() + timedelta(days=INVITE_DAYS), now(), row["id"]),
        )
        cur.execute("UPDATE received_invoice_otps SET used_at=COALESCE(used_at,%s) WHERE request_id=%s", (now(), row["id"]))
        _event(cur, row["id"], "invitation_sent", "admin", actor_ip, {"to": row["counterparty_email"]})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def _otp_hash(request_id: int, email: str, code: str) -> str:
    secret = str(current_app.secret_key or current_app.config.get("SECRET_KEY") or "")
    return hashlib.sha256(f"{secret}\0{request_id}\0{email.lower()}\0{code}".encode()).hexdigest()


def mask_email(email: str) -> str:
    local, _, domain = _text(email).partition("@")
    return f"{local[:1]}{'*' * max(3, len(local)-1)}@{domain}" if domain else "未設定"


def send_received_otp(row: dict, request_ip: str, view_url: str) -> None:
    if row["status"] not in {"sent", "editing", "revision_requested"}:
        raise ReceivedInvoiceError("現在、この請求書は編集できません。", status=409)
    if row.get("expires_at") and row["expires_at"] < now():
        raise ReceivedInvoiceError("この作成依頼は期限切れです。", status=410)
    code = f"{secrets.randbelow(1_000_000):06d}"
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT created_at FROM received_invoice_otps WHERE request_id=%s ORDER BY id DESC LIMIT 1", (row["id"],))
        latest = cur.fetchone() or {}
        if latest.get("created_at") and (now() - latest["created_at"]).total_seconds() < OTP_RESEND_SECONDS:
            raise ReceivedInvoiceError("認証コードは60秒後に再送できます。", status=429)
        cur.execute("SELECT COUNT(*) cnt FROM received_invoice_otps WHERE request_id=%s AND created_at>=NOW()-INTERVAL 1 HOUR", (row["id"],))
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_REQUEST_HOURLY_LIMIT:
            raise ReceivedInvoiceError("認証コード送信回数が上限に達しました。", status=429)
        cur.execute("SELECT COUNT(*) cnt FROM received_invoice_otps WHERE request_ip=%s AND created_at>=NOW()-INTERVAL 1 HOUR", (_text(request_ip,64),))
        if int((cur.fetchone() or {}).get("cnt") or 0) >= OTP_IP_HOURLY_LIMIT:
            raise ReceivedInvoiceError("認証コード送信回数が上限に達しました。", status=429)
        cur.execute("UPDATE received_invoice_otps SET used_at=COALESCE(used_at,%s) WHERE request_id=%s", (now(), row["id"]))
        cur.execute(
            "INSERT INTO received_invoice_otps (request_id,email,code_hash,request_ip,attempts,expires_at,created_at) VALUES (%s,%s,%s,%s,0,%s,%s)",
            (row["id"], row["counterparty_email"].lower(), _otp_hash(row["id"], row["counterparty_email"], code), _text(request_ip,64), now()+timedelta(minutes=OTP_TTL_MINUTES), now()),
        )
        otp_id = int(cur.lastrowid)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()
    try:
        send_mail(
            row["counterparty_email"], "【MFU】請求書作成用認証コード",
            f"認証コード: {code}\n有効期限: {OTP_TTL_MINUTES}分\n\n請求書作成ページ: {view_url}\n\n心当たりがない場合は破棄してください。",
            from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_otp",
        )
        record_event(row["id"], "otp_sent", "recipient", request_ip)
    except Exception:
        db = get_db(); cur = db.cursor()
        cur.execute("UPDATE received_invoice_otps SET used_at=%s WHERE id=%s", (now(), otp_id)); db.commit(); db.close()
        raise


def verify_received_otp(row: dict, code: str, request_ip: str) -> bool:
    candidate = _text(code)
    if len(candidate) != 6 or not candidate.isdigit():
        return False
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM received_invoice_otps WHERE request_id=%s AND email=%s AND used_at IS NULL ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (row["id"], row["counterparty_email"].lower()),
        )
        otp = cur.fetchone()
        if not otp or otp["expires_at"] < now() or int(otp["attempts"]) >= OTP_MAX_ATTEMPTS:
            return False
        valid = hmac.compare_digest(otp["code_hash"], _otp_hash(row["id"], row["counterparty_email"], candidate))
        if valid:
            cur.execute("UPDATE received_invoice_otps SET used_at=%s WHERE id=%s", (now(), otp["id"]))
            _event(cur, row["id"], "otp_verified", "recipient", request_ip)
        else:
            cur.execute("UPDATE received_invoice_otps SET attempts=attempts+1,used_at=IF(attempts+1>=%s,%s,used_at) WHERE id=%s", (OTP_MAX_ATTEMPTS, now(), otp["id"]))
        db.commit()
        return valid
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close(); db.close()


def update_recipient_draft(row: dict, form, files, actor_ip: str, *, submit: bool) -> dict:
    if row["status"] not in {"sent", "editing", "revision_requested"}:
        raise ReceivedInvoiceError("現在、この請求書は編集できません。", status=409)
    tax_mode = _tax_mode(row.get("tax_mode"))
    existing = row["items"]
    existing_by_id = {str(item["id"]): item for item in existing}
    posted_ids = form.getlist("item_id[]")
    row_types = form.getlist("row_type[]")
    names = form.getlist("item_name[]")
    memos = form.getlist("memo_text[]")
    quantities = form.getlist("quantity[]")
    units = form.getlist("unit_name[]")
    prices = form.getlist("unit_price_yen[]")
    taxes = form.getlist("tax_category[]")
    dates = form.getlist("usage_date[]")
    details = form.getlist("detail[]")
    items: list[dict] = []
    seen_ids: set[str] = set()
    posted_locked_ids: list[str] = []
    row_count = max(len(posted_ids), len(row_types), len(names), len(memos))
    for index in range(row_count):
        item_id = posted_ids[index] if index < len(posted_ids) else ""
        if item_id and (item_id not in existing_by_id or item_id in seen_ids):
            raise ReceivedInvoiceError("明細の構成が不正です。画面を再読み込みしてください。", status=409)
        existing_item = existing_by_id.get(item_id)
        if item_id:
            seen_ids.add(item_id)
        if existing_item and existing_item["source"] == "admin" and int(existing_item["locked_by_admin"]):
            posted_locked_ids.append(item_id)
            calculated = calculate_item({**existing_item, "sort_order": len(items)}, tax_mode)
        else:
            row_type = "memo" if index < len(row_types) and row_types[index] == "memo" else "normal"
            calculated = calculate_item({
                **(existing_item or {}),
                "source": (existing_item or {}).get("source") or "recipient",
                "locked_by_admin": 0,
                "sort_order": len(items),
                "row_type": row_type,
                "item_name": _text(names[index] if index < len(names) else "", 255),
                "memo_text": _text(memos[index] if index < len(memos) else ""),
                "quantity": quantities[index] if index < len(quantities) else "1",
                "unit_name": _text(units[index] if index < len(units) else "式", 32),
                "unit_price_yen": prices[index] if index < len(prices) else "0",
                "tax_category": taxes[index] if index < len(taxes) else "tax10",
                "usage_date": _date(dates[index] if index < len(dates) else ""),
                "detail": _text(details[index] if index < len(details) else "", 255),
            }, tax_mode)
        if calculated["row_type"] == "normal" and not _text(calculated.get("item_name")):
            raise ReceivedInvoiceError("明細名を入力してください。")
        if calculated["row_type"] == "memo" and not _text(calculated.get("memo_text")):
            continue
        items.append(calculated)
    missing_locked = [item for item in existing if item["source"] == "admin" and int(item["locked_by_admin"]) and str(item["id"]) not in seen_ids]
    if missing_locked:
        raise ReceivedInvoiceError("固定明細が不足しています。画面を再読み込みしてください。", status=409)
    expected_locked_ids = [str(item["id"]) for item in existing if item["source"] == "admin" and int(item["locked_by_admin"])]
    if posted_locked_ids != expected_locked_ids:
        raise ReceivedInvoiceError("固定明細の並び順は変更できません。", status=409)
    if not any(item["row_type"] == "normal" for item in items):
        raise ReceivedInvoiceError("明細を1件以上入力してください。")
    issuer_name = _text(form.get("issuer_name"), 191)
    invoice_no = _text(form.get("invoice_no"), 64)
    bank_info = _text(form.get("bank_info"))
    issue_date = _date(form.get("issue_date"))
    if submit and (not issuer_name or not issue_date or not bank_info):
        raise ReceivedInvoiceError("提出には請求者名・請求日・振込先が必要です。")
    totals = _totals(items)
    # Validate and persist supporting documents before changing the request to
    # submitted. A file error must never leave a half-submitted invoice.
    _save_attachments(row["id"], files, actor_ip)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE received_invoice_requests SET
              issuer_name=%s,issuer_postal_code=%s,issuer_address1=%s,issuer_address2=%s,
              issuer_phone=%s,issuer_email=%s,issuer_registration_no=%s,invoice_no=%s,
              issue_date=%s,bank_info=%s,recipient_note=%s,subtotal_yen=%s,tax_10_yen=%s,
              tax_8_yen=%s,tax_yen=%s,total_yen=%s,
              status=%s,submitted_at=CASE WHEN %s=1 THEN %s ELSE submitted_at END,
              content_version=content_version+1,updated_at=%s
            WHERE id=%s AND auth_version=%s AND content_version=%s
            """,
            (
                issuer_name, _text(form.get("issuer_postal_code"),32), _text(form.get("issuer_address1"),255),
                _text(form.get("issuer_address2"),255), _text(form.get("issuer_phone"),64),
                _text(form.get("issuer_email"),320), _text(form.get("issuer_registration_no"),32),
                invoice_no, issue_date, bank_info, _text(form.get("recipient_note")), totals["subtotal_yen"],
                totals["tax_10_yen"], totals["tax_8_yen"], totals["tax_yen"], totals["total_yen"],
                "submitted" if submit else "editing", 1 if submit else 0, now(), now(), row["id"], row["auth_version"],
                int(form.get("content_version") or 0),
            ),
        )
        if cur.rowcount != 1:
            raise ReceivedInvoiceError("別の画面で更新されました。画面を再読み込みしてください。", status=409)
        _write_items(cur, row["id"], items)
        _event(cur, row["id"], "submitted" if submit else "draft_saved", "recipient", actor_ip, {"total_yen": totals["total_yen"], "tax_mode": tax_mode})
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close(); db.close()
    return get_received_request(request_id=row["id"]) or row


def _save_attachments(request_id: int, files, actor_ip: str) -> None:
    uploads = [f for f in files if f and _text(getattr(f, "filename", ""))]
    if not uploads:
        return
    row = get_received_request(request_id=request_id) or {}
    if len(row.get("attachments", [])) + len(uploads) > MAX_ATTACHMENTS:
        raise ReceivedInvoiceError(f"添付は最大{MAX_ATTACHMENTS}件です。")
    root = Path(current_app.config.get("RECEIVED_INVOICE_ATTACHMENT_DIR") or "/mnt/mfu/data/received_invoice_attachments") / str(request_id)
    root.mkdir(parents=True, exist_ok=True)
    db = get_db(); cur = db.cursor()
    stored_paths: list[Path] = []
    try:
        for upload in uploads:
            original = _text(upload.filename, 255)
            ext = Path(original).suffix.lower()
            if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise ReceivedInvoiceError("添付できる形式はPDF・JPEG・PNG・HEICです。")
            data = upload.read(MAX_ATTACHMENT_BYTES + 1)
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ReceivedInvoiceError("添付ファイルは1件20MB以下にしてください。")
            stored = root / f"{uuid.uuid4().hex}{ext}"
            stored.write_bytes(data)
            stored_paths.append(stored)
            cur.execute(
                "INSERT INTO received_invoice_attachments (request_id,original_name,storage_path,content_type,size_bytes,uploaded_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (request_id, original, str(stored), _text(upload.mimetype,128) or mimetypes.guess_type(original)[0], len(data), now()),
            )
        _event(cur, request_id, "attachments_added", "recipient", actor_ip, {"count": len(uploads)})
        db.commit()
    except Exception:
        db.rollback()
        for stored in stored_paths:
            try:
                stored.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        cur.close(); db.close()


def render_received_pdf_bytes(row: dict) -> bytes:
    from .pdf import _build_font_css, _build_pdf_context, _require_weasyprint, _resolve_font_paths
    HTML, CSS, FontConfiguration = _require_weasyprint()
    regular, bold = _resolve_font_paths()
    font_config = FontConfiguration()
    invoice = {
        **row,
        "contact_name_snapshot": row.get("bill_to_name") or "",
        "contact_department_snapshot": "",
        "contact_person_snapshot": "",
        "contact_honorific_snapshot": "御中",
        "contact_email_snapshot": row.get("bill_to_email") or "",
        "contact_postal_code_snapshot": row.get("bill_to_postal_code") or "",
        "contact_address1_snapshot": row.get("bill_to_address1") or "",
        "contact_address2_snapshot": row.get("bill_to_address2") or "",
        "contact_phone_snapshot": row.get("bill_to_phone") or "",
        "note": row.get("recipient_note") or "",
    }
    pdf_context = _build_pdf_context(invoice)
    html = render_template("invoice_pdf.html", invoice=invoice, pdf=pdf_context)
    stylesheet = CSS(string=_build_font_css(regular, bold), base_url=Path(current_app.root_path).resolve().as_uri()+"/", font_config=font_config)
    return HTML(string=html, base_url=Path(current_app.root_path).resolve().as_uri()+"/").write_pdf(stylesheets=[stylesheet], font_config=font_config)


def generate_received_pdf(row: dict) -> tuple[str, str, bytes]:
    data = render_received_pdf_bytes(row)
    root = Path(current_app.config.get("RECEIVED_INVOICE_PDF_DIR") or "/mnt/mfu/data/received_invoice_pdf")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"received_{row['id']}_{now():%Y%m%d%H%M%S}.pdf"
    path.write_bytes(data)
    filename = f"{_text(row.get('issuer_name')) or '請求者'}_{(row.get('issue_date') or now().date()):%Y年%m月%d日}.pdf"
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        snapshot = {key: value for key, value in row.items() if key not in {"events", "versions"}}
        cur.execute("SELECT id FROM received_invoice_requests WHERE id=%s FOR UPDATE", (row["id"],))
        cur.fetchone()
        cur.execute("SELECT COALESCE(MAX(version_no),0)+1 next_no FROM received_invoice_versions WHERE request_id=%s", (row["id"],))
        version_no = int((cur.fetchone() or {}).get("next_no") or 1)
        cur.execute(
            "INSERT INTO received_invoice_versions (request_id,version_no,snapshot_json,pdf_storage_path,submitted_at) VALUES (%s,%s,%s,%s,%s)",
            (row["id"], version_no, json.dumps(snapshot, ensure_ascii=False, default=str), str(path), now()),
        )
        cur.execute("UPDATE received_invoice_requests SET pdf_storage_path=%s,submitted_at=COALESCE(submitted_at,%s),updated_at=%s WHERE id=%s", (str(path), now(), now(), row["id"]))
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close(); db.close()
    return str(path), filename, data


def notify_submitted(row: dict, filename: str, pdf_data: bytes) -> None:
    attachment = [{"filename": filename, "data": pdf_data, "content_type": "application/pdf"}]
    send_mail(
        _admin_email(row), "【MFU】請求書が提出されました",
        f"{row['counterparty_name']}様から請求書が提出されました。\n件名: {row['subject']}\n合計: ¥{int(row['total_yen']):,}",
        attachments=attachment, from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_submitted_admin",
    )
    send_mail(
        row["counterparty_email"], "【MFU】請求書を受け付けました",
        f"請求書を受け付けました。\n件名: {row['subject']}\n合計: ¥{int(row['total_yen']):,}",
        attachments=attachment, from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_submitted_recipient",
    )


def admin_action(row: dict, action: str, message: str, actor_ip: str, edit_url: str) -> None:
    mapping = {"accept": "accepted", "paid": "paid", "revision": "revision_requested", "cancel": "cancelled"}
    target = mapping.get(action)
    if not target:
        raise ReceivedInvoiceError("操作が不正です。")
    allowed = {
        "accept": {"submitted"}, "revision": {"submitted", "accepted"},
        "paid": {"accepted"}, "cancel": set(RECEIVED_STATUSES) - {"paid"},
    }
    if row["status"] not in allowed[action]:
        raise ReceivedInvoiceError("現在の状態では実行できません。", status=409)
    sets = ["status=%s", "updated_at=%s"]
    params: list[Any] = [target, now()]
    if target == "accepted":
        sets.append("accepted_at=%s"); params.append(now())
    elif target == "paid":
        sets.append("paid_at=%s"); params.append(now())
    elif target == "revision_requested":
        sets.extend(["revision_requested_at=%s", "auth_version=auth_version+1", "expires_at=%s"])
        params.extend([now(), now()+timedelta(days=INVITE_DAYS)])
    params.append(row["id"])
    db = get_db(); cur = db.cursor()
    try:
        cur.execute(f"UPDATE received_invoice_requests SET {','.join(sets)} WHERE id=%s", tuple(params))
        _event(cur, row["id"], target, "admin", actor_ip, {"message": _text(message)})
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        cur.close(); db.close()
    if target == "revision_requested":
        send_mail(
            row["counterparty_email"], "【MFU】請求書の修正をお願いします",
            f"次の請求書に修正依頼があります。\n件名: {row['subject']}\n修正内容: {_text(message) or '内容をご確認ください。'}\n編集ページ: {edit_url}",
            from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_revision",
        )
    elif target == "accepted":
        send_mail(row["counterparty_email"], "【MFU】請求書を承認しました", f"請求書を承認しました。\n件名: {row['subject']}\n合計: ¥{int(row['total_yen']):,}", from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_accepted")
    elif target == "paid":
        send_mail(row["counterparty_email"], "【MFU】請求書のお支払い処理が完了しました", f"お支払い処理を完了しました。\n件名: {row['subject']}\n合計: ¥{int(row['total_yen']):,}", from_display_name="MFU_System", append_signature=True, mail_kind="received_invoice_paid")
