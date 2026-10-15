"""Pure helpers for MFU Media Hub connection-state notifications."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


DISCONNECT_GRACE_SECONDS = 60
SERVER_START_GRACE_SECONDS = 120


def should_notify_connection(
    *,
    was_online: bool | None,
    recent_disconnect: bool,
    already_connected: bool,
    server_uptime_seconds: float,
) -> bool:
    """Ignore duplicate sockets, brief outages and reconnects during server startup."""
    if recent_disconnect or already_connected:
        return False
    if was_online is None or not was_online:
        return True
    return server_uptime_seconds >= SERVER_START_GRACE_SECONDS


def connection_notification_payload(
    *, device_name: str, occurred_at: datetime, ip_address: str, connected: bool
) -> dict:
    local_time = occurred_at.astimezone(ZoneInfo("Asia/Tokyo"))
    title = "Windows端末が接続しました" if connected else "Windows端末との接続が切れました"
    return {
        "kind": "windows_device_connection",
        "embeds": [{
            "title": title,
            "color": 0x2ECC71 if connected else 0xF1C40F,
            "fields": [
                {"name": "接続端末名", "value": device_name, "inline": False},
                {"name": "日時", "value": local_time.strftime("%Y/%m/%d %H:%M:%S"), "inline": False},
                {"name": "IPアドレス", "value": ip_address or "不明", "inline": False},
            ],
        }],
    }
