"""Japanese national holiday calculation for the calendar UI."""

from __future__ import annotations

from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    day = 1 + (weekday - first.weekday()) % 7 + (occurrence - 1) * 7
    return date(year, month, day)


def _vernal_equinox(year: int) -> int:
    if year < 1980:
        return int(20.8357 + 0.242194 * (year - 1980) - int((year - 1983) / 4))
    return int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _autumnal_equinox(year: int) -> int:
    if year < 1980:
        return int(23.2588 + 0.242194 * (year - 1980) - int((year - 1983) / 4))
    return int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def japanese_holidays(year: int) -> dict[date, str]:
    """Return statutory Japanese holidays for supported calendar years.

    Equinox calculations are defined here for 1948 through 2099. Future legal
    changes can be added without changing the calendar API contract.
    """
    if not 1948 <= year <= 2099:
        return {}
    holidays: dict[date, str] = {}

    def add(month: int, day: int, name: str) -> None:
        holidays[date(year, month, day)] = name

    if year >= 1949:
        add(1, 1, "元日")
        holidays[_nth_weekday(year, 1, 0, 2) if year >= 2000 else date(year, 1, 15)] = "成人の日"
    if year >= 1967:
        add(2, 11, "建国記念の日")
    if year >= 2020:
        add(2, 23, "天皇誕生日")
    if year >= 1949:
        add(3, _vernal_equinox(year), "春分の日")
    if 1949 <= year <= 1988:
        add(4, 29, "天皇誕生日")
    elif 1989 <= year <= 2006:
        add(4, 29, "みどりの日")
    elif year >= 2007:
        add(4, 29, "昭和の日")
    if year >= 1949:
        add(5, 3, "憲法記念日")
        if year >= 2007:
            add(5, 4, "みどりの日")
        add(5, 5, "こどもの日")
    if 1996 <= year <= 2002:
        add(7, 20, "海の日")
    elif year == 2020:
        add(7, 23, "海の日")
    elif year == 2021:
        add(7, 22, "海の日")
    elif year >= 2003:
        holidays[_nth_weekday(year, 7, 0, 3)] = "海の日"
    if year == 2020:
        add(8, 10, "山の日")
    elif year == 2021:
        add(8, 8, "山の日")
    elif year >= 2016:
        add(8, 11, "山の日")
    if 1966 <= year <= 2002:
        add(9, 15, "敬老の日")
    elif year >= 2003:
        holidays[_nth_weekday(year, 9, 0, 3)] = "敬老の日"
    add(9, _autumnal_equinox(year), "秋分の日")
    if 1966 <= year <= 1999:
        add(10, 10, "スポーツの日")
    elif year == 2020:
        add(7, 24, "スポーツの日")
    elif year == 2021:
        add(7, 23, "スポーツの日")
    elif year >= 2000:
        holidays[_nth_weekday(year, 10, 0, 2)] = "スポーツの日"
    add(11, 3, "文化の日")
    add(11, 23, "勤労感謝の日")
    if 1989 <= year <= 2018:
        add(12, 23, "天皇誕生日")

    if year == 1959:
        add(4, 10, "皇太子明仁親王の結婚の儀")
    elif year == 1989:
        add(2, 24, "昭和天皇の大喪の礼")
    elif year == 1990:
        add(11, 12, "即位礼正殿の儀")
    elif year == 1993:
        add(6, 9, "皇太子徳仁親王の結婚の儀")
    elif year == 2019:
        add(4, 30, "国民の休日")
        add(5, 1, "天皇の即位の日")
        add(5, 2, "国民の休日")
        add(10, 22, "即位礼正殿の儀")

    if year >= 1986:
        current = date(year, 1, 2)
        last = date(year, 12, 30)
        while current <= last:
            if (
                current not in holidays
                and current - timedelta(days=1) in holidays
                and current + timedelta(days=1) in holidays
            ):
                holidays[current] = "国民の休日"
            current += timedelta(days=1)

    if year >= 1973:
        for holiday in sorted(holidays):
            if holiday.weekday() != 6:
                continue
            substitute = holiday + timedelta(days=1)
            if year >= 2007:
                while substitute in holidays:
                    substitute += timedelta(days=1)
            if substitute.year == year and substitute not in holidays:
                holidays[substitute] = "振替休日"

    return dict(sorted(holidays.items()))
