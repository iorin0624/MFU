# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, datetime


EVENT_ALBUM_PREFIX = "【イベント】"
FULL_WIDTH_SPACE = "\u3000"


def _event_date_text(starts_at: date | datetime | str | None) -> str:
    if isinstance(starts_at, datetime):
        event_date = starts_at.date()
    elif isinstance(starts_at, date):
        event_date = starts_at
    elif isinstance(starts_at, str):
        value = starts_at.strip()
        if not value:
            return ""
        try:
            event_date = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                event_date = datetime.strptime(value[:10], "%Y-%m-%d").date()
            except ValueError:
                return ""
    else:
        return ""

    return event_date.strftime("%Y年%m月%d日")


def format_event_album_name(*, title: str, starts_at: date | datetime | str | None) -> str:
    """イベント連携アルバムの名称を共通形式に整える。"""
    clean_title = (title or "").strip()
    event_date = _event_date_text(starts_at)
    parts = [EVENT_ALBUM_PREFIX]
    if event_date:
        parts.append(event_date)
    if clean_title:
        parts.append(clean_title)
    return FULL_WIDTH_SPACE.join(parts)
