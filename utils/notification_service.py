from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, has_app_context, jsonify, redirect, render_template_string, request, session, url_for
from flask_socketio import disconnect, emit, join_room

from app.chat.socketio_ext import socketio
from app.discord_notifications.repository import FEATURE_DEFINITIONS
from app.utils.db import get_db


notification_service_bp = Blueprint("notification_service", __name__)
TOKEN_PREFIX = "mfu_nt_"
SOURCE_TOKEN_PREFIX = "mfu_ns_"
TOKEN_DAYS = 180
DEFAULT_RECIPIENT = "admin"
_schema_ready = False
_socket_tokens: dict[str, dict[str, Any]] = {}


def _with_notification_app_context(function):
    """Allow notification delivery from systemd jobs and background threads."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if has_app_context():
            return function(*args, **kwargs)
        from app import create_app

        flask_app = create_app()
        with flask_app.app_context():
            return function(*args, **kwargs)

    return wrapped


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token() -> str:
    header = str(request.headers.get("Authorization") or "")
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def ensure_notification_service_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS media_hub_notification_tokens (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              token_hash CHAR(64) NOT NULL,
              username VARCHAR(191) NOT NULL,
              device_uuid CHAR(36) NOT NULL,
              device_name VARCHAR(120) NOT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              last_used_at DATETIME NULL,
              last_connected_at DATETIME NULL,
              revoked_at DATETIME NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_media_hub_notification_token (token_hash),
              KEY idx_media_hub_notification_user (username, revoked_at),
              KEY idx_media_hub_notification_device (device_uuid, revoked_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_notification_preferences (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              recipient_type VARCHAR(32) NOT NULL,
              recipient_value VARCHAR(191) NOT NULL,
              feature_key VARCHAR(64) NOT NULL,
              media_hub_enabled TINYINT(1) NOT NULL DEFAULT 1,
              web_push_enabled TINYINT(1) NOT NULL DEFAULT 1,
              discord_enabled TINYINT(1) NOT NULL DEFAULT 1,
              sound_enabled TINYINT(1) NOT NULL DEFAULT 1,
              muted_forever TINYINT(1) NOT NULL DEFAULT 0,
              mute_until DATETIME NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uq_mfu_notification_preference (recipient_type, recipient_value, feature_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_notification_sources (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              source_name VARCHAR(120) NOT NULL,
              device_name VARCHAR(120) NOT NULL DEFAULT '',
              feature_key VARCHAR(64) NOT NULL,
              recipient_username VARCHAR(191) NOT NULL DEFAULT 'admin',
              token_hash CHAR(64) NOT NULL,
              enabled TINYINT(1) NOT NULL DEFAULT 1,
              discord_enabled TINYINT(1) NOT NULL DEFAULT 1,
              rate_limit_per_minute INT UNSIGNED NOT NULL DEFAULT 60,
              last_seen_at DATETIME NULL,
              last_ip VARCHAR(64) NOT NULL DEFAULT '',
              accepted_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
              error_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              revoked_at DATETIME NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_mfu_notification_source_token (token_hash),
              KEY idx_mfu_notification_source_enabled (enabled, revoked_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_notification_ingress_audit (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              source_id BIGINT UNSIGNED NULL,
              received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              remote_ip VARCHAR(64) NOT NULL DEFAULT '',
              mode VARCHAR(24) NOT NULL,
              payload_sha256 CHAR(64) NOT NULL,
              status VARCHAR(24) NOT NULL,
              detail VARCHAR(500) NOT NULL DEFAULT '',
              notification_id BIGINT UNSIGNED NULL,
              PRIMARY KEY (id),
              KEY idx_mfu_notification_ingress_source (source_id, received_at),
              KEY idx_mfu_notification_ingress_status (status, received_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_notification_ingress_rate (
              source_id BIGINT UNSIGNED NOT NULL,
              window_start DATETIME NOT NULL,
              request_count INT UNSIGNED NOT NULL DEFAULT 0,
              PRIMARY KEY (source_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
        _schema_ready = True
    finally:
        cur.close()
        db.close()


def ensure_notification_service_nav_item() -> None:
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id FROM mfu_nav_items WHERE parent_id IS NULL AND (label LIKE %s OR label LIKE %s) ORDER BY id LIMIT 1",
            ("%システム系%", "%通知%"),
        )
        parent = cur.fetchone()
        parent_id = int(parent["id"]) if parent else None
        for label, url in (
            ("外部通知元", "/admin/notification-sources"),
            ("共通通知設定", "/admin/notification-settings"),
        ):
            cur.execute("SELECT id,parent_id FROM mfu_nav_items WHERE url=%s LIMIT 1", (url,))
            existing = cur.fetchone()
            if existing:
                if existing.get("parent_id") != parent_id:
                    cur.execute("UPDATE mfu_nav_items SET parent_id=%s WHERE id=%s", (parent_id, existing["id"]))
                continue
            cur.execute("SELECT COALESCE(MAX(order_no),0) AS max_order FROM mfu_nav_items WHERE parent_id <=> %s", (parent_id,))
            order_no = int((cur.fetchone() or {}).get("max_order") or 0) + 10
            cur.execute(
                "INSERT INTO mfu_nav_items (parent_id,label,url,order_no,is_enabled,feature_key,open_in_new_tab,is_external) VALUES (%s,%s,%s,%s,1,NULL,0,0)",
                (parent_id, label, url, order_no),
            )
        db.commit()
    finally:
        cur.close()
        db.close()


def issue_notification_token(username: str, device_uuid: str, device_name: str) -> str:
    ensure_notification_service_schema()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=TOKEN_DAYS)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO media_hub_notification_tokens
              (token_hash, username, device_uuid, device_name, created_at, expires_at)
            VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), %s)
            """,
            (_hash_token(token), username[:191], device_uuid, device_name[:120], expires_at),
        )
        db.commit()
        return token
    finally:
        cur.close()
        db.close()


