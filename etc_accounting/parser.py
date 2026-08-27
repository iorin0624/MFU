from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from bs4 import BeautifulSoup


class ETCAuthenticationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class ETCStatementPage:
    records: list[dict]
    page_numbers: list[int]
    form_token: str


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _amount(text: str) -> int:
    values = re.findall(r"(?<!\d)(\d[\d,]*)", text or "")
    return int(values[-1].replace(",", "")) if values else 0


def _amount_breakdown(cell) -> tuple[int | None, int | None]:
    """Return ETC mileage redemption and postpaid amounts from the payment cell."""
    values = [
        int(value.replace(",", ""))
        for value in re.findall(r"(?<!\d)(\d[\d,]*)", _text(cell))
    ]
    if not values:
        return None, None
    if len(values) == 1:
        return 0, values[0]
    return values[0], values[-1]


def _line_values(cell) -> list[str]:
    """Extract BR-separated values while preserving an empty vehicle-number line."""
    if cell is None:
        return []
    fragment = BeautifulSoup(str(cell), "html.parser")
    for br in fragment.find_all("br"):
        br.replace_with("\n")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in fragment.get_text().split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _vehicle_card_parts(cell) -> tuple[str, str, str]:
    text = _text(cell)
    card_match = re.search(r"\*+\d{4,}", text)
    card_mask = card_match.group(0) if card_match else ""
    lines = _line_values(cell)
    if len(lines) >= 3:
        return lines[0], lines[1], card_mask

    # Test fixtures and older saved HTML may not retain BR elements.
    prefix = text.replace(card_mask, " ") if card_mask else text
    values = prefix.split()
    vehicle_type = values[0] if values else ""
    vehicle_number = values[1] if len(values) >= 2 else ""
    return vehicle_type, vehicle_number, card_mask


_ROUTE_DATETIME_RE = re.compile(r"(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>\d{2}:\d{2})")


def is_provisional_record(record: dict) -> bool:
    """Return True while ETC still marks the toll as unconfirmed."""
    remarks = re.sub(r"\s+", "", str(record.get("remarks") or ""))
    return "確認中" in remarks


def _parse_route_datetime(match: re.Match) -> datetime:
    return datetime.strptime(
        f"20{match.group('date')} {match.group('time')}",
        "%Y/%m/%d %H:%M",
    )


def _route_parts(cell) -> tuple[str, datetime | None, str, datetime | None, datetime]:
    text = _text(cell)
    matches = list(_ROUTE_DATETIME_RE.finditer(text))
    if not matches:
        raise ValueError(f"利用日時を解析できません: {text}")
    first = matches[0]
    first_at = _parse_route_datetime(first)
    before = text[: first.start()].strip()
    if len(matches) >= 2:
        second = matches[1]
        # ETC一覧は「入口日時 入口IC 出口日時 出口IC」の順で返る。
        entry_ic = " ".join(part for part in (before, text[first.end() : second.start()].strip()) if part)
        exit_at = _parse_route_datetime(second)
        exit_ic = text[second.end() :].strip()
        return entry_ic, first_at, exit_ic, exit_at, first_at

    # 1時刻だけの行は、入口時刻が記録されず「入口IC 出口日時 出口IC」
    # または「出口日時 出口IC」の形で返る。時刻は出口側として保持する。
    return before, None, text[first.end() :].strip(), first_at, first_at


def parse_statement_page(html: str, statement_month: str) -> ETCStatementPage:
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form", attrs={"name": "frm"}) or soup.find("form")
    checkboxes = soup.select('input[name="hakkoMeisai"]')
    if not form or not checkboxes:
        title = _text(soup.title)
        body = _text(soup)[:400]
        raise ETCAuthenticationRequired(f"ETC利用明細を取得できません。再ログインが必要です。{title or body}")

    token_node = form.select_one('input[name="p"]')
    form_token = str(token_node.get("value") or "") if token_node else ""
    records: list[dict] = []
    for checkbox in checkboxes:
        row = checkbox.find_parent("tr")
        cells = row.find_all("td", recursive=False) if row else []
        if len(cells) < 6:
            continue
        try:
            entry_ic, entry_at, exit_ic, exit_at, used_at = _route_parts(cells[1])
        except ValueError:
            continue
        vehicle_type, vehicle_number, card_mask = _vehicle_card_parts(cells[4])
        redemption_amount, postpaid_amount = _amount_breakdown(cells[3])
        records.append(
            {
                "transaction_key": str(checkbox.get("value") or "").strip(),
                "statement_month": statement_month,
                "used_at": used_at,
                "entry_at": entry_at,
                "exit_at": exit_at,
                "entry_ic": entry_ic,
                "exit_ic": exit_ic,
                "amount": _amount(_text(cells[2])),
                "vehicle_type": vehicle_type,
                "vehicle_number": vehicle_number,
                "card_mask": card_mask,
                "redemption_amount": redemption_amount,
                "postpaid_amount": postpaid_amount,
                "remarks": _text(cells[5]),
            }
        )

    page_numbers = {1}
    for node in form.select("[onclick]"):
        match = re.search(r"(?:[?&]pageNo=)(\d+)", str(node.get("onclick") or ""))
        if match:
            page_numbers.add(int(match.group(1)))
    return ETCStatementPage(records=records, page_numbers=sorted(page_numbers), form_token=form_token)
