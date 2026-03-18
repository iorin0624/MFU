from __future__ import annotations

import csv
import io
from flask import Response

from .utils import FREEE_TAX_MODE_MAP, format_ymd


FREEE_COLUMNS = [
    "収支区分",
    "管理番号",
    "発生日",
    "決済期日",
    "取引先",
    "勘定科目",
    "税区分",
    "金額",
    "税計算区分",
    "税額",
    "備考",
    "品目",
    "部門",
    "メモタグ",
    "セグメント1",
    "セグメント2",
    "セグメント3",
    "決済日",
    "決済口座",
    "決済金額",
]


def build_invoice_freee_csv(invoice: dict) -> tuple[bytes, str]:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(FREEE_COLUMNS)
    partner_name = invoice.get("freee_partner_name") or invoice.get("contact_name_snapshot") or ""
    subject = invoice.get("subject") or ""
    invoice_no = invoice.get("invoice_no") or ""
    writer.writerow(
        [
            "収入",
            invoice_no,
            format_ymd(invoice.get("issue_date"), slash=True),
            format_ymd(invoice.get("due_date"), slash=True),
            partner_name,
            "売上高",
            "課税売上10%",
            str(int(invoice.get("total_yen") or 0)),
            FREEE_TAX_MODE_MAP.get(invoice.get("tax_mode"), "外税"),
            str(int(invoice.get("tax_yen") or 0)),
            f"{subject} / {invoice_no}".strip(" /"),
            subject,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )
    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()
    return csv_bytes, f"freee_invoice_{invoice_no or 'export'}.csv"


def build_invoice_freee_csv_response(invoice: dict) -> Response:
    csv_bytes, filename = build_invoice_freee_csv(invoice)
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