def verify_notification_token(token: str | None = None, *, touch: bool = True) -> dict[str, Any] | None:
    raw = (token or _bearer_token()).strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, username, device_uuid, device_name, expires_at, revoked_at
              FROM media_hub_notification_tokens
             WHERE token_hash=%s LIMIT 1
            """,
            (_hash_token(raw),),
        )
        row = cur.fetchone()
        if not row or row.get("revoked_at"):
            return None
        if row.get("expires_at") and row["expires_at"] < datetime.utcnow():
            return None
        if touch:
            cur.execute("UPDATE media_hub_notification_tokens SET last_used_at=UTC_TIMESTAMP() WHERE id=%s", (row["id"],))
            db.commit()
        return row
    finally:
        cur.close()
        db.close()


def revoke_notification_token(token: str) -> bool:
    if not token:
        return False
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE media_hub_notification_tokens SET revoked_at=UTC_TIMESTAMP() WHERE token_hash=%s AND revoked_at IS NULL",
            (_hash_token(token),),
        )
        db.commit()
        return bool(cur.rowcount)
    finally:
        cur.close()
        db.close()


def _preference(username: str, feature_key: str) -> dict[str, Any]:
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT * FROM mfu_notification_preferences
             WHERE recipient_type='mfu_username' AND recipient_value=%s AND feature_key IN (%s, '*')
             ORDER BY feature_key=%s DESC LIMIT 1
            """,
            (username, feature_key, feature_key),
        )
        row = cur.fetchone() or {}
        return {
            "media_hub_enabled": bool(row.get("media_hub_enabled", 1)),
            "web_push_enabled": bool(row.get("web_push_enabled", 1)),
            "discord_enabled": bool(row.get("discord_enabled", 1)),
            "sound_enabled": bool(row.get("sound_enabled", 1)),
            "muted_forever": bool(row.get("muted_forever", 0)),
            "mute_until": row.get("mute_until"),
        }
    finally:
        cur.close()
        db.close()


def _is_muted(preference: dict[str, Any]) -> bool:
    if preference.get("muted_forever"):
        return True
    mute_until = preference.get("mute_until")
    return bool(mute_until and mute_until > datetime.utcnow())


@_with_notification_app_context
def publish_common_notification(
    *,
    recipient_username: str,
    feature_key: str,
    kind: str,
    severity: str,
    title: str,
    description: str,
    fields: list[dict[str, Any]] | None = None,
    cards: list[dict[str, Any]] | None = None,
    lead_text: str = "",
    image_url: str = "",
    footer: str = "",
    target_url: str = "/mfu-notifications",
    topic_key: str = "",
    actions: list[dict[str, Any]] | None = None,
    dedup_key: str,
    source_id: int | None = None,
) -> dict[str, Any]:
    from app.utils.push import send_push

    recipient = (recipient_username or DEFAULT_RECIPIENT).strip()[:191]
    feature = (feature_key or "general").strip()[:64]
    preference = _preference(recipient, feature)
    muted = _is_muted(preference)
    normalized_actions: list[dict[str, str]] = []
    for row in actions or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()[:80]
        action_type = str(row.get("type") or "link").strip().lower()[:24]
        if not label:
            continue
        if action_type == "copy":
            value = str(row.get("value") or "")[:500]
            if not value:
                continue
            normalized_actions.append({
                "type": "copy",
                "label": label,
                "value": value,
                "expires_at": str(row.get("expires_at") or "")[:64],
            })
            continue
        url = str(row.get("url") or "").strip()[:1000]
        if url:
            normalized_actions.append({"type": "link", "label": label, "url": url})

    content = {
        "fields": [row for row in (fields or []) if isinstance(row, dict)][:30],
        "cards": [row for row in (cards or []) if isinstance(row, dict)][:10],
        "lead_text": str(lead_text or "")[:2000],
        "image_url": (image_url or "")[:1000],
        "footer": (footer or "")[:255],
        "sound_enabled": bool(preference.get("sound_enabled")),
        "media_hub_enabled": bool(preference.get("media_hub_enabled")),
        "actions": normalized_actions[:8],
    }
    result = send_push(
        recipient_type="mfu_username",
        recipient_value=recipient,
        kind=(kind or feature)[:64],
        feature_key=feature,
        severity=severity,
        topic_key=topic_key,
        title=title,
        body=description,
        target_url=target_url or "/mfu-notifications",
        sender_label=footer or "MFU",
        content=content,
        source_id=source_id,
        muted_at=datetime.utcnow() if muted else None,
        dedup_key=dedup_key,
        create_in_app=True,
        send_web_push=bool(preference.get("web_push_enabled")) and not muted,
    )
    result["muted"] = muted
    result["preference"] = preference
    result.setdefault("delivery", {})["media_hub"] = (
        "muted" if muted else ("queued" if preference.get("media_hub_enabled") else "disabled")
    )
    return result


