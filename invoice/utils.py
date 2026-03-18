from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

JST = timezone(timedelta(hours=9))
TAX_RATE = Decimal("0.10")

STATUS_LABELS = {
    "draft": "下書き",
    "issued": "発行済み",
    "mailed": "送付済み",
    "paid": "入金済み",
    "cancelled": "取消",
}

STATUS_BADGES = {
    "draft": "bg-secondary",
    "issued": "bg-primary",
    "mailed": "bg-info text-dark",
    "paid": "bg-success",
    "cancelled": "bg-danger",
}

TAX_MODE_LABELS = {
    "external": "外税",
    "internal": "内税",
    "none": "税なし",
}

FREEE_TAX_MODE_MAP = {
    "external": "外税",
    "internal": "内税",
    "none": "対象外",
}


def now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def format_ymd(value: date | datetime | None, *, slash: bool = False) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%Y/%m/%d" if slash else "%Y-%m-%d")


def format_jp_date(value: date | datetime | None) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.year}年{value.month}月{value.day}日"


def sanitize_filename_component(value: str | None) -> str:
    text = (value or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" ._")
    return text or "invoice"


def visible_pdf_filename(issuer_name: str | None, issue_date: date | datetime | None) -> str:
    safe_name = sanitize_filename_component(issuer_name or "invoice")
    if isinstance(issue_date, datetime):
        issue_date = issue_date.date()
    if not issue_date:
        issue_date = now_jst().date()
    return f"{safe_name}_{issue_date.year}年{issue_date.month}月{issue_date.day}日.pdf"


def internal_pdf_filename(invoice_id: int, generated_at: datetime | None = None) -> str:
    ts = (generated_at or now_jst()).strftime("%Y%m%d%H%M%S")
    return f"invoice_{invoice_id}_{ts}.pdf"


def to_decimal(value, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        text = str(value).replace(",", "").strip()
        return Decimal(text or default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def quantize_quantity(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_yen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_tax(subtotal: int, tax_mode: str) -> int:
    subtotal_dec = Decimal(subtotal)
    if tax_mode == "external":
        return quantize_yen(subtotal_dec * TAX_RATE)
    if tax_mode == "internal":
        included = subtotal_dec - (subtotal_dec / (Decimal("1.0") + TAX_RATE))
        return quantize_yen(included)
    return 0


def normalize_status(status: str | None) -> str:
    return status if status in STATUS_LABELS else "draft"


def normalize_tax_mode(tax_mode: str | None) -> str:
    return tax_mode if tax_mode in TAX_MODE_LABELS else "external"


def default_due_date(issue_date: date, due_days: int | None) -> date:
    return issue_date + timedelta(days=int(due_days or 30))


def split_emails(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]


def build_mail_subject(issue_date: date | None) -> str:
    base = issue_date or now_jst().date()
    return f"【請求書送付】{base.year}年{base.month}月分のご請求"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
