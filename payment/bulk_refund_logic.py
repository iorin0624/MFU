# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib
import hmac
import json
from datetime import datetime


def normalize_preview_rows(rows: list[dict]) -> list[dict]:
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


def build_preview_hash(*, secret: str, payment_event_id: int, payment_event_uuid: str, external_event_id: int | None, rows: list[dict]) -> str:
    payload = {
        "payment_event_id": int(payment_event_id),
        "payment_event_uuid": payment_event_uuid or "",
        "external_event_id": int(external_event_id) if external_event_id else None,
        "rows": normalize_preview_rows(rows),
    }
    msg = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def decide_bulk_refund_status(*, has_member: bool, member_event_match: bool, square_status: str, override_fee: int, diff: int, remaining: int) -> tuple[str, str]:
    if not has_member or not member_event_match:
        return "excluded", "missing_identity"
    if (square_status or "").upper() not in ("AUTHORIZED", "APPROVED", "COMPLETED"):
        return "excluded", "non_success_status"
    if int(override_fee or 0) > 0:
        return "manual", "member_fee_override_present"
    if int(diff or 0) <= 0:
        return "excluded", "diff_non_positive"
    if int(diff or 0) > int(remaining or 0):
        return "excluded", "diff_exceeds_remaining"
    return "eligible", "eligible"


def format_wareki_like_date(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日"


def build_refund_note(*, dt: datetime, refund_yen: int) -> str:
    return f"{format_wareki_like_date(dt)}に{int(refund_yen)}円差額返金済"


def append_note_if_missing(original: str | None, note: str) -> str:
    base = (original or "").strip()
    if note in base:
        return base
    if not base:
        return note
    return f"{base}\n{note}"


def recalculate_paid_amount(*, original_paid: int, refunded_total: int) -> int:
    return max(int(original_paid or 0) - int(refunded_total or 0), 0)
