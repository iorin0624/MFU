"""Pure Square payment/refund state rules shared by runtime and tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


PAYMENT_SUCCESS_STATUSES = frozenset({"COMPLETED"})
PAYMENT_IN_PROGRESS_STATUSES = frozenset({"PENDING", "UNKNOWN", "APPROVED", "AUTHORIZED"})
PAYMENT_FAILURE_STATUSES = frozenset({"FAILED", "CANCELED"})

REFUND_SUCCESS_STATUSES = frozenset({"COMPLETED"})
REFUND_IN_PROGRESS_STATUSES = frozenset({"PENDING", "UNKNOWN", "APPROVED"})
REFUND_FAILURE_STATUSES = frozenset({"FAILED", "REJECTED", "CANCELED"})


def normalize_square_status(value: Any, *, default: str = "UNKNOWN") -> str:
    status = str(value or "").strip().upper()
    return status or default


def is_payment_completed(value: Any) -> bool:
    return normalize_square_status(value) in PAYMENT_SUCCESS_STATUSES


def is_refund_completed(value: Any) -> bool:
    return normalize_square_status(value) in REFUND_SUCCESS_STATUSES


def completed_refund_total(rows: Iterable[dict[str, Any]]) -> int:
    return sum(
        max(0, int(row.get("amount_yen") or 0))
        for row in rows
        if is_refund_completed(row.get("status"))
    )


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def square_datetime(value: Any) -> datetime | None:
    """Return a naive UTC datetime suitable for a MySQL DATETIME column."""

    parsed = _as_utc_datetime(value)
    return parsed.replace(tzinfo=None) if parsed else None


def should_apply_square_update(current_updated_at: Any, incoming_updated_at: Any) -> bool:
    """Reject an older Square object while allowing timestamps we cannot compare."""

    current = _as_utc_datetime(current_updated_at)
    incoming = _as_utc_datetime(incoming_updated_at)
    if not current or not incoming:
        return True
    return incoming >= current
