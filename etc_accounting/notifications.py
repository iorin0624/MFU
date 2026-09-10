from __future__ import annotations

import logging
import hashlib
from datetime import datetime

import requests

from app.utils.db import get_db
from app.discord_notifications.repository import get_discord_webhook
from app.discord_notifications.repository import record_discord_delivery

from .presentation import format_travel_duration
from .repository import (
    claim_pending_record_notifications,
    finish_record_notifications,
    list_records,
    mark_fetch_alerts_notified,
    update_fetch_alert_states,
)


LOGGER = logging.getLogger(__name__)
DISCORD_CONTENT_LIMIT = 2000
DISCORD_MAX_EMBEDS = 10
ETC_ACCOUNTING_URL = "https://mfu.iori0624.jp/etc-accounting/"
DISCORD_COLOR_SUMMARY = 0xFFFFFF
DISCORD_COLOR_PENDING = 0xF59E0B
DISCORD_COLOR_FINAL = 0x3498DB
DISCORD_COLOR_DELETED = 0xEF4444
DISCORD_COLOR_ERROR = 0xDC2626


def get_admin_discord_webhook() -> str:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT webhook_url FROM users
            WHERE username='admin' AND webhook_url IS NOT NULL AND webhook_url <> ''
            LIMIT 1
            """
        )
        row = cur.fetchone() or {}
        return get_discord_webhook("etc_accounting", str(row.get("webhook_url") or "").strip())
    finally:
        db.close()


def _notification_kind(record: dict, fallback: str = "new_record") -> str:
    value = str(record.get("notification_kind") or fallback)
    return value if value in {"new_record", "finalized", "source_deleted"} else "new_record"


def _record_embed(record: dict, notification_kind: str = "new_record") -> dict:
    used_at = record.get("used_at")
    if isinstance(used_at, datetime):
        used_at_text = used_at.strftime("%Y/%m/%d %H:%M")
    else:
        used_at_text = str(used_at or "日時不明")

    entry_at = record.get("entry_at")
    exit_at = record.get("exit_at")
    if isinstance(entry_at, datetime) or isinstance(exit_at, datetime):
        entry_at_text = entry_at.strftime("%Y/%m/%d %H:%M") if isinstance(entry_at, datetime) else "未記録"
        exit_at_text = exit_at.strftime("%Y/%m/%d %H:%M") if isinstance(exit_at, datetime) else "未記録"
        time_fields = [{
            "name": "入出日時",
            "value": f"入口 {entry_at_text}\n出口 {exit_at_text}",
            "inline": False,
        }]
        travel_duration = format_travel_duration(entry_at, exit_at)
        if travel_duration:
            time_fields.append({
                "name": "走行時間",
                "value": travel_duration,
                "inline": True,
            })
    else:
        time_fields = [{
            "name": "利用日時",
            "value": used_at_text[:1024],
            "inline": True,
        }]

    entry_ic = str(record.get("entry_ic") or "入口記録なし")
    exit_ic = str(record.get("exit_ic") or "出口記録なし")
    is_pending = "確認中" in str(record.get("remarks") or "")
    kind = _notification_kind(record, notification_kind)
    if kind == "source_deleted":
        status_text = "🔴 明細削除"
        color = DISCORD_COLOR_DELETED
    elif kind == "finalized" or not is_pending:
        status_text = "🔵 料金確定"
        color = DISCORD_COLOR_FINAL
    else:
        status_text = "🟠 料金確認中"
        color = DISCORD_COLOR_PENDING
    embed = {
        "title": f"🚗 {entry_ic} → {exit_ic}"[:256],
        "color": color,
        "fields": [
            *time_fields,
            {"name": "料金", "value": f"**¥{int(record.get('amount') or 0):,}**", "inline": True},
            {"name": "状態", "value": status_text, "inline": True},
        ],
    }
    record_id = int(record.get("record_id") or 0)
    if record_id > 0:
        embed["footer"] = {"text": f"ETC明細 ID: {record_id}"}
    return embed


def _record_order_key(record: dict) -> tuple[bool, str, int]:
    used_at = record.get("used_at")
    record_id = int(record.get("record_id") or record.get("notification_id") or 0)
    return used_at is None, str(used_at or ""), record_id


def _discord_batches(
    records: list[dict],
    notification_kind: str | None = None,
) -> list[tuple[list[dict], dict]]:
    if not records:
        return []

    ordered_records = sorted(records, key=_record_order_key)
    kind = _notification_kind(ordered_records[0], notification_kind or "new_record")
    total = sum(int(record.get("amount") or 0) for record in ordered_records)
    summary_values = {
        "new_record": (
            "🚗 ETCの新しい明細を取得しました",
            f"**{len(ordered_records)}件**の明細を取得しました\n合計 **¥{total:,}**",
            DISCORD_COLOR_SUMMARY,
            "🚗 ETC新規明細",
        ),
        "finalized": (
            "🔵 ETCの料金が確定しました",
            f"**{len(ordered_records)}件**の料金が確定しました\n合計 **¥{total:,}**",
            DISCORD_COLOR_FINAL,
            "🔵 ETC料金確定",
        ),
        "source_deleted": (
            "🔴 ETC明細が削除されました",
            f"ETC利用照会サービスから **{len(ordered_records)}件**の明細が削除されました。\nMFUの「削除された明細」へ移動しました。",
            DISCORD_COLOR_DELETED,
            "🔴 ETC明細削除",
        ),
    }
    summary_title, summary_description, summary_color, continuation_title = summary_values[kind]
    summary = {
        "title": summary_title,
        "url": ETC_ACCOUNTING_URL,
        "description": summary_description,
        "color": summary_color,
        "footer": {"text": "タイトルをクリックするとMFUのETC明細を開きます"},
    }

    record_groups: list[list[dict]] = []
    remaining = list(ordered_records)
    first_capacity = DISCORD_MAX_EMBEDS - 1
    record_groups.append(remaining[:first_capacity])
    remaining = remaining[first_capacity:]
    while remaining:
        record_groups.append(remaining[:DISCORD_MAX_EMBEDS])
        remaining = remaining[DISCORD_MAX_EMBEDS:]

    batches: list[tuple[list[dict], dict]] = []
    page_total = len(record_groups)
    for page_index, group in enumerate(record_groups, start=1):
        embeds = [_record_embed(record, kind) for record in group]
        payload = {
            "embeds": ([summary] if page_index == 1 else []) + embeds,
            "allowed_mentions": {"parse": []},
        }
        if page_index > 1:
            payload["content"] = f"{continuation_title}（続き {page_index}/{page_total}）"
        batches.append((group, payload))
    return batches


def _post_discord(webhook_url: str, payload: dict) -> None:
    if not webhook_url:
        raise RuntimeError("管理者のDiscord Webhookが設定されていません。")
    response = requests.post(
        webhook_url,
        params={"wait": "true"},
        json=payload,
        timeout=10,
    )
    if not response.ok:
        detail = (response.text or "").replace("\n", " ")[:300]
        raise RuntimeError(f"Discord通知に失敗しました（HTTP {response.status_code}: {detail}）")
    record_discord_delivery("etc_accounting", success=True)


def _fetch_failure_error_code(item: dict) -> str:
    explicit = str(item.get("error_code") or "").strip()
    if explicit:
        return explicit[:64]
    error = str(item.get("error") or item.get("message") or "")
    markers = (
        ("PDF", "PDF_DOWNLOAD_FAILED"),
        ("自動ログイン", "AUTHENTICATION_FAILED"),
        ("再ログイン", "AUTHENTICATION_REQUIRED"),
        ("ログイン有効期限", "AUTHENTICATION_REQUIRED"),
        ("遷移情報", "NAVIGATION_STATE_INVALID"),
        ("送信フォーム", "NAVIGATION_STATE_INVALID"),
        ("メンテナンス", "OFFICIAL_MAINTENANCE"),
    )
    return next((code for marker, code in markers if marker in error), "FETCH_FAILED")


def _fetch_failure_alert(item: dict) -> dict:
    statement_month = str(item.get("statement_month") or "")[:6]
    record_key = str(item.get("transaction_key") or item.get("record_id") or "month")
    error_code = _fetch_failure_error_code(item)
    fingerprint = hashlib.sha256(
        f"{statement_month}|{record_key}|{error_code}".encode("utf-8")
    ).hexdigest()
    return {
        **item,
        "statement_month": statement_month,
        "record_key": record_key,
        "error_code": error_code,
        "fingerprint": fingerprint,
    }


def send_fetch_failure_notification(
    failures: list[dict],
    *,
    scheduled: bool,
    checked_months: list[str] | None = None,
) -> dict:
    """Send one consolidated alert when an ETC acquisition run cannot complete."""
    failed = [item for item in failures if str(item.get("status") or "") in {"error", "auth_required"}]
    alerts = [_fetch_failure_alert(item) for item in failed]
    due_alerts = update_fetch_alert_states(
        alerts,
        checked_months=checked_months,
        cooldown_hours=12,
    )
    if not failed:
        return {"status": "empty", "count": 0}

    if not due_alerts:
        return {"status": "suppressed", "count": 0, "suppressed": len(failed)}

    auth_required = any(str(item.get("status") or "") == "auth_required" for item in due_alerts)
    target_months = [str(item.get("statement_month") or "").strip() for item in due_alerts]
    target_text = "、".join(month for month in target_months if month) or "対象月不明"
    reasons = []
    suppressed_total = 0
    for item in due_alerts:
        reason = str(item.get("error") or item.get("message") or "取得処理に失敗しました。")
        reason = " ".join(reason.split())[:700]
        if reason not in reasons:
            reasons.append(reason)
        suppressed_total += int(item.get("suppressed_count") or 0)
    if suppressed_total:
        reasons.append(f"同一エラーのDiscord通知を直前まで{suppressed_total}回抑制しました。")
    payload = {
        "embeds": [{
            "title": "🔐 ETCの再ログインが必要です" if auth_required else "⚠️ ETC明細を取得できませんでした",
            "url": ETC_ACCOUNTING_URL,
            "description": "\n".join(f"・{reason}" for reason in reasons)[:3500],
            "color": DISCORD_COLOR_ERROR,
            "fields": [
                {"name": "実行方法", "value": "定期取得" if scheduled else "手動取得", "inline": True},
                {"name": "対象月", "value": target_text[:1024], "inline": True},
            ],
            "footer": {"text": "タイトルをクリックしてETC利用証明書の状態を確認してください"},
            "timestamp": datetime.now().astimezone().isoformat(),
        }],
        "allowed_mentions": {"parse": []},
    }
    try:
        _post_discord(get_admin_discord_webhook(), payload)
        mark_fetch_alerts_notified([str(item["fingerprint"]) for item in due_alerts])
    except Exception as exc:
        record_discord_delivery("etc_accounting", success=False, error=str(exc))
        raise
    LOGGER.warning("ETC fetch failure notification sent: count=%s", len(due_alerts))
    return {"status": "sent", "count": len(due_alerts)}


def dispatch_pending_new_record_notifications() -> dict:
    records = claim_pending_record_notifications(100)
    if not records:
        return {"status": "empty", "count": 0}
    notification_ids = [int(record["notification_id"]) for record in records]
    sent_ids: set[int] = set()
    try:
        webhook_url = get_admin_discord_webhook()
        grouped: dict[str, list[dict]] = {}
        for record in records:
            kind = _notification_kind(record)
            grouped.setdefault(kind, []).append(record)
        for kind in ("new_record", "finalized", "source_deleted"):
            for batch_records, payload in _discord_batches(grouped.get(kind) or [], kind):
                batch_ids = [int(record["notification_id"]) for record in batch_records]
                _post_discord(webhook_url, payload)
                finish_record_notifications(batch_ids)
                sent_ids.update(batch_ids)
        LOGGER.info("ETC Discord notification sent: count=%s", len(records))
        return {"status": "sent", "count": len(records)}
    except Exception as exc:
        unsent_ids = [notification_id for notification_id in notification_ids if notification_id not in sent_ids]
        finish_record_notifications(unsent_ids, error=str(exc))
        LOGGER.exception("ETC Discord notification failed: count=%s", len(records))
        raise


def send_test_notification() -> int:
    records = list_records(limit=2)
    if not records:
        raise RuntimeError("テスト通知に使用できるETC利用明細がありません。")

    batches = _discord_batches(records)
    if len(batches) != 1:
        raise RuntimeError("ETCテスト通知の作成に失敗しました。")

    selected, payload = batches[0]
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    total = sum(int(record.get("amount") or 0) for record in selected)
    summary = payload["embeds"][0]
    summary.update(
        {
            "title": "🧪 ETC通知カードのテスト",
            "description": (
                f"最新の利用明細 **{len(selected)}件** を使用した表示テストです。\n"
                f"合計 **¥{total:,}**"
            ),
            "footer": {"text": f"MFU ETC test / {now}"},
        }
    )
    payload["content"] = "✅ ETC定期取得のテスト通知です"
    _post_discord(get_admin_discord_webhook(), payload)
    return len(selected)
