from __future__ import annotations

from datetime import datetime


def travel_duration_minutes(entry_at: object, exit_at: object) -> int | None:
    if not isinstance(entry_at, datetime) or not isinstance(exit_at, datetime):
        return None
    total_minutes = int((exit_at - entry_at).total_seconds()) // 60
    return total_minutes if total_minutes >= 0 else None


def format_travel_duration(entry_at: object, exit_at: object) -> str | None:
    total_minutes = travel_duration_minutes(entry_at, exit_at)
    if total_minutes is None:
        return None
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    duration = f"{hours}:{minutes:02d}"
    return f"{days}日 {duration}" if days else duration
