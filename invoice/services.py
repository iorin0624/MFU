from __future__ import annotations

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
    to_decimal,
)


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


def _ensure_invoice_item_column(cur, column_name: str, ddl: str) -> None:
    if not _column_exists(cur, "invoice_items", column_name):
        cur.execute(f"ALTER TABLE invoice_items ADD COLUMN {ddl}")


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
                subtotal_yen INT NOT NULL DEFAULT 0,
                tax_yen INT NOT NULL DEFAULT 0,
                total_yen INT NOT NULL DEFAULT 0,
                tax_mode VARCHAR(16) NOT NULL DEFAULT 'external',
                status VARCHAR(16) NOT NULL DEFAULT 'draft',
                pdf_generated_at DATETIME NULL,
                pdf_storage_path VARCHAR(512) NULL,
                mailed_at DATETIME NULL,
                freee_exported_at DATETIME NULL,
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
                sort_order INT NOT NULL DEFAULT 0,
                is_default TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_invoice_issuer_templates_sort_order (sort_order, id),
                INDEX idx_invoice_issuer_templates_is_default (is_default, id)
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
                freee_partner_name, default_due_days, note, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    snapshot = _snapshot_contact(contact)
    payload = {
        "issue_date": issue,
        "due_date": due,
        "contact_id": contact.get("id"),
        **snapshot,
        "subject": subject,
        "note": (form.get("note") or "").strip() or None,
        "bank_info": (form.get("bank_info") or "").strip() or None,
        "issuer_name": issuer_name,
        "issuer_postal_code": (form.get("issuer_postal_code") or "").strip() or None,
        "issuer_address1": (form.get("issuer_address1") or "").strip() or None,
        "issuer_address2": (form.get("issuer_address2") or "").strip() or None,
        "issuer_phone": (form.get("issuer_phone") or "").strip() or None,
        **totals,
        "tax_mode": tax_mode,
        "status": normalize_status(form.get("status") or "draft"),
    }
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
                    payload["issuer_address1"], payload["issuer_address2"], payload["issuer_phone"],
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
                    issuer_name, issuer_postal_code, issuer_address1, issuer_address2, issuer_phone,
                    subtotal_yen, tax_yen, total_yen, tax_mode, status,
                    pdf_generated_at, pdf_storage_path, mailed_at, freee_exported_at,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
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
                    payload["issuer_name"], payload["issuer_postal_code"], payload["issuer_address1"], payload["issuer_address2"], payload["issuer_phone"],
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
        where: list[str] = []
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


def get_invoice(invoice_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM invoice_headers WHERE id = %s", (invoice_id,))
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
                issuer_name, issuer_postal_code, issuer_address1, issuer_address2, issuer_phone,
                subtotal_yen, tax_yen, total_yen, tax_mode, status,
                pdf_generated_at, pdf_storage_path, mailed_at, freee_exported_at,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
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
                original.get("issuer_name"), original.get("issuer_postal_code"), original.get("issuer_address1"), original.get("issuer_address2"), original.get("issuer_phone"),
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
            "issuer_template_id": "",
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
    return data
