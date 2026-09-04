from datetime import datetime
from decimal import Decimal

from app.records.uber_parser import activity_key, normalize_list_row, parse_detail_text, parse_yen


def test_parse_yen_handles_full_width_currency_and_negative_value():
    assert parse_yen("￥1,234.00") == 1234
    assert parse_yen("− ￥500") == -500


def test_activity_key_uses_stable_uber_identifiers():
    key, kind = activity_key("https://drivers.uber.com/earnings/trips/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert key == "TRIP:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert kind == "delivery"

    key, kind = activity_key(
        "https://drivers.uber.com/earnings/activities/detail?eventType=QUEST&activityFeedUUID=11111111-2222-3333-4444-555555555555&timestamp=1"
    )
    assert key == "ACTIVITY:QUEST:11111111-2222-3333-4444-555555555555"
    assert kind == "quest"


def test_normalize_list_row_parses_english_activity_date():
    row = normalize_list_row(
        {
            "dateText": "Friday, September 4th, 2026",
            "timeText": "20:44",
            "amountText": "￥1,150",
            "url": "https://drivers.uber.com/earnings/trips/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
    )
    assert row["occurred_at"] == datetime(2026, 9, 4, 20, 44)
    assert row["list_amount_yen"] == 1150


def test_delivery_detail_extracts_operational_fields():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/trips/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        occurred_at=datetime(2026, 9, 4, 20, 44),
        list_amount_yen=900,
        detail_text="""
売り上げ
￥1,100
時間
1時間 5分
距離
12.3 km
獲得ポイント
2ポイント
店舗
A店
配達先住所
B市C町1丁目
現金で受け取った金額
￥2,000
Uberへの支払い
− ￥900
チップ
￥100
""",
    )
    assert result["deliveries"] == 2
    assert result["duration_seconds"] == 3900
    assert result["distance_km"] == Decimal("12.3")
    assert result["earnings_yen"] == 1100
    assert result["cash_collected_yen"] == 2000
    assert result["uber_payment_yen"] == -900
    assert result["merchant_name"] == "A店"
    assert result["delivery_address"] == "B市C町1丁目"


def test_quest_amount_is_promotion_and_not_delivery_count():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/activities/detail?eventType=MISC&activityFeedUUID=11111111-2222-3333-4444-555555555555",
        occurred_at=datetime(2026, 9, 4, 20, 44),
        list_amount_yen=750,
        detail_text="クエスト\n売り上げ\n￥750",
    )
    assert result["activity_type"] == "quest"
    assert result["deliveries"] == 0
    assert result["promo_yen"] == 750


def test_current_uber_layout_extracts_unlabelled_merchant_and_address():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/trips/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        occurred_at=datetime(2026, 9, 4, 20, 31),
        list_amount_yen=320,
        detail_text="""Delivery • 2026年9月4日 • 午後8時31分
￥320
このサービスの見積もり料金は ￥320 でした。
時間
13 分 27 秒
距離
3.89 km
A店
B市C町1丁目
1 ポイント を獲得
売り上げ
料金
￥320
売り上げ
￥320""",
    )
    assert result["merchant_name"] == "A店"
    assert result["delivery_address"] == "B市C町1丁目"
    assert result["points"] == 1


def test_current_uber_layout_extracts_inline_cash_and_payout():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/trips/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        occurred_at=datetime(2026, 9, 4, 19, 38),
        list_amount_yen=1150,
        detail_text="""Delivery • Sep 4, 2026 • 7:38 PM
¥1,150
Duration
30 min 28 sec
Distance
9.62 km
A店
B市C町1丁目
2 points earned
¥4,906 cash collected
Your earnings
Fare
¥1,150
Payouts
-¥4,906
Trip balance
-¥3,756""",
    )
    assert result["points"] == 2
    assert result["deliveries"] == 2
    assert result["cash_collected_yen"] == 4906
    assert result["uber_payment_yen"] == -4906


def test_zero_yen_incomplete_delivery_is_kept_as_zero_deliveries():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/trips/7b2228ef-e241-4c3b-94a9-ebee7f7e35f9",
        occurred_at=datetime(2026, 6, 15, 15, 32),
        detail_text="""Delivery • Jun 15, 2026 • 3:32 PM
¥0
Duration
0 sec
Distance
---
A店
B市C町
0 point earned""",
    )
    assert result["activity_type"] == "delivery"
    assert result["earnings_yen"] == 0
    assert result["points"] == 0
    assert result["deliveries"] == 0


def test_misc_adjustment_is_recorded_as_other_income():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/activities/detail?eventType=MISC&activityFeedUUID=559165b2-a95d-43d1-a861-aef39e67e714",
        occurred_at=datetime(2026, 6, 15, 15, 59),
        detail_text="""¥200
Adjustment
Jun. 15, 3:59 PM
DESCRIPTION
Support Adjustment""",
    )
    assert result["activity_type"] == "other"
    assert result["earnings_yen"] == 200
    assert result["promo_yen"] == 0
    assert result["other_yen"] == 200


def test_tip_is_separate_from_delivery_fare():
    result = parse_detail_text(
        detail_url="https://drivers.uber.com/earnings/trips/bd33cc7c-0f1b-4ec4-bb9e-c88b43ef5ba8",
        occurred_at=datetime(2026, 9, 3, 20, 3),
        detail_text="""Delivery • Sep 3, 2026 • 8:03 PM
¥1,181
Duration
35 min 11 sec
Distance
8.90 km
A店
B市C町
3 points earned
¥281 tip included
Your earnings
Fare
¥900
Tip
¥281
Your earnings
¥1,181""",
    )
    assert result["earnings_yen"] == 1181
    assert result["sales_yen"] == 900
    assert result["tip_yen"] == 281
