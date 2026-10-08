#!/usr/bin/env python3
"""Parse an au PAY 3-D Secure email and publish a time-sensitive MFU event."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path


EXPECTED_SENDER = "info@mail-jcn.dnp-cdms.jp"
EXPECTED_SUBJECT = "ネットショッピング認証コードのお知らせ"
JST_SUFFIX = "+09:00"


def _message_text(message) -> str:
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        value = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        value = payload.decode(part.get_content_charset() or "utf-8", "replace")
    if (part.get_content_type() or "").lower() == "text/html":
        value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", value)
        value = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", value)
        value = re.sub(r"<[^>]+>", "", value)
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _field(text: str, label: str, pattern: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(label)}\s*[:：]\s*({pattern})\s*$", text)
    return match.group(1).strip() if match else ""


def _authenticated(message) -> bool:
    results = "\n".join(str(value) for value in message.get_all("Authentication-Results", [])).lower()
    return (
        "spf=pass" in results
        and "dkim=pass" in results
        and "dmarc=pass" in results
        and "header.d=mail-jcn.dnp-cdms.jp" in results
    )


def parse_au_pay_otp(path: Path) -> dict | None:
    with path.open("rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)
    sender = parseaddr(str(message.get("From") or ""))[1].strip().lower()
    subject = str(message.get("Subject") or "").strip()
    if sender != EXPECTED_SENDER or subject != EXPECTED_SUBJECT or not _authenticated(message):
        return None

    body = _message_text(message)
    otp = _field(body, "認証コード", r"\d{6,10}")
    merchant = _field(body, "ご利用加盟店名", r".+")
    amount = _field(body, "ご利用金額", r".+")
    used_at = _field(body, "ご利用時刻", r"\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}")
    expires_at = _field(body, "認証コード有効期限", r"\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}")
    if not all((otp, merchant, amount, used_at, expires_at)):
        raise ValueError("required au PAY authentication fields are missing")

    expires_iso = datetime.strptime(expires_at, "%Y/%m/%d %H:%M:%S").isoformat() + JST_SUFFIX
    message_id = str(message.get("Message-ID") or "").strip()
    identity = message_id or f"{sender}|{used_at}|{merchant}|{amount}"
    dedup_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "kind": "au_pay_otp",
        "topic_key": "au-pay-otp",
        "embeds": [{
            "title": "🔐 au PAY ネットショッピング認証",
            "description": "au PAYのワンタイムパスワードを受信しました。",
            "color": 0xF1C40F,
            "fields": [
                {"name": "ご利用加盟店名", "value": merchant, "inline": False},
                {"name": "ご利用金額", "value": amount, "inline": True},
                {"name": "ご利用時刻", "value": used_at, "inline": True},
                {"name": "有効期限", "value": expires_at, "inline": False},
            ],
            "footer": {"text": "au PAY認証"},
        }],
        "actions": [
            {
                "type": "copy",
                "label": "ワンタイムパスワードをコピー",
                "value": otp,
                "expires_at": expires_iso,
            }
        ],
        "dedup_key": f"mail:au-pay-otp:{dedup_hash}",
    }


def post_event(endpoint: str, event: dict) -> None:
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(2):
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "mfu-au-pay-otp/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status in {200, 202, 204}:
                    return
                raise RuntimeError(f"unexpected HTTP status {response.status}")
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
    raise RuntimeError(f"notification delivery failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eml", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--print-event", action="store_true")
    args = parser.parse_args()
    try:
        event = parse_au_pay_otp(args.eml)
        if event is None:
            return 2
        if args.print_event:
            print(json.dumps(event, ensure_ascii=False, indent=2))
            return 0
        post_event(args.endpoint, event)
        print(f"au_pay_otp delivered dedup_key={event['dedup_key']}")
        return 0
    except Exception as exc:
        print(f"au_pay_otp error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
