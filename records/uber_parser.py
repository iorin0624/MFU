from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse


_MONEY_RE = re.compile(r"[-−]?\s*[￥¥]\s*[\d,]+(?:\.\d+)?")


def parse_yen(value: str | None) -> int:
    match = _MONEY_RE.search(str(value or ""))
    if not match:
        return 0
    normalized = match.group(0).replace("￥", "").replace("¥", "").replace(",", "").replace(" ", "")
    normalized = normalized.replace("−", "-")
    try:
        return int(Decimal(normalized))
    except Exception:
        return 0


def activity_key(detail_url: str) -> tuple[str, str]:
    parsed = urlparse(detail_url)
    trip_match = re.search(r"/earnings/trips/([0-9a-f-]+)", parsed.path, re.I)
    if trip_match:
        return f"TRIP:{trip_match.group(1).lower()}", "delivery"
    query = parse_qs(parsed.query)
    feed_id = (query.get("activityFeedUUID") or [""])[0]
    event_type = (query.get("eventType") or ["MISC"])[0].upper()
    if not feed_id:
        raise ValueError("Uber明細URLから明細IDを取得できません。")
    return f"ACTIVITY:{event_type}:{feed_id.lower()}", "quest" if event_type in {"QUEST", "MISC"} else "other"


def parse_activity_datetime(date_text: str, time_text: str = "") -> datetime:
    value = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", f"{date_text} {time_text}".strip(), flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    for fmt in (
        "%A, %B %d, %Y %H:%M", "%A, %B %d, %Y", "%a %b %d, %Y %H:%M",
        "%Y/%m/%d %H:%M", "%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Uber明細の日付を解釈できません: {value}")


def _label_value(lines: list[str], labels: tuple[str, ...]) -> str:
    lowered = tuple(label.lower() for label in labels)
    for index, line in enumerate(lines):
        clean = line.strip()
        low = clean.lower().rstrip(":：")
        for label in lowered:
            base_label = label.lower().rstrip(":：")
            if low == base_label:
                return lines[index + 1].strip() if index + 1 < len(lines) else ""
            if low.startswith(base_label):
                raw_remainder = clean[len(label.rstrip(':：')):]
                if not raw_remainder or raw_remainder[0] not in " :：\t":
                    continue
                remainder = raw_remainder.lstrip(" :：")
                if remainder:
                    return remainder
    return ""


def _duration_seconds(text: str) -> int | None:
    hour = re.search(r"(\d+)\s*(?:時間|hours?|hrs?|h)\b", text, re.I)
    minute = re.search(r"(\d+)\s*(?:分|minutes?|mins?|m)\b", text, re.I)
    second = re.search(r"(\d+)\s*(?:秒|seconds?|secs?|s)\b", text, re.I)
    if not any((hour, minute, second)):
        colon = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
        if not colon:
            return None
        if colon.group(3):
            return int(colon.group(1)) * 3600 + int(colon.group(2)) * 60 + int(colon.group(3))
        return int(colon.group(1)) * 60 + int(colon.group(2))
    return int(hour.group(1) if hour else 0) * 3600 + int(minute.group(1) if minute else 0) * 60 + int(second.group(1) if second else 0)


def _distance_km(text: str) -> Decimal | None:
    match = re.search(r"([\d,.]+)\s*km\b", text, re.I)
    if match:
        return Decimal(match.group(1).replace(",", ""))
    match = re.search(r"([\d,.]+)\s*(?:mi|miles?)\b", text, re.I)
    if match:
        return (Decimal(match.group(1).replace(",", "")) * Decimal("1.609344")).quantize(Decimal("0.001"))
    return None


def _points(text: str) -> int:
    match = re.search(r"(\d+)\s*(?:ポイント|points?)\b", text, re.I)
    return int(match.group(1)) if match else 0


def _money_after_labels(lines: list[str], labels: tuple[str, ...]) -> int:
    return parse_yen(_label_value(lines, labels))


