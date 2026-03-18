from __future__ import annotations

import os
from datetime import datetime

from flask import current_app

from .services import mark_invoice_pdf_generated
from .utils import ensure_dir, format_jp_date, internal_pdf_filename, visible_pdf_filename

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN_X = 48
MARGIN_Y = 48
LINE_H = 16
FONT_NAME = "F1"
BOLD_FONT_NAME = "F2"


def _pdf_escape_text(text: str) -> str:
    if text is None:
        text = ""
    text = str(text)
    if not text:
        return "<FEFF>"
    hex_text = text.encode("utf-16-be").hex().upper()
    return f"<FEFF{hex_text}>"


def _stream_text(lines: list[tuple[float, float, str, int, str]]) -> bytes:
    chunks = ["BT\n"]
    for x, y, text, size, font_name in lines:
        chunks.append(f"/{font_name} {size} Tf\n")
        chunks.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm\n")
        chunks.append(f"{_pdf_escape_text(text)} Tj\n")
    chunks.append("ET\n")
    return "".join(chunks).encode("latin-1")


def _stream_rects(rects: list[tuple[float, float, float, float]]) -> bytes:
    if not rects:
        return b""
    chunks = ["0.3 w\n"]
    for x, y, w, h in rects:
        chunks.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re S\n")
    return "".join(chunks).encode("latin-1")


def _wrap(text: str | None, width: int) -> list[str]:
    source = (text or "").strip()
    if not source:
        return []
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in source.split("\n"):
        buff = ""
        for ch in raw:
            buff += ch
            if len(buff) >= width:
                lines.append(buff)
                buff = ""
        if buff:
            lines.append(buff)
        elif raw == "":
            lines.append("")
    return lines or [source]


def _build_layout(invoice: dict) -> tuple[list[tuple[float, float, str, int, str]], list[tuple[float, float, float, float]]]:
    lines: list[tuple[float, float, str, int, str]] = []
    rects: list[tuple[float, float, float, float]] = []
    y = PAGE_H - MARGIN_Y

    def add(text: str, *, x=MARGIN_X, size=10, bold=False, step=LINE_H):
        nonlocal y
        lines.append((x, y, text, size, BOLD_FONT_NAME if bold else FONT_NAME))
        y -= step

    add("請求書", size=22, bold=True, step=28)
    add(f"請求日: {format_jp_date(invoice.get('issue_date'))}", x=360)
    add(f"請求書番号: {invoice.get('invoice_no') or ''}", x=360)
    y -= 4

    add("宛先", bold=True)
    for text in [
        f"〒{invoice.get('contact_postal_code_snapshot') or ''}" if invoice.get("contact_postal_code_snapshot") else "",
        invoice.get("contact_address1_snapshot") or "",
        invoice.get("contact_address2_snapshot") or "",
        " ".join([v for v in [invoice.get("contact_name_snapshot"), invoice.get("contact_department_snapshot"), invoice.get("contact_person_snapshot"), invoice.get("contact_honorific_snapshot")] if v]),
        f"TEL: {invoice.get('contact_phone_snapshot')}" if invoice.get("contact_phone_snapshot") else "",
        invoice.get("contact_email_snapshot") or "",
    ]:
        if text:
            add(text, x=MARGIN_X + 10)
    y -= 6

    add("発行者", bold=True)
    for text in [
        invoice.get("issuer_name") or "",
        f"〒{invoice.get('issuer_postal_code') or ''}" if invoice.get("issuer_postal_code") else "",
        invoice.get("issuer_address1") or "",
        invoice.get("issuer_address2") or "",
        f"TEL: {invoice.get('issuer_phone')}" if invoice.get("issuer_phone") else "",
    ]:
        if text:
            add(text, x=360)
    y -= 4

    add(f"件名: {invoice.get('subject') or ''}", bold=True, step=20)
    add(f"ご請求金額: ¥{int(invoice.get('total_yen') or 0):,}", size=16, bold=True, step=24)

    table_top = y
    col_x = [MARGIN_X, 285, 355, 430, 505]
    headers = ["明細", "数量", "単位", "単価", "金額"]
    rects.append((MARGIN_X, table_top - 20, PAGE_W - MARGIN_X * 2, 24))
    for idx, head in enumerate(headers):
        lines.append((col_x[idx] + 4, table_top - 12, head, 10, BOLD_FONT_NAME))
    row_y = table_top - 20
    for item in invoice.get("items", []):
        row_y -= 24
        rects.append((MARGIN_X, row_y, PAGE_W - MARGIN_X * 2, 24))
        lines.append((col_x[0] + 4, row_y + 8, item.get("item_name") or "", 10, FONT_NAME))
        lines.append((col_x[1] + 4, row_y + 8, str(item.get("quantity") or ""), 10, FONT_NAME))
        lines.append((col_x[2] + 4, row_y + 8, item.get("unit_name") or "", 10, FONT_NAME))
        lines.append((col_x[3] + 4, row_y + 8, f"¥{int(item.get('unit_price_yen') or 0):,}", 10, FONT_NAME))
        lines.append((col_x[4] + 4, row_y + 8, f"¥{int(item.get('line_total_yen') or 0):,}", 10, FONT_NAME))
    y = row_y - 24

    summary_x = 360
    add(f"小計: ¥{int(invoice.get('subtotal_yen') or 0):,}", x=summary_x)
    add(f"消費税: ¥{int(invoice.get('tax_yen') or 0):,}", x=summary_x)
    add(f"合計: ¥{int(invoice.get('total_yen') or 0):,}", x=summary_x, bold=True)
    y -= 8

    note_top = y
    note_h = 100
    rects.append((MARGIN_X, note_top - note_h, 240, note_h))
    rects.append((320, note_top - note_h, PAGE_W - 320 - MARGIN_X, note_h))
    lines.append((MARGIN_X + 4, note_top - 12, "備考", 10, BOLD_FONT_NAME))
    note_y = note_top - 28
    for line in _wrap(invoice.get("note"), 22)[:5]:
        lines.append((MARGIN_X + 8, note_y, line, 10, FONT_NAME))
        note_y -= 14
    lines.append((324, note_top - 12, "振込先", 10, BOLD_FONT_NAME))
    bank_y = note_top - 28
    for line in _wrap(invoice.get("bank_info"), 22)[:5]:
        lines.append((328, bank_y, line, 10, FONT_NAME))
        bank_y -= 14
    return lines, rects


