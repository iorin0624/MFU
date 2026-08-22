from __future__ import annotations

import calendar
import copy
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import current_app, has_app_context

from .repository import get_shop_locations, save_shop_locations


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
CACHE_SECONDS = 5 * 60
SHOP_LOCATION_SUCCESS_CACHE_DAYS = 30
SHOP_LOCATION_ERROR_CACHE_DAYS = 1

_cache_lock = threading.Lock()
_cache_payload: dict | None = None
_cache_saved_at = 0.0


def _logger():
    if has_app_context():
        return current_app.logger
    return logging.getLogger(__name__)


def format_date(str_val):
    if not str_val or str_val == "不明":
        return "不明", "9999-99-99"

    str_val = str_val.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    str_val = re.sub(r"[◇☆迄】まで。]", "", str_val).strip()

    match = re.search(r"(\d{4})[\/\-年](\d{1,2})[\/\-月](\d{1,2})", str_val)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        return f"{year}年{month}月{day}日", f"{year:04d}-{month:02d}-{day:02d}"

    match_end = re.search(r"(\d{4})[\/\-年](\d{1,2})[\/\-月]末", str_val)
    if match_end:
        year = int(match_end.group(1))
        month = int(match_end.group(2))
        day = calendar.monthrange(year, month)[1]
        return f"{year}年{month}月{day}日", f"{year:04d}-{month:02d}-{day:02d}"

    return str_val, "9999-99-99"


def extract_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d]", "", price_str)
    return int(cleaned) if cleaned else None


def _normalize_item(item: dict) -> dict:
    return {
        "title": item.get("商品名") or "",
        "detail_url": item.get("商品詳細リンク") or "",
        "price": item.get("価格"),
        "expiry_display": item.get("有効期限_表示") or "不明",
        "expiry_sort": item.get("有効期限_ソート") or "9999-99-99",
        "shop_name": item.get("店舗名") or "",
        "shop_url": item.get("店舗リンク") or "",
        "shop_area": item.get("店舗所在地") or "",
    }


def _official_shop_details(box, base_url: str) -> tuple[str, str]:
    detail_node = next(
        (
            link
            for link in box.select(".shop a[href]")
            if "spot/detail" in str(link.get("href") or "")
        ),
        None,
    )
    if not detail_node:
        return "", ""
    detail_url = urllib.parse.urljoin(base_url, detail_node.get("href") or "")
    parsed = urllib.parse.urlparse(detail_url)
    shop_code = (urllib.parse.parse_qs(parsed.query).get("code") or [""])[0]
    if parsed.scheme == "http":
        detail_url = urllib.parse.urlunparse(parsed._replace(scheme="https"))
    return str(shop_code or "")[:64], detail_url


