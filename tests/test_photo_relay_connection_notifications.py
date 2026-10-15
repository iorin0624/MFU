from datetime import datetime, timezone
from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "utils" / "photo_relay_connection_notifications.py"


def _module():
    spec = importlib.util.spec_from_file_location("photo_relay_connection_notifications", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connection_notification_contains_only_requested_details():
    module = _module()
    when = datetime(2026, 10, 16, 3, 4, 5, tzinfo=timezone.utc)
    connected = module.connection_notification_payload(
        device_name="編集用PC", occurred_at=when, ip_address="2001:db8::1", connected=True
    )
    disconnected = module.connection_notification_payload(
        device_name="編集用PC", occurred_at=when, ip_address="2001:db8::1", connected=False
    )
    assert connected["embeds"][0]["title"] == "Windows端末が接続しました"
    assert disconnected["embeds"][0]["title"] == "Windows端末との接続が切れました"
    for payload in (connected, disconnected):
        assert [(item["name"], item["value"]) for item in payload["embeds"][0]["fields"]] == [
            ("接続端末名", "編集用PC"),
            ("日時", "2026/10/16 12:04:05"),
            ("IPアドレス", "2001:db8::1"),
        ]


def test_connection_notification_suppresses_short_outages_and_server_restart():
    module = _module()
    should = module.should_notify_connection
    assert should(was_online=None, recent_disconnect=False, already_connected=False, server_uptime_seconds=10)
    assert should(was_online=False, recent_disconnect=False, already_connected=False, server_uptime_seconds=10)
    assert not should(was_online=True, recent_disconnect=True, already_connected=False, server_uptime_seconds=300)
    assert not should(was_online=True, recent_disconnect=False, already_connected=True, server_uptime_seconds=300)
    assert not should(was_online=True, recent_disconnect=False, already_connected=False, server_uptime_seconds=30)
    assert should(was_online=True, recent_disconnect=False, already_connected=False, server_uptime_seconds=300)


def test_server_routes_connection_events_to_common_notification_without_desktop_popup():
    server = (ROOT / "utils" / "photo_relay.py").read_text(encoding="utf-8")
    common = (ROOT / "utils" / "notification_service.py").read_text(encoding="utf-8")
    features = (ROOT / "discord_notifications" / "repository.py").read_text(encoding="utf-8")
    assert '"windows_device_connection"' in server
    assert "media_hub_enabled=False" in server
    assert "DISCONNECT_GRACE_SECONDS" in server
    assert "photo_relay_connection_state" in server
    assert "photo_relay_socket_sessions" in server
    assert "lease_until" in server
    assert '"media_hub_enabled": desktop_enabled' in common
    assert '"windows_device_connection", {"label": "Windows端末接続"' in features
