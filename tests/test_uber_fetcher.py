from datetime import datetime

from app.records.uber_browser import _access_restriction_reason
from app.records.uber_fetcher import _without_mirrored_quest_rows


def _row(event_type: str, feed_id: str, minute: int, amount: int = 150) -> dict:
    return {
        "detail_url": f"https://drivers.uber.com/earnings/activities/detail?eventType={event_type}&activityFeedUUID={feed_id}",
        "occurred_at": datetime(2026, 9, 4, 20, minute),
        "list_amount_yen": amount,
    }


def test_mirrored_misc_quest_is_removed_one_to_one():
    rows = [
        _row("MISC", "misc-a", 8),
        _row("QUEST", "quest-a", 8),
        _row("MISC", "misc-b", 27),
        _row("QUEST", "quest-b", 26),
    ]
    assert [row["detail_url"] for row in _without_mirrored_quest_rows(rows)] == [
        rows[1]["detail_url"],
        rows[3]["detail_url"],
    ]


def test_distinct_misc_quest_is_preserved():
    rows = [_row("MISC", "misc-a", 1), _row("QUEST", "quest-a", 10)]
    assert _without_mirrored_quest_rows(rows) == rows


def test_uber_access_restriction_detection():
    assert "HTTP 429" in _access_restriction_reason("https://drivers.uber.com", 429, "")
    assert "ロボット確認" in _access_restriction_reason(
        "https://drivers.uber.com/challenge", 200, "Verify you are human"
    )
    assert _access_restriction_reason("https://drivers.uber.com", 200, "通常の明細") is None
