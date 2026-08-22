import re
from datetime import datetime
from typing import Optional

import requests

DISCORD_CONTENT_LIMIT = 2000
DISCORD_RESPONSE_BODY_LOG_LIMIT = 400
COMMENT_PREVIEW_LIMIT = 500
DISCORD_EMBED_FIELD_LIMIT = 1024
DISCORD_UPLOAD_COLOR = 0x3498DB


def normalize_notification_settings(notify_method: Optional[str], webhook_url: Optional[str]) -> tuple[str, str]:
    return (notify_method or "").strip().lower(), (webhook_url or "").strip()


def _mask_webhook(webhook_url: str) -> str:
    if not webhook_url:
        return "none"
    if len(webhook_url) <= 12:
        return "set(masked)"
    return f"set({webhook_url[:8]}...{webhook_url[-4:]})"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return text[: limit - 1].rstrip() + "…"


def _feature_webhook(feature_key: str, legacy_webhook: str) -> str:
    try:
        from app.discord_notifications.repository import get_discord_webhook
        return get_discord_webhook(feature_key, legacy_webhook)
    except Exception:
        return legacy_webhook


def _record_feature_delivery(feature_key: str, *, success: bool, error: str = "") -> None:
    try:
        from app.discord_notifications.repository import record_discord_delivery
        record_discord_delivery(feature_key, success=success, error=error)
    except Exception:
        pass


def _fit_discord_content(content: str, limit: int = DISCORD_CONTENT_LIMIT) -> str:
    if len(content) <= limit:
        return content

    urls = re.findall(r"https?://\S+", content)
    trailing_url = urls[-1] if urls else ""
    if trailing_url:
        sep = "\n"
        reserve = len(sep) + len(trailing_url)
        if reserve < limit:
            head = _truncate(content, limit - reserve)
            return f"{head}{sep}{trailing_url}"
    return _truncate(content, limit)


def build_processed_upload_message(
    *,
    title: str,
    comment: str,
    detail_url: str,
    image_count: int,
    posted_at: datetime,
) -> str:
    comment_text = (comment or "").strip() or "（なし）"
    comment_text = _truncate(comment_text, COMMENT_PREVIEW_LIMIT)
    raw = (
        "📸 加工済み写真がアップロードされました\n"
        f"タイトル: {title}\n"
        f"画像枚数: {max(0, int(image_count))}枚\n"
        f"コメント: {comment_text}\n"
        f"投稿日時: {posted_at.strftime('%Y年%m月%d日 %H:%M:%S')}\n\n"
        f"詳細を確認:\n{detail_url}"
    )
    return _fit_discord_content(raw)


def build_processed_upload_discord_embed(
    *,
    title: str,
    comment: str,
    detail_url: str,
    image_count: int,
    posted_at: datetime,
) -> dict:
    comment_text = (comment or "").strip() or "（なし）"
    return {
        "title": "📸 加工済み写真がアップロードされました",
        "url": detail_url,
        "color": DISCORD_UPLOAD_COLOR,
        "fields": [
            {
                "name": "撮影タイトル",
                "value": _truncate((title or "（タイトルなし）").strip(), DISCORD_EMBED_FIELD_LIMIT),
                "inline": False,
            },
            {
                "name": "画像枚数",
                "value": f"{max(0, int(image_count))}枚",
                "inline": True,
            },
            {
                "name": "投稿日時",
                "value": posted_at.strftime("%Y年%m月%d日 %H:%M:%S"),
                "inline": True,
            },
            {
                "name": "コメント",
                "value": _truncate(comment_text, DISCORD_EMBED_FIELD_LIMIT),
                "inline": False,
            },
        ],
        "footer": {"text": "タイトルをクリックするとMFUの詳細画面を開きます"},
    }


def send_discord_upload_notification(
    *,
    logger,
    username: str,
    notify_method: Optional[str],
    webhook_url: Optional[str],
    upload_id: str,
    message: str,
    context_label: str,
    embed: Optional[dict] = None,
    feature_key: str = "upload_complete",
) -> bool:
    normalized_method, normalized_webhook = normalize_notification_settings(notify_method, webhook_url)
    normalized_webhook = _feature_webhook(feature_key, normalized_webhook)
    webhook_masked = _mask_webhook(normalized_webhook)

    if normalized_method not in ("discord", "both"):
        logger.info(
            "%s Discord通知スキップ: reason=notify_method user=%s notify_method=%s upload_id=%s webhook=%s",
            context_label,
            username,
            normalized_method or "(empty)",
            upload_id,
            webhook_masked,
        )
        return False

    if not normalized_webhook:
        logger.warning(
            "%s Discord通知スキップ: reason=no_webhook user=%s notify_method=%s upload_id=%s webhook=%s",
            context_label,
            username,
            normalized_method,
            upload_id,
            webhook_masked,
        )
        return False

    payload_content = _fit_discord_content(message)
    payload = (
        {"embeds": [embed], "allowed_mentions": {"parse": []}}
        if embed
        else {"content": payload_content, "allowed_mentions": {"parse": []}}
    )
    logger.info(
        "%s Discord通知開始: user=%s notify_method=%s upload_id=%s webhook=%s content_len=%s",
        context_label,
        username,
        normalized_method,
        upload_id,
        webhook_masked,
        len(payload_content),
    )

    resp = None
    try:
        resp = requests.post(normalized_webhook, json=payload, timeout=10)
        body_preview = _truncate((resp.text or "").replace("\n", "\\n"), DISCORD_RESPONSE_BODY_LOG_LIMIT)
        logger.info(
            "%s Discord response: user=%s upload_id=%s status=%s body=%s",
            context_label,
            username,
            upload_id,
            resp.status_code,
            body_preview or "(empty)",
        )
        resp.raise_for_status()
        _record_feature_delivery(feature_key, success=True)
        logger.info(
            "%s Discord通知成功: user=%s notify_method=%s upload_id=%s",
            context_label,
            username,
            normalized_method,
            upload_id,
        )
        return True
    except Exception as exc:
        _record_feature_delivery(feature_key, success=False, error=f"{type(exc).__name__}: {exc}")
        status = getattr(resp, "status_code", "n/a")
        body = _truncate((getattr(resp, "text", "") or "").replace("\n", "\\n"), DISCORD_RESPONSE_BODY_LOG_LIMIT)
        logger.exception(
            "%s Discord通知失敗: user=%s notify_method=%s upload_id=%s webhook=%s status=%s body=%s err=%r",
            context_label,
            username,
            normalized_method or "(empty)",
            upload_id,
            webhook_masked,
            status,
            body or "(empty)",
            exc,
        )
        return False
