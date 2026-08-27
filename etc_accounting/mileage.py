from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime

from .parser import is_provisional_record


_NEXCO_OPERATORS = {
    "東日本高速道路株式会社",
    "中日本高速道路株式会社",
    "西日本高速道路株式会社",
    "宮城県道路公社",
}

_REGISTRATION_OPERATOR = {
    "T9010001095716": "東日本高速道路株式会社",
    "T4180001056169": "中日本高速道路株式会社",
    "T3120001112341": "西日本高速道路株式会社",
    "T5370005001613": "宮城県道路公社",
    "T3140001024527": "本州四国連絡高速道路株式会社",
    "T2180001124464": "愛知道路コンセッション株式会社",
    "T4240005001692": "広島高速道路公社",
    "T4290005003008": "福岡北九州高速道路公社",
}


def _month_key(record: dict) -> str:
    used_at = record.get("used_at")
    if isinstance(used_at, (date, datetime)):
        return used_at.strftime("%Y%m")
    value = str(record.get("statement_month") or "")
    return value if len(value) == 6 and value.isdigit() else "unknown"


def _credit_date(month: str) -> str:
    if len(month) != 6 or not month.isdigit():
        return ""
    year, month_number = int(month[:4]), int(month[4:])
    if month_number == 12:
        year, month_number = year + 1, 1
    else:
        month_number += 1
    return f"{year}年{month_number}月20日予定"


def _operator_name(record: dict) -> str:
    observed = str(record.get("tollgate_operator_name") or "").strip()
    if observed:
        return observed
    registration = str(record.get("invoice_registration_number") or "").strip().upper()
    return _REGISTRATION_OPERATOR.get(registration, "")


def _operator_bucket(record: dict) -> dict | None:
    operator = _operator_name(record)
    if operator in _NEXCO_OPERATORS:
        return {"key": "nexco", "label": "NEXCO3社・宮城県道路公社", "unit": 10}
    if operator == "本州四国連絡高速道路株式会社":
        return {"key": "jb", "label": "本州四国連絡高速道路", "unit": 10}
    if operator == "愛知道路コンセッション株式会社":
        return {"key": "aichi", "label": "愛知道路コンセッション", "unit": 100, "bonus": "aichi"}
    if operator == "広島高速道路公社":
        return {"key": "hiroshima", "label": "広島高速道路", "unit": 100, "bonus": "standard"}
    if operator == "福岡北九州高速道路公社":
        road = str(record.get("tollgate_road_name") or "")
        if "北九州" in road:
            return {"key": "kitakyushu", "label": "北九州高速道路", "unit": 100, "bonus": "standard"}
        if "福岡" in road:
            return {"key": "fukuoka", "label": "福岡高速道路", "unit": 100, "bonus": "standard"}
        return {
            "key": "fukuoka_kitakyushu_unknown",
            "label": "福岡・北九州高速道路（道路未特定）",
            "unit": 100,
            "bonus_unavailable": True,
        }
    return None


def _tier_bonus(amount: int, schedule: str) -> int:
    rates = (0, 4, 8, 12, 18) if schedule == "aichi" else (0, 3, 6, 12, 19)
    boundaries = ((0, 5_000), (5_000, 10_000), (10_000, 20_000), (20_000, 30_000), (30_000, None))
    points = 0
    for (lower, upper), rate in zip(boundaries, rates):
        if not rate or amount <= lower:
            continue
        applicable = amount - lower if upper is None else min(amount, upper) - lower
        points += max(0, applicable) // 100 * rate
    return points


def calculate_monthly_points(records: list[dict]) -> list[dict]:
    months: dict[str, dict] = {}
    grouped: dict[tuple[str, str, str], dict] = {}

    def month_row(month: str) -> dict:
        return months.setdefault(
            month,
            {
                "month": month,
                "label": f"{month[:4]}年{month[4:]}月" if month != "unknown" else "年月不明",
                "credit_date": _credit_date(month),
                "eligible_count": 0,
                "eligible_amount": 0,
                "redemption_amount": 0,
                "base_points": 0,
                "bonus_points": 0,
                "total_points": 0,
                "excluded_count": 0,
                "exclusion_counter": Counter(),
                "groups": [],
            },
        )

    for record in records:
        month = _month_key(record)
        summary = month_row(month)
        vehicle_number = str(record.get("vehicle_number") or "").strip()
        if record.get("source_state") == "deleted":
            reason = "照会サービスから削除"
        elif not vehicle_number:
            reason = "車両番号なし・未取得"
        elif is_provisional_record(record):
            reason = "料金確認中"
        elif "確定" not in str(record.get("remarks") or "").replace(" ", ""):
            reason = "料金未確定"
        elif not str(record.get("card_mask") or "").strip():
            reason = "ETCカード番号未取得"
        elif record.get("postpaid_amount") is None:
            reason = "支払内訳未取得"
        elif int(record.get("postpaid_amount") or 0) <= 0:
            reason = "還元額で全額支払"
        else:
            bucket = _operator_bucket(record)
            reason = "" if bucket else "マイレージ対象外事業者"
        if reason:
            summary["excluded_count"] += 1
            summary["exclusion_counter"][reason] += 1
            continue

        postpaid_amount = int(record.get("postpaid_amount") or 0)
        redemption_amount = int(record.get("redemption_amount") or 0)
        card_mask = str(record.get("card_mask") or "")
        group_key = (month, card_mask, str(bucket["key"]))
        group = grouped.setdefault(
            group_key,
            {
                **bucket,
                "card_mask": card_mask,
                "count": 0,
                "amount": 0,
                "redemption_amount": 0,
                "base_points": 0,
                "bonus_points": 0,
                "total_points": 0,
            },
        )
        base_points = postpaid_amount // int(bucket["unit"])
        group["count"] += 1
        group["amount"] += postpaid_amount
        group["redemption_amount"] += redemption_amount
        group["base_points"] += base_points
        summary["eligible_count"] += 1
        summary["eligible_amount"] += postpaid_amount
        summary["redemption_amount"] += redemption_amount
        summary["base_points"] += base_points

    for (month, _card_mask, _bucket_key), group in grouped.items():
        if group.get("bonus"):
            group["bonus_points"] = _tier_bonus(int(group["amount"]), str(group["bonus"]))
        group["total_points"] = int(group["base_points"]) + int(group["bonus_points"])
        summary = month_row(month)
        summary["bonus_points"] += int(group["bonus_points"])
        summary["groups"].append(group)

    for summary in months.values():
        summary["total_points"] = int(summary["base_points"]) + int(summary["bonus_points"])
        summary["groups"].sort(key=lambda group: (str(group["label"]), str(group["card_mask"])))
        summary["exclusions"] = [
            {"reason": reason, "count": count}
            for reason, count in summary.pop("exclusion_counter").most_common()
        ]
    return sorted(months.values(), key=lambda row: row["month"], reverse=True)