def _common_notification_target_url(value: Any) -> str:
    """Convert same-site Discord links to safe app-relative notification links."""
    raw = str(value or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw[:512]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "/mfu-notifications"
    if parsed.scheme.lower() in {"http", "https"} and (parsed.hostname or "").lower() == "mfu.iori0624.jp":
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return target[:512]
    return "/mfu-notifications"


def _common_notification_card_url(value: Any) -> str:
    """Keep safe per-card links, including external source pages."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 1000 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port  # Reject malformed ports.
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in raw
    ):
        return ""
    if hostname.lower() == "mfu.iori0624.jp" and port is None:
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        if parsed.fragment:
            target += f"#{parsed.fragment}"
        return target
    return raw


def discord_payload_to_event(feature_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    embeds = payload.get("embeds") if isinstance(payload.get("embeds"), list) else []
    embed = embeds[0] if embeds and isinstance(embeds[0], dict) else {}
    content = str(payload.get("content") or "")
    feature_label = str(FEATURE_DEFINITIONS.get(feature_key, {}).get("label") or "お知らせ")
    title = str(embed.get("title") or feature_label)
    description = str(embed.get("description") or content)
    fields = []
    for row in embed.get("fields") or []:
        if isinstance(row, dict):
            fields.append({
                "name": str(row.get("name") or "")[:255],
                "value": str(row.get("value") or "")[:2000],
                "inline": bool(row.get("inline")),
            })
    color = int(embed.get("color") or 0)
    severity = "info"
    if color in {0x2ECC71, 0x57F287, 3066993}:
        severity = "success"
    elif color in {0xF1C40F, 0xFEE75C, 16776960}:
        severity = "warning"
    elif color in {0xE74C3C, 0xED4245, 15158332}:
        severity = "error"
    footer_data = embed.get("footer") if isinstance(embed.get("footer"), dict) else {}
    image_data = embed.get("image") if isinstance(embed.get("image"), dict) else {}
    target_url = _common_notification_target_url(embed.get("url"))
    cards: list[dict[str, Any]] = []
    for raw_card in embeds[:10]:
        if not isinstance(raw_card, dict):
            continue
        card_fields = []
        for row in raw_card.get("fields") or []:
            if isinstance(row, dict):
                card_fields.append({
                    "name": str(row.get("name") or "")[:255],
                    "value": str(row.get("value") or "")[:2000],
                    "inline": bool(row.get("inline")),
                })
        card_footer = raw_card.get("footer") if isinstance(raw_card.get("footer"), dict) else {}
        card_image = raw_card.get("image") if isinstance(raw_card.get("image"), dict) else {}
        cards.append({
            "title": str(raw_card.get("title") or "")[:255],
            "description": str(raw_card.get("description") or "")[:4000],
            "url": _common_notification_card_url(raw_card.get("url")),
            "color": int(raw_card.get("color") or 0),
            "fields": card_fields[:25],
            "footer": str(card_footer.get("text") or "")[:255],
            "timestamp": str(raw_card.get("timestamp") or "")[:64],
            "image_url": str(card_image.get("url") or "")[:1000],
        })
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    if not actions:
        actions = []
        for component_row in payload.get("components") or []:
            if not isinstance(component_row, dict):
                continue
            for component in component_row.get("components") or []:
                if not isinstance(component, dict) or int(component.get("type") or 0) != 2:
                    continue
                label = str(component.get("label") or "").strip()
                url = str(component.get("url") or "").strip()
                if label and url:
                    actions.append({"label": label[:80], "url": url[:1000]})
    return {
        "feature_key": feature_key,
        "kind": str(payload.get("kind") or feature_key)[:64],
        "severity": severity,
        "title": title[:255],
        "description": description,
        "fields": fields,
        "cards": cards,
        # Discordの content だけで送られた通知は description に一度だけ保存する。
        # embed 付きの content は、従来どおりカード前の案内文として保持する。
        "lead_text": content[:2000] if embeds else "",
        "image_url": str(image_data.get("url") or "")[:1000],
        "footer": str(footer_data.get("text") or payload.get("username") or "MFU")[:255],
        "target_url": target_url,
        "topic_key": str(payload.get("topic_key") or "")[:191],
        "actions": actions[:8],
    }


@_with_notification_app_context
def mirror_discord_payload(feature_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = discord_payload_to_event(feature_key, payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    bucket = datetime.utcnow().strftime("%Y%m%d%H%M")
    dedup = f"discord:{feature_key}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}:{bucket}"
    return publish_common_notification(
        recipient_username=DEFAULT_RECIPIENT,
        dedup_key=dedup,
        **event,
    )


@_with_notification_app_context
def dispatch_discord_notification(
    feature_key: str,
    payload: dict[str, Any],
    *,
    recipient_username: str = DEFAULT_RECIPIENT,
    legacy_webhook: str | None = None,
    timeout: int = 10,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register once in the common store, then fan out to every enabled channel."""
    event = discord_payload_to_event(feature_key, payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    bucket = datetime.utcnow().strftime("%Y%m%d%H%M")
    result = publish_common_notification(
        recipient_username=recipient_username,
        dedup_key=f"dispatch:{feature_key}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}:{bucket}",
        **event,
    )
    preference = result.get("preference") if isinstance(result.get("preference"), dict) else {}
    discord_status = "muted" if result.get("muted") else "disabled"
    if not result.get("muted") and preference.get("discord_enabled", True):
        from app.discord_notifications.service import post_discord_notification
        delivered = post_discord_notification(
            feature_key,
            payload,
            legacy_webhook=legacy_webhook,
            timeout=timeout,
            params=params,
            mirror=False,
        )
        discord_status = "sent" if delivered else "disabled"
    result.setdefault("delivery", {})["discord"] = discord_status
    result["ok"] = bool(result.get("ok"))
    return result


def mirror_discord_payload_best_effort(feature_key: str, payload: dict[str, Any]) -> None:
    """Mirror a legacy direct-Discord sender without changing its delivery path."""
    try:
        mirror_discord_payload(feature_key, payload)
    except Exception:
        # A common-notification outage must never suppress the original alert.
        try:
            current_app.logger.warning(
                "legacy Discord notification mirror failed feature=%s", feature_key, exc_info=True
            )
        except Exception:
            pass


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.external_login_user.notifications import _serialize_mfu_notification_item
    return [_serialize_mfu_notification_item(row) for row in rows]


def _desktop_notifications(row: dict[str, Any], since_id: int = 0, limit: int = 100) -> dict[str, Any]:
    from app.external_login_user.notifications import (
        _compute_mfu_channel_unread_counts,
        _mfu_notification_channels,
    )

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        params: list[Any] = [str(row["username"])]
        where = "user_kind='mfu' AND recipient_key=%s"
        if since_id > 0:
            where += " AND id>%s"
            params.append(since_id)
        cur.execute(
            f"""
            SELECT id, kind, feature_key, severity, topic_key, content_json,
                   title, body, target_url, sender_label, room_type, room_id,
                   created_at, read_at
              FROM mfu_notifications WHERE {where}
             ORDER BY id {'ASC' if since_id else 'DESC'} LIMIT %s
            """,
            tuple(params + [max(1, min(limit, 200))]),
        )
        items = _serialize_rows(cur.fetchall() or [])
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM mfu_notifications WHERE user_kind='mfu' AND recipient_key=%s AND read_at IS NULL",
            (str(row["username"]),),
        )
        unread = int((cur.fetchone() or {}).get("cnt") or 0)
        return {
            "ok": True,
            "items": items,
            "unread_count": unread,
            "channels": _mfu_notification_channels(),
            "channel_unread": _compute_mfu_channel_unread_counts(str(row["username"])),
        }
    finally:
        cur.close()
        db.close()


@notification_service_bp.get("/desktop/media-hub/api/notifications")
def desktop_notification_list():
    row = verify_notification_token()
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    return jsonify(_desktop_notifications(row, max(int(request.args.get("since_id") or 0), 0), int(request.args.get("limit") or 100)))


@notification_service_bp.post("/desktop/media-hub/api/notifications/<int:notification_id>/read")
def desktop_notification_read(notification_id: int):
    row = verify_notification_token()
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE mfu_notifications SET read_at=COALESCE(read_at, UTC_TIMESTAMP()) WHERE id=%s AND user_kind='mfu' AND recipient_key=%s",
            (notification_id, row["username"]),
        )
        db.commit()
        return jsonify({"ok": True, "updated": bool(cur.rowcount)})
    finally:
        cur.close()
        db.close()