def _build_pdf_bytes(invoice: dict) -> bytes:
    lines, rects = _build_layout(invoice)
    text_stream = _stream_text(lines)
    rect_stream = _stream_rects(rects)
    content = rect_stream + text_stream

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] /Resources << /Font << /{FONT_NAME} 5 0 R /{BOLD_FONT_NAME} 6 0 R >> >> /Contents 4 0 R >>".encode("latin-1")
    )
    objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiKakuGo-W5 /Encoding /UniJIS-UCS2-H /DescendantFonts [7 0 R] >>")
    objects.append(b"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiMin-W3 /Encoding /UniJIS-UCS2-H /DescendantFonts [8 0 R] >>")
    objects.append(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiKakuGo-W5 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 5 >> >>")
    objects.append(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiMin-W3 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 5 >> >>")

    buffer = bytearray(b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer.extend(f"{idx} 0 obj\n".encode("latin-1"))
        buffer.extend(obj)
        buffer.extend(b"\nendobj\n")
    xref_pos = len(buffer)
    buffer.extend(f"xref\n0 {len(objects)+1}\n".encode("latin-1"))
    buffer.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    buffer.extend(
        f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode("latin-1")
    )
    return bytes(buffer)


def generate_invoice_pdf(invoice: dict) -> tuple[str, str, bytes]:
    pdf_bytes = _build_pdf_bytes(invoice)
    visible_name = visible_pdf_filename(invoice.get("issuer_name"), invoice.get("issue_date"))
    pdf_dir = ensure_dir(current_app.config.get("INVOICE_PDF_DIR") or "/tmp/mfu/invoice_pdf")
    internal_name = internal_pdf_filename(int(invoice["id"]), datetime.now())
    pdf_path = os.path.join(pdf_dir, internal_name)
    with open(pdf_path, "wb") as fp:
        fp.write(pdf_bytes)
    mark_invoice_pdf_generated(int(invoice["id"]), pdf_path)
    return pdf_path, visible_name, pdf_bytes
