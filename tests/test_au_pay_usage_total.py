"""au PAY daily usage totals exclude refunds and remain distinct from push text."""

import importlib.util
from datetime import date
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "utils" / "au_pay_usage_total.py"


def _module():
    spec = importlib.util.spec_from_file_location("au_pay_usage_total", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fields(used_at: str, amount: str):
    return [
        {"name": "利用日時", "value": used_at},
        {"name": "店舗名", "value": "テスト店舗"},
        {"name": "利用金額", "value": amount},
        {"name": "残高", "value": "9,000円"},
    ]


def test_parses_japanese_and_slash_dates_with_yen():
    module = _module()
    assert module.parse_usage_fields(_fields("2026年10月28日(水) 12:10:01", "1,615円")) == (date(2026, 10, 28), 1615)
    assert module.parse_usage_fields(_fields("2026/10/28 19:54:04", "¥ 400円")) == (date(2026, 10, 28), 400)


def test_rejects_malformed_or_negative_purchase_values():
    module = _module()
    assert module.parse_usage_fields(_fields("2026/10/28", "-436円")) is None
    assert module.parse_usage_fields(_fields("2026/13/28", "436円")) is None
    assert module.parse_usage_fields(_fields("unknown", "436円")) is None


def test_only_real_usage_gets_a_fifth_field():
    module = _module()
    calls = []
    module.record_usage_and_total = lambda **kwargs: calls.append(kwargs) or 2400
    original = {
        "feature_key": "au_pay_usage", "kind": "au_pay_transaction",
        "title": "💳 au PAY 利用速報", "description": "金額：400円　店舗名：テスト店舗　残高：9,000円",
        "fields": _fields("2026/10/28 19:54:04", "400円"),
        "cards": [{"title": "💳 au PAY 利用速報", "fields": _fields("2026/10/28 19:54:04", "400円")}],
    }
    enriched = module.add_usage_total_to_event(original, recipient="admin", dedup_key="mail:test")
    assert [row["name"] for row in enriched["cards"][0]["fields"]] == [
        "利用日時", "店舗名", "利用金額", "残高", "本日使用合計金額",
    ]
    assert enriched["cards"][0]["fields"][-1]["value"] == "2,400円"
    assert enriched["description"] == original["description"]
    assert len(calls) == 1

    for title in ("↩️ au PAY 取消・返金", "↩️ au PAY 返金完了", "💰 au PAY チャージ完了", "💸 au PAY 出金完了"):
        other = dict(original, title=title)
        assert module.add_usage_total_to_event(other, recipient="admin", dedup_key="other") is other
    test_notice = dict(original, kind="au_pay_transaction_test")
    assert module.add_usage_total_to_event(test_notice, recipient="admin", dedup_key="test") is test_notice
    assert len(calls) == 1
