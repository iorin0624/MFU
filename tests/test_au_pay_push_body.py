from __future__ import annotations

import importlib.util
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "utils" / "au_pay_push_body.py"


def _module():
    spec = importlib.util.spec_from_file_location("au_pay_push_body", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fields(**values: str) -> list[dict[str, str]]:
    return [{"name": key, "value": value} for key, value in values.items()]


def test_usage_body_contains_amount_store_and_balance():
    build = _module().build_au_pay_transaction_body
    assert build("💳 au PAY 利用速報", _fields(利用金額="1,615円", 店舗名="Steam", 残高="67,321円")) == (
        "金額：1,615円　店舗名：Steam　残高：67,321円"
    )


def test_charge_body_contains_amount_method_and_balance():
    build = _module().build_au_pay_transaction_body
    assert build("💰 au PAY チャージ完了", _fields(金額="13,000円", 方法="セブン銀行ATM", 残高="68,936円")) == (
        "金額：13,000円　方法：セブン銀行ATM　残高：68,936円"
    )


def test_withdrawal_body_contains_amount_destination_and_balance():
    build = _module().build_au_pay_transaction_body
    assert build("💸 au PAY 出金完了", _fields(金額="15,000円", 出金先="auじぶん銀行", 残高="50,127円")) == (
        "金額：15,000円　出金先：auじぶん銀行　残高：50,127円"
    )


def test_refund_body_contains_amount_store_and_balance():
    build = _module().build_au_pay_transaction_body
    assert build("↩️ au PAY 返金完了", _fields(返金額="-436円", 店舗名="テスト店舗", 残高="36,607円")) == (
        "金額：-436円　店舗名：テスト店舗　残高：36,607円"
    )


def test_other_events_and_incomplete_fields_keep_existing_behavior():
    build = _module().build_au_pay_transaction_body
    assert build("↩️ au PAY 取消・返金", _fields(金額="1,000円", 店舗名="店", 残高="500円")) == ""
    assert build("💳 au PAY 利用速報", _fields(利用金額="1,000円", 残高="500円")) == ""


def test_common_dispatcher_uses_summary_only_for_blank_au_pay_description():
    source = (Path(__file__).resolve().parents[1] / "utils" / "notification_service.py").read_text(encoding="utf-8")
    assert 'if feature_key == "au_pay_usage" and not description:' in source
    assert "description = build_au_pay_transaction_body(title, fields)" in source
