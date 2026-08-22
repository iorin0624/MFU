from __future__ import annotations

import os
import uuid
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Any

from app.utils.db import get_db

from .utils import (
    DEFAULT_TAX_CATEGORY,
    ISSUER_TEMPLATE_FIELDS,
    ROW_TYPE_MEMO,
    ROW_TYPE_NORMAL,
    TAX_CATEGORY_LABELS,
    calculate_line_amounts,
    default_due_date,
    format_quantity,
    format_ymd,
    normalize_row_type,
    normalize_status,
    normalize_tax_category,
    normalize_tax_mode,
    now_jst,
    quantize_quantity,
    quantize_yen,
    split_emails,
    to_decimal,
)

BANK_INFO_MODE_INLINE = "inline"
BANK_INFO_MODE_PAYOUT_LINK = "payout_link"
BANK_INFO_MODE_LABELS = {
    BANK_INFO_MODE_INLINE: "請求書に直接記載",
    BANK_INFO_MODE_PAYOUT_LINK: "payoutで確認してもらう",
}
PAYOUT_LINK_BANK_INFO_MESSAGE = "メール本文にて振込先一覧のリンクがあります。ご確認お願い致します。"
PAYOUT_LINK_MAIL_GUIDANCE = "振込先は下からご確認ください。"
CARD_PAYMENT_PDF_GUIDANCE = "クレジットカードでのお支払いURLはメール本文をご確認ください。"
CARD_PAYMENT_MAIL_GUIDANCE = "クレジットカードでのお支払いは下記URLよりお願いいたします。"
CARD_PAYMENT_SUCCESS_STATUSES = ("COMPLETED",)
INVOICE_DELETABLE_STATUSES = ("draft", "cancelled")
INVOICE_PURGE_SAFE_PAYMENT_STATUSES = ("FAILED", "CANCELED", "CANCELLED", "REJECTED")
INVOICE_SOFT_DELETE_BLOCKING_PAYMENT_STATUSES = ("PENDING", "UNKNOWN", "APPROVED", "AUTHORIZED")


@dataclass
class InvoiceItemInput:
    item_name: str = ""
    quantity: Decimal = Decimal("0")
    unit_name: str = ""
    unit_price_yen: int = 0
    tax_category: str = DEFAULT_TAX_CATEGORY
    sort_order: int = 0
    row_type: str = ROW_TYPE_NORMAL
    memo_text: str = ""

    @property
    def base_amount_yen(self) -> int:
        return quantize_yen(self.quantity * Decimal(self.unit_price_yen))

    @property
    def is_memo(self) -> bool:
        return self.row_type == ROW_TYPE_MEMO

    def line_amounts(self, tax_mode: str) -> dict[str, int]:
        if self.is_memo:
            return {
                "tax_category": DEFAULT_TAX_CATEGORY,
                "tax_rate": Decimal("0"),
                "line_subtotal_yen": 0,
                "line_tax_yen": 0,
                "line_total_yen": 0,
            }
        return calculate_line_amounts(self.base_amount_yen, tax_mode, self.tax_category)

    @property
    def line_total_yen(self) -> int:
        return self.base_amount_yen


class InvoiceValidationError(ValueError):
    pass


def normalize_multiline_text(value: Any) -> str | None:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def _normalize_stripped_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_optional_int(value: Any) -> int | None:
    text = _normalize_stripped_text(value)
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_bank_info_mode(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized == BANK_INFO_MODE_PAYOUT_LINK:
        return BANK_INFO_MODE_PAYOUT_LINK
    return BANK_INFO_MODE_INLINE


def get_invoice_effective_bank_info(invoice: dict[str, Any]) -> str:
    if normalize_bank_info_mode(invoice.get("bank_info_mode")) == BANK_INFO_MODE_PAYOUT_LINK:
        base_message = PAYOUT_LINK_BANK_INFO_MESSAGE
    else:
        base_message = normalize_multiline_text(invoice.get("bank_info")) or ""
    if int(invoice.get("card_payment_enabled") or 0) != 1:
        return base_message
    if CARD_PAYMENT_PDF_GUIDANCE in base_message:
        return base_message
    if not base_message:
        return CARD_PAYMENT_PDF_GUIDANCE
    return f"{base_message}\n{CARD_PAYMENT_PDF_GUIDANCE}"


def append_payout_guidance_to_mail_body(body: Any, access_url: str) -> str:
    normalized_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    normalized_url = _normalize_stripped_text(access_url)
    if not normalized_url:
        return normalized_body
    if normalized_url in normalized_body:
        return normalized_body
    suffix = f"{PAYOUT_LINK_MAIL_GUIDANCE}\n{normalized_url}"
    if not normalized_body:
        return suffix
    return f"{normalized_body}\n{suffix}"


def append_card_payment_guidance_to_mail_body(body: Any, access_url: str) -> str:
    normalized_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    normalized_url = _normalize_stripped_text(access_url)
    if not normalized_url:
        return normalized_body
    if normalized_url in normalized_body:
        return normalized_body
    suffix = f"{CARD_PAYMENT_MAIL_GUIDANCE}\n{normalized_url}"
    if not normalized_body:
        return suffix
    return f"{normalized_body}\n{suffix}"


def resolve_invoice_issuer_email(invoice: dict[str, Any]) -> str:
    issuer_email = _normalize_stripped_text(invoice.get("issuer_email"))
    if issuer_email:
        return issuer_email

    issuer_template_id = _normalize_optional_int(invoice.get("issuer_template_id"))
    if not issuer_template_id:
        return ""

    template = get_issuer_template_by_id(issuer_template_id)
    if not template:
        return ""
    return _normalize_stripped_text(template.get("issuer_email"))


def merge_invoice_cc_emails(effective_issuer_email: str | None, cc_email: str | None) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in (effective_issuer_email, cc_email):
        for email in split_emails(value):
            key = email.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(email)
    return ", ".join(merged)


def _issuer_template_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row.get("id"),
        "template_name": row.get("template_name") or "",
        "issuer_name": row.get("issuer_name") or "",
        "issuer_postal_code": row.get("issuer_postal_code") or "",
        "issuer_address1": row.get("issuer_address1") or "",
        "issuer_address2": row.get("issuer_address2") or "",
        "issuer_phone": row.get("issuer_phone") or "",
        "issuer_email": row.get("issuer_email") or "",
        "bank_info": row.get("bank_info") or "",
        "note": row.get("note") or "",
        "sort_order": int(row.get("sort_order") or 0),
        "is_default": bool(row.get("is_default")),
    }


def list_issuer_templates() -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT *
            FROM invoice_issuer_templates
            ORDER BY sort_order ASC, id ASC
            """
        )
        return [_issuer_template_to_dict(row) for row in cur.fetchall()]
    finally:
        cur.close()
        db.close()


def ensure_invoice_issuer_templates_table(cur=None) -> None:
    should_close = cur is None
    db = None
    if cur is None:
        db = get_db()
        cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_issuer_templates (
                id INT AUTO_INCREMENT PRIMARY KEY,
                template_name VARCHAR(255) NOT NULL,
                issuer_name VARCHAR(191) NOT NULL,
                issuer_postal_code VARCHAR(32) NULL,
                issuer_address1 VARCHAR(255) NULL,
                issuer_address2 VARCHAR(255) NULL,
                issuer_phone VARCHAR(64) NULL,
                issuer_email VARCHAR(255) NULL,
                bank_info TEXT NULL,
                note TEXT NULL,
                sort_order INT NOT NULL DEFAULT 0,
                is_default TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_invoice_issuer_templates_sort_order (sort_order, id),
                INDEX idx_invoice_issuer_templates_is_default (is_default, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        if not _column_exists(cur, "invoice_issuer_templates", "bank_info"):
            cur.execute("ALTER TABLE invoice_issuer_templates ADD COLUMN bank_info TEXT NULL AFTER issuer_phone")
        if not _column_exists(cur, "invoice_issuer_templates", "note"):
            cur.execute("ALTER TABLE invoice_issuer_templates ADD COLUMN note TEXT NULL AFTER bank_info")
        if not _column_exists(cur, "invoice_issuer_templates", "issuer_email"):
            cur.execute("ALTER TABLE invoice_issuer_templates ADD COLUMN issuer_email VARCHAR(255) NULL AFTER issuer_phone")
        if should_close and db is not None:
            db.commit()
    finally:
        if should_close:
            cur.close()
            db.close()


def get_default_issuer_template() -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT *
            FROM invoice_issuer_templates
            WHERE is_default = 1
            ORDER BY sort_order ASC, id ASC
            LIMIT 1
            """
        )
        return _issuer_template_to_dict(cur.fetchone())
    finally:
        cur.close()
        db.close()


def get_issuer_template_by_id(template_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM invoice_issuer_templates WHERE id = %s",
            (template_id,),
        )
        return _issuer_template_to_dict(cur.fetchone())
    finally:
        cur.close()
        db.close()


def _parse_issuer_template_form(form: dict[str, Any]) -> dict[str, Any]:
    template_name = (form.get("template_name") or "").strip()
    issuer_name = (form.get("issuer_name") or "").strip()
    sort_order_raw = (form.get("sort_order") or "0").strip()

    if not template_name:
        raise InvoiceValidationError("テンプレート名を入力してください。")
    if not issuer_name:
        raise InvoiceValidationError("発行者名を入力してください。")
    try:
        sort_order = int(sort_order_raw or "0")
    except (TypeError, ValueError) as exc:
        raise InvoiceValidationError("並び順は数値で入力してください。") from exc

    return {
        "template_name": template_name,
        "issuer_name": issuer_name,
        "issuer_postal_code": (form.get("issuer_postal_code") or "").strip() or None,
        "issuer_address1": (form.get("issuer_address1") or "").strip() or None,
        "issuer_address2": (form.get("issuer_address2") or "").strip() or None,
        "issuer_phone": (form.get("issuer_phone") or "").strip() or None,
        "issuer_email": (form.get("issuer_email") or "").strip() or None,
        "bank_info": normalize_multiline_text(form.get("bank_info")),
        "note": normalize_multiline_text(form.get("note")),
        "sort_order": sort_order,
        "is_default": 1 if str(form.get("is_default") or "").lower() in {"1", "true", "on", "yes"} else 0,
    }


