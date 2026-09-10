from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.records.uber_browser import _access_restriction_reason
from app.records.uber_fetcher import (
    _cached_activity_is_complete,
    _without_mirrored_quest_rows,
    _without_placeholder_quest_rows,
)
from app.records.uber_repository import (
    _is_mirrored_quest,
    _median_rate,
    monthly_delivery_unit_statistics,
    _quest_adjusted_rate_statistics,
    _quest_goal_count,
)


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


def test_placeholder_trip_quest_is_skipped_before_opening_detail():
    placeholder = _row("QUEST", "quest-placeholder", 10, 900)
    placeholder["list_type"] = "{0} Trip Quest"
    payment = _row("MISC", "misc-payment", 20, 900)
    payment["list_type"] = "Quest"
    assert _without_placeholder_quest_rows([placeholder, payment]) == [payment]


def test_japanese_placeholder_trip_quest_is_skipped():
    placeholder = _row("QUEST", "quest-placeholder", 10, 0)
    placeholder["list_type"] = "{0} 回乗車クエスト"
    assert _without_placeholder_quest_rows([placeholder]) == []


def test_uber_access_restriction_detection():
    assert "HTTP 429" in _access_restriction_reason("https://drivers.uber.com", 429, "")
    assert "ロボット確認" in _access_restriction_reason(
        "https://drivers.uber.com/challenge", 200, "Verify you are human"
    )
    assert _access_restriction_reason("https://drivers.uber.com", 200, "通常の明細") is None


def test_incomplete_delivery_cache_is_refetched():
    assert not _cached_activity_is_complete(
        {
            "activity_type": "delivery",
            "raw_text": "Delivery · 2026-06-05 · ¥500",
            "earnings_yen": 500,
            "duration_seconds": None,
            "distance_km": None,
            "merchant_name": None,
            "delivery_address": None,
        }
    )


def test_complete_delivery_cache_is_reused():
    assert _cached_activity_is_complete(
        {
            "activity_type": "delivery",
            "raw_text": "complete detail",
            "earnings_yen": 500,
            "duration_seconds": 900,
            "distance_km": 3.2,
            "merchant_name": "merchant",
            "delivery_address": "destination",
        }
    )


def test_zero_delivery_cache_requires_duration_but_not_route():
    cached = {
        "activity_type": "delivery",
        "raw_text": "cancelled detail",
        "earnings_yen": 0,
        "points": 0,
        "deliveries": 0,
        "duration_seconds": 0,
        "distance_km": None,
        "merchant_name": None,
        "delivery_address": None,
    }
    assert _cached_activity_is_complete(cached)


def test_quest_goal_count_supports_uber_quest_and_misc_text():
    assert _quest_goal_count("Get ¥900 extra by completing 9 trips") == 9
    assert _quest_goal_count("Completed 12/12 trips\nGet ¥900 extra by completing 3 trips") == 12
    assert _quest_goal_count("クエスト: 9 回の乗車 (レベル 1) を達成しました") == 9


def test_mirrored_quest_matches_same_amount_and_goal_outside_two_minutes():
    misc = {
        "occurred_at": datetime(2026, 9, 5, 20, 49),
        "earnings_yen": 900,
        "raw_text": "クエスト: 9 回の乗車を達成しました。",
    }
    quest = {
        "occurred_at": datetime(2026, 9, 5, 20, 38),
        "earnings_yen": 900,
        "raw_text": "Get ¥900 extra by completing 9 trips",
    }
    assert _is_mirrored_quest(misc, quest)


def test_mirrored_staged_quest_matches_delayed_misc_payment():
    misc = {
        "occurred_at": datetime(2026, 9, 10, 19, 11),
        "earnings_yen": 520,
        "raw_text": (
            "クエスト: 9 回の乗車 (レベル 3) を達成しました。"
            "お支払い明細に ￥520 が追加されました。"
        ),
    }
    quest = {
        "occurred_at": datetime(2026, 9, 10, 18, 30),
        "earnings_yen": 520,
        "raw_text": "QUEST COMPLETE\nCompleted 3/3 trips",
    }
    assert _is_mirrored_quest(misc, quest)


def test_unrelated_same_amount_quests_are_not_merged_without_completion_pair():
    misc = {
        "occurred_at": datetime(2026, 9, 10, 19, 11),
        "earnings_yen": 520,
        "raw_text": "その他の調整金 ￥520",
    }
    quest = {
        "occurred_at": datetime(2026, 9, 10, 18, 30),
        "earnings_yen": 520,
        "raw_text": "QUEST COMPLETE\nCompleted 3/3 trips",
    }
    assert not _is_mirrored_quest(misc, quest)


def test_same_amount_is_not_enough_to_remove_distinct_quest():
    misc = {
        "occurred_at": datetime(2026, 9, 5, 20, 49),
        "earnings_yen": 900,
        "raw_text": "クエスト: 8 回の乗車を達成しました。",
    }
    quest = {
        "occurred_at": datetime(2026, 9, 5, 20, 38),
        "earnings_yen": 900,
        "raw_text": "Get ¥900 extra by completing 9 trips",
    }
    assert not _is_mirrored_quest(misc, quest)


