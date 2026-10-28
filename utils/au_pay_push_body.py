"""Build a short Web Push body from authenticated au PAY transaction fields."""

from __future__ import annotations

from typing import Any


_BODY_FIELDS = (
    ("au PAY 利用速報", (("金額", "利用金額"), ("店舗名", "店舗名"), ("残高", "残高"))),
    ("au PAY チャージ完了", (("金額", "金額"), ("方法", "方法"), ("残高", "残高"))),
    ("au PAY 出金完了", (("金額", "金額"), ("出金先", "出金先"), ("残高", "残高"))),
    ("au PAY 返金完了", (("金額", "返金額"), ("店舗名", "店舗名"), ("残高", "残高"))),
)


def build_au_pay_transaction_body(title: str, fields: list[dict[str, Any]]) -> str:
    """Return short push text, adding today's total only for ordinary usage."""
    matching = next((mapping for label, mapping in _BODY_FIELDS if label in title), None)
    if matching is None:
        return ""
    values = {
        str(row.get("name") or "").strip(): str(row.get("value") or "").splitlines()[0].strip()
        for row in fields
        if isinstance(row, dict) and str(row.get("value") or "").strip()
    }
    if any(not values.get(source) for _, source in matching):
        return ""
    parts = [f"{label}：{values[source]}" for label, source in matching]
    if "au PAY 利用速報" in title and values.get("本日使用合計金額"):
        parts.append(f"合計金額：{values['本日使用合計金額']}")
    return "　".join(parts)
