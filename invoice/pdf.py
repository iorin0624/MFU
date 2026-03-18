from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app, render_template

from .services import mark_invoice_pdf_generated
from .services import normalize_multiline_text
from .utils import (
    ensure_dir,
    format_currency_yen,
    format_jp_date,
    format_quantity,
    internal_pdf_filename,
    visible_pdf_filename,
)

FONT_CANDIDATES = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "IPAexGothic",
    "IPAGothic",
)

FONT_STYLE_CANDIDATES = {
    "regular": ("Regular", "Book", "Normal", "Roman"),
    "bold": ("Bold", "DemiBold", "Medium"),
}


class InvoicePdfFontError(RuntimeError):
    pass


def _require_weasyprint():
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
    except Exception as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("WeasyPrint が利用できません。依存関係をインストールしてください。") from exc
    return HTML, CSS, FontConfiguration


def _ensure_font_file(path_or_pattern: str | None, *, role: str) -> Path:
    value = (path_or_pattern or "").strip()
    if not value:
        raise InvoicePdfFontError(f"請求書PDF用フォント({role}) が設定されていません。")
    path = Path(value).expanduser()
    if path.is_file():
        return path.resolve()
    matched = _fc_match(value)
    if matched:
        return matched
    raise InvoicePdfFontError(
        f"請求書PDF用フォント({role}) が見つかりません: {value}"
    )


def _fc_match(pattern: str) -> Path | None:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}\n", pattern],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    output = (result.stdout or "").strip().splitlines()
    if not output:
        return None
    path = Path(output[0]).expanduser()
    return path.resolve() if path.is_file() else None


def _discover_font_file(*, role: str) -> Path:
    for family in FONT_CANDIDATES:
        for style in FONT_STYLE_CANDIDATES[role]:
            matched = _fc_match(f"{family}:style={style}")
            if matched:
                return matched
    raise InvoicePdfFontError(
        "請求書PDF用の日本語フォントが見つかりません。"
        f" 候補: {', '.join(FONT_CANDIDATES)} / role={role}"
    )


def _resolve_font_paths() -> tuple[Path, Path]:
    regular_cfg = current_app.config.get("INVOICE_PDF_FONT_REGULAR")
    bold_cfg = current_app.config.get("INVOICE_PDF_FONT_BOLD")
    regular_path = (
        _ensure_font_file(regular_cfg, role="regular")
        if regular_cfg
        else _discover_font_file(role="regular")
    )
    bold_path = (
        _ensure_font_file(bold_cfg, role="bold")
        if bold_cfg
        else _discover_font_file(role="bold")
    )
    return regular_path, bold_path