@notification_service_bp.post("/desktop/media-hub/api/notifications/read-all")
def desktop_notification_read_all():
    row = verify_notification_token()
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    from app.external_login_user.notifications import (
        _compute_mfu_channel_unread_counts,
        _mfu_notification_channel_sql,
        _normalize_mfu_notification_channel,
    )
    body = request.get_json(silent=True) or {}
    channel = _normalize_mfu_notification_channel(body.get("channel"))
    channel_sql, channel_params = _mfu_notification_channel_sql(channel)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            f"""
            UPDATE mfu_notifications SET read_at=UTC_TIMESTAMP()
             WHERE user_kind='mfu' AND recipient_key=%s AND read_at IS NULL
               AND {channel_sql}
            """,
            tuple([row["username"], *channel_params]),
        )
        updated = int(cur.rowcount or 0)
        db.commit()
        return jsonify({
            "ok": True,
            "updated": updated,
            "selected_channel": channel,
            "channel_unread": _compute_mfu_channel_unread_counts(str(row["username"])),
        })
    finally:
        cur.close()
        db.close()


@notification_service_bp.post("/desktop/media-hub/api/notifications/revoke")
def desktop_notification_revoke():
    token = _bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    revoke_notification_token(token)
    return jsonify({"ok": True})


@notification_service_bp.post("/desktop/media-hub/api/notifications/mute")
def desktop_notification_mute():
    row = verify_notification_token()
    if not row:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    body = request.get_json(silent=True) or {}
    feature_key = str(body.get("feature_key") or "").strip()[:64]
    if not feature_key:
        return jsonify({"ok": False, "error": "feature_key_required"}), 400
    action = str(body.get("action") or "mute")
    minutes = max(0, min(int(body.get("minutes") or 0), 525600))
    forever = action == "forever"
    mute_until = datetime.utcnow() + timedelta(minutes=minutes) if minutes else None
    if action == "unmute":
        forever = False
        mute_until = None
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mfu_notification_preferences
              (recipient_type, recipient_value, feature_key, muted_forever, mute_until)
            VALUES ('mfu_username', %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE muted_forever=VALUES(muted_forever), mute_until=VALUES(mute_until)
            """,
            (row["username"], feature_key, 1 if forever else 0, mute_until),
        )
        db.commit()
        return jsonify({"ok": True, "feature_key": feature_key, "muted_forever": forever, "mute_until": mute_until.isoformat() if mute_until else None})
    finally:
        cur.close()
        db.close()


@socketio.on("connect", namespace="/media-hub-notifications")
def media_hub_notifications_connect(auth=None):
    auth = auth if isinstance(auth, dict) else {}
    row = verify_notification_token(str(auth.get("notification_token") or auth.get("token") or ""))
    if not row:
        return False
    _socket_tokens[request.sid] = row
    join_room(f"media-hub-notifications:{row['username']}")
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE media_hub_notification_tokens SET last_connected_at=UTC_TIMESTAMP() WHERE id=%s", (row["id"],))
        db.commit()
    finally:
        cur.close()
        db.close()
    # Re-send enough history to repair notifications missed while the Windows
    # application was offline.  Deduplication in the client keeps reconnects safe.
    emit("notification_connected", _desktop_notifications(row, limit=200))
    return True


@socketio.on("notification_heartbeat", namespace="/media-hub-notifications")
def media_hub_notification_heartbeat(_payload=None):
    row = _socket_tokens.get(request.sid)
    if not row:
        disconnect()
        return {"ok": False}
    return {"ok": True, "server_time": datetime.now(timezone.utc).isoformat()}


@socketio.on("notification_received", namespace="/media-hub-notifications")
def media_hub_notification_received(payload=None):
    row = _socket_tokens.get(request.sid)
    if not row:
        return {"ok": False}
    body = payload if isinstance(payload, dict) else {}
    notification_id = int(body.get("notification_id") or 0)
    from app.external_login_user.notifications import _record_notification_delivery
    _record_notification_delivery(
        notification_id=notification_id or None,
        dedup_key=str(body.get("dedup_key") or f"notification:{notification_id}"),
        recipient_type="mfu_username",
        recipient_value=row["username"],
        channel="media_hub",
        status="sent",
        detail=f"device={row['device_uuid']}",
        sent_at=datetime.utcnow(),
    )
    return {"ok": True}


@socketio.on("disconnect", namespace="/media-hub-notifications")
def media_hub_notifications_disconnect():
    _socket_tokens.pop(request.sid, None)


def _source_from_token(token: str) -> dict[str, Any] | None:
    if not token.startswith(SOURCE_TOKEN_PREFIX):
        return None
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM mfu_notification_sources WHERE token_hash=%s LIMIT 1", (_hash_token(token),))
        row = cur.fetchone()
        if not row or not row.get("enabled") or row.get("revoked_at"):
            return None
        return row
    finally:
        cur.close()
        db.close()


def _source_rate_allowed(source: dict[str, Any]) -> bool:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        db.start_transaction()
        cur.execute("SELECT window_start, request_count FROM mfu_notification_ingress_rate WHERE source_id=%s FOR UPDATE", (source["id"],))
        row = cur.fetchone()
        now = datetime.utcnow()
        if not row or not row.get("window_start") or now - row["window_start"] >= timedelta(minutes=1):
            cur.execute(
                "INSERT INTO mfu_notification_ingress_rate (source_id, window_start, request_count) VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE window_start=VALUES(window_start), request_count=1",
                (source["id"], now),
            )
            db.commit()
            return True
        count = int(row.get("request_count") or 0)
        if count >= int(source.get("rate_limit_per_minute") or 60):
            db.rollback()
            return False
        cur.execute("UPDATE mfu_notification_ingress_rate SET request_count=request_count+1 WHERE source_id=%s", (source["id"],))
        db.commit()
        return True
    finally:
        cur.close()
        db.close()


def _audit_ingress(source_id: int | None, mode: str, payload: bytes, status: str, detail: str, notification_id: int | None = None) -> None:
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO mfu_notification_ingress_audit (source_id, remote_ip, mode, payload_sha256, status, detail, notification_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (source_id, str(request.remote_addr or "")[:64], mode[:24], hashlib.sha256(payload).hexdigest(), status[:24], detail[:500], notification_id),
        )
        if source_id:
            field = "accepted_count" if status in {"accepted", "duplicate", "muted"} else "error_count"
            cur.execute(f"UPDATE mfu_notification_sources SET last_seen_at=UTC_TIMESTAMP(), last_ip=%s, {field}={field}+1 WHERE id=%s", (str(request.remote_addr or "")[:64], source_id))
        db.commit()
        cur.close()
        db.close()
    except Exception:
        current_app.logger.warning("notification ingress audit failed", exc_info=True)


def _accept_source_event(source: dict[str, Any], event: dict[str, Any], raw: bytes, mode: str):
    if not _source_rate_allowed(source):
        _audit_ingress(int(source["id"]), mode, raw, "rate_limited", "rate limit exceeded")
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    requested_feature = str(event.get("feature_key") or source["feature_key"]).strip()[:64]
    if requested_feature != str(source["feature_key"]):
        _audit_ingress(int(source["id"]), mode, raw, "rejected", "feature_key_not_allowed")
        return jsonify({"ok": False, "error": "feature_key_not_allowed"}), 403
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dedup = str(event.get("dedup_key") or f"source:{source['id']}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}")[:191]
    result = publish_common_notification(
        recipient_username=str(source.get("recipient_username") or DEFAULT_RECIPIENT),
        feature_key=requested_feature,
        kind=str(event.get("kind") or requested_feature),
        severity=str(event.get("severity") or "info"),
        title=str(event.get("title") or "お知らせ")[:255],
        description=str(event.get("description") or event.get("body") or ""),
        fields=event.get("fields") if isinstance(event.get("fields"), list) else [],
        cards=event.get("cards") if isinstance(event.get("cards"), list) else [],
        lead_text=str(event.get("lead_text") or ""),
        image_url=str(event.get("image_url") or ""),
        footer=str(event.get("footer") or source.get("source_name") or "MFU"),
        target_url=str(event.get("target_url") or "/mfu-notifications"),
        topic_key=str(event.get("topic_key") or ""),
        actions=event.get("actions") if isinstance(event.get("actions"), list) else [],
        dedup_key=dedup,
        source_id=int(source["id"]),
    )
    notification_id = int(result.get("notification_id") or result.get("existing_notification_id") or 0) or None
    status = "muted" if result.get("muted") else ("accepted" if result.get("created") else "duplicate")
    _audit_ingress(int(source["id"]), mode, raw, status, "", notification_id)
    if source.get("discord_enabled") and not result.get("muted"):
        try:
            from app.discord_notifications.service import post_discord_notification
            raw_cards = event.get("cards") if isinstance(event.get("cards"), list) else []
            embeds = []
            for raw_card in raw_cards[:10]:
                if not isinstance(raw_card, dict):
                    continue
                card: dict[str, Any] = {
                    "title": str(raw_card.get("title") or "")[:255],
                    "description": str(raw_card.get("description") or "")[:4000],
                    "color": int(raw_card.get("color") or 0),
                    "fields": raw_card.get("fields") if isinstance(raw_card.get("fields"), list) else [],
                }
                if raw_card.get("footer"):
                    card["footer"] = {"text": str(raw_card.get("footer"))[:255]}
                if raw_card.get("image_url"):
                    card["image"] = {"url": str(raw_card.get("image_url"))[:1000]}
                if str(raw_card.get("url") or "").startswith(("http://", "https://")):
                    card["url"] = str(raw_card.get("url"))[:1000]
                if raw_card.get("timestamp"):
                    card["timestamp"] = str(raw_card.get("timestamp"))[:64]
                embeds.append(card)
            if not embeds:
                discord_fields = event.get("discord_fields") if isinstance(event.get("discord_fields"), list) else event.get("fields")
                embeds = [{
                "title": str(event.get("title") or "お知らせ")[:255],
                "description": str(event.get("description") or event.get("body") or "")[:4000],
                "fields": discord_fields if isinstance(discord_fields, list) else [],
                "footer": {"text": str(event.get("footer") or source.get("source_name") or "MFU")[:255]},
                }]
            discord_payload: dict[str, Any] = {
                "content": str(event.get("lead_text") or "")[:2000],
                "embeds": embeds,
                "allowed_mentions": {"parse": []},
            }
            event_actions = event.get("actions") if isinstance(event.get("actions"), list) else []
            components = [
                {"type": 2, "style": 5, "label": str(row.get("label") or "")[:80], "url": str(row.get("url") or "")[:1000]}
                for row in event_actions[:5]
                if isinstance(row, dict) and row.get("label") and row.get("url")
            ]
            if components:
                discord_payload["components"] = [{"type": 1, "components": components}]
            post_discord_notification(
                requested_feature,
                discord_payload,
                mirror=False,
                params={"with_components": "true"} if components else None,
            )
        except Exception:
            current_app.logger.warning("notification ingress Discord forwarding failed source_id=%s", source["id"], exc_info=True)
    return jsonify({"ok": True, "status": status, "notification_id": notification_id}), 202


@notification_service_bp.post("/api/notification-ingress/discord/<token>")
def ingress_discord(token: str):
    if int(request.content_length or 0) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "payload_too_large"}), 413
    source = _source_from_token(token)
    if not source:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    event = discord_payload_to_event(str(source["feature_key"]), payload)
    event["dedup_key"] = str(payload.get("dedup_key") or "")
    return _accept_source_event(source, event, request.get_data(cache=True), "discord")


@notification_service_bp.post("/api/notification-ingress/v1/events")
def ingress_event():
    if int(request.content_length or 0) > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "payload_too_large"}), 413
    source = _source_from_token(_bearer_token())
    if not source:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    return _accept_source_event(source, event, request.get_data(cache=True), "native")


def _require_admin() -> None:
    if session.get("user") != "admin":
        abort(403)


@notification_service_bp.get("/admin/notification-sources")
def notification_sources_admin():
    _require_admin()
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM mfu_notification_sources ORDER BY id DESC")
    sources = cur.fetchall() or []
    cur.close()
    db.close()
    new_token = session.pop("mfu_notification_source_token", "")
    return render_template_string(
        """
        {% extends "base.html" %}{% block title %}外部通知元{% endblock %}{% block content %}
        <div class="container py-3" style="max-width:1100px"><h1 class="h3">外部通知元</h1>
        <p class="text-muted">ラズパイ・Asterisk・外部スクリプトからMFU共通通知へ送信します。</p>
        {% if new_token %}<div class="alert alert-warning"><strong>このトークンは今だけ表示されます。</strong><br><code>{{ new_token }}</code><br>
        Discord互換URL：<code>{{ request.url_root.rstrip('/') }}/api/notification-ingress/discord/{{ new_token }}</code><br>
        ネイティブAPI：<code>POST {{ request.url_root.rstrip('/') }}/api/notification-ingress/v1/events</code> に <code>Authorization: Bearer {{ new_token }}</code></div>{% endif %}
        <div class="mb-3"><a class="btn btn-outline-secondary" href="{{ url_for('notification_service.notification_settings_admin') }}">共通通知設定</a></div>
        <form method="post" action="{{ url_for('notification_service.notification_source_create') }}" class="card card-body mb-3">
          <div class="row g-2"><div class="col-md-3"><input class="form-control" name="source_name" required placeholder="通知元名"></div>
          <div class="col-md-3"><input class="form-control" name="device_name" placeholder="端末名"></div>
          <div class="col-md-3"><select class="form-select" name="feature_key">{% for key, item in features.items() %}<option value="{{ key }}">{{ item.label }}</option>{% endfor %}</select></div>
          <div class="col-md-2"><input class="form-control" name="recipient_username" value="admin" required></div>
          <div class="col-md-1"><button class="btn btn-primary w-100">追加</button></div></div>
        </form>
        <div class="table-responsive"><table class="table table-striped align-middle"><thead><tr><th>ID</th><th>通知元</th><th>分類</th><th>宛先</th><th>状態</th><th>最終受信</th><th>件数</th><th></th></tr></thead><tbody>
        {% for row in sources %}<tr><td>{{ row.id }}</td><td>{{ row.source_name }}<br><small>{{ row.device_name }}</small></td><td>{{ row.feature_key }}</td><td>{{ row.recipient_username }}</td>
        <td>{{ '有効' if row.enabled and not row.revoked_at else '停止' }}</td><td>{{ row.last_seen_at or '-' }}<br><small>{{ row.last_ip }}</small></td><td>{{ row.accepted_count }} / エラー{{ row.error_count }}</td>
        <td><form method="post" action="{{ url_for('notification_service.notification_source_revoke', source_id=row.id) }}"><button class="btn btn-sm btn-outline-danger">失効</button></form></td></tr>{% endfor %}
        </tbody></table></div></div>{% endblock %}
        """,
        sources=sources,
        features=FEATURE_DEFINITIONS,
        new_token=new_token,
    )


@notification_service_bp.post("/admin/notification-sources/create")
def notification_source_create():
    _require_admin()
    feature_key = str(request.form.get("feature_key") or "")
    if feature_key not in FEATURE_DEFINITIONS:
        abort(400)
    token = SOURCE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO mfu_notification_sources (source_name, device_name, feature_key, recipient_username, token_hash) VALUES (%s, %s, %s, %s, %s)",
            (str(request.form.get("source_name") or "")[:120], str(request.form.get("device_name") or "")[:120], feature_key, str(request.form.get("recipient_username") or DEFAULT_RECIPIENT)[:191], _hash_token(token)),
        )
        db.commit()
    finally:
        cur.close()
        db.close()
    session["mfu_notification_source_token"] = token
    flash("外部通知元を追加しました。", "success")
    return redirect(url_for("notification_service.notification_sources_admin"))


@notification_service_bp.post("/admin/notification-sources/<int:source_id>/revoke")
def notification_source_revoke(source_id: int):
    _require_admin()
    db = get_db()
    cur = db.cursor()
    cur.execute("UPDATE mfu_notification_sources SET enabled=0, revoked_at=UTC_TIMESTAMP() WHERE id=%s", (source_id,))
    db.commit()
    cur.close()
    db.close()
    flash("外部通知元を失効しました。", "success")
    return redirect(url_for("notification_service.notification_sources_admin"))


@notification_service_bp.get("/admin/notification-settings")
def notification_settings_admin():
    _require_admin()
    ensure_notification_service_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM mfu_notification_preferences WHERE recipient_type='mfu_username' AND recipient_value='admin'"
        )
        saved = {str(row["feature_key"]): row for row in (cur.fetchall() or [])}
    finally:
        cur.close()
        db.close()
    rows = []
    for key, definition in FEATURE_DEFINITIONS.items():
        row = dict(saved.get(key) or {})
        row.update(feature_key=key, **definition)
        row.setdefault("media_hub_enabled", 1)
        row.setdefault("web_push_enabled", 1)
        row.setdefault("discord_enabled", 1)
        row.setdefault("sound_enabled", 1)
        rows.append(row)
    return render_template_string(
        """
        {% extends "base.html" %}{% block title %}共通通知設定{% endblock %}{% block content %}
        <div class="container py-3" style="max-width:1200px"><h1 class="h3">共通通知設定</h1>
        <p class="text-muted">Media Hub・Web Push・Discordの配信先を機能ごとに管理します。ミュート中も履歴には保存されます。</p>
        <form method="post" action="{{ url_for('notification_service.notification_settings_save') }}">
        <div class="table-responsive"><table class="table table-striped align-middle"><thead><tr><th>機能</th><th>Media Hub</th><th>Web Push</th><th>Discord</th><th>音</th></tr></thead><tbody>
        {% for row in rows %}<tr><td><strong>{{ row.label }}</strong><br><small>{{ row.description }}</small></td>
        {% for field in ['media_hub_enabled','web_push_enabled','discord_enabled','sound_enabled'] %}<td><input class="form-check-input" type="checkbox" name="{{ row.feature_key }}__{{ field }}" {% if row[field] %}checked{% endif %}></td>{% endfor %}</tr>{% endfor %}
        </tbody></table></div><div class="d-flex gap-2"><button class="btn btn-primary">保存</button><a class="btn btn-outline-secondary" href="{{ url_for('notification_service.notification_sources_admin') }}">外部通知元</a></div></form></div>
        {% endblock %}
        """,
        rows=rows,
    )


@notification_service_bp.post("/admin/notification-settings")
def notification_settings_save():
    _require_admin()
    ensure_notification_service_schema()
    from app.discord_notifications.repository import ensure_discord_notification_schema
    ensure_discord_notification_schema()
    db = get_db()
    cur = db.cursor()
    try:
        for feature_key in FEATURE_DEFINITIONS:
            values = tuple(1 if request.form.get(f"{feature_key}__{field}") else 0 for field in (
                "media_hub_enabled", "web_push_enabled", "discord_enabled", "sound_enabled"
            ))
            cur.execute(
                """
                INSERT INTO mfu_notification_preferences
                  (recipient_type, recipient_value, feature_key, media_hub_enabled, web_push_enabled, discord_enabled, sound_enabled)
                VALUES ('mfu_username', 'admin', %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE media_hub_enabled=VALUES(media_hub_enabled), web_push_enabled=VALUES(web_push_enabled),
                  discord_enabled=VALUES(discord_enabled), sound_enabled=VALUES(sound_enabled)
                """,
                (feature_key, *values),
            )
            cur.execute(
                "UPDATE mfu_discord_notification_settings SET enabled=%s, updated_by='common_notification_settings' WHERE feature_key=%s",
                (values[2], feature_key),
            )
        db.commit()
    finally:
        cur.close()
        db.close()
    flash("共通通知設定を保存しました。", "success")
    return redirect(url_for("notification_service.notification_settings_admin"))
