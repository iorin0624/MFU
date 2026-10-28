#!/usr/bin/env python3
"""Publish authenticated au PAY transaction mail as focused notification cards."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from au_pay_otp_mail import _message_text, post_event


EXPECTED_SENDER = "info@wallet.auone.jp"
SUBJECT_RE = re.compile(r"^【au PAY】(ご利用のお知らせ|チャージ完了のお知らせ|出金完了のお知らせ|返金のお知らせ)\s*\[メールコード([PCS]\d+)\]$")


def _authenticated(message) -> bool:
    trusted_results = [
        str(value).lower()
        for value in message.get_all("Authentication-Results", [])
        if str(value).split(";", 1)[0].strip().lower() == "mail.iori0624.jp"
    ]
    results = "\n".join(trusted_results)
    return all(value in results for value in (
        "spf=pass", "dkim=pass", "dmarc=pass", "header.d=wallet.auone.jp",
        "header.from=wallet.auone.jp",
    ))


def _label_value(text: str, *labels: str) -> str:
    for label in labels:
        pattern = rf"(?m)^\s*[・■]?\s*{re.escape(label)}\s*(?:[:：]\s*([^\r\n]+)|\s*\n\s*([^\r\n]+))"
        match = re.search(pattern, text)
        if match:
            return (match.group(1) or match.group(2) or "").strip()
    return ""


def parse_au_pay_transaction(path: Path) -> dict | None:
    with path.open("rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)
    sender = parseaddr(str(message.get("From") or ""))[1].strip().lower()
    subject = str(message.get("Subject") or "").strip()
    match = SUBJECT_RE.fullmatch(subject)
    if sender != EXPECTED_SENDER or not match or not _authenticated(message):
        return None

    notice_type, code = match.groups()
    text = _message_text(message)
    is_charge = notice_type == "チャージ完了のお知らせ" and code.startswith("C")
    is_usage = notice_type == "ご利用のお知らせ" and code.startswith("P")
    is_withdrawal = notice_type == "出金完了のお知らせ" and code == "S3101"
    is_refund = notice_type == "返金のお知らせ" and code == "P1009"
    if not (is_charge or is_usage or is_withdrawal or is_refund):
        return None

    if is_withdrawal:
        detail = _label_value(text, "詳細")
        destination_match = re.fullmatch(r"出金[\s\u3000]+(.+)", detail)
        destination = _label_value(text, "出金先") or (destination_match.group(1).strip() if destination_match else "")
        title = "💸 au PAY 出金完了"
        labels = (
            ("日時", _label_value(text, "出金日時")),
            ("出金先", destination),
            ("手数料", _label_value(text, "出金手数料")),
            ("金額", _label_value(text, "出金金額")),
            ("残高", _label_value(text, "au PAY 残高")),
        )
        color = 0x9B59B6
    elif is_refund:
        amount = _label_value(text, "返金額").removesuffix("※").strip()
        title = "↩️ au PAY 返金完了"
        labels = (
            ("利用日時", _label_value(text, "利用日時")),
            ("店舗名", _label_value(text, "利用店舗")),
            ("返金額", amount),
            ("残高", _label_value(text, "決済後残高")),
        )
        color = 0xF39C12
    elif is_charge:
        title = "💰 au PAY チャージ完了"
        labels = (
            ("日時", _label_value(text, "チャージ日時")),
            ("方法", _label_value(text, "チャージ方法")),
            ("金額", _label_value(text, "チャージ金額")),
            ("残高", _label_value(text, "au PAY 残高", "決済後残高")),
        )
        color = 0x2ECC71
    else:
        transaction_type = _label_value(text, "種別")
        cancellation = "取消" in transaction_type or "au PAYの返金が完了" in text
        title = "↩️ au PAY 取消・返金" if cancellation else "💳 au PAY 利用速報"
        balance = _label_value(text, "au PAY 残高", "決済後残高")
        if cancellation and not balance:
            balance = "記載なし"
        labels = (
            ("利用日時", _label_value(text, "ご利用日時", "利用日時")),
            ("店舗名", _label_value(text, "ご利用店舗", "利用店舗")),
            ("利用金額", _label_value(text, "ご利用金額", "支払金額", "支払い金額", "支払い取消金額")),
            ("残高", balance),
        )
        color = 0xF39C12 if cancellation else 0x3498DB
    if not all(value for _, value in labels):
        raise ValueError(f"required au PAY {code} fields are missing")

    message_id = str(message.get("Message-ID") or "").strip()
    identity = message_id or f"{sender}|{subject}|{labels!r}"
    dedup_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "kind": "au_pay_transaction",
        "topic_key": "au-pay-usage",
        "embeds": [{
            "title": title,
            "color": color,
            "fields": [
                {"name": label, "value": value, "inline": False}
                for label, value in labels
            ],
        }],
        "dedup_key": f"mail:au-pay-transaction:{dedup_hash}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eml", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--print-event", action="store_true")
    args = parser.parse_args()
    try:
        event = parse_au_pay_transaction(args.eml)
        if event is None:
            return 2
        if args.print_event:
            print(json.dumps(event, ensure_ascii=False, indent=2))
            return 0
        post_event(args.endpoint, event)
        print(f"au_pay_transaction delivered dedup_key={event['dedup_key']}")
        return 0
    except Exception as exc:
        print(f"au_pay_transaction error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