def build_issuer_template_form_data(template: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "template_name": "",
        "issuer_name": "",
        "issuer_postal_code": "",
        "issuer_address1": "",
        "issuer_address2": "",
        "issuer_phone": "",
        "issuer_email": "",
        "bank_info": "",
        "note": "",
        "sort_order": "0",
        "is_default": "",
    }
    if not template:
        return data
    data.update(
        {
            "template_name": template.get("template_name") or "",
            "issuer_name": template.get("issuer_name") or "",
            "issuer_postal_code": template.get("issuer_postal_code") or "",
            "issuer_address1": template.get("issuer_address1") or "",
            "issuer_address2": template.get("issuer_address2") or "",
            "issuer_phone": template.get("issuer_phone") or "",
            "issuer_email": template.get("issuer_email") or "",
            "bank_info": template.get("bank_info") or "",
            "note": template.get("note") or "",
            "sort_order": str(int(template.get("sort_order") or 0)),
            "is_default": "1" if template.get("is_default") else "",
        }
    )
    return data


def set_default_issuer_template(template_id: int) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        ensure_invoice_issuer_templates_table(cur)
        cur.execute("UPDATE invoice_issuer_templates SET is_default = 0, updated_at = %s WHERE is_default = 1", (now,))
        cur.execute(
            "UPDATE invoice_issuer_templates SET is_default = 1, updated_at = %s WHERE id = %s",
            (now, template_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def create_issuer_template(form: dict[str, Any]) -> int:
    payload = _parse_issuer_template_form(form)
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        ensure_invoice_issuer_templates_table(cur)
        if payload["is_default"]:
            cur.execute("UPDATE invoice_issuer_templates SET is_default = 0, updated_at = %s WHERE is_default = 1", (now,))
        cur.execute(
            """
            INSERT INTO invoice_issuer_templates (
                template_name, issuer_name, issuer_postal_code, issuer_address1,
                issuer_address2, issuer_phone, issuer_email, bank_info, note, sort_order, is_default, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                payload["template_name"],
                payload["issuer_name"],
                payload["issuer_postal_code"],
                payload["issuer_address1"],
                payload["issuer_address2"],
                payload["issuer_phone"],
                payload["issuer_email"],
                payload["bank_info"],
                payload["note"],
                payload["sort_order"],
                payload["is_default"],
                now,
                now,
            ),
        )
        db.commit()
        return int(cur.lastrowid)
    finally:
        cur.close()
        db.close()


def update_issuer_template(template_id: int, form: dict[str, Any]) -> None:
    payload = _parse_issuer_template_form(form)
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        ensure_invoice_issuer_templates_table(cur)
        if payload["is_default"]:
            cur.execute(
                "UPDATE invoice_issuer_templates SET is_default = 0, updated_at = %s WHERE is_default = 1 AND id <> %s",
                (now, template_id),
            )
        cur.execute(
            """
            UPDATE invoice_issuer_templates
            SET template_name = %s,
                issuer_name = %s,
                issuer_postal_code = %s,
                issuer_address1 = %s,
                issuer_address2 = %s,
                issuer_phone = %s,
                issuer_email = %s,
                bank_info = %s,
                note = %s,
                sort_order = %s,
                is_default = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                payload["template_name"],
                payload["issuer_name"],
                payload["issuer_postal_code"],
                payload["issuer_address1"],
                payload["issuer_address2"],
                payload["issuer_phone"],
                payload["issuer_email"],
                payload["bank_info"],
                payload["note"],
                payload["sort_order"],
                payload["is_default"],
                now,
                template_id,
            ),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def delete_issuer_template(template_id: int) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        ensure_invoice_issuer_templates_table(cur)
        cur.execute("DELETE FROM invoice_issuer_templates WHERE id = %s", (template_id,))
        db.commit()
    finally:
        cur.close()
        db.close()


def apply_issuer_template_to_form_data(form_data: dict[str, Any], template: dict[str, Any] | None) -> dict[str, Any]:
    if not template:
        return form_data
    for field_name in ISSUER_TEMPLATE_FIELDS:
        form_data[field_name] = template.get(field_name) or ""
    form_data["issuer_template_id"] = str(template.get("id") or "")
    return form_data


def get_latest_bike_fuel_log() -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, fill_date, created_at, trip_km, liters, yen_per_liter
            FROM bike_fuel_log
            ORDER BY fill_date DESC, created_at DESC, id DESC
            LIMIT 1
            """
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def calculate_yen_per_km_from_fuel_log(trip_km, liters, yen_per_liter) -> int:
    trip_km_dec = to_decimal(trip_km)
    liters_dec = to_decimal(liters)
    yen_per_liter_dec = to_decimal(yen_per_liter)
    if trip_km_dec <= 0:
        raise ValueError("trip_km must be greater than zero")
    if liters_dec <= 0:
        raise ValueError("liters must be greater than zero")
    if yen_per_liter_dec <= 0:
        raise ValueError("yen_per_liter must be greater than zero")
    yen_per_km = (yen_per_liter_dec * liters_dec) / trip_km_dec
    return int(yen_per_km.quantize(Decimal("1"), rounding=ROUND_CEILING))


def build_fuel_cost_helper() -> dict[str, Any]:
    unavailable = {
        "available": False,
        "message": "最新の燃費記録から単価を計算できません",
        "trip_km": "",
        "liters": "",
        "yen_per_liter": "",
        "yen_per_km": None,
        "item_name": "ガソリン代",
        "quantity": "1",
        "unit_name": "km",
        "tax_category": DEFAULT_TAX_CATEGORY,
    }
    try:
        latest = get_latest_bike_fuel_log()
    except Exception:
        return unavailable
    if not latest:
        return unavailable

    trip_km = latest.get("trip_km")
    liters = latest.get("liters")
    yen_per_liter = latest.get("yen_per_liter")
    try:
        yen_per_km = calculate_yen_per_km_from_fuel_log(trip_km, liters, yen_per_liter)
    except (ArithmeticError, ValueError):
        return {
            **unavailable,
            "trip_km": format_quantity(trip_km),
            "liters": format_quantity(liters),
            "yen_per_liter": str(int(to_decimal(yen_per_liter))) if yen_per_liter not in (None, "") else "",
        }

    return {
        "available": True,
        "message": "",
        "trip_km": format_quantity(trip_km),
        "liters": format_quantity(liters),
        "yen_per_liter": str(int(to_decimal(yen_per_liter))),
        "yen_per_km": yen_per_km,
        "item_name": "ガソリン代",
        "quantity": "1",
        "unit_name": "km",
        "tax_category": DEFAULT_TAX_CATEGORY,
    }


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cur.fetchone() is not None


def _index_exists(cur, table_name: str, index_name: str) -> bool:
    cur.execute(f"SHOW INDEX FROM {table_name} WHERE Key_name = %s", (index_name,))
    return bool(cur.fetchall())


def _ensure_invoice_item_column(cur, column_name: str, ddl: str) -> None:
    if not _column_exists(cur, "invoice_items", column_name):
        cur.execute(f"ALTER TABLE invoice_items ADD COLUMN {ddl}")


def build_invoice_mail_recipient_label(
    contact_name: Any,
    contact_person: Any,
    honorific: Any,
) -> str:
    company_name = str(contact_name or "").strip()
    person_name = str(contact_person or "").strip()
    suffix = str(honorific or "").strip()

    if company_name and person_name:
        return f"{company_name}　{person_name}{suffix}"
    if company_name:
        return f"{company_name}{suffix}"
    if person_name:
        return f"{person_name}{suffix}"
    return "お客様"


def build_default_invoice_mail_body(invoice_data: dict[str, Any], *, payout_access_url: str | None = None) -> str:
    recipient_label = build_invoice_mail_recipient_label(
        invoice_data.get("contact_name_snapshot") or invoice_data.get("contact_name"),
        invoice_data.get("contact_person_snapshot") or invoice_data.get("contact_person"),
        invoice_data.get("contact_honorific_snapshot") or invoice_data.get("honorific"),
    )
    issuer_name = (invoice_data.get("issuer_name") or "").strip() or "請求元"
    body = (
        f"いつもお世話になっております、{recipient_label}\n"
        f"{issuer_name}です。\n\n"
        "請求書をお送りいたします。ご確認のほどよろしくお願いいたします。"
    )
    if normalize_bank_info_mode(invoice_data.get("bank_info_mode")) == BANK_INFO_MODE_PAYOUT_LINK and payout_access_url:
        return append_payout_guidance_to_mail_body(body, payout_access_url)
    return body


def build_invoice_mail_body_with_payment_guidance(
    *,
    invoice: dict[str, Any],
    body: Any,
    payout_access_url: str | None = None,
    card_payment_url: str | None = None,
) -> str:
    normalized_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = [line.rstrip() for line in normalized_body.split("\n")]
    normalized_bank_info_mode = normalize_bank_info_mode(invoice.get("bank_info_mode"))
    invoice_status = str(invoice.get("status") or "").strip().lower()
    try:
        card_payment_enabled = int(invoice.get("card_payment_enabled") or 0) == 1
    except (TypeError, ValueError):
        card_payment_enabled = False

    normalized_payout_url = _normalize_stripped_text(payout_access_url)
    normalized_card_url = _normalize_stripped_text(card_payment_url)

    filtered_lines: list[str] = []
    for line in normalized_lines:
        stripped_line = line.strip()
        if stripped_line == PAYOUT_LINK_MAIL_GUIDANCE:
            continue
        if stripped_line == CARD_PAYMENT_MAIL_GUIDANCE:
            continue
        if normalized_payout_url and stripped_line == normalized_payout_url:
            continue
        if normalized_card_url and stripped_line == normalized_card_url:
            continue
        if "/payout?iv=" in stripped_line:
            continue
        if "/invoice/pay/" in stripped_line:
            continue
        filtered_lines.append(line)

    while filtered_lines and filtered_lines[-1] == "":
        filtered_lines.pop()

    cleaned_body = "\n".join(filtered_lines)

    # payout案内の可否は bank_info_mode + payout URL のみで判定する
    should_append_payout_guidance = (
        normalized_bank_info_mode == BANK_INFO_MODE_PAYOUT_LINK
        and bool(normalized_payout_url)
    )
    # カード案内の可否は card_payment_enabled + status + card URL のみで判定する
    # （bank_info_mode とは独立）
    should_append_card_guidance = (
        card_payment_enabled
        and invoice_status not in {"paid", "cancelled"}
        and bool(normalized_card_url)
    )

    payment_lines: list[str] = []
    if should_append_payout_guidance:
        payment_lines.extend([PAYOUT_LINK_MAIL_GUIDANCE, normalized_payout_url])
    if should_append_card_guidance:
        payment_lines.extend([CARD_PAYMENT_MAIL_GUIDANCE, normalized_card_url])

    if payment_lines:
        if cleaned_body:
            final_body = f"{cleaned_body}\n" + "\n".join(payment_lines)
        else:
            final_body = "\n".join(payment_lines)
    else:
        final_body = cleaned_body

    if normalized_payout_url:
        assert final_body.count(normalized_payout_url) == 1
    if normalized_card_url:
        assert final_body.count(normalized_card_url) == 1
    assert final_body.count(PAYOUT_LINK_MAIL_GUIDANCE) <= 1
    assert final_body.count(CARD_PAYMENT_MAIL_GUIDANCE) <= 1
    return final_body


def ensure_invoice_schema() -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_contacts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(191) NOT NULL,
                department_name VARCHAR(191) NULL,
                contact_name VARCHAR(191) NULL,
                honorific VARCHAR(32) NULL,
                email VARCHAR(255) NULL,
                postal_code VARCHAR(32) NULL,
                address1 VARCHAR(255) NULL,
                address2 VARCHAR(255) NULL,
                phone VARCHAR(64) NULL,
                freee_partner_name VARCHAR(191) NULL,
                freee_partner_id BIGINT NULL,
                freee_partner_code VARCHAR(191) NULL,
                default_due_days INT NULL,
                note TEXT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_invoice_contacts_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_headers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                invoice_no VARCHAR(32) NOT NULL,
                issue_date DATE NOT NULL,
                due_date DATE NOT NULL,
                contact_id INT NULL,
                contact_name_snapshot VARCHAR(191) NOT NULL,
                contact_department_snapshot VARCHAR(191) NULL,
                contact_person_snapshot VARCHAR(191) NULL,
                contact_honorific_snapshot VARCHAR(32) NULL,
                contact_email_snapshot VARCHAR(255) NULL,
                contact_postal_code_snapshot VARCHAR(32) NULL,
                contact_address1_snapshot VARCHAR(255) NULL,
                contact_address2_snapshot VARCHAR(255) NULL,
                contact_phone_snapshot VARCHAR(64) NULL,
                subject VARCHAR(255) NOT NULL,
                note TEXT NULL,
                bank_info TEXT NULL,
                issuer_name VARCHAR(191) NOT NULL,
                issuer_postal_code VARCHAR(32) NULL,
                issuer_address1 VARCHAR(255) NULL,
                issuer_address2 VARCHAR(255) NULL,
                issuer_phone VARCHAR(64) NULL,
                issuer_email VARCHAR(255) NULL,
                issuer_template_id INT NULL,
                bank_info_mode VARCHAR(32) NOT NULL DEFAULT 'inline',
                payout_access_token_id BIGINT NULL,
                card_payment_enabled TINYINT(1) NOT NULL DEFAULT 0,
                card_payment_public_token CHAR(36) NULL,
                card_payment_public_expires_at DATETIME NULL,
                card_paid_at DATETIME NULL,
                subtotal_yen INT NOT NULL DEFAULT 0,
                tax_yen INT NOT NULL DEFAULT 0,
                total_yen INT NOT NULL DEFAULT 0,
                tax_mode VARCHAR(16) NOT NULL DEFAULT 'external',
                status VARCHAR(16) NOT NULL DEFAULT 'draft',
                pdf_generated_at DATETIME NULL,
                pdf_storage_path VARCHAR(512) NULL,
                mailed_at DATETIME NULL,
                freee_exported_at DATETIME NULL,
                freee_deal_id BIGINT NULL,
                freee_api_synced_at DATETIME NULL,
                freee_api_registered_at DATETIME NULL,
                freee_api_modified_at DATETIME NULL,
                freee_api_status VARCHAR(32) NULL,
                freee_api_error TEXT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uniq_invoice_headers_invoice_no (invoice_no),
                INDEX idx_invoice_headers_issue_date (issue_date),
                INDEX idx_invoice_headers_due_date (due_date),
                INDEX idx_invoice_headers_status (status),
                INDEX idx_invoice_headers_contact_name (contact_name_snapshot)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INT AUTO_INCREMENT PRIMARY KEY,
                invoice_id INT NOT NULL,
                sort_order INT NOT NULL DEFAULT 0,
                row_type VARCHAR(16) NOT NULL DEFAULT 'normal',
                item_name VARCHAR(255) NOT NULL,
                memo_text TEXT NULL,
                quantity DECIMAL(12,2) NOT NULL DEFAULT 1.00,
                unit_name VARCHAR(32) NULL,
                unit_price_yen INT NOT NULL DEFAULT 0,
                line_total_yen INT NOT NULL DEFAULT 0,
                tax_category VARCHAR(64) NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_invoice_items_invoice_id (invoice_id),
                INDEX idx_invoice_items_sort_order (sort_order)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _ensure_invoice_item_column(cur, "row_type", "row_type VARCHAR(16) NOT NULL DEFAULT 'normal' AFTER sort_order")
        _ensure_invoice_item_column(cur, "memo_text", "memo_text TEXT NULL AFTER item_name")
        if not _column_exists(cur, "invoice_headers", "issuer_email"):
            cur.execute("ALTER TABLE invoice_headers ADD COLUMN issuer_email VARCHAR(255) NULL AFTER issuer_phone")
        if not _column_exists(cur, "invoice_headers", "issuer_template_id"):
            cur.execute("ALTER TABLE invoice_headers ADD COLUMN issuer_template_id INT NULL AFTER issuer_email")
        if not _column_exists(cur, "invoice_headers", "bank_info_mode"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN bank_info_mode VARCHAR(32) NOT NULL DEFAULT 'inline' AFTER issuer_template_id"
            )
        if not _column_exists(cur, "invoice_headers", "payout_access_token_id"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN payout_access_token_id BIGINT NULL AFTER bank_info_mode"
            )
        if not _column_exists(cur, "invoice_headers", "card_payment_enabled"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN card_payment_enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER payout_access_token_id"
            )
        if not _column_exists(cur, "invoice_headers", "card_payment_public_token"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN card_payment_public_token CHAR(36) NULL AFTER card_payment_enabled"
            )
        if not _column_exists(cur, "invoice_headers", "card_payment_public_expires_at"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN card_payment_public_expires_at DATETIME NULL AFTER card_payment_public_token"
            )
        if not _column_exists(cur, "invoice_headers", "card_paid_at"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN card_paid_at DATETIME NULL AFTER card_payment_public_expires_at"
            )
        if not _column_exists(cur, "invoice_contacts", "freee_partner_id"):
            cur.execute(
                "ALTER TABLE invoice_contacts ADD COLUMN freee_partner_id BIGINT NULL AFTER freee_partner_name"
            )
        if not _column_exists(cur, "invoice_contacts", "freee_partner_code"):
            cur.execute(
                "ALTER TABLE invoice_contacts ADD COLUMN freee_partner_code VARCHAR(191) NULL AFTER freee_partner_id"
            )
        if not _column_exists(cur, "invoice_headers", "freee_deal_id"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_deal_id BIGINT NULL AFTER freee_exported_at"
            )
        if not _column_exists(cur, "invoice_headers", "freee_api_synced_at"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_api_synced_at DATETIME NULL AFTER freee_deal_id"
            )
        if not _column_exists(cur, "invoice_headers", "freee_api_registered_at"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_api_registered_at DATETIME NULL AFTER freee_api_synced_at"
            )
        if not _column_exists(cur, "invoice_headers", "freee_api_modified_at"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_api_modified_at DATETIME NULL AFTER freee_api_registered_at"
            )
        if not _column_exists(cur, "invoice_headers", "freee_api_status"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_api_status VARCHAR(32) NULL AFTER freee_api_modified_at"
            )
        if not _column_exists(cur, "invoice_headers", "freee_api_error"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN freee_api_error TEXT NULL AFTER freee_api_status"
            )
        if not _column_exists(cur, "invoice_headers", "deleted_at"):
            cur.execute("ALTER TABLE invoice_headers ADD COLUMN deleted_at DATETIME NULL AFTER updated_at")
        if not _column_exists(cur, "invoice_headers", "deleted_by"):
            cur.execute("ALTER TABLE invoice_headers ADD COLUMN deleted_by VARCHAR(191) NULL AFTER deleted_at")
        if not _column_exists(cur, "invoice_headers", "deleted_original_status"):
            cur.execute(
                "ALTER TABLE invoice_headers ADD COLUMN deleted_original_status VARCHAR(16) NULL AFTER deleted_by"
            )
        if not _index_exists(cur, "invoice_headers", "idx_invoice_headers_deleted_at"):
            cur.execute("ALTER TABLE invoice_headers ADD INDEX idx_invoice_headers_deleted_at (deleted_at, issue_date)")
        ensure_invoice_issuer_templates_table(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_card_payments (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                invoice_id INT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                payment_token CHAR(36) NOT NULL,
                amount_yen_snapshot INT UNSIGNED NOT NULL,
                currency_code VARCHAR(8) NOT NULL DEFAULT 'JPY',
                buyer_email VARCHAR(255) NULL,
                buyer_name VARCHAR(191) NULL,
                idempotency_key CHAR(36) NOT NULL,
                square_payment_id VARCHAR(64) NULL UNIQUE,
                square_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                square_receipt_url VARCHAR(512) NULL,
                card_brand VARCHAR(32) NULL,
                card_last4 CHAR(4) NULL,
                card_exp_mm TINYINT NULL,
                card_exp_yyyy SMALLINT NULL,
                wallet_type VARCHAR(32) NULL,
                discord_notified TINYINT(1) NOT NULL DEFAULT 0,
                error_code VARCHAR(64) NULL,
                error_detail TEXT NULL,
                paid_at DATETIME NULL,
                square_updated_at DATETIME(6) NULL,
                last_synced_at DATETIME NULL,
                sync_attempts INT UNSIGNED NOT NULL DEFAULT 0,
                sync_error TEXT NULL,
                INDEX ix_invoice_card_payments_invoice_id (invoice_id, created_at),
                INDEX ix_invoice_card_payments_status (invoice_id, square_status),
                INDEX ix_invoice_card_payments_payment_token (payment_token)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        if not _column_exists(cur, "invoice_card_payments", "discord_notified"):
            cur.execute(
                "ALTER TABLE invoice_card_payments ADD COLUMN discord_notified TINYINT(1) NOT NULL DEFAULT 0 AFTER card_exp_yyyy"
            )
        if not _column_exists(cur, "invoice_card_payments", "wallet_type"):
            cur.execute(
                "ALTER TABLE invoice_card_payments ADD COLUMN wallet_type VARCHAR(32) NULL AFTER card_exp_yyyy"
            )
        for column_name, ddl in (
            ("square_updated_at", "ALTER TABLE invoice_card_payments ADD COLUMN square_updated_at DATETIME(6) NULL"),
            ("last_synced_at", "ALTER TABLE invoice_card_payments ADD COLUMN last_synced_at DATETIME NULL"),
            ("sync_attempts", "ALTER TABLE invoice_card_payments ADD COLUMN sync_attempts INT UNSIGNED NOT NULL DEFAULT 0"),
            ("sync_error", "ALTER TABLE invoice_card_payments ADD COLUMN sync_error TEXT NULL"),
        ):
            if not _column_exists(cur, "invoice_card_payments", column_name):
                cur.execute(ddl)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS square_sync_control (
                id TINYINT UNSIGNED PRIMARY KEY,
                managed_from DATETIME(6) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("INSERT IGNORE INTO square_sync_control (id, managed_from) VALUES (1, NOW(6))")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS square_webhook_events (
                id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
                square_event_id VARCHAR(96) NOT NULL UNIQUE,
                event_type VARCHAR(96) NOT NULL,
                object_id VARCHAR(96) NULL,
                payload_sha256 CHAR(64) NOT NULL,
                processing_status ENUM('RECEIVED','PROCESSED','FAILED','IGNORED') NOT NULL DEFAULT 'RECEIVED',
                error_detail TEXT NULL,
                received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME NULL,
                INDEX ix_square_webhook_type_received (event_type, received_at),
                INDEX ix_square_webhook_processing (processing_status, received_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_mail_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                invoice_id INT NOT NULL,
                to_email VARCHAR(512) NOT NULL,
                cc_email VARCHAR(512) NULL,
                bcc_email VARCHAR(512) NULL,
                subject VARCHAR(255) NOT NULL,
                body TEXT NULL,
                attachment_filename VARCHAR(255) NULL,
                sent_at DATETIME NULL,
                status VARCHAR(32) NOT NULL,
                error_message TEXT NULL,
                created_at DATETIME NOT NULL,
                INDEX idx_invoice_mail_logs_invoice_id (invoice_id),
                INDEX idx_invoice_mail_logs_sent_at (sent_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_csv_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                invoice_id INT NOT NULL,
                filename VARCHAR(255) NOT NULL,
                exported_at DATETIME NOT NULL,
                status VARCHAR(32) NOT NULL,
                error_message TEXT NULL,
                created_at DATETIME NOT NULL,
                INDEX idx_invoice_csv_logs_invoice_id (invoice_id),
                INDEX idx_invoice_csv_logs_exported_at (exported_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_deletion_audit (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                invoice_id INT NOT NULL,
                invoice_no VARCHAR(32) NOT NULL,
                action ENUM('SOFT_DELETE','RESTORE','PURGE') NOT NULL,
                invoice_status VARCHAR(16) NOT NULL,
                acted_by VARCHAR(191) NULL,
                acted_at DATETIME NOT NULL,
                INDEX idx_invoice_deletion_audit_invoice (invoice_id, acted_at),
                INDEX idx_invoice_deletion_audit_action (action, acted_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def fetch_contacts(*, q: str = "") -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        params: list[Any] = []
        where = ""
        if q:
            where = "WHERE name LIKE %s OR contact_name LIKE %s OR email LIKE %s"
            like = f"%{q}%"
            params.extend([like, like, like])
        cur.execute(
            f"""
            SELECT *
            FROM invoice_contacts
            {where}
            ORDER BY updated_at DESC, id DESC
            """,
            params,
        )
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def get_contact(contact_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_contacts WHERE id = %s", (contact_id,))
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def save_contact(contact_id: int | None, form: dict[str, Any]) -> int:
    now = now_jst()
    payload = (
        (form.get("name") or "").strip(),
        (form.get("department_name") or "").strip() or None,
        (form.get("contact_name") or "").strip() or None,
        (form.get("honorific") or "").strip() or None,
        (form.get("email") or "").strip() or None,
        (form.get("postal_code") or "").strip() or None,
        (form.get("address1") or "").strip() or None,
        (form.get("address2") or "").strip() or None,
        (form.get("phone") or "").strip() or None,
        (form.get("freee_partner_name") or "").strip() or None,
        int(form.get("freee_partner_id") or 0) or None,
        (form.get("freee_partner_code") or "").strip() or None,
        int(form.get("default_due_days") or 30),
        (form.get("note") or "").strip() or None,
    )
    if not payload[0]:
        raise InvoiceValidationError("取引先名を入力してください。")

    db = get_db()
    cur = db.cursor()
    try:
        if contact_id:
            cur.execute(
                """
                UPDATE invoice_contacts
                SET name=%s,
                    department_name=%s,
                    contact_name=%s,
                    honorific=%s,
                    email=%s,
                    postal_code=%s,
                    address1=%s,
                    address2=%s,
                    phone=%s,
                    freee_partner_name=%s,
                    freee_partner_id=%s,
                    freee_partner_code=%s,
                    default_due_days=%s,
                    note=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (*payload, now, contact_id),
            )
            db.commit()
            return contact_id
        cur.execute(
            """
            INSERT INTO invoice_contacts (
                name, department_name, contact_name, honorific, email,
                postal_code, address1, address2, phone,
                freee_partner_name, freee_partner_id, freee_partner_code,
                default_due_days, note, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (*payload, now, now),
        )
        db.commit()
        return int(cur.lastrowid)
    finally:
        cur.close()
        db.close()


def delete_contact(contact_id: int) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM invoice_contacts WHERE id = %s", (contact_id,))
        db.commit()
    finally:
        cur.close()
        db.close()


def _snapshot_contact(contact: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_name_snapshot": contact.get("name") or "",
        "contact_department_snapshot": contact.get("department_name") or None,
        "contact_person_snapshot": contact.get("contact_name") or None,
        "contact_honorific_snapshot": contact.get("honorific") or None,
        "contact_email_snapshot": contact.get("email") or None,
        "contact_postal_code_snapshot": contact.get("postal_code") or None,
        "contact_address1_snapshot": contact.get("address1") or None,
        "contact_address2_snapshot": contact.get("address2") or None,
        "contact_phone_snapshot": contact.get("phone") or None,
    }


def parse_invoice_items(form) -> list[InvoiceItemInput]:
    row_types = form.getlist("row_type[]") if hasattr(form, "getlist") else form.get("row_type[]", [])
    names = form.getlist("item_name[]") if hasattr(form, "getlist") else form.get("item_name[]", [])
    memo_texts = form.getlist("memo_text[]") if hasattr(form, "getlist") else form.get("memo_text[]", [])
    quantities = form.getlist("quantity[]") if hasattr(form, "getlist") else form.get("quantity[]", [])
    unit_names = form.getlist("unit_name[]") if hasattr(form, "getlist") else form.get("unit_name[]", [])
    prices = form.getlist("unit_price_yen[]") if hasattr(form, "getlist") else form.get("unit_price_yen[]", [])
    tax_categories = form.getlist("tax_category[]") if hasattr(form, "getlist") else form.get("tax_category[]", [])

    items: list[InvoiceItemInput] = []
    row_count = max(
        len(row_types),
        len(names),
        len(memo_texts),
        len(quantities),
        len(unit_names),
        len(prices),
        len(tax_categories),
    )
    for index in range(row_count):
        row_type = normalize_row_type(row_types[index] if index < len(row_types) else ROW_TYPE_NORMAL)
        name = (names[index] if index < len(names) else "").strip()
        memo_text = (memo_texts[index] if index < len(memo_texts) else "").strip()
        quantity_raw = quantities[index] if index < len(quantities) else ""
        unit_name = (unit_names[index] if index < len(unit_names) else "").strip()
        price_raw = prices[index] if index < len(prices) else ""
        tax_category = normalize_tax_category(
            (tax_categories[index] if index < len(tax_categories) else DEFAULT_TAX_CATEGORY).strip() or DEFAULT_TAX_CATEGORY
        )

        if row_type == ROW_TYPE_MEMO:
            if not memo_text:
                continue
            items.append(
                InvoiceItemInput(
                    row_type=ROW_TYPE_MEMO,
                    memo_text=memo_text,
                    sort_order=index,
                    tax_category=DEFAULT_TAX_CATEGORY,
                )
            )
            continue

        if not any([name, quantity_raw, unit_name, price_raw]):
            continue
        quantity = quantize_quantity(to_decimal(quantity_raw, "0"))
        unit_price = quantize_yen(to_decimal(price_raw, "0"))
        if not name:
            raise InvoiceValidationError(f"明細{index + 1}行目の商品名を入力してください。")
        if quantity <= 0:
            raise InvoiceValidationError(f"明細{index + 1}行目の数量は0より大きい値を入力してください。")
        if unit_price < 0:
            raise InvoiceValidationError(f"明細{index + 1}行目の単価が不正です。")
        items.append(
            InvoiceItemInput(
                item_name=name,
                quantity=quantity,
                unit_name=unit_name,
                unit_price_yen=unit_price,
                tax_category=tax_category,
                sort_order=index,
                row_type=ROW_TYPE_NORMAL,
                memo_text="",
            )
        )
    if not items:
        raise InvoiceValidationError("明細を1行以上入力してください。")
    return items


def summarize_invoice_totals(items: list[InvoiceItemInput], tax_mode: str) -> dict[str, int]:
    subtotal = 0
    tax_10 = 0
    tax_8 = 0
    total = 0
    tax_mode = normalize_tax_mode(tax_mode)
    for item in items:
        if item.is_memo:
            continue
        line_amounts = item.line_amounts(tax_mode)
        subtotal += line_amounts["line_subtotal_yen"]
        total += line_amounts["line_total_yen"]
        if line_amounts["tax_category"] == "tax8":
            tax_8 += line_amounts["line_tax_yen"]
        else:
            tax_10 += line_amounts["line_tax_yen"]
    tax = tax_10 + tax_8
    return {
        "subtotal_yen": subtotal,
        "tax_10_yen": tax_10,
        "tax_8_yen": tax_8,
        "tax_yen": tax,
        "total_yen": total,
    }


def calculate_invoice_totals(items: list[InvoiceItemInput], tax_mode: str) -> dict[str, int]:
    totals = summarize_invoice_totals(items, tax_mode)
    if totals["total_yen"] <= 0:
        raise InvoiceValidationError("合計金額は0円より大きくしてください。")
    return totals


def _build_invoice_payload(form, contact: dict[str, Any], items: list[InvoiceItemInput]) -> dict[str, Any]:
    issue_date = form.get("issue_date")
    due_date = form.get("due_date")
    issue = issue_date if isinstance(issue_date, date) else None
    due = due_date if isinstance(due_date, date) else None
    if not issue:
        raise InvoiceValidationError("請求日を入力してください。")
    if not due:
        due = default_due_date(issue, contact.get("default_due_days"))
    tax_mode = normalize_tax_mode(form.get("tax_mode"))
    totals = calculate_invoice_totals(items, tax_mode)
    subject = (form.get("subject") or "").strip()
    if not subject:
        raise InvoiceValidationError("件名を入力してください。")
    issuer_name = (form.get("issuer_name") or "").strip()
    if not issuer_name:
        raise InvoiceValidationError("発行者名を入力してください。")
    issuer_template_id = _normalize_optional_int(form.get("issuer_template_id"))
    card_payment_enabled = 1 if str(form.get("card_payment_enabled") or "").strip() in {"1", "true", "on", "yes"} else 0
    snapshot = _snapshot_contact(contact)
    payload = {
        "issue_date": issue,
        "due_date": due,
        "contact_id": contact.get("id"),
        **snapshot,
        "subject": subject,
        "note": normalize_multiline_text(form.get("note")),
        "bank_info": normalize_multiline_text(form.get("bank_info")),
        "bank_info_mode": normalize_bank_info_mode(form.get("bank_info_mode")),
        "issuer_name": issuer_name,
        "issuer_postal_code": (form.get("issuer_postal_code") or "").strip() or None,
        "issuer_address1": (form.get("issuer_address1") or "").strip() or None,
        "issuer_address2": (form.get("issuer_address2") or "").strip() or None,
        "issuer_phone": (form.get("issuer_phone") or "").strip() or None,
        "issuer_template_id": issuer_template_id,
        "issuer_email": (form.get("issuer_email") or "").strip() or None,
        "payout_access_token_id": None,
        "card_payment_enabled": card_payment_enabled,
        "card_payment_public_token": form.get("card_payment_public_token"),
        "card_payment_public_expires_at": form.get("card_payment_public_expires_at"),
        "card_paid_at": form.get("card_paid_at"),
        **totals,
        "tax_mode": tax_mode,
        "status": normalize_status(form.get("status") or "draft"),
    }
    if not payload["issuer_email"]:
        payload["issuer_email"] = resolve_invoice_issuer_email(
            {
                "issuer_email": payload["issuer_email"],
                "issuer_template_id": payload["issuer_template_id"],
            }
        ) or None
    return payload


def serialize_invoice_item(item: InvoiceItemInput, tax_mode: str) -> dict[str, Any]:
    line_amounts = item.line_amounts(tax_mode)
    return {
        "row_type": item.row_type,
        "item_name": item.item_name,
        "memo_text": item.memo_text,
        "quantity": str(item.quantity),
        "unit_name": item.unit_name,
        "unit_price_yen": str(item.unit_price_yen),
        "line_subtotal_yen": line_amounts["line_subtotal_yen"],
        "line_tax_yen": line_amounts["line_tax_yen"],
        "line_total_yen": line_amounts["line_total_yen"],
        "tax_category": normalize_tax_category(item.tax_category),
        "tax_category_label": TAX_CATEGORY_LABELS[normalize_tax_category(item.tax_category)],
        "sort_order": item.sort_order,
    }


def _hydrate_invoice_item(item: dict[str, Any], tax_mode: str) -> dict[str, Any]:
    row_type = normalize_row_type(item.get("row_type"))
    tax_category = normalize_tax_category(item.get("tax_category"))
    quantity = quantize_quantity(to_decimal(item.get("quantity"), "0"))
    unit_price_yen = quantize_yen(to_decimal(item.get("unit_price_yen"), "0"))
    base_amount_yen = quantize_yen(quantity * Decimal(unit_price_yen))
    line_amounts = (
        {
            "line_subtotal_yen": 0,
            "line_tax_yen": 0,
            "line_total_yen": 0,
        }
        if row_type == ROW_TYPE_MEMO
        else calculate_line_amounts(base_amount_yen, tax_mode, tax_category)
    )
    hydrated = dict(item)
    hydrated.update(
        {
            "row_type": row_type,
            "memo_text": (item.get("memo_text") or "").strip(),
            "quantity": quantity,
            "unit_price_yen": unit_price_yen,
            "base_amount_yen": base_amount_yen,
            "tax_category": tax_category,
            "tax_category_label": TAX_CATEGORY_LABELS[tax_category],
            **line_amounts,
        }
    )
    return hydrated


def _next_invoice_no(cur, issue_date: date) -> str:
    prefix = f"INV-{issue_date.strftime('%Y%m')}-"
    lock_name = f"invoice_no_{issue_date.strftime('%Y%m')}"
    cur.execute("SELECT GET_LOCK(%s, 10)", (lock_name,))
    lock_row = cur.fetchone()
    lock_val = lock_row[0] if isinstance(lock_row, tuple) else next(iter(lock_row.values()))
    if int(lock_val or 0) != 1:
        raise InvoiceValidationError("請求書番号の採番ロック取得に失敗しました。しばらく待って再試行してください。")
    cur.execute(
        """
        SELECT invoice_no
        FROM invoice_headers
        WHERE invoice_no LIKE %s
        ORDER BY invoice_no DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    )
    row = cur.fetchone()
    last_seq = 0
    if row:
        invoice_no = row[0] if isinstance(row, tuple) else row.get("invoice_no")
        try:
            last_seq = int(str(invoice_no).split("-")[-1])
        except (TypeError, ValueError):
            last_seq = 0
    return f"{prefix}{last_seq + 1:03d}"


def _release_invoice_lock(cur, issue_date: date) -> None:
    cur.execute("SELECT RELEASE_LOCK(%s)", (f"invoice_no_{issue_date.strftime('%Y%m')}",))
    cur.fetchone()


def save_invoice(invoice_id: int | None, form) -> int:
    contact_id = int(form.get("contact_id") or 0)
    if contact_id <= 0:
        raise InvoiceValidationError("請求先を選択してください。")
    contact = get_contact(contact_id)
    if not contact:
        raise InvoiceValidationError("選択した請求先が見つかりません。")

    issue_date = form.get("issue_date")
    due_date = form.get("due_date")
    issue = issue_date if isinstance(issue_date, date) else None
    due = due_date if isinstance(due_date, date) else None
    if not issue:
        raise InvoiceValidationError("請求日を入力してください。")
    items = parse_invoice_items(form)
    payload = _build_invoice_payload({**form, "issue_date": issue, "due_date": due}, contact, items)
    now = now_jst()

    db = get_db()
    cur = db.cursor()
    try:
        if invoice_id:
            cur.execute(
                """
                UPDATE invoice_headers
                SET issue_date=%s,
                    due_date=%s,
                    contact_id=%s,
                    contact_name_snapshot=%s,
                    contact_department_snapshot=%s,
                    contact_person_snapshot=%s,
                    contact_honorific_snapshot=%s,
                    contact_email_snapshot=%s,
                    contact_postal_code_snapshot=%s,
                    contact_address1_snapshot=%s,
                    contact_address2_snapshot=%s,
                    contact_phone_snapshot=%s,
                    subject=%s,
                    note=%s,
                    bank_info=%s,
                    issuer_name=%s,
                    issuer_postal_code=%s,
                    issuer_address1=%s,
                    issuer_address2=%s,
                    issuer_phone=%s,
                    issuer_email=%s,
                    issuer_template_id=%s,
                    bank_info_mode=%s,
                    payout_access_token_id=%s,
                    card_payment_enabled=%s,
                    subtotal_yen=%s,
                    tax_yen=%s,
                    total_yen=%s,
                    tax_mode=%s,
                    status=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (
                    payload["issue_date"], payload["due_date"], payload["contact_id"],
                    payload["contact_name_snapshot"], payload["contact_department_snapshot"],
                    payload["contact_person_snapshot"], payload["contact_honorific_snapshot"],
                    payload["contact_email_snapshot"], payload["contact_postal_code_snapshot"],
                    payload["contact_address1_snapshot"], payload["contact_address2_snapshot"],
                    payload["contact_phone_snapshot"], payload["subject"], payload["note"],
                    payload["bank_info"], payload["issuer_name"], payload["issuer_postal_code"],
                    payload["issuer_address1"], payload["issuer_address2"], payload["issuer_phone"], payload["issuer_email"], payload["issuer_template_id"],
                    payload["bank_info_mode"], payload["payout_access_token_id"], payload["card_payment_enabled"],
                    payload["subtotal_yen"], payload["tax_yen"], payload["total_yen"],
                    payload["tax_mode"], payload["status"], now, invoice_id,
                ),
            )
            cur.execute("DELETE FROM invoice_items WHERE invoice_id = %s", (invoice_id,))
        else:
            invoice_no = _next_invoice_no(cur, payload["issue_date"])
            cur.execute(
                """
                INSERT INTO invoice_headers (
                    invoice_no, issue_date, due_date, contact_id,
                    contact_name_snapshot, contact_department_snapshot, contact_person_snapshot,
                    contact_honorific_snapshot, contact_email_snapshot, contact_postal_code_snapshot,
                    contact_address1_snapshot, contact_address2_snapshot, contact_phone_snapshot,
                    subject, note, bank_info,
                    issuer_name, issuer_postal_code, issuer_address1, issuer_address2, issuer_phone, issuer_email, issuer_template_id,
                    bank_info_mode, payout_access_token_id,
                    card_payment_enabled, card_payment_public_token, card_payment_public_expires_at, card_paid_at,
                    subtotal_yen, tax_yen, total_yen, tax_mode, status,
                    pdf_generated_at, pdf_storage_path, mailed_at, freee_exported_at,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, NULL, NULL, NULL,
                    %s, %s, %s, %s, %s,
                    NULL, NULL, NULL, NULL,
                    %s, %s
                )
                """,
                (
                    invoice_no, payload["issue_date"], payload["due_date"], payload["contact_id"],
                    payload["contact_name_snapshot"], payload["contact_department_snapshot"], payload["contact_person_snapshot"],
                    payload["contact_honorific_snapshot"], payload["contact_email_snapshot"], payload["contact_postal_code_snapshot"],
                    payload["contact_address1_snapshot"], payload["contact_address2_snapshot"], payload["contact_phone_snapshot"],
                    payload["subject"], payload["note"], payload["bank_info"],
                    payload["issuer_name"], payload["issuer_postal_code"], payload["issuer_address1"], payload["issuer_address2"], payload["issuer_phone"], payload["issuer_email"], payload["issuer_template_id"],
                    payload["bank_info_mode"], payload["payout_access_token_id"], payload["card_payment_enabled"],
                    payload["subtotal_yen"], payload["tax_yen"], payload["total_yen"], payload["tax_mode"], "draft",
                    now, now,
                ),
            )
            invoice_id = int(cur.lastrowid)
        cur.executemany(
            """
            INSERT INTO invoice_items (
                invoice_id, sort_order, row_type, item_name, memo_text, quantity, unit_name,
                unit_price_yen, line_total_yen, tax_category, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    invoice_id,
                    item.sort_order,
                    item.row_type,
                    item.item_name or "",
                    item.memo_text or None,
                    item.quantity,
                    item.unit_name or None,
                    item.unit_price_yen,
                    item.line_amounts(payload["tax_mode"])["line_total_yen"],
                    item.tax_category,
                    now,
                    now,
                )
                for item in items
            ],
        )
        _release_invoice_lock(cur, payload["issue_date"])
        db.commit()
        return int(invoice_id)
    except Exception:
        try:
            _release_invoice_lock(cur, payload["issue_date"])
        except Exception:
            pass
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def list_invoices(*, q: str = "", status: str = "", start: str = "", end: str = "") -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        where: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if q:
            like = f"%{q}%"
            where.append("(invoice_no LIKE %s OR contact_name_snapshot LIKE %s OR subject LIKE %s)")
            params.extend([like, like, like])
        if status:
            where.append("status = %s")
            params.append(status)
        if start:
            where.append("issue_date >= %s")
            params.append(start)
        if end:
            where.append("issue_date <= %s")
            params.append(end)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        cur.execute(
            f"""
            SELECT *
            FROM invoice_headers
            {where_sql}
            ORDER BY issue_date DESC, id DESC
            """,
            params,
        )
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def list_deleted_invoices(*, q: str = "") -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        where = ["deleted_at IS NOT NULL"]
        params: list[Any] = []
        if q:
            like = f"%{q}%"
            where.append("(invoice_no LIKE %s OR contact_name_snapshot LIKE %s OR subject LIKE %s)")
            params.extend([like, like, like])
        cur.execute(
            f"""
            SELECT *
              FROM invoice_headers
             WHERE {' AND '.join(where)}
             ORDER BY deleted_at DESC, id DESC
            """,
            params,
        )
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def _record_invoice_deletion_audit(cur, invoice: dict[str, Any], action: str, acted_by: str | None) -> None:
    cur.execute(
        """
        INSERT INTO invoice_deletion_audit (
            invoice_id, invoice_no, action, invoice_status, acted_by, acted_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            int(invoice["id"]),
            invoice.get("invoice_no") or "",
            action,
            invoice.get("status") or "",
            (acted_by or "").strip() or None,
            now_jst(),
        ),
    )


def _lock_invoice_for_deletion(cur, invoice_id: int, *, must_be_deleted: bool) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id, invoice_no, status, deleted_at, pdf_storage_path
          FROM invoice_headers
         WHERE id=%s
         FOR UPDATE
        """,
        (invoice_id,),
    )
    invoice = cur.fetchone()
    if not invoice:
        raise InvoiceValidationError("請求書が見つかりません。")
    is_deleted = invoice.get("deleted_at") is not None
    if must_be_deleted and not is_deleted:
        raise InvoiceValidationError("この請求書は削除済みではありません。")
    if not must_be_deleted and is_deleted:
        raise InvoiceValidationError("この請求書は既に削除済みです。")
    status = str(invoice.get("status") or "").strip().lower()
    if status not in INVOICE_DELETABLE_STATUSES:
        raise InvoiceValidationError("発行済み・送付済み・入金済みの請求書は削除できません。")
    return invoice


