from datetime import datetime

from app.etc_accounting.mileage import calculate_monthly_points


def _record(**overrides):
    record = {
        "statement_month": "202608",
        "used_at": datetime(2026, 8, 1, 12, 0),
        "amount": 270,
        "vehicle_number": "19",
        "card_mask": "********2159",
        "redemption_amount": 0,
        "postpaid_amount": 270,
        "remarks": "確定",
        "source_state": "present",
        "tollgate_operator_name": "東日本高速道路株式会社",
        "tollgate_road_name": "館山自動車道",
    }
    record.update(overrides)
    return record


def test_monthly_points_use_postpaid_amount_per_transaction():
    rows = calculate_monthly_points([
        _record(amount=15, postpaid_amount=15),
        _record(amount=15, postpaid_amount=15, used_at=datetime(2026, 8, 2, 12, 0)),
    ])
    assert rows[0]["eligible_amount"] == 30
    assert rows[0]["base_points"] == 2
    assert rows[0]["total_points"] == 2


def test_blank_vehicle_provisional_deleted_and_nonparticipant_are_excluded():
    rows = calculate_monthly_points([
        _record(vehicle_number=""),
        _record(remarks="確認中", used_at=datetime(2026, 8, 2, 12, 0)),
        _record(source_state="deleted", used_at=datetime(2026, 8, 3, 12, 0)),
        _record(
            tollgate_operator_name="首都高速道路株式会社",
            used_at=datetime(2026, 8, 4, 12, 0),
        ),
    ])
    month = rows[0]
    assert month["eligible_count"] == 0
    assert month["excluded_count"] == 4
    assert {item["reason"] for item in month["exclusions"]} == {
        "車両番号なし・未取得",
        "料金確認中",
        "照会サービスから削除",
        "マイレージ対象外事業者",
    }


def test_redemption_payment_is_not_counted_for_points():
    month = calculate_monthly_points([
        _record(amount=640, redemption_amount=640, postpaid_amount=0),
    ])[0]
    assert month["eligible_count"] == 0
    assert month["total_points"] == 0
    assert month["exclusions"][0]["reason"] == "還元額で全額支払"


def test_record_without_explicit_final_status_is_not_counted():
    month = calculate_monthly_points([_record(remarks="")])[0]
    assert month["eligible_count"] == 0
    assert month["total_points"] == 0
    assert month["exclusions"][0]["reason"] == "料金未確定"


def test_aichi_monthly_tier_bonus_is_applied_per_card_and_month():
    month = calculate_monthly_points([
        _record(
            amount=10_000,
            postpaid_amount=10_000,
            tollgate_operator_name="愛知道路コンセッション株式会社",
        ),
    ])[0]
    assert month["base_points"] == 100
    assert month["bonus_points"] == 200
    assert month["total_points"] == 300
