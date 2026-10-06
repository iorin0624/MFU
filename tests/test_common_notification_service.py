from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "utils" / "notification_service.py"
NOTIFICATIONS = ROOT / "external_login_user" / "notifications.py"
DISCORD = ROOT / "discord_notifications" / "service.py"
SENDER = ROOT / "tools" / "mfu_notification_sender" / "mfu_notify.py"


def test_common_notification_schema_keeps_rich_content_and_muted_history():
    source = NOTIFICATIONS.read_text(encoding="utf-8")
    for column in ("feature_key", "severity", "topic_key", "content_json", "source_id", "muted_at"):
        assert column in source
    assert "read_at" in source
    assert "if inserted and muted_at is None" in source


def test_external_ingress_supports_native_and_discord_compatible_requests():
    source = SERVICE.read_text(encoding="utf-8")
    assert '"/api/notification-ingress/discord/<token>"' in source
    assert '"/api/notification-ingress/v1/events"' in source
    assert "_source_rate_allowed" in source
    assert "mfu_notification_ingress_audit" in source


def test_existing_discord_delivery_is_mirrored_without_creating_a_loop():
    service = DISCORD.read_text(encoding="utf-8")
    common = SERVICE.read_text(encoding="utf-8")
    assert "mirror_discord_payload(feature_key, payload)" in service
    assert "mirror=False" in common


def test_linux_sender_has_durable_retry_queue():
    source = SENDER.read_text(encoding="utf-8")
    assert "sqlite3" in source
    assert "pending_notifications" in source
    assert 'parser.add_argument("--drain"' in source
