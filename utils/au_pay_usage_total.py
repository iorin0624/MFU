"""Durable, idempotent daily gross usage totals for au PAY notifications."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any


TOTAL_LABEL = "本日使用合計金額"
USAGE_TITLE = "💳 au PAY 利用速報"
USAGE_KIND = "au_pay_transaction"


def parse_usage_fields(fields: list[dict[str, Any]]) -> tuple[date, int] | None:
    values = {
        str(row.get("name") or "").strip(): str(row.get("value") or "").strip()
        for row in fields
        if isinstance(row, dict)
    }
    used_at = values.get("利用日時", "")
    date_match = re.match(r"^(\d{4})(?:年|/)(\d{1,2})(?:月|/)(\d{1,2})(?:日)?(?:\D|$)", used_at)
    amount_text = values.get("利用金額", "").removesuffix("※").strip()
    amount_match = re.fullmatch(r"(?:[¥￥]\s*)?([0-9][0-9,]*)\s*円", amount_text)
    if not date_match or not amount_match:
        return None
    try:
        used_on = date(*(int(part) for part in date_match.groups()))
        amount_yen = int(amount_match.group(1).replace(",", ""))
    except ValueError:
        return None
    return used_on, amount_yen


def _card_fields(content_json: str) -> list[dict[str, Any]]:
    try:
        content = json.loads(content_json or "{}")
        cards = content.get("cards") or []
        if isinstance(cards, list) and cards and isinstance(cards[0], dict):
            fields = cards[0].get("fields") or []
            return fields if isinstance(fields, list) else []
    except (TypeError, ValueError):
        pass
    return []


def record_usage_and_total(*, recipient: str, dedup_key: str, fields: list[dict[str, Any]]) -> int | None:
    """Backfill retained notices, record this purchase once, and return its day's gross total."""
    parsed = parse_usage_fields(fields)
    if parsed is None:
        return None
    usage_date, amount_yen = parsed
    from app.utils.db import get_db

    db = get_db()
    cur = db.cursor(dictionary=True)
    lock_name = f"mfu_aupay_{hashlib.sha1(recipient.encode('utf-8')).hexdigest()[:12]}_{usage_date:%Y%m%d}"
    locked = False
    try:
        cur.execute("SELECT GET_LOCK(%s, 10) AS acquired", (lock_name,))
        locked = int((cur.fetchone() or {}).get("acquired") or 0) == 1
        if not locked:
            raise TimeoutError("au PAY usage ledger lock timeout")

        # Older notifications are not kept indefinitely. Import retained notices
        # before the first new purchase so the deployment day's total starts at
        # the correct value when those source messages are still available.
        start_utc = datetime.combine(usage_date, time.min) - timedelta(hours=9, days=1)
        end_utc = datetime.combine(usage_date + timedelta(days=2), time.min) - timedelta(hours=9)
        cur.execute(
            """SELECT dedup_key, content_json FROM mfu_notifications
               WHERE user_kind='mfu' AND recipient_key=%s
                 AND feature_key='au_pay_usage' AND kind=%s AND title=%s
                 AND created_at >= %s AND created_at < %s""",
            (recipient, USAGE_KIND, USAGE_TITLE, start_utc, end_utc),
        )
        for row in cur.fetchall() or []:
            previous = parse_usage_fields(_card_fields(row.get("content_json") or ""))
            if previous and previous[0] == usage_date:
                cur.execute(
                    """INSERT IGNORE INTO mfu_au_pay_usage_ledger
                       (recipient_username, dedup_key, usage_date, amount_yen)
                       VALUES (%s, %s, %s, %s)""",
                    (recipient, str(row["dedup_key"])[:191], usage_date, previous[1]),
                )
        cur.execute(
            """INSERT IGNORE INTO mfu_au_pay_usage_ledger
               (recipient_username, dedup_key, usage_date, amount_yen)
               VALUES (%s, %s, %s, %s)""",
            (recipient, dedup_key[:191], usage_date, amount_yen),
        )
        cur.execute(
            """SELECT COALESCE(SUM(amount_yen), 0) AS total_yen
               FROM mfu_au_pay_usage_ledger
               WHERE recipient_username=%s AND usage_date=%s""",
            (recipient, usage_date),
        )
        total_yen = int((cur.fetchone() or {}).get("total_yen") or 0)
        db.commit()
        return total_yen
    except Exception:
        db.rollback()
        raise
    finally:
        if locked:
            try:
                cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                cur.fetchone()
            except Exception:
                pass
        cur.close()
        db.close()


def add_usage_total_to_event(event: dict[str, Any], *, recipient: str, dedup_key: str) -> dict[str, Any]:
    """Enrich only real purchase cards; refunds, charge and tests never affect totals."""
    if (
        event.get("feature_key") != "au_pay_usage"
        or event.get("kind") != USAGE_KIND
        or event.get("title") != USAGE_TITLE
    ):
        return event
    cards = event.get("cards") or []
    if not isinstance(cards, list) or not cards or not isinstance(cards[0], dict):
        return event
    fields = cards[0].get("fields") or []
    if not isinstance(fields, list):
        return event
    total_yen = record_usage_and_total(recipient=recipient, dedup_key=dedup_key, fields=fields)
    if total_yen is None:
        return event
    total_field = {"name": TOTAL_LABEL, "value": f"{total_yen:,}円", "inline": False}
    updated_card = dict(cards[0], fields=[
        row for row in fields if isinstance(row, dict) and row.get("name") != TOTAL_LABEL
    ] + [total_field])
    return dict(event, cards=[updated_card, *cards[1:]], fields=[
        row for row in (event.get("fields") or []) if isinstance(row, dict) and row.get("name") != TOTAL_LABEL
    ] + [total_field])
