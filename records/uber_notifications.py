from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.discord_notifications.service import post_discord_notification

from .uber_repository import activity_range_summary


FEATURE_KEY = "uber_continuous_summary"
UBER_RECORDS_URL = "https://mfu.iori0624.jp/records/uber"


def _integer(value: Any) -> int:
    return int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _yen(value: Any) -> str:
    return f"¥{_integer(value):,}"


def _yen_pair(average: Any, median: Any) -> str:
    if average is None and median is None:
        return "- / -"
    left = _yen(average) if average is not None else "-"
    right = _yen(median) if median is not None else "-"
    return f"{left} / {right}"


def _period_label(date_from: date, date_to: date) -> str:
    first = f"{date_from.year}年{date_from.month}月{date_from.day}日"
    if date_from == date_to:
        return first
    return f"{first}～{date_to.year}年{date_to.month}月{date_to.day}日"


def build_uber_summary_payload(
    date_from: date,
    date_to: date,
    *,
    summary: dict[str, Any] | None = None,
    inserted_count: int | None = None,
    test: bool = False,
) -> dict[str, Any]:
    """Build the same four summary sections shown on the Uber Web screen."""
    row = dict(summary if summary is not None else activity_range_summary(date_from, date_to))
    deliveries = int(row.get("deliveries") or 0)
    net_yen = int(row.get("net_yen") or 0)
    promo_yen = int(row.get("promo_yen") or 0)
    other_yen = int(row.get("other_yen") or 0)
    tip_yen = int(row.get("tip_yen") or 0)
    total_yen = net_yen + promo_yen + other_yen + tip_yen
    delivery_quest_yen = net_yen + promo_yen
    duration_seconds = int(row.get("duration_seconds") or 0)
    distance_km = Decimal(str(row.get("distance_km") or 0))
    duration_hours = Decimal(duration_seconds) / Decimal(3600) if duration_seconds else Decimal(0)
    deliveries_per_hour = (
        Decimal(deliveries) * Decimal(3600) / Decimal(duration_seconds)
        if duration_seconds else None
    )
    net_per_delivery = Decimal(net_yen) / Decimal(deliveries) if deliveries else None
    net_per_hour = Decimal(net_yen) * Decimal(3600) / Decimal(duration_seconds) if duration_seconds else None
    net_per_km = Decimal(net_yen) / distance_km if distance_km else None

    prefix = "【テスト】" if test else ""
    title = f"{prefix}Uber売上途中集計"
    description_parts = [_period_label(date_from, date_to), "確定前の金額です。"]
    if inserted_count is not None:
        description_parts.append(f"新規取得：{int(inserted_count)}件")

    return {
        "embeds": [
            {
                "title": title,
                "url": UBER_RECORDS_URL,
                "description": "\n".join(description_parts),
                "color": 0x276EF1,
                "fields": [
                    {"name": "配達件数", "value": f"{deliveries:,}件", "inline": True},
                    {"name": "売上合計", "value": _yen(total_yen), "inline": True},
                    {"name": "配達＋クエスト合計", "value": _yen(delivery_quest_yen), "inline": True},
                    {"name": "配達のみ", "value": _yen(net_yen), "inline": True},
                    {"name": "クエスト", "value": _yen(promo_yen), "inline": True},
                    {"name": "その他・調整金", "value": _yen(other_yen), "inline": True},
                    {"name": "チップ", "value": _yen(tip_yen), "inline": True},
                    {"name": "現金徴収", "value": _yen(row.get("cash_collected_yen")), "inline": True},
                ],
            },
            {
                "title": "稼働状況",
                "color": 0x3498DB,
                "fields": [
                    {"name": "稼働時間", "value": f"{duration_hours:.1f}時間", "inline": True},
                    {"name": "距離", "value": f"{distance_km:.1f}km", "inline": True},
                    {"name": "1時間当たりの件数", "value": f"{deliveries_per_hour:.1f}件" if deliveries_per_hour is not None else "-", "inline": True},
                ],
            },
            {
                "title": "売上合計（平均値 / 中央値）",
                "color": 0x2ECC71,
                "fields": [
                    {"name": "1件当たり", "value": _yen_pair(row.get("total_per_delivery_average"), row.get("total_per_delivery_median")), "inline": True},
                    {"name": "1時間当たり", "value": _yen_pair(row.get("total_per_hour_average"), row.get("total_per_hour_median")), "inline": True},
                    {"name": "1km当たり", "value": _yen_pair(row.get("total_per_km_average"), row.get("total_per_km_median")), "inline": True},
                ],
            },
            {
                "title": "配達のみ（平均値 / 中央値）",
                "color": 0x95A5A6,
                "fields": [
                    {"name": "1件当たり", "value": _yen_pair(net_per_delivery, row.get("net_per_delivery_median")), "inline": True},
                    {"name": "1時間当たり", "value": _yen_pair(net_per_hour, row.get("net_per_hour_median")), "inline": True},
                    {"name": "1km当たり", "value": _yen_pair(net_per_km, row.get("net_per_km_median")), "inline": True},
                ],
            },
        ],
        "allowed_mentions": {"parse": []},
    }


def send_uber_summary_notification(
    date_from: date,
    date_to: date,
    *,
    inserted_count: int | None = None,
    test: bool = False,
) -> bool:
    payload = build_uber_summary_payload(
        date_from,
        date_to,
        inserted_count=inserted_count,
        test=test,
    )
    return post_discord_notification(FEATURE_KEY, payload)
