from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen


try:
    import requests as _requests
except Exception:
    _requests = None


YAHOO_TRANSIT_BASE_URL = "https://transit.yahoo.co.jp/search/result"
DEFAULT_FROM_PLACE = "五井"
ROUTE_TIME_HOUR = "11"
ROUTE_TIME_M1 = "0"
ROUTE_TIME_M2 = "0"
REQUEST_TIMEOUT_SECONDS = 10
MAX_DESTINATION_LENGTH = 120
MAX_PARKING_FEE = 1_000_000


class FareEstimateError(Exception):
    """交通費概算の取得/計算で利用者に通知するエラー。"""


def build_yahoo_transit_url(from_place: str, to_place: str, target_date: date) -> str:
    params = {
        "from": from_place,
        "to": to_place,
        "fromgid": "",
        "togid": "",
        "flatlon": "",
        "tlatlon": "",
        "via": "",
        "viacode": "",
        "y": f"{target_date.year:04d}",
        "m": f"{target_date.month:02d}",
        "d": f"{target_date.day:02d}",
        "hh": ROUTE_TIME_HOUR,
        "m1": ROUTE_TIME_M1,
        "m2": ROUTE_TIME_M2,
        "type": "4",
        "ticket": "ic",
        "expkind": "1",
        "userpass": "1",
        "ws": "3",
        "s": "0",
        "al": "1",
        "shin": "1",
        "ex": "0",
        "hb": "1",
        "lb": "1",
        "sr": "1",
    }
    return f"{YAHOO_TRANSIT_BASE_URL}?{urlencode(params)}"


def fetch_transit_html(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> str:
    user_agent = "Mozilla/5.0 (compatible; MFU fare-estimate/1.0)"

    if _requests is not None:
        response = _requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    req = Request(url, headers={"User-Agent": user_agent})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return body


def parse_route1_fare(html: str) -> int:
    if not html:
        raise FareEstimateError("empty_html")

    route1_block = _extract_route1_block(html)
    fare = _extract_fare_from_text(route1_block)
    if fare is None:
        fare = _extract_fare_from_text(html)

    if fare is None:
        raise FareEstimateError("fare_not_found")
    return fare


def calculate_total_fare(one_way_fare: int, parking_fee: int) -> dict[str, int]:
    round_trip_fare = one_way_fare * 2
    return {
        "one_way_fare": one_way_fare,
        "round_trip_fare": round_trip_fare,
        "parking_fee": parking_fee,
        "total_fare": round_trip_fare + parking_fee,
    }


def validate_destination(value: str | None) -> str:
    destination = (value or "").strip()
    if not destination:
        raise FareEstimateError("到着地点を入力してください。")
    if len(destination) > MAX_DESTINATION_LENGTH:
        raise FareEstimateError(f"到着地点は{MAX_DESTINATION_LENGTH}文字以内で入力してください。")
    return destination


def validate_target_date(value: str | None) -> date:
    if not value:
        raise FareEstimateError("利用日を正しく入力してください。")

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise FareEstimateError("利用日を正しく入力してください。") from exc


def validate_parking_fee(value: str | None) -> int:
    raw = (value or "").strip()
    if raw == "":
        raise FareEstimateError("駐輪場代は0以上の数値で入力してください。")

    try:
        parking = int(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise FareEstimateError("駐輪場代は0以上の数値で入力してください。") from exc

    if parking < 0 or parking > MAX_PARKING_FEE:
        raise FareEstimateError("駐輪場代は0以上の数値で入力してください。")
    return parking


def _extract_route1_block(html: str) -> str:
    patterns = [
        r'(?is)(<li[^>]+id="route01".*?</li>)',
        r'(?is)(<section[^>]+id="route01".*?</section>)',
        r'(?is)(<div[^>]+id="route01".*?</div>)',
        r'(?is)(<li[^>]+data-route="1".*?</li>)',
        r'(?is)(<li[^>]+data-routeno="1".*?</li>)',
    ]
    for pattern in patterns:
        matched = re.search(pattern, html)
        if matched:
            return matched.group(1)
    return html


def _extract_fare_from_text(text: str) -> int | None:
    cleaned = _normalize_html_text(text)
    patterns = [
        r"IC(?:\s*優先)?[:：]\s*([0-9][0-9,]*)円",
        r"運賃[:：]\s*([0-9][0-9,]*)円",
        r"片道[:：]\s*([0-9][0-9,]*)円",
        r"([0-9][0-9,]*)円",
    ]

    for pattern in patterns:
        matched = re.search(pattern, cleaned)
        if matched:
            return _parse_yen(matched.group(1))

    state_json = _extract_json_from_state(text)
    if state_json is not None:
        fares = re.findall(r'"(?:fare|fareIc|icFare|ic)"\s*:\s*"?([0-9][0-9,]*)"?', state_json)
        for fare in fares:
            parsed = _parse_yen(fare)
            if parsed is not None:
                return parsed

    return None


def _normalize_html_text(text: str) -> str:
    without_tag = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", without_tag)


def _extract_json_from_state(text: str) -> str | None:
    for key in ("__NEXT_DATA__", "__PRELOADED_STATE__"):
        pattern = rf'(?is)<script[^>]*id="{key}"[^>]*>(.*?)</script>'
        matched = re.search(pattern, text)
        if matched:
            return matched.group(1)

    pattern = r'(?is)window\.__INITIAL_STATE__\s*=\s*(\{.*?\});'
    matched = re.search(pattern, text)
    if matched:
        return matched.group(1)
    return None


def _parse_yen(raw: str) -> int | None:
    normalized = raw.replace(",", "")
    if not normalized.isdigit():
        return None
    value = int(normalized)
    return value if value >= 0 else None
