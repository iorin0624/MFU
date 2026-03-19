from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any


from app.utils.db import get_db


_LOG = logging.getLogger("mfu.push")
_ALLOWED_RECIPIENT_TYPES = {"external_user_id", "mfu_username"}
_ALLOWED_BOOL_TRUE = {True, 1, "1", "true", "True", "yes", "on"}


class PushDispatchError(Exception):
    def __init__(self, reason: str, *, status_code: int = 400, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class PushRequest:
    recipient_type: str
    recipient_value: str | int
    title: str
    body: str
    target_url: str
    kind: str
    sender_label: str | None
    dedup_key: str
    room_type: str | None
    room_id: str | None
    event_id: int | None
    chat_event_id: int | None
    chat_room_id: str | None
    create_in_app: bool
    send_web_push: bool


"""
Push通知の共通ゲートウェイ。

Python 呼び出し例:
    from app.utils.push import send_push

    send_push(
        recipient_type="external_user_id",
        recipient_value=123,
        title="写真アップロード完了",
        body="アルバムに新しい写真が追加されました。",
        target_url="/albums/123",
        kind="album_upload_complete",
        sender_label="アルバム機能",
        dedup_key="album:123:upload_complete:user:123",
    )

curl 例:
    curl -X POST 'https://mfu.iori0624.jp/api/internal/push/send' \
      -H 'Content-Type: application/json' \
      -H 'X-MFU-Internal-Key: YOUR_KEY' \
      --data '{
        "recipient_type":"external_user_id",
        "recipient_value":123,
        "title":"写真アップロード完了",
        "body":"アルバムに新しい写真が追加されました。",
        "target_url":"/albums/123",
        "kind":"album_upload_complete",
        "sender_label":"アルバム機能",
        "dedup_key":"album:123:upload_complete:user:123",
        "create_in_app":true,
        "send_web_push":true
      }'
"""


def _normalize_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value in _ALLOWED_BOOL_TRUE


def _normalize_text(value: Any, *, default: str = "", limit: int | None = None) -> str:
    normalized = str(value if value is not None else default).strip()
    if not normalized and default:
        normalized = str(default).strip()
    if limit is not None:
        normalized = normalized[:limit]
    return normalized


def _normalize_optional_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        normalized = int(value)
    except Exception as exc:
        raise PushDispatchError(f"invalid_{field_name}", detail=str(exc)) from exc
    if normalized <= 0:
        raise PushDispatchError(f"invalid_{field_name}")
    return normalized


def _normalize_recipient_value(recipient_type: str, recipient_value: Any) -> str | int:
    if recipient_type == "external_user_id":
        normalized = _normalize_optional_int(recipient_value, field_name="recipient_value")
        if normalized is None:
            raise PushDispatchError("recipient_value_required")
        return normalized
    normalized = _normalize_text(recipient_value, limit=191)
    if not normalized:
        raise PushDispatchError("recipient_value_required")
    return normalized


def _normalize_request(**kwargs: Any) -> PushRequest:
    recipient_type = _normalize_text(kwargs.get("recipient_type"), limit=32)
    if recipient_type not in _ALLOWED_RECIPIENT_TYPES:
        raise PushDispatchError("unsupported_recipient_type")

    recipient_value = _normalize_recipient_value(recipient_type, kwargs.get("recipient_value"))
    title = _normalize_text(kwargs.get("title"), limit=255)
    if not title:
        raise PushDispatchError("title_required")
    body = _normalize_text(kwargs.get("body") if kwargs.get("body") is not None else "", limit=1000)
    target_url = _normalize_text(kwargs.get("target_url"), limit=512)
    if not target_url:
        raise PushDispatchError("target_url_required")
    if not target_url.startswith("/") or target_url.startswith("//"):
        raise PushDispatchError("invalid_target_url")
    dedup_key = _normalize_text(kwargs.get("dedup_key"), limit=191)
    if not dedup_key:
        raise PushDispatchError("dedup_key_required")

    create_in_app = _normalize_bool(kwargs.get("create_in_app"), default=True)
    send_web_push = _normalize_bool(kwargs.get("send_web_push"), default=True)
    if not create_in_app and not send_web_push:
        raise PushDispatchError("at_least_one_channel_required")

    kind = _normalize_text(kwargs.get("kind") or "general", limit=64) or "general"
    sender_label = _normalize_text(kwargs.get("sender_label"), limit=255) or None
    room_type = _normalize_text(kwargs.get("room_type"), limit=32) or None
    room_id = _normalize_text(kwargs.get("room_id"), limit=64) or None
    chat_room_id = _normalize_text(kwargs.get("chat_room_id"), limit=64) or None
    event_id = _normalize_optional_int(kwargs.get("event_id"), field_name="event_id")
    chat_event_id = _normalize_optional_int(kwargs.get("chat_event_id"), field_name="chat_event_id")

    if kind == "chat_message" and not chat_room_id:
        raise PushDispatchError("chat_room_id_required")
    if kind == "chat_message" and not room_id:
        room_id = chat_room_id

    return PushRequest(
        recipient_type=recipient_type,
        recipient_value=recipient_value,
        title=title,
        body=body,
        target_url=target_url,
        kind=kind,
        sender_label=sender_label,
        dedup_key=dedup_key,
        room_type=room_type,
        room_id=room_id,
        event_id=event_id,
        chat_event_id=chat_event_id,
        chat_room_id=chat_room_id,
        create_in_app=create_in_app,
        send_web_push=send_web_push,
    )


def _count_push_subscriptions(actor_type: str, actor_id: str) -> int:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        actor_ids = [str(actor_id)]
        if actor_type == "admin":
            actor_ids.extend(["admin", "1"])
        actor_ids = list(dict.fromkeys([x for x in actor_ids if x]))
        placeholders = ",".join(["%s"] * len(actor_ids))
        cur.execute(
            f"""
            SELECT COUNT(*) AS cnt
              FROM chat_push_subscriptions
             WHERE actor_type=%s AND actor_id IN ({placeholders})
            """,
            (actor_type, *actor_ids),
        )
        return int((cur.fetchone() or {}).get("cnt") or 0)
    except Exception:
        return 0
    finally:
        cur.close()
        db.close()


def _resolve_push_actor(request_data: PushRequest) -> tuple[str, str]:
    if request_data.recipient_type == "external_user_id":
        return "line", str(request_data.recipient_value)
    username = str(request_data.recipient_value)
    return ("admin", username) if username == "admin" else ("acl", username)


def _deliver_web_push(request_data: PushRequest, notification_id: int | None) -> tuple[str, str]:
    try:
        from app.chat import _send_push_to_actor  # type: ignore
    except Exception:
        return "failed", "chat_push_sender_unavailable"

    actor_type, actor_id = _resolve_push_actor(request_data)
    subscription_count = _count_push_subscriptions(actor_type, actor_id)
    if subscription_count <= 0:
        return "skipped", "no_subscriptions"

    try:
        import pywebpush  # noqa: F401
    except Exception:
        return "failed", "pywebpush_not_installed"

    if not os.getenv("CHAT_VAPID_PUBLIC_KEY") or not os.getenv("CHAT_VAPID_PRIVATE_KEY"):
        return "failed", "vapid_not_configured"

    payload = {
        "title": request_data.title,
        "body": request_data.body,
        "url": request_data.target_url,
        "kind": request_data.kind,
        "sender_label": request_data.sender_label or "",
        "notification_id": notification_id,
        "dedup_key": request_data.dedup_key,
        "event_id": request_data.event_id,
        "chat_event_id": request_data.chat_event_id,
        "chat_room_id": request_data.chat_room_id,
        "room_type": request_data.room_type,
        "room_id": request_data.room_id,
    }
    metrics: dict[str, Any] = {}
    sent_count = int(_send_push_to_actor(actor_type, actor_id, payload, metrics) or 0)
    failure_count = int(metrics.get("failure_count", 0) or 0)
    if sent_count > 0:
        return "sent", f"subscriptions={subscription_count} sent={sent_count} failed={failure_count}"
    return "failed", f"subscriptions={subscription_count} sent=0 failed={failure_count}"


def send_push(
    *,
    recipient_type: str,
    recipient_value: str | int,
    title: str,
    body: str,
    target_url: str,
    kind: str = "general",
    sender_label: str | None = None,
    dedup_key: str,
    room_type: str | None = None,
    room_id: str | None = None,
    event_id: int | None = None,
    chat_event_id: int | None = None,
    chat_room_id: str | None = None,
    create_in_app: bool = True,
    send_web_push: bool = True,
) -> dict[str, Any]:
    from app.external_login_user.notifications import (
        _has_notification_delivery_attempt,
        _record_notification_delivery,
        create_notification_dispatch_result,
    )

    started = perf_counter()
    req = _normalize_request(
        recipient_type=recipient_type,
        recipient_value=recipient_value,
        title=title,
        body=body,
        target_url=target_url,
        kind=kind,
        sender_label=sender_label,
        dedup_key=dedup_key,
        room_type=room_type,
        room_id=room_id,
        event_id=event_id,
        chat_event_id=chat_event_id,
        chat_room_id=chat_room_id,
        create_in_app=create_in_app,
        send_web_push=send_web_push,
    )

    result: dict[str, Any] = {
        "ok": True,
        "created": False,
        "duplicate": False,
        "notification_id": None,
        "delivery": {"in_app": "skipped", "web_push": "skipped"},
    }

    if req.create_in_app:
        create_result = create_notification_dispatch_result(
            recipient_type=req.recipient_type,
            recipient_value=req.recipient_value,
            kind=req.kind,
            title=req.title,
            body=req.body,
            target_url=req.target_url,
            dedup_key=req.dedup_key,
            sender_label=req.sender_label or "",
            room_type=req.room_type,
            room_id=req.room_id,
            event_id=req.event_id,
            chat_event_id=req.chat_event_id,
            chat_room_id=req.chat_room_id,
        )
        if not create_result.get("ok"):
            raise PushDispatchError(str(create_result.get("reason") or "notification_create_failed"), status_code=500)

        result["created"] = bool(create_result.get("created"))
        result["duplicate"] = bool(create_result.get("duplicate"))
        result["notification_id"] = create_result.get("notification_id")
        result["delivery"]["in_app"] = "created" if create_result.get("created") else "duplicate"
        _record_notification_delivery(
            notification_id=result.get("notification_id") or create_result.get("existing_notification_id"),
            dedup_key=req.dedup_key,
            recipient_type=req.recipient_type,
            recipient_value=req.recipient_value,
            channel="in_app",
            status="sent" if create_result.get("created") else "duplicate",
            detail=f"kind={req.kind}",
            sent_at=datetime.utcnow() if create_result.get("created") else None,
        )
        if create_result.get("duplicate"):
            if req.send_web_push:
                _record_notification_delivery(
                    notification_id=create_result.get("existing_notification_id"),
                    dedup_key=req.dedup_key,
                    recipient_type=req.recipient_type,
                    recipient_value=req.recipient_value,
                    channel="web_push",
                    status="skipped",
                    detail="duplicate_notification",
                )
            elapsed_ms = int((perf_counter() - started) * 1000)
            _LOG.info(
                "push dispatch recipient_type=%s recipient_value=%s kind=%s title=%s dedup_key=%s in_app=%s web_push=%s duplicate=%s notification_id=%s elapsed_ms=%s",
                req.recipient_type,
                req.recipient_value,
                req.kind,
                req.title,
                req.dedup_key,
                result["delivery"]["in_app"],
                result["delivery"]["web_push"],
                result["duplicate"],
                result["notification_id"],
                elapsed_ms,
            )
            return result
    elif req.send_web_push and _has_notification_delivery_attempt(
        dedup_key=req.dedup_key,
        recipient_type=req.recipient_type,
        recipient_value=req.recipient_value,
        channel="web_push",
    ):
        result["duplicate"] = True
        _record_notification_delivery(
            notification_id=None,
            dedup_key=req.dedup_key,
            recipient_type=req.recipient_type,
            recipient_value=req.recipient_value,
            channel="web_push",
            status="duplicate",
            detail="duplicate_web_push_only",
        )
        elapsed_ms = int((perf_counter() - started) * 1000)
        _LOG.info(
            "push dispatch recipient_type=%s recipient_value=%s kind=%s title=%s dedup_key=%s in_app=%s web_push=%s duplicate=%s notification_id=%s elapsed_ms=%s",
            req.recipient_type,
            req.recipient_value,
            req.kind,
            req.title,
            req.dedup_key,
            result["delivery"]["in_app"],
            result["delivery"]["web_push"],
            result["duplicate"],
            result["notification_id"],
            elapsed_ms,
        )
        return result

    if req.send_web_push:
        web_push_status, detail = _deliver_web_push(req, result.get("notification_id"))
        result["delivery"]["web_push"] = web_push_status
        _record_notification_delivery(
            notification_id=result.get("notification_id"),
            dedup_key=req.dedup_key,
            recipient_type=req.recipient_type,
            recipient_value=req.recipient_value,
            channel="web_push",
            status=web_push_status,
            detail=detail,
            sent_at=datetime.utcnow() if web_push_status == "sent" else None,
        )

    elapsed_ms = int((perf_counter() - started) * 1000)
    _LOG.info(
        "push dispatch recipient_type=%s recipient_value=%s kind=%s title=%s dedup_key=%s in_app=%s web_push=%s duplicate=%s notification_id=%s elapsed_ms=%s",
        req.recipient_type,
        req.recipient_value,
        req.kind,
        req.title,
        req.dedup_key,
        result["delivery"]["in_app"],
        result["delivery"]["web_push"],
        result["duplicate"],
        result["notification_id"],
        elapsed_ms,
    )
    return result
