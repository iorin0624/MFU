from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime


MAX_LABEL_LENGTH = 100


class MonthUpdateValidationError(ValueError):
    def __init__(self, message: str, *, date_value: str = "") -> None:
        super().__init__(message)
        self.date_value = date_value


@dataclass(frozen=True)
class MonthDayUpdate:
    day_date: date
    status: str
    label: str
    is_public: int

    @property
    def date_key(self) -> str:
        return self.day_date.isoformat()

    @property
    def comparable_values(self) -> tuple[str, str, int]:
        return (self.status, self.label, self.is_public)


def parse_month_updates(form, *, year: int, month: int) -> list[MonthDayUpdate]:
    """Validate and normalize one complete calendar month's submitted values."""
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        raise MonthUpdateValidationError("年月が正しくありません。")

    last_day = monthrange(year, month)[1]
    expected_dates = [date(year, month, day).isoformat() for day in range(1, last_day + 1)]
    submitted_dates = [str(value or "").strip() for value in form.getlist("dates")]

    if len(submitted_dates) != len(set(submitted_dates)):
        raise MonthUpdateValidationError("同じ日付が重複しています。")
    if set(submitted_dates) != set(expected_dates):
        raise MonthUpdateValidationError("表示中の月の日付をすべて送信してください。")

    updates: list[MonthDayUpdate] = []
    for date_key in expected_dates:
        try:
            day_date = datetime.strptime(date_key, "%Y-%m-%d").date()
        except ValueError as exc:
            raise MonthUpdateValidationError(
                "日付が正しくありません。", date_value=date_key
            ) from exc

        status = str(form.get(f"status__{date_key}", "") or "").strip()
        if status not in {"free", "busy"}:
            raise MonthUpdateValidationError(
                "空き／予定ありの指定が正しくありません。", date_value=date_key
            )

        label = str(form.get(f"label__{date_key}", "") or "").strip()
        if len(label) > MAX_LABEL_LENGTH:
            raise MonthUpdateValidationError(
                f"コメントは{MAX_LABEL_LENGTH}文字以内で入力してください。",
                date_value=date_key,
            )

        is_public = 1 if str(form.get(f"is_public__{date_key}", "")) == "1" else 0
        updates.append(
            MonthDayUpdate(
                day_date=day_date,
                status=status,
                label=label,
                is_public=is_public,
            )
        )

    return updates


def existing_comparable_values(row: dict | None) -> tuple[str, str, int]:
    """Return the stored values, treating a missing row as the UI defaults."""
    if not row:
        return ("free", "", 1)
    return (
        str(row.get("status") or "free"),
        str(row.get("label") or "").strip(),
        1 if int(row.get("is_public") or 0) == 1 else 0,
    )
