from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - 起動時に依存未導入でもアプリ全体を落とさない
    BeautifulSoup = None  # type: ignore

from .models import CARRIER_MASTER


def _text(el) -> str:
    if not el:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _first_by_keywords(soup: BeautifulSoup, keywords: list[str]):
    for text_node in soup.find_all(string=True):
        src = str(text_node)
        if all(k in src for k in keywords):
            return text_node.parent
    return None


def _find_value_near_label(soup: BeautifulSoup, labels: list[str]) -> str | None:
    for label in labels:
        node = soup.find(string=re.compile(re.escape(label)))
        if not node:
            continue
        parent = node.parent
        if parent:
            next_td = parent.find_next("td")
            if next_td:
                v = _text(next_td)
                if v:
                    return v
            for sib in parent.next_siblings:
                if getattr(sib, "get_text", None):
                    v = _text(sib)
                    if v and v != label:
                        return v
    return None


def _parse_jp_datetime(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    patterns = [
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d",
        "%m/%d %H:%M",
        "%m/%d",
    ]
    for pattern in patterns:
        try:
            dt = datetime.strptime(raw, pattern)
            if pattern.startswith("%m"):
                dt = dt.replace(year=datetime.now().year)
            if "%H" in pattern:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _base_payload(carrier_code: str, tracking_number: str, tracking_url: str) -> dict[str, Any]:
    return {
        "carrier_code": carrier_code,
        "carrier_name": CARRIER_MASTER[carrier_code],
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "service_name": None,
        "current_status": None,
        "current_status_detail": None,
        "latest_event_at": None,
        "scheduled_delivery_date": None,
        "scheduled_delivery_time_slot": None,
        "package_count": None,
        "size": None,
        "additional_service": None,
        "ship_date": None,
        "completed": False,
        "origin_office": {
            "name": None,
            "phone": None,
            "fax": None,
            "office_code": None,
            "postal_code": None,
            "prefecture": None,
        },
        "delivery_office": {
            "name": None,
            "phone": None,
            "fax": None,
            "office_code": None,
            "postal_code": None,
            "prefecture": None,
        },
        "contact_offices": [],
        "history": [],
    }


def parse_sagawa(html: str, tracking_number: str, tracking_url: str) -> dict[str, Any]:
    if BeautifulSoup is None:
        raise RuntimeError("bs4 が未インストールです。python3-bs4 または beautifulsoup4 を導入してください。")
    soup = BeautifulSoup(html, "html.parser")
    payload = _base_payload("sagawa", tracking_number, tracking_url)

    payload["current_status"] = _find_value_near_label(soup, ["現在の状況", "お問い合わせ送り状No.", "配達状況"])  # type: ignore
    payload["current_status_detail"] = _find_value_near_label(soup, ["詳細", "担当営業所"])
    payload["scheduled_delivery_date"] = _parse_jp_datetime(_find_value_near_label(soup, ["お届け予定日", "配達指定日"]))
    payload["ship_date"] = _parse_jp_datetime(_find_value_near_label(soup, ["出荷日", "発送日"]))
    payload["package_count"] = _find_value_near_label(soup, ["個数"])

    payload["origin_office"]["name"] = _find_value_near_label(soup, ["出荷営業所", "荷送人"])
    payload["origin_office"]["phone"] = _find_value_near_label(soup, ["出荷営業所電話番号", "荷送人電話番号"])
    payload["origin_office"]["fax"] = _find_value_near_label(soup, ["出荷営業所FAX", "荷送人FAX"])
    payload["delivery_office"]["name"] = _find_value_near_label(soup, ["配達営業所", "お届け先"])
    payload["delivery_office"]["phone"] = _find_value_near_label(soup, ["配達営業所電話番号", "お届け先電話番号"])
    payload["delivery_office"]["fax"] = _find_value_near_label(soup, ["配達営業所FAX", "お届け先FAX"])

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        values = [_text(td) for td in tds]
        status = values[1] if len(values) > 1 else None
        occurred = _parse_jp_datetime(values[0])
        office = values[2] if len(values) > 2 else None
        if not status:
            continue
        payload["history"].append(
            {
                "status": status,
                "occurred_at": occurred,
                "office_name": office,
                "office_code": None,
                "postal_code": None,
                "prefecture": None,
                "detail": values[3] if len(values) > 3 else None,
            }
        )

    payload["history"] = [h for h in payload["history"] if h.get("status")]
    if payload["history"]:
        payload["latest_event_at"] = payload["history"][-1].get("occurred_at")
        payload["current_status"] = payload["current_status"] or payload["history"][-1].get("status")

    current_status = payload.get("current_status") or ""
    payload["completed"] = "配達完了" in current_status
    return payload


def parse_yamato(html: str, tracking_number: str, tracking_url: str) -> dict[str, Any]:
    if BeautifulSoup is None:
        raise RuntimeError("bs4 が未インストールです。python3-bs4 または beautifulsoup4 を導入してください。")
    soup = BeautifulSoup(html, "html.parser")
    payload = _base_payload("yamato", tracking_number, tracking_url)

    summary_values: dict[str, str] = {}
    for li in soup.select(".tracking-invoice-block-summary li"):
        text = _text(li)
        if not text:
            continue
        label = None
        value = None
        match = re.match(r"^\s*([^:：]+)\s*[：:]\s*(.+)$", text)
        if match:
            label = match.group(1).strip()
            value = match.group(2).strip()
        else:
            parts = [p.strip() for p in li.stripped_strings if p.strip()]
            if len(parts) >= 2:
                label = parts[0].rstrip("：:")
                value = " ".join(parts[1:]).strip()
        if label and value:
            summary_values[label] = value

    payload["service_name"] = summary_values.get("商品名")
    payload["current_status"] = _text(soup.select_one(".tracking-invoice-block-state-title")) or None

    state_summary = _text(soup.select_one(".tracking-invoice-block-state-summary"))
    state_note = _text(soup.select_one(".tracking-invoice-block-state-note"))
    detail_parts = [part for part in [state_summary, state_note] if part]
    payload["current_status_detail"] = "\n".join(detail_parts) if detail_parts else None

    scheduled_raw = summary_values.get("お届け予定日時")
    if scheduled_raw:
        match = re.search(
            r"(\d{1,2}/\d{1,2})\s*[　 ]*([0-9]{1,2}:[0-9]{2}\s*-\s*[0-9]{1,2}:[0-9]{2}|午前中|14時-16時|16時-18時|18時-20時|19時-21時)",
            scheduled_raw,
        )
        if match:
            payload["scheduled_delivery_date"] = _parse_jp_datetime(match.group(1))
            payload["scheduled_delivery_time_slot"] = re.sub(r"\s+", "", match.group(2))
        else:
            payload["scheduled_delivery_date"] = _parse_jp_datetime(scheduled_raw)

    for item in soup.select(".tracking-invoice-block-detail ol li"):
        status = _text(item.select_one(".item")) or None
        occurred = _parse_jp_datetime(_text(item.select_one(".date")))
        office_name = _text(item.select_one(".name")) or None
        office_code = None
        link = item.select_one(".name a[href]")
        if link:
            href = link.get("href", "")
            qs = parse_qs(urlparse(href).query)
            jc = qs.get("JC")
            if jc and jc[0]:
                office_code = jc[0]
        if not status:
            continue
        payload["history"].append(
            {
                "status": status,
                "occurred_at": occurred,
                "office_name": office_name,
                "office_code": office_code,
                "postal_code": None,
                "prefecture": None,
                "detail": None,
            }
        )

    payload["history"] = [h for h in payload["history"] if h.get("status")]
    if payload["history"]:
        payload["latest_event_at"] = payload["history"][-1].get("occurred_at")
        payload["current_status"] = payload["current_status"] or payload["history"][-1].get("status")

    payload["completed"] = (payload.get("current_status") or "") == "配達完了"
    return payload


def parse_japanpost(html: str, tracking_number: str, tracking_url: str) -> dict[str, Any]:
    if BeautifulSoup is None:
        raise RuntimeError("bs4 が未インストールです。python3-bs4 または beautifulsoup4 を導入してください。")
    soup = BeautifulSoup(html, "html.parser")
    payload = _base_payload("japanpost", tracking_number, tracking_url)

    payload["service_name"] = _find_value_near_label(soup, ["商品種別", "取扱商品"]) or "ゆうパック"
    payload["scheduled_delivery_date"] = _parse_jp_datetime(_find_value_near_label(soup, ["お届け予定日", "配達予定日"]))
    payload["scheduled_delivery_time_slot"] = _find_value_near_label(soup, ["お届け予定時間帯", "配達予定時間帯"])
    payload["size"] = _find_value_near_label(soup, ["サイズ"])
    payload["delivery_office"]["name"] = _find_value_near_label(soup, ["配達予定局", "配達郵便局"])

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        values = [_text(td) for td in tds]
        occurred = _parse_jp_datetime(values[0])
        status = values[1] if len(values) > 1 else None
        office_name = values[2] if len(values) > 2 else None
        postal = None
        prefecture = None
        if office_name:
            postal_match = re.search(r"〒?\s*(\d{3}-?\d{4})", office_name)
            if postal_match:
                postal = postal_match.group(1).replace("-", "")
            pref_match = re.search(r"(北海道|東京都|(?:京都|大阪)府|.{2,3}県)", office_name)
            if pref_match:
                prefecture = pref_match.group(1)
        if not status:
            continue
        payload["history"].append(
            {
                "status": status,
                "occurred_at": occurred,
                "office_name": office_name,
                "office_code": None,
                "postal_code": postal,
                "prefecture": prefecture,
                "detail": values[3] if len(values) > 3 else None,
            }
        )

    payload["history"] = [h for h in payload["history"] if h.get("status")]
    if payload["history"]:
        payload["current_status"] = payload["history"][-1].get("status")
        payload["latest_event_at"] = payload["history"][-1].get("occurred_at")

    office_section = _first_by_keywords(soup, ["お問合せ先"])
    if office_section:
        text = _text(office_section.parent if office_section.parent else office_section)
        phone_matches = re.findall(r"(\d{2,4}-\d{2,4}-\d{3,4})", text)
        if phone_matches:
            payload["contact_offices"].append(
                {
                    "type": "お問合せ先",
                    "office_name": _text(office_section),
                    "phone": phone_matches[0],
                }
            )

    payload["completed"] = (payload.get("current_status") or "") == "お届け先にお届け済み"
    return payload