def _build_contact_lines(invoice: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if invoice.get("contact_postal_code_snapshot"):
        lines.append(f"〒{invoice['contact_postal_code_snapshot']}")
    for key in ("contact_address1_snapshot", "contact_address2_snapshot"):
        value = (invoice.get(key) or "").strip()
        if value:
            lines.append(value)

    organization = " ".join(
        part
        for part in [
            (invoice.get("contact_name_snapshot") or "").strip(),
            (invoice.get("contact_department_snapshot") or "").strip(),
        ]
        if part
    )
    if organization:
        lines.append(organization)

    person = " ".join(
        part
        for part in [
            (invoice.get("contact_person_snapshot") or "").strip(),
            (invoice.get("contact_honorific_snapshot") or "").strip(),
        ]
        if part
    )
    if person:
        lines.append(person)

    if invoice.get("contact_email_snapshot"):
        lines.append(f"Email: {invoice['contact_email_snapshot']}")
    if invoice.get("contact_phone_snapshot"):
        lines.append(f"TEL: {invoice['contact_phone_snapshot']}")
    return lines


def _build_issuer_lines(invoice: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ("issuer_name",):
        value = (invoice.get(key) or "").strip()
        if value:
            lines.append(value)
    if invoice.get("issuer_postal_code"):
        lines.append(f"〒{invoice['issuer_postal_code']}")
    for key in ("issuer_address1", "issuer_address2"):
        value = (invoice.get(key) or "").strip()
        if value:
            lines.append(value)
    if invoice.get("issuer_phone"):
        lines.append(f"TEL: {invoice['issuer_phone']}")
    return lines


def _build_item_rows(invoice: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in invoice.get("items", []):
        row_type = item.get("row_type") or "normal"
        if row_type == "memo":
            rows.append(
                {
                    "row_type": "memo",
                    "item_name": (item.get("memo_text") or "").strip(),
                    "quantity": "",
                    "unit_name": "",
                    "unit_price_yen": "",
                    "line_total_yen": "",
                }
            )
            continue
        rows.append(
            {
                "row_type": "normal",
                "item_name": (item.get("item_name") or "").strip(),
                "quantity": format_quantity(item.get("quantity")),
                "unit_name": (item.get("unit_name") or "").strip(),
                "unit_price_yen": format_currency_yen(item.get("unit_price_yen")),
                "line_total_yen": format_currency_yen(item.get("line_total_yen")),
            }
        )
    return rows


def _build_pdf_context(invoice: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "請求書",
        "invoice_no": invoice.get("invoice_no") or "",
        "issue_date_label": format_jp_date(invoice.get("issue_date")),
        "subject": (invoice.get("subject") or "").strip(),
        "contact_lines": _build_contact_lines(invoice),
        "issuer_lines": _build_issuer_lines(invoice),
        "line_items": _build_item_rows(invoice),
        "subtotal_yen_label": format_currency_yen(invoice.get("subtotal_yen")),
        "tax_10_yen_label": format_currency_yen(invoice.get("tax_10_yen")),
        "tax_8_yen_label": format_currency_yen(invoice.get("tax_8_yen")),
        "tax_yen_label": format_currency_yen(invoice.get("tax_yen")),
        "total_yen_label": format_currency_yen(invoice.get("total_yen")),
        "note": normalize_multiline_text(invoice.get("note")) or "",
        "bank_info": normalize_multiline_text(invoice.get("bank_info")) or "",
    }


def _build_font_css(regular_path: Path, bold_path: Path) -> str:
    return f"""
    @font-face {{
      font-family: 'InvoicePdfRegular';
      src: url('{regular_path.as_uri()}');
      font-weight: 400;
      font-style: normal;
    }}
    @font-face {{
      font-family: 'InvoicePdfBold';
      src: url('{bold_path.as_uri()}');
      font-weight: 700;
      font-style: normal;
    }}
    :root {{
      --invoice-font-regular: 'InvoicePdfRegular';
      --invoice-font-bold: 'InvoicePdfBold';
    }}
    """


def _render_invoice_pdf_bytes(invoice: dict[str, Any]) -> bytes:
    HTML, CSS, FontConfiguration = _require_weasyprint()
    pdf_context = _build_pdf_context(invoice)
    html = render_template("invoice_pdf.html", invoice=invoice, pdf=pdf_context)
    regular_path, bold_path = _resolve_font_paths()
    font_config = FontConfiguration()
    base_url = Path(current_app.root_path).resolve().as_uri() + "/"
    stylesheet = CSS(
        string=_build_font_css(regular_path, bold_path),
        base_url=base_url,
        font_config=font_config,
    )
    return HTML(string=html, base_url=base_url).write_pdf(
        stylesheets=[stylesheet],
        font_config=font_config,
    )


def generate_invoice_pdf(invoice: dict) -> tuple[str, str, bytes]:
    pdf_bytes = _render_invoice_pdf_bytes(invoice)
    visible_name = visible_pdf_filename(invoice.get("issuer_name"), invoice.get("issue_date"))
    pdf_dir = ensure_dir(current_app.config.get("INVOICE_PDF_DIR") or "/tmp/mfu/invoice_pdf")
    internal_name = internal_pdf_filename(int(invoice["id"]), datetime.now())
    pdf_path = os.path.join(pdf_dir, internal_name)
    with open(pdf_path, "wb") as fp:
        fp.write(pdf_bytes)
    mark_invoice_pdf_generated(int(invoice["id"]), pdf_path)
    return pdf_path, visible_name, pdf_bytes