def _ensure_invoice_has_no_protected_card_payment(cur, invoice_id: int) -> None:
    safe_statuses = ",".join(["%s"] * len(INVOICE_PURGE_SAFE_PAYMENT_STATUSES))
    cur.execute(
        f"""
        SELECT square_status
          FROM invoice_card_payments
         WHERE invoice_id=%s
           AND UPPER(square_status) NOT IN ({safe_statuses})
         LIMIT 1
        """,
        (invoice_id, *INVOICE_PURGE_SAFE_PAYMENT_STATUSES),
    )
    payment = cur.fetchone()
    if payment:
        status = str(payment.get("square_status") or "UNKNOWN").upper()
        raise InvoiceValidationError(
            f"Square決済履歴（{status}）があるため、この請求書は削除できません。"
        )


def _ensure_invoice_has_no_inflight_card_payment(cur, invoice_id: int) -> None:
    blocking_statuses = ",".join(["%s"] * len(INVOICE_SOFT_DELETE_BLOCKING_PAYMENT_STATUSES))
    cur.execute(
        f"""
        SELECT square_status
          FROM invoice_card_payments
         WHERE invoice_id=%s
           AND UPPER(square_status) IN ({blocking_statuses})
         LIMIT 1
        """,
        (invoice_id, *INVOICE_SOFT_DELETE_BLOCKING_PAYMENT_STATUSES),
    )
    payment = cur.fetchone()
    if payment:
        status = str(payment.get("square_status") or "UNKNOWN").upper()
        raise InvoiceValidationError(
            f"Square決済が処理中または確認待ち（{status}）のため、この請求書は削除できません。"
        )