def _extract_area_from_shop_page(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")

    # 公式店舗ページのパンくずは「検索トップ > 都道府県 > 市区町村 > 店舗名」。
    # 住所表記より揺れが少ないため、所在地表示にはこちらを優先する。
    for node in soup.select(".widget, [class*=bread], [id*=bread]"):
        parts = [part.strip() for part in node.get_text(" ", strip=True).split(">")]
        parts = [part for part in parts if part]
        if len(parts) >= 4 and parts[0] == "検索トップ":
            area = f"{parts[1]}{parts[2]}"
            if re.match(r"^(?:北海道|東京都|大阪府|京都府|.{2,3}県).+", area):
                return area, ""

    page_text = " ".join(soup.stripped_strings)
    address_match = re.search(
        r"〒\s*\d{3}-?\d{4}\s*((?:北海道|東京都|大阪府|京都府|.{2,3}県)[^\s,、]+)",
        page_text,
    )
    if not address_match:
        return "", ""

    full_address = address_match.group(1).strip()
    area_match = re.match(
        r"((?:北海道|東京都|大阪府|京都府|.{2,3}県)"
        r"(?:[^0-9０-９\s]+?市[^0-9０-９\s]+?区|[^0-9０-９\s]+?郡[^0-9０-９\s]+?[町村]|[^0-9０-９\s]+?[市区町村]))",
        full_address,
    )
    return (area_match.group(1) if area_match else ""), full_address


def _fetch_shop_location(shop: dict) -> dict:
    response = requests.get(shop["detail_url"], headers=HEADERS, timeout=8)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    area, full_address = _extract_area_from_shop_page(response.text)
    if not area:
        raise ValueError("公式店舗ページから所在地を判別できませんでした")
    return {"area": area, "full_address": full_address}


def _cache_is_fresh(row: dict) -> bool:
    checked_at = row.get("checked_at")
    if not isinstance(checked_at, datetime):
        return False
    age_seconds = max(0.0, (datetime.now() - checked_at).total_seconds())
    ttl_days = (
        SHOP_LOCATION_SUCCESS_CACHE_DAYS
        if row.get("fetch_status") == "ok" and row.get("area")
        else SHOP_LOCATION_ERROR_CACHE_DAYS
    )
    return age_seconds < ttl_days * 86400


def _resolve_shop_locations(shops: dict[str, dict]) -> dict[str, str]:
    if not shops:
        return {}
    try:
        cached = get_shop_locations(shops)
    except Exception:
        _logger().warning("店舗所在地キャッシュの読み込みに失敗しました", exc_info=True)
        cached = {}

    areas = {
        code: str(row.get("area") or "")
        for code, row in cached.items()
        if row.get("area")
    }
    refresh_codes = [
        code for code in shops if code not in cached or not _cache_is_fresh(cached[code])
    ]
    if not refresh_codes:
        return areas

    cache_updates = []
    with ThreadPoolExecutor(max_workers=min(4, len(refresh_codes))) as executor:
        futures = {
            executor.submit(_fetch_shop_location, shops[code]): code
            for code in refresh_codes
        }
        for future in as_completed(futures):
            code = futures[future]
            shop = shops[code]
            try:
                location = future.result()
                areas[code] = location["area"]
                cache_updates.append(
                    {
                        "shop_code": code,
                        "shop_name": shop["shop_name"],
                        "detail_url": shop["detail_url"],
                        "full_address": location["full_address"],
                        "area": location["area"],
                        "fetch_status": "ok",
                    }
                )
            except Exception as exc:
                _logger().warning(
                    "店舗所在地の取得に失敗しました shop=%s code=%s",
                    shop.get("shop_name"),
                    code,
                    exc_info=True,
                )
                cache_updates.append(
                    {
                        "shop_code": code,
                        "shop_name": shop["shop_name"],
                        "detail_url": shop["detail_url"],
                        "full_address": str((cached.get(code) or {}).get("full_address") or ""),
                        "area": str((cached.get(code) or {}).get("area") or ""),
                        "fetch_status": "error",
                        "last_error": str(exc),
                    }
                )
    try:
        save_shop_locations(cache_updates)
    except Exception:
        _logger().warning("店舗所在地キャッシュの保存に失敗しました", exc_info=True)
    return areas


def get_kakuyasu_stock(item_id):
    order_url = f"https://www.shopmaker.jp/pro/order.cgi?user=pr006214&number={item_id}&kosuu=1000"
    try:
        response = requests.get(order_url, headers=HEADERS, timeout=10)
        response.encoding = response.apparent_encoding
        text = response.text

        if "売切れです" in text:
            return 0

        match = re.search(r"この商品は(\d+)個までしか注文できません", text)
        if match:
            return int(match.group(1))

        if "お客様情報入力画面へ" in text or "shopping_cart" in text.lower() or "ショッピングカート" in text:
            return "1000+"

        return 0
    except Exception:
        _logger().warning("格安チケットコムの在庫確認に失敗しました", exc_info=True)
        return "不明"


def fetch_daikoku():
    url = "https://ticket.e-daikoku.com/goods/list?_category1Id=4&_category2Id=MC0000006&sort=price+ASC#ticketList"
    base_url = "https://ticket.e-daikoku.com/"
    items = []
    shops = {}

    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    for box in soup.select(".ticket_box"):
        box_link = box.find("a")
        if box_link and box_link.get("href"):
            detail_url = urllib.parse.urljoin(base_url, box_link["href"])
        else:
            title_tag_for_link = box.find("h4")
            link_tag = title_tag_for_link.find("a", class_="box_link") if title_tag_for_link else None
            detail_url = urllib.parse.urljoin(base_url, link_tag["href"]) if link_tag and link_tag.get("href") else ""

        title_tag = box.find("h4")
        title = title_tag.get_text(strip=True) if title_tag else "不明"
        price_node = box.select_one(".price ul li")
        shop_node = box.select_one(".shop ul li")
        price_text = price_node.get_text(strip=True) if price_node else ""
        shop = shop_node.get_text(strip=True) if shop_node else ""
        shop_code, official_shop_url = _official_shop_details(box, base_url)

        if price_text == "問合せください":
            continue

        date_span = box.select_one(".detail dl dd span")
        raw_expiry = date_span.get_text(strip=True) if date_span else "不明"
        details = box.select(".detail dl dd")
        if details:
            detail_text = details[-1].get_text(strip=True)
            expiry_match = re.search(r"有効期限[：:]\s*(\d{4}年\d{1,2}月(\d{1,2}日|末))", detail_text)
            if expiry_match:
                raw_expiry = expiry_match.group(1).strip()
            else:
                expiry_match_fallback = re.search(r"有効期限[：:]([^\n]+)", detail_text)
                if expiry_match_fallback:
                    raw_expiry = expiry_match_fallback.group(1).strip()

        price_num = extract_price(price_text)
        if title != "不明" and price_num is not None and shop:
            display_date, sort_date = format_date(raw_expiry)
            search_query = urllib.parse.quote(f"チケット大黒屋 {shop}")
            if shop_code and official_shop_url:
                shops[shop_code] = {
                    "shop_name": shop,
                    "detail_url": official_shop_url,
                }
            items.append(
                {
                    "商品名": title,
                    "商品詳細リンク": detail_url,
                    "価格": price_num,
                    "有効期限_表示": display_date,
                    "有効期限_ソート": sort_date,
                    "店舗名": shop,
                    "店舗リンク": f"https://www.google.com/maps/search/?api=1&query={search_query}",
                    "店舗コード": shop_code,
                }
            )

    shop_areas = _resolve_shop_locations(shops)
    for item in items:
        item["店舗所在地"] = shop_areas.get(item.get("店舗コード") or "", "")
    return [_normalize_item(item) for item in items]


def fetch_kakuyasu():
    url = "https://www.kakuyasu-ticket.com/park/tdr.html"
    items = []

    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")

    for form in soup.find_all("form"):
        num_input = form.find("input", {"name": "number"})
        if not num_input:
            continue

        item_id = num_input.get("value")
        prev = form.previous_sibling
        context_text = ""
        while prev:
            if hasattr(prev, "get_text"):
                context_text = prev.get_text() + " " + context_text
            if "有効期限" in context_text:
                break
            prev = prev.previous_sibling

        clean_text = " ".join((context_text + " " + form.get_text()).split())
        expiry_match = re.search(r"有効期限[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)", clean_text)
        price_match = re.search(r"当店販売価格[：:]\s*1枚\s*([\d,]+)円", clean_text)

        if expiry_match and price_match:
            display_date, sort_date = format_date(expiry_match.group(1))
            price_num = extract_price(price_match.group(1) + "円")
            stock = get_kakuyasu_stock(item_id)

            if stock != 0 and price_num is not None:
                stock_str = stock if isinstance(stock, int) else stock
                order_url = f"https://www.shopmaker.jp/pro/order.cgi?user=pr006214&number={item_id}&kosuu=1"
                items.append(
                    {
                        "商品名": f"東京ディズニーリゾート 大人/1DAYパスポート (在庫数：{stock_str})",
                        "商品詳細リンク": order_url,
                        "価格": price_num,
                        "有効期限_表示": display_date,
                        "有効期限_ソート": sort_date,
                        "店舗名": "格安チケットコム",
                        "店舗リンク": "https://www.kakuyasu-ticket.com/park/tdr.html",
                    }
                )

            time.sleep(0.5)

    return [_normalize_item(item) for item in items]


def _build_response(items, warnings, *, cached=False):
    items = sorted(items, key=lambda item: item.get("price") or 999999999)
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not items:
        warnings = ["チケット情報を取得できませんでした"]
    return {
        "ok": bool(items),
        "cached": cached,
        "fetched_at": fetched_at,
        "count": len(items),
        "items": items,
        "warnings": warnings,
    }


def fetch_disney_ticket_items(force_refresh: bool = False) -> dict:
    global _cache_payload, _cache_saved_at

    now = time.time()
    with _cache_lock:
        if not force_refresh and _cache_payload and now - _cache_saved_at < CACHE_SECONDS:
            cached_payload = copy.deepcopy(_cache_payload)
            cached_payload["cached"] = True
            return cached_payload

    all_items = []
    warnings = []

    try:
        _logger().info("チケット大黒屋からディズニーチケット情報を取得します")
        all_items.extend(fetch_daikoku())
    except Exception:
        _logger().exception("チケット大黒屋の取得に失敗しました")
        warnings.append("チケット大黒屋の取得に失敗しました")

    try:
        _logger().info("格安チケットコムからディズニーチケット情報を取得します")
        all_items.extend(fetch_kakuyasu())
    except Exception:
        _logger().exception("格安チケットコムの取得に失敗しました")
        warnings.append("格安チケットコムの取得に失敗しました")

    payload = _build_response(all_items, warnings, cached=False)
    with _cache_lock:
        _cache_payload = copy.deepcopy(payload)
        _cache_saved_at = time.time()

    return payload
