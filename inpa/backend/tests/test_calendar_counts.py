from datetime import date

from inpa_app.calendar import _park_counts
from inpa_app.holidays import japanese_holidays


def test_park_counts_keep_both_land_sea_and_undecided_separate():
    entries = [
        {"park": "both"},
        {"park": "land"},
        {"park": "sea"},
        {"park": "sea"},
        {"park": "undecided"},
        {},
    ]

    assert _park_counts(entries) == {
        "both": 1,
        "land": 1,
        "sea": 2,
        "undecided": 1,
    }


def test_2026_holidays_include_citizens_and_substitute_holidays():
    holidays = japanese_holidays(2026)

    assert holidays[date(2026, 1, 1)] == "元日"
    assert holidays[date(2026, 5, 6)] == "振替休日"
    assert holidays[date(2026, 9, 22)] == "国民の休日"
    assert holidays[date(2026, 9, 23)] == "秋分の日"


def test_olympic_holiday_exceptions_are_preserved():
    holidays = japanese_holidays(2021)

    assert holidays[date(2021, 7, 22)] == "海の日"
    assert holidays[date(2021, 7, 23)] == "スポーツの日"
    assert holidays[date(2021, 8, 9)] == "振替休日"
