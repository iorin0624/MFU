from __future__ import annotations

from typing import Any

from app.utils.db import get_db
from app.utils.push import send_external_event_push

from .utils import _uuid_bytes_to_str


_STATUS_LABELS = {
    "approved": "承認",
    "pending": "保留・承認待ち",
    "rejected": "拒否",
    "canceled": "キャンセル",
    "active": "参加中",
}

_PAYMENT_MESSAGES = {
    "paid": "お支払いが確認されました。領収書や支払内容はイベント詳細から確認できます。",
    "pending": "お支払い内容を確認しています。確認完了までしばらくお待ちください。",
    "unpaid": "お支払いが未確認の状態です。イベント詳細をご確認ください。",
    "refunded": "返金済みになりました。詳細はイベントページをご確認ください。",
}


def _member_event_context(event_id: int, user_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT e.title, e.event_uuid, u.nickname
              FROM mfu_event_member m
              JOIN mfu_event e ON e.id=m.event_id
              JOIN external_login_user u ON u.id=m.user_id
             WHERE m.event_id=%s AND m.user_id=%s
             LIMIT 1
            """,
            (int(event_id), int(user_id)),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def _event_user_context(event_id: int, user_id: int) -> dict[str, Any] | None:
    """Resolve trusted payment recipients even when no membership row exists."""

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT e.title, e.event_uuid, u.nickname
              FROM mfu_event e
              JOIN external_login_user u ON u.id=%s
             WHERE e.id=%s
             LIMIT 1
            """,
            (int(user_id), int(event_id)),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def notify_member_status_push(
    *, event_id: int, user_id: int, new_status: str, old_status: str | None = None
) -> dict[str, Any]:
    row = _member_event_context(event_id, user_id)
    if not row:
        return {"ok": False, "reason": "member_not_found"}
    event_uuid = _uuid_bytes_to_str(row.get("event_uuid")) or ""
    event_title = str(row.get("title") or "イベント")
    new_label = _STATUS_LABELS.get(str(new_status), str(new_status))
    old_label = _STATUS_LABELS.get(str(old_status), str(old_status)) if old_status else ""
    transition = f"{old_label}から{new_label}へ" if old_label else f"{new_label}へ"
    return send_external_event_push(
        user_id=user_id,
        event_id=event_id,
        event_uuid=event_uuid,
        kind="event_membership_status",
        title=f"【{event_title}】参加ステータスが更新されました",
        body=f"参加ステータスが{transition}更新されました。イベント詳細をご確認ください。",
        sender_label="イベント",
    )


def notify_member_payment_push(
    *,
    event_id: int,
    user_id: int,
    payment_status: str,
    body: str | None = None,
    kind: str = "event_payment_status",
    title_suffix: str | None = None,
    dedup_token: str | None = None,
) -> dict[str, Any]:
    row = _event_user_context(event_id, user_id)
    if not row:
        return {"ok": False, "reason": "member_not_found"}
    event_uuid = _uuid_bytes_to_str(row.get("event_uuid")) or ""
    event_title = str(row.get("title") or "イベント")
    message = body or _PAYMENT_MESSAGES.get(
        str(payment_status), "お支払い情報が更新されました。イベント詳細をご確認ください。"
    )
    return send_external_event_push(
        user_id=user_id,
        event_id=event_id,
        event_uuid=event_uuid,
        kind=kind,
        title=f"【{event_title}】{title_suffix or 'お支払い情報が更新されました'}",
        body=message,
        target_suffix="/payment",
        sender_label="支払",
        dedup_token=dedup_token,
    )