def soft_delete_invoice(invoice_id: int, *, deleted_by: str | None = None) -> dict[str, Any]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        invoice = _lock_invoice_for_deletion(cur, invoice_id, must_be_deleted=False)
        _ensure_invoice_has_no_inflight_card_payment(cur, invoice_id)
        now = now_jst()
        cur.execute(
            """
            UPDATE invoice_headers
               SET deleted_at=%s,
                   deleted_by=%s,
                   deleted_original_status=status,
                   updated_at=%s
             WHERE id=%s AND deleted_at IS NULL
            """,
            (now, (deleted_by or "").strip() or None, now, invoice_id),
        )
        if cur.rowcount != 1:
            raise InvoiceValidationError("請求書を削除済みに移動できませんでした。")
        _record_invoice_deletion_audit(cur, invoice, "SOFT_DELETE", deleted_by)
        db.commit()
        return invoice
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def restore_deleted_invoice(invoice_id: int, *, restored_by: str | None = None) -> dict[str, Any]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        invoice = _lock_invoice_for_deletion(cur, invoice_id, must_be_deleted=True)
        cur.execute(
            """
            UPDATE invoice_headers
               SET deleted_at=NULL,
                   deleted_by=NULL,
                   deleted_original_status=NULL,
                   updated_at=%s
             WHERE id=%s AND deleted_at IS NOT NULL
            """,
            (now_jst(), invoice_id),
        )
        if cur.rowcount != 1:
            raise InvoiceValidationError("請求書を復元できませんでした。")
        _record_invoice_deletion_audit(cur, invoice, "RESTORE", restored_by)
        db.commit()
        return invoice
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def purge_deleted_invoice(
    invoice_id: int,
    *,
    confirmed_invoice_no: str,
    purged_by: str | None = None,
) -> dict[str, Any]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        invoice = _lock_invoice_for_deletion(cur, invoice_id, must_be_deleted=True)
        if (confirmed_invoice_no or "").strip() != str(invoice.get("invoice_no") or ""):
            raise InvoiceValidationError("確認用の請求書番号が一致しません。")
        _ensure_invoice_has_no_protected_card_payment(cur, invoice_id)
        _record_invoice_deletion_audit(cur, invoice, "PURGE", purged_by)
        for table_name in (
            "invoice_items",
            "invoice_mail_logs",
            "invoice_csv_logs",
            "invoice_card_payments",
        ):
            cur.execute(f"DELETE FROM {table_name} WHERE invoice_id=%s", (invoice_id,))
        cur.execute("DELETE FROM invoice_headers WHERE id=%s AND deleted_at IS NOT NULL", (invoice_id,))
        if cur.rowcount != 1:
            raise InvoiceValidationError("請求書を完全削除できませんでした。")
        db.commit()
        return invoice
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def get_invoice(invoice_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_headers WHERE id = %s AND deleted_at IS NULL", (invoice_id,))
        invoice = cur.fetchone()
        if not invoice:
            return None
        cur.execute(
            "SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY sort_order ASC, id ASC",
            (invoice_id,),
        )
        invoice["items"] = [
            _hydrate_invoice_item(item, invoice.get("tax_mode") or "external")
            for item in cur.fetchall()
        ]
        invoice.update(summarize_invoice_totals([
            InvoiceItemInput(
                row_type=item.get("row_type") or ROW_TYPE_NORMAL,
                item_name=item.get("item_name") or "",
                memo_text=item.get("memo_text") or "",
                quantity=to_decimal(item.get("quantity"), "0"),
                unit_name=item.get("unit_name") or "",
                unit_price_yen=int(item.get("unit_price_yen") or 0),
                tax_category=item.get("tax_category") or DEFAULT_TAX_CATEGORY,
                sort_order=int(item.get("sort_order") or 0),
            )
            for item in invoice["items"]
        ], invoice.get("tax_mode") or "external"))
        cur.execute(
            "SELECT * FROM invoice_mail_logs WHERE invoice_id = %s ORDER BY created_at DESC, id DESC",
            (invoice_id,),
        )
        invoice["mail_logs"] = cur.fetchall()
        cur.execute(
            "SELECT * FROM invoice_csv_logs WHERE invoice_id = %s ORDER BY created_at DESC, id DESC",
            (invoice_id,),
        )
        invoice["csv_logs"] = cur.fetchall()
        return invoice
    finally:
        cur.close()
        db.close()


def get_latest_invoice_sent_mail_log(invoice_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT *
              FROM invoice_mail_logs
             WHERE invoice_id=%s
               AND status='sent'
             ORDER BY sent_at DESC, created_at DESC, id DESC
             LIMIT 1
            """,
            (invoice_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def has_invoice_receipt_mail_sent(invoice_id: int) -> bool:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT id
              FROM invoice_mail_logs
             WHERE invoice_id=%s
               AND status='receipt_sent'
             LIMIT 1
            """,
            (invoice_id,),
        )
        return bool(cur.fetchone())
    finally:
        cur.close()
        db.close()


def duplicate_invoice(invoice_id: int) -> int:
    original = get_invoice(invoice_id)
    if not original:
        raise InvoiceValidationError("複製元の請求書が見つかりません。")
    db = get_db()
    cur = db.cursor()
    now = now_jst()
    try:
        new_invoice_no = _next_invoice_no(cur, original["issue_date"])
        cur.execute(
            """
            INSERT INTO invoice_headers (
                invoice_no, issue_date, due_date, contact_id,
                contact_name_snapshot, contact_department_snapshot, contact_person_snapshot,
                contact_honorific_snapshot, contact_email_snapshot, contact_postal_code_snapshot,
                contact_address1_snapshot, contact_address2_snapshot, contact_phone_snapshot,
                subject, note, bank_info,
                issuer_name, issuer_postal_code, issuer_address1, issuer_address2, issuer_phone, issuer_email, issuer_template_id,
                bank_info_mode, payout_access_token_id,
                card_payment_enabled, card_payment_public_token, card_payment_public_expires_at, card_paid_at,
                subtotal_yen, tax_yen, total_yen, tax_mode, status,
                pdf_generated_at, pdf_storage_path, mailed_at, freee_exported_at,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, NULL, NULL, NULL,
                %s, %s, %s, %s, %s,
                NULL, NULL, NULL, NULL,
                %s, %s
            )
            """,
            (
                new_invoice_no, original["issue_date"], original["due_date"], original.get("contact_id"),
                original.get("contact_name_snapshot"), original.get("contact_department_snapshot"), original.get("contact_person_snapshot"),
                original.get("contact_honorific_snapshot"), original.get("contact_email_snapshot"), original.get("contact_postal_code_snapshot"),
                original.get("contact_address1_snapshot"), original.get("contact_address2_snapshot"), original.get("contact_phone_snapshot"),
                original.get("subject"), original.get("note"), original.get("bank_info"),
                original.get("issuer_name"), original.get("issuer_postal_code"), original.get("issuer_address1"), original.get("issuer_address2"), original.get("issuer_phone"), original.get("issuer_email"), original.get("issuer_template_id"),
                normalize_bank_info_mode(original.get("bank_info_mode")), None, int(original.get("card_payment_enabled") or 0),
                original.get("subtotal_yen"), original.get("tax_yen"), original.get("total_yen"), original.get("tax_mode"), "draft",
                now, now,
            ),
        )
        new_id = int(cur.lastrowid)
        cur.executemany(
            """
            INSERT INTO invoice_items (
                invoice_id, sort_order, row_type, item_name, memo_text, quantity, unit_name,
                unit_price_yen, line_total_yen, tax_category, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    new_id,
                    item.get("sort_order") or 0,
                    normalize_row_type(item.get("row_type")),
                    item.get("item_name") or "",
                    (item.get("memo_text") or "").strip() or None,
                    item.get("quantity"),
                    item.get("unit_name"),
                    item.get("unit_price_yen") or 0,
                    item.get("line_total_yen") or 0,
                    normalize_tax_category(item.get("tax_category")),
                    now,
                    now,
                )
                for item in original.get("items", [])
            ],
        )
        _release_invoice_lock(cur, original["issue_date"])
        db.commit()
        return new_id
    except Exception:
        try:
            _release_invoice_lock(cur, original["issue_date"])
        except Exception:
            pass
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def update_invoice_status(invoice_id: int, status: str) -> None:
    status = normalize_status(status)
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        if status == "cancelled":
            cur.execute(
                """
                UPDATE invoice_headers
                SET status=%s,
                    card_payment_public_token=NULL,
                    card_payment_public_expires_at=NULL,
                    updated_at=%s
                WHERE id=%s
                """,
                (status, now, invoice_id),
            )
        else:
            cur.execute(
                "UPDATE invoice_headers SET status=%s, updated_at=%s WHERE id=%s",
                (status, now, invoice_id),
            )
        db.commit()
    finally:
        cur.close()
        db.close()


def mark_invoice_issued(invoice_id: int) -> None:
    update_invoice_status(invoice_id, "issued")


def mark_invoice_pdf_generated(invoice_id: int, path: str) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE invoice_headers
            SET pdf_generated_at=%s, pdf_storage_path=%s, updated_at=%s
            WHERE id=%s
            """,
            (now, path, now, invoice_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def log_mail_result(invoice_id: int, *, to_email: str, cc_email: str | None, bcc_email: str | None, subject: str, body: str, attachment_filename: str, status: str, error_message: str | None = None, sent_at=None) -> None:
    db = get_db()
    cur = db.cursor()
    now = now_jst()
    try:
        cur.execute(
            """
            INSERT INTO invoice_mail_logs (
                invoice_id, to_email, cc_email, bcc_email, subject, body,
                attachment_filename, sent_at, status, error_message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (invoice_id, to_email, cc_email, bcc_email, subject, body, attachment_filename, sent_at, status, error_message, now),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def mark_invoice_mailed(invoice_id: int) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE invoice_headers SET mailed_at=%s, status='mailed', updated_at=%s WHERE id=%s",
            (now, now, invoice_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def save_invoice_payout_token(invoice_id: int, token_id: int | None) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE invoice_headers
            SET payout_access_token_id=%s, updated_at=%s
            WHERE id=%s
            """,
            (token_id, now, invoice_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def _square_env() -> str:
    db = None
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key` = 'square_env_payment'")
        row = cur.fetchone()
        if row:
            value = row[0] if isinstance(row, tuple) else row.get("value")
            if value:
                return str(value).upper()
    except Exception:
        pass
    finally:
        if db:
            db.close()
    return os.environ.get("SQUARE_ENV", "SANDBOX").upper()


def _square_env_value(name: str) -> str | None:
    suffix = "SANDBOX" if _square_env() == "SANDBOX" else "PRODUCTION"
    return os.environ.get(f"SQUARE_{suffix}_{name}") or os.environ.get(f"SQUARE_{name}")


def get_invoice_square_config() -> dict[str, Any]:
    env = _square_env()
    return {
        "env": env,
        "application_id": _square_env_value("APPLICATION_ID"),
        "location_id": _square_env_value("LOCATION_ID"),
        "access_token": _square_env_value("ACCESS_TOKEN"),
        "webhook_signature_key": _square_env_value("INVOICE_WEBHOOK_SIGNATURE_KEY") or _square_env_value("WEBHOOK_SIGNATURE_KEY"),
        "api_base": "https://connect.squareupsandbox.com" if env == "SANDBOX" else "https://connect.squareup.com",
        "js_url": "https://sandbox.web.squarecdn.com/v1/square.js" if env == "SANDBOX" else "https://web.squarecdn.com/v1/square.js",
    }


def _invoice_public_base_url() -> str:
    return os.environ.get("MFU_PUBLIC_BASE_URL", "https://mfu.iori0624.jp").rstrip("/")


def ensure_invoice_card_payment_token(invoice_id: int) -> str:
    db = get_db()
    cur = db.cursor(dictionary=True)
    now = now_jst()
    try:
        cur.execute(
            "SELECT card_payment_public_token, status FROM invoice_headers WHERE id=%s AND deleted_at IS NULL LIMIT 1",
            (invoice_id,),
        )
        row = cur.fetchone()
        if not row:
            raise InvoiceValidationError("請求書が見つかりません。")
        if (row.get("status") or "").strip().lower() in {"paid", "cancelled", "canceled"}:
            raise InvoiceValidationError("現在のステータスではカード決済URLを発行できません。")
        token = (row.get("card_payment_public_token") or "").strip()
        if token:
            return token
        token = str(uuid.uuid4())
        cur.execute(
            """
            UPDATE invoice_headers
            SET card_payment_public_token=%s, updated_at=%s
            WHERE id=%s
            """,
            (token, now, invoice_id),
        )
        db.commit()
        return token
    finally:
        cur.close()
        db.close()


def build_invoice_card_payment_url(token: str) -> str:
    return f"{_invoice_public_base_url()}/invoice/pay/{token}"


def get_invoice_by_card_payment_token(token: str) -> dict[str, Any] | None:
    token = _normalize_stripped_text(token)
    if not token:
        return None
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM invoice_headers WHERE card_payment_public_token=%s AND deleted_at IS NULL LIMIT 1",
            (token,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def get_latest_invoice_card_payment(invoice_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT *
            FROM invoice_card_payments
            WHERE invoice_id=%s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (invoice_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def create_invoice_card_payment_pending(
    invoice: dict[str, Any],
    *,
    buyer_name: str | None = None,
    wallet_type: str | None = None,
) -> dict[str, Any]:
    now = now_jst()
    payment_token = str(uuid.uuid4())
    idempotency_key = str(uuid.uuid4())
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO invoice_card_payments (
                invoice_id, created_at, updated_at, payment_token,
                amount_yen_snapshot, currency_code, buyer_email, buyer_name, wallet_type,
                idempotency_key, square_status
            ) VALUES (%s, %s, %s, %s, %s, 'JPY', %s, %s, %s, %s, 'PENDING')
            """,
            (
                int(invoice.get("id")),
                now,
                now,
                payment_token,
                int(invoice.get("total_yen") or 0),
                (invoice.get("contact_email_snapshot") or "").strip() or None,
                _normalize_stripped_text(buyer_name) or None,
                wallet_type if wallet_type in {"APPLE_PAY", "GOOGLE_PAY"} else None,
                idempotency_key,
            ),
        )
        payment_id = int(cur.lastrowid)
        db.commit()
        return {"id": payment_id, "payment_token": payment_token, "idempotency_key": idempotency_key}
    finally:
        cur.close()
        db.close()


def update_invoice_card_payment_result(payment_row_id: int, *, status: str, square_payment_id: str | None = None, receipt_url: str | None = None, card_brand: str | None = None, card_last4: str | None = None, card_exp_mm: int | None = None, card_exp_yyyy: int | None = None, error_code: str | None = None, error_detail: str | None = None, paid_at=None, square_updated_at=None, sync_error: str | None = None) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE invoice_card_payments
            SET updated_at=%s,
                square_payment_id=COALESCE(%s, square_payment_id),
                square_status=%s,
                square_receipt_url=COALESCE(%s, square_receipt_url),
                card_brand=COALESCE(%s, card_brand),
                card_last4=COALESCE(%s, card_last4),
                card_exp_mm=COALESCE(%s, card_exp_mm),
                card_exp_yyyy=COALESCE(%s, card_exp_yyyy),
                error_code=%s,
                error_detail=%s,
                paid_at=COALESCE(%s, paid_at),
                square_updated_at=COALESCE(%s, square_updated_at),
                last_synced_at=%s,
                sync_attempts=sync_attempts+1,
                sync_error=%s
            WHERE id=%s
            """,
            (
                now,
                square_payment_id,
                status,
                receipt_url,
                card_brand,
                card_last4,
                card_exp_mm,
                card_exp_yyyy,
                error_code,
                error_detail,
                paid_at,
                square_updated_at,
                now,
                sync_error,
                payment_row_id,
            ),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def get_invoice_card_payment_by_square_payment_id(square_payment_id: str) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_card_payments WHERE square_payment_id=%s LIMIT 1", (square_payment_id,))
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def get_invoice_card_payment_by_id(payment_row_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_card_payments WHERE id=%s LIMIT 1", (payment_row_id,))
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def get_invoice_discord_webhook_url() -> str | None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT webhook_url
            FROM users
            WHERE username='admin'
              AND webhook_url IS NOT NULL
              AND webhook_url <> ''
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        legacy = row[0] if isinstance(row, tuple) else row.get("webhook_url")
        from app.discord_notifications.repository import get_discord_webhook
        return get_discord_webhook("invoice_payment", legacy) or None
    except Exception:
        logging.exception("failed to load invoice discord webhook url")
        return None
    finally:
        cur.close()
        db.close()


def send_invoice_payment_discord_embed(*, webhook_url: str, fields: list[tuple[str, str, bool]]) -> None:
    import requests

    try:
        embeds = [{
            "title": "💳 請求書の決済が承認されました",
            "description": "請求書のお支払いが承認/確定しました。",
            "color": 0x2ECC71,
            "fields": [{"name": n, "value": v, "inline": i} for (n, v, i) in fields],
        }]
        requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
    except Exception:
        logging.exception("invoice discord notify failed")


def notify_invoice_card_payment_if_needed(payment_row_id: int) -> None:
    payment = get_invoice_card_payment_by_id(payment_row_id)
    if not payment:
        return
    if int(payment.get("discord_notified") or 0) == 1:
        return
    status = (payment.get("square_status") or "").upper()
    if status != "COMPLETED":
        return

    invoice_id = int(payment.get("invoice_id") or 0)
    invoice = get_invoice(invoice_id)
    if not invoice:
        return
    webhook_url = get_invoice_discord_webhook_url()
    if not webhook_url:
        return

    partner_name = (
        _normalize_stripped_text(invoice.get("contact_name_snapshot"))
        or _normalize_stripped_text(invoice.get("contact_person_snapshot"))
        or "(不明)"
    )
    fields = [
        ("請求書番号", _normalize_stripped_text(invoice.get("invoice_no")) or "-", False),
        ("件名", _normalize_stripped_text(invoice.get("subject")) or "-", False),
        ("相手名", partner_name, True),
        ("決済金額", f"¥{int(payment.get('amount_yen_snapshot') or 0):,}", True),
        ("レシートURL", _normalize_stripped_text(payment.get("square_receipt_url")) or "-", False),
        ("管理画面URL", f"{_invoice_public_base_url()}/invoice/{invoice_id}", False),
    ]
    send_invoice_payment_discord_embed(webhook_url=webhook_url, fields=fields)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE invoice_card_payments SET discord_notified=1, updated_at=%s WHERE id=%s",
            (now_jst(), payment_row_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def ensure_invoice_square_customer(*, access_token: str, invoice: dict[str, Any], buyer_name: str) -> str | None:
    from app.payment.square_gateway import request_square

    buyer_email = (invoice.get("contact_email_snapshot") or "").strip()
    if not buyer_email:
        return None
    reference_id = f"invoice_contact:{int(invoice.get('id') or 0)}"
    square = get_invoice_square_config()
    try:
        sresp = request_square(
            "POST",
            f"{square['api_base']}/v2/customers/search",
            access_token=access_token,
            json_body={"query": {"filter": {"reference_id": {"exact": reference_id}}}},
            timeout=15,
            retry_safe=True,
        )
        if sresp.status_code < 400:
            customers = (sresp.json() or {}).get("customers") or []
            if customers:
                customer = customers[0] or {}
                customer_id = customer.get("id")
                if customer_id:
                    update_payload: dict[str, str] = {}
                    if buyer_email and not (customer.get("email_address") or "").strip():
                        update_payload["email_address"] = buyer_email
                    if buyer_name and not (customer.get("given_name") or "").strip():
                        update_payload["given_name"] = buyer_name
                    if update_payload:
                        uresp = request_square(
                            "PUT",
                            f"{square['api_base']}/v2/customers/{customer_id}",
                            access_token=access_token,
                            json_body=update_payload,
                            timeout=15,
                            retry_safe=True,
                        )
                        if uresp.status_code >= 400:
                            logging.warning("invoice customer update failed: %s", uresp.text)
                    return customer_id
    except Exception:
        logging.exception("invoice search_customers failed")

    try:
        customer_idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"square-customer:{reference_id}"))
        customer_body = {
            "idempotency_key": customer_idempotency_key,
            "given_name": buyer_name,
            "reference_id": reference_id,
            "email_address": buyer_email,
        }
        cresp = request_square(
            "POST",
            f"{square['api_base']}/v2/customers",
            access_token=access_token,
            json_body=customer_body,
            timeout=15,
            idempotency_key=customer_idempotency_key,
        )
        if cresp.status_code >= 400:
            return None
        return ((cresp.json() or {}).get("customer") or {}).get("id")
    except Exception:
        logging.exception("invoice create_customer failed")
        return None


def mark_invoice_paid_by_card(invoice_id: int, paid_at=None) -> None:
    paid_time = paid_at or now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE invoice_headers
            SET status='paid', card_paid_at=COALESCE(card_paid_at, %s), updated_at=%s
            WHERE id=%s
              AND status <> 'cancelled'
            """,
            (paid_time, paid_time, invoice_id),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def log_csv_export(invoice_id: int, filename: str, *, status: str, error_message: str | None = None) -> None:
    now = now_jst()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO invoice_csv_logs (
                invoice_id, filename, exported_at, status, error_message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (invoice_id, filename, now, status, error_message, now),
        )
        if status == "success":
            cur.execute(
                "UPDATE invoice_headers SET freee_exported_at=%s, updated_at=%s WHERE id=%s",
                (now, now, invoice_id),
            )
        db.commit()
    finally:
        cur.close()
        db.close()


def build_invoice_form_data(invoice: dict[str, Any] | None = None) -> dict[str, Any]:
    if not invoice:
        today = now_jst().date()
        return {
            "issue_date": format_ymd(today),
            "due_date": format_ymd(today),
            "tax_mode": "external",
            "status": "draft",
            "bank_info_mode": BANK_INFO_MODE_INLINE,
            "card_payment_enabled": "0",
            "issuer_template_id": "",
            "issuer_email": "",
            "items": [
                {
                    "row_type": ROW_TYPE_NORMAL,
                    "item_name": "",
                    "memo_text": "",
                    "quantity": "1.00",
                    "unit_name": "式",
                    "unit_price_yen": "0",
                    "line_total_yen": "0",
                    "tax_category": DEFAULT_TAX_CATEGORY,
                }
            ],
        }
    data = dict(invoice)
    data["issue_date"] = format_ymd(invoice.get("issue_date"))
    data["due_date"] = format_ymd(invoice.get("due_date"))
    if not data.get("items"):
        data["items"] = []
    for item in data["items"]:
        item["row_type"] = normalize_row_type(item.get("row_type"))
        item["memo_text"] = (item.get("memo_text") or "").strip()
        item["quantity"] = str(item.get("quantity") or "1.00")
        item["unit_price_yen"] = str(item.get("unit_price_yen") or 0)
        item["line_total_yen"] = str(item.get("line_total_yen") or 0)
        item["tax_category"] = normalize_tax_category(item.get("tax_category"))
    data["issuer_template_id"] = str(data.get("issuer_template_id") or "")
    data["bank_info_mode"] = normalize_bank_info_mode(data.get("bank_info_mode"))
    data["card_payment_enabled"] = "1" if int(data.get("card_payment_enabled") or 0) == 1 else "0"
    return data
