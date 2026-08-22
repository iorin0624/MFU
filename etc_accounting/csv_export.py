from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Iterable

from .presentation import format_travel_duration


CSV_HEADERS = (
    "入り日時",
    "IC名",
    "出た日時",
    "IC名",
    "走行時間",
    "管轄名",
    "金額",
    "備考",
)


def _safe_csv_text(value: object) -> str:
    """Keep spreadsheet applications from evaluating exported text as formulas."""
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    if text.startswith(("\t", "\r")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _format_datetime(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.strftime("%Y/%m/%d %H:%M")


def _sort_key(record: dict) -> tuple[str, int]:
    value = record.get("entry_at") or record.get("used_at") or record.get("exit_at")
    if isinstance(value, datetime):
        date_key = value.isoformat()
    else:
        date_key = "9999-12-31T23:59:59.999999"
    return date_key, int(record.get("id") or 0)


def build_csv_rows(records: Iterable[dict]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = [CSV_HEADERS]
    for record in sorted(records, key=_sort_key):
        operator_name = ""
        if record.get("tollgate_match_status") == "matched":
            operator_name = str(record.get("tollgate_operator_name") or "").strip()

        amount = record.get("amount")
        if amount is not None:
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                amount = _safe_csv_text(amount)

        rows.append(
            (
                _format_datetime(record.get("entry_at")),
                _safe_csv_text(record.get("entry_ic") or "入口記録なし"),
                _format_datetime(record.get("exit_at")),
                _safe_csv_text(record.get("exit_ic") or "出口記録なし"),
                format_travel_duration(record.get("entry_at"), record.get("exit_at")) or "",
                _safe_csv_text(operator_name or "未特定"),
                "" if amount is None else amount,
                _safe_csv_text(record.get("remarks")),
            )
        )
    return rows


def render_csv(records: Iterable[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows(build_csv_rows(records))
    return output.getvalue().encode("utf-8-sig")
