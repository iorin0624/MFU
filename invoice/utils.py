from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

JST = timezone(timedelta(hours=9))
TAX_RATE = Decimal("0.10")
TAX_CATEGORY_RATES = {
    "tax10": Decimal("0.10"),
    "tax8": Decimal("0.08"),
    "nontax": Decimal("0"),
}
TAX_CATEGORY_LABELS = {
    "tax10": "課税10%",
    "tax8": "課税8%",
    "nontax": "非課税",
}
DEFAULT_TAX_CATEGORY = "tax10"
ROW_TYPE_NORMAL = "normal"
ROW_TYPE_MEMO = "memo"
ROW_TYPE_VALUES = (ROW_TYPE_NORMAL, ROW_TYPE_MEMO)

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

ISSUER_TEMPLATE_FIELDS = (
    "issuer_name",
    "issuer_postal_code",
    "issuer_address1",
    "issuer_address2",
    "issuer_phone",
    "bank_info",
    "note",
)


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


def format_currency_yen(value: int | Decimal | str | None) -> str:
    amount = int(to_decimal(value))
    return f"¥{amount:,}"


def format_quantity(value: Decimal | int | float | str | None) -> str:
    if value is None or value == "":
        return ""
    dec = quantize_quantity(to_decimal(value))
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


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


def normalize_tax_category(tax_category: str | None) -> str:
    return tax_category if tax_category in TAX_CATEGORY_RATES else DEFAULT_TAX_CATEGORY


def normalize_row_type(row_type: str | None) -> str:
    return row_type if row_type in ROW_TYPE_VALUES else ROW_TYPE_NORMAL


def tax_rate_for_category(tax_category: str | None) -> Decimal:
    return TAX_CATEGORY_RATES[normalize_tax_category(tax_category)]


def calculate_line_amounts(base_amount: int, tax_mode: str, tax_category: str | None) -> dict[str, int]:
    amount_dec = Decimal(base_amount)
    normalized_tax_mode = normalize_tax_mode(tax_mode)
    normalized_tax_category = normalize_tax_category(tax_category)
    tax_rate = tax_rate_for_category(normalized_tax_category)

    if normalized_tax_mode == "external":
        line_subtotal = base_amount
        line_tax = quantize_yen(amount_dec * tax_rate)
        line_total = line_subtotal + line_tax
    elif normalized_tax_mode == "internal":
        line_total = base_amount
        if tax_rate == 0:
            line_tax = 0
        else:
            line_tax = quantize_yen(amount_dec - (amount_dec / (Decimal("1.0") + tax_rate)))
        line_subtotal = line_total - line_tax
    else:
        line_subtotal = base_amount
        line_tax = 0
        line_total = base_amount

    return {
        "tax_category": normalized_tax_category,
        "tax_rate": tax_rate,
        "line_subtotal_yen": line_subtotal,
        "line_tax_yen": line_tax,
        "line_total_yen": line_total,
    }


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