def test_explicit_misc_payment_mirrors_completed_quest_with_different_stage_counts():
    misc = {
        "occurred_at": datetime(2026, 9, 5, 20, 49),
        "earnings_yen": 900,
        "raw_text": "クエスト: 12 回の乗車 (レベル4) を達成しました。お支払い明細に ¥900 が追加されました。",
    }
    quest = {
        "occurred_at": datetime(2026, 9, 5, 20, 38),
        "earnings_yen": 900,
        "raw_text": "QUEST COMPLETE\nCompleted 3/3 trips\nGet ¥900 extra by completing 3 trips",
    }
    assert _is_mirrored_quest(misc, quest)


def test_activity_summary_median_uses_each_delivery_rate():
    rows = [
        {"sales_yen": 100, "tip_yen": 0, "deliveries": 1},
        {"sales_yen": 600, "tip_yen": 0, "deliveries": 3},
        {"sales_yen": 900, "tip_yen": 0, "deliveries": 3},
    ]
    assert _median_rate(rows, ("sales_yen", "tip_yen"), "deliveries") == 200


def test_activity_summary_median_uses_each_delivery_duration_and_ignores_zero():
    rows = [
        {"sales_yen": 600, "duration_seconds": 1800},
        {"sales_yen": 900, "duration_seconds": 1800},
        {"sales_yen": 999, "duration_seconds": 0},
    ]

    assert _median_rate(rows, ("sales_yen",), "duration_seconds", 3600) == 1500


def test_sales_total_statistics_allocate_quest_by_delivery_count_before_row_rates():
    rows = [
        {"sales_yen": 1000, "tip_yen": 100, "deliveries": 2, "duration_seconds": 3600, "distance_km": 10},
        {"sales_yen": 600, "tip_yen": 0, "deliveries": 1, "duration_seconds": 1800, "distance_km": 5},
    ]

    result = _quest_adjusted_rate_statistics(rows, 900)

    # Quest allocation is 300 yen per delivery. Row totals become 1700 and 900.
    assert result["total_per_delivery_average"] == Decimal("875")
    assert result["total_per_delivery_median"] == Decimal("875")
    assert result["total_per_hour_average"] == Decimal("1750")
    assert result["total_per_hour_median"] == Decimal("1750")
    assert result["total_per_km_average"] == Decimal("175")
    assert result["total_per_km_median"] == Decimal("175")


def test_sales_total_statistics_exclude_zero_delivery_rows_and_zero_denominators():
    rows = [
        {"sales_yen": 0, "tip_yen": 0, "deliveries": 0, "duration_seconds": 60, "distance_km": 1},
        {"sales_yen": 500, "tip_yen": 50, "deliveries": 1, "duration_seconds": 0, "distance_km": 0},
    ]

    result = _quest_adjusted_rate_statistics(rows, 50, 100)

    assert result["total_per_delivery_average"] == Decimal("700")
    assert result["total_per_delivery_median"] == Decimal("700")
    assert result["total_per_hour_average"] is None
    assert result["total_per_hour_median"] is None
    assert result["total_per_km_average"] is None
    assert result["total_per_km_median"] is None


def test_monthly_statistics_expand_multi_drop_rows_into_individual_deliveries():
    rows = [
        {"sales_yen": 100, "tip_yen": 0, "deliveries": 1},
        {"sales_yen": 600, "tip_yen": 60, "deliveries": 2},
    ]

    result = monthly_delivery_unit_statistics(rows, 300)

    # Expanded net values are 100, 300, 300. Quest is 100 per delivery;
    # the two-drop tip is divided evenly, yielding totals 200, 430, 430.
    assert result["deliveries_sum"] == 3
    assert result["net_sum"] == Decimal("700")
    assert result["total_sum"] == Decimal("1060")
    assert result["net_avg"] == Decimal("700") / Decimal("3")
    assert result["net_median"] == Decimal("300")
    assert result["total_avg"] == Decimal("1060") / Decimal("3")
    assert result["total_median"] == Decimal("430")


def test_monthly_statistics_include_adjustments_and_exclude_zero_delivery_rows():
    rows = [
        {"sales_yen": 500, "tip_yen": 50, "deliveries": 1},
        {"sales_yen": 999, "tip_yen": 999, "deliveries": 0},
    ]

    result = monthly_delivery_unit_statistics(rows, 50, 200)

    assert result["deliveries_sum"] == 1
    assert result["net_sum"] == Decimal("500")
    assert result["total_sum"] == Decimal("800")
    assert result["net_median"] == Decimal("500")
    assert result["total_median"] == Decimal("800")


def test_range_summary_source_uses_expanded_monthly_population_for_delivery_cards():
    source = (Path(__file__).resolve().parents[1] / "records" / "uber_repository.py").read_text(
        encoding="utf-8"
    )

    assert 'row["net_per_delivery_median"] = unit_stats["net_median"]' in source
    assert 'row["total_per_delivery_average"] = unit_stats["total_avg"]' in source
    assert 'row["total_per_delivery_median"] = unit_stats["total_median"]' in source