def _money_around_labels(lines: list[str], labels: tuple[str, ...]) -> int:
    lowered = tuple(label.lower().rstrip(":：") for label in labels)
    for index, line in enumerate(lines):
        low = line.lower().rstrip(":：")
        if not any(label in low for label in lowered):
            continue
        inline = parse_yen(line)
        if inline:
            return inline
        for neighbor in (index + 1, index - 1):
            if 0 <= neighbor < len(lines):
                amount = parse_yen(lines[neighbor])
                if amount:
                    return amount
    return 0


def _route_values_after_distance(lines: list[str]) -> tuple[str, str]:
    """Uber currently renders merchant/address as unlabeled lines after distance."""
    for index, line in enumerate(lines):
        if line.lower().rstrip(":：") not in {"距離", "distance"}:
            continue
        candidates = []
        for value in lines[index + 2:]:
            if re.search(r"ポイント|points?", value, re.I):
                break
            if value.lower().rstrip(":：") in {"売り上げ", "売上", "earnings", "料金", "fare"}:
                break
            candidates.append(value)
        merchants = candidates[0::2]
        addresses = candidates[1::2]
        return " / ".join(merchants), " / ".join(addresses)
    return "", ""


def parse_detail_text(
    *,
    detail_url: str,
    detail_text: str,
    occurred_at: datetime,
    list_amount_yen: int = 0,
) -> dict:
    key, kind = activity_key(detail_url)
    lines = [re.sub(r"\s+", " ", line).strip() for line in detail_text.splitlines() if line.strip()]
    full_text = "\n".join(lines)
    points = _points(full_text)
    if kind == "delivery" and points <= 0:
        points = 1
    displayed_earnings = _money_after_labels(
        lines,
        ("売り上げ", "売上", "あなたの売り上げ", "your earnings", "earnings"),
    ) or int(list_amount_yen or 0)
    sales = _money_after_labels(lines, ("売上", "配送料", "fare", "sales")) or displayed_earnings
    cash = _money_around_labels(
        lines,
        ("現金で受け取った金額", "現金徴収額", "cash collected", "cash received"),
    )
    uber_payment = _money_around_labels(
        lines,
        ("支払い", "支払", "Uberへの支払い", "payment", "paid to uber", "payouts"),
    )
    tip = _money_after_labels(lines, ("チップ", "tip"))
    merchant = _label_value(lines, ("店舗", "加盟店", "集荷先", "merchant", "pickup"))
    pickup_address = _label_value(lines, ("集荷先住所", "pickup address"))
    delivery_address = _label_value(lines, ("配達先住所", "お届け先", "dropoff", "delivery address"))
    inferred_merchant, inferred_delivery_address = _route_values_after_distance(lines)
    merchant = merchant or inferred_merchant
    delivery_address = delivery_address or inferred_delivery_address
    promo = displayed_earnings if kind == "quest" else 0
    other = displayed_earnings if kind == "other" else 0
    return {
        "activity_key": key,
        "activity_type": kind,
        "occurred_at": occurred_at,
        "work_date": occurred_at.date(),
        "duration_seconds": _duration_seconds(_label_value(lines, ("時間", "所要時間", "duration"))),
        "distance_km": _distance_km(_label_value(lines, ("距離", "distance"))),
        "points": points,
        "deliveries": points if kind == "delivery" else 0,
        "earnings_yen": displayed_earnings,
        "sales_yen": sales,
        "promo_yen": promo,
        "other_yen": other,
        "tip_yen": tip,
        "cash_collected_yen": cash,
        "uber_payment_yen": uber_payment,
        "merchant_name": merchant or None,
        "pickup_address": pickup_address or None,
        "delivery_address": delivery_address or None,
        "detail_url": detail_url,
        "raw_text": detail_text,
    }


def normalize_list_row(row: dict) -> dict:
    occurred_at = parse_activity_datetime(str(row.get("dateText") or ""), str(row.get("timeText") or ""))
    return {
        "detail_url": str(row.get("url") or ""),
        "occurred_at": occurred_at,
        "list_amount_yen": parse_yen(str(row.get("amountText") or "")),
        "list_type": str(row.get("typeText") or ""),
    }
