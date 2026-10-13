from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_media_hub_only_fetches_unread_count_and_opens_web_center():
    source = (ROOT / "tools/mfu_media_hub/main.py").read_text(encoding="utf-8")
    assert '"/desktop/media-hub/api/notifications/unread-count"' in source
    assert 'webbrowser.open(self.api.url("/mfu-notifications"))' in source
    assert "NotificationCenterDialog" not in source
    assert "NotificationPopup" not in source
    assert "notification_snapshot" not in source
    assert "notification_received" not in source


def test_photo_receiver_no_longer_subscribes_to_notification_content():
    source = (ROOT / "tools/mfu_photo_relay/main.py").read_text(encoding="utf-8")
    assert 'namespaces.append("/media-hub-notifications")' not in source
    assert '@self.sio.on("notification_new"' not in source


def test_unread_endpoint_returns_count_only():
    source = (ROOT / "utils/notification_service.py").read_text(encoding="utf-8")
    route = source.split('def desktop_notification_unread_count():', 1)[1].split('\n\n@notification_service_bp.', 1)[0]
    assert "verify_notification_token()" in route
    assert "COUNT(*) AS cnt" in route
    assert '"unread_count": count' in route
    assert "_desktop_notifications(" not in route
