from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]


def test_media_hub_keeps_popups_but_opens_web_center_for_list():
    source = (ROOT / "tools/mfu_media_hub/main.py").read_text(encoding="utf-8")
    assert '"/desktop/media-hub/api/notifications/unread-count"' in source
    assert 'webbrowser.open(self.api.url("/mfu-notifications"))' in source
    assert "NotificationCenterDialog" not in source
    assert "class NotificationPopup(QDialog)" in source
    assert "self.photo_receiver.notification_received.connect(self._notification_received)" in source
    assert "notification_snapshot" not in source


def test_photo_receiver_receives_new_notifications_without_history():
    source = (ROOT / "tools/mfu_photo_relay/main.py").read_text(encoding="utf-8")
    assert 'namespaces.append("/media-hub-notifications")' in source
    assert '@self.sio.on("notification_new"' in source
    assert '"popup_only": True' in source
    assert '"notification_connected"' not in source


def test_unread_endpoint_returns_count_only():
    source = (ROOT / "utils/notification_service.py").read_text(encoding="utf-8")
    route = source.split('def desktop_notification_unread_count():', 1)[1].split('\n\n@notification_service_bp.', 1)[0]
    assert "verify_notification_token()" in route
    assert "COUNT(*) AS cnt" in route
    assert '"unread_count": count' in route
    assert "_desktop_notifications(" not in route


def test_popup_only_socket_does_not_send_history():
    source = (ROOT / "utils/notification_service.py").read_text(encoding="utf-8")
    connect = source.split('def media_hub_notifications_connect(auth=None):', 1)[1].split('\n\n@socketio.on(', 1)[0]
    assert 'if not auth.get("popup_only"):' in connect
    assert 'emit("notification_connected", _desktop_notifications(row, limit=200))' in connect


def test_new_notification_popup_can_be_shown_without_local_list():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    from tools.mfu_media_hub.main import NotificationPopup

    class Controller:
        def __init__(self):
            self.opened = []

        def _open_notification_center(self):
            self.opened.append("web")

        def _open_notification(self, _item):
            self.opened.append("detail")

        def _append_notification_actions(self, _card, _item):
            pass

        def _mute_notification(self, _item, **_kwargs):
            pass

    app = QApplication.instance() or QApplication([])
    controller = Controller()
    popup = NotificationPopup(controller, {
        "id": 1, "title": "通知テスト", "body": "本文", "severity": "critical",
        "target_url": "https://example.com/detail",
    })
    popup.show()
    app.processEvents()
    assert popup.isVisible()
    assert popup.windowType() == Qt.Window
    buttons = {button.text(): button for button in popup.findChildren(QPushButton)}
    QTest.mouseClick(buttons["Webで開く"], Qt.LeftButton)
    QTest.mouseClick(buttons["詳細を見る"], Qt.LeftButton)
    assert controller.opened == ["web", "detail"]
    popup.close()


def test_new_socket_event_reaches_popup_signal():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from tools.mfu_photo_relay.main import ReceiverThread

    receiver = ReceiverThread({"base_url": "https://mfu.iori0624.jp"}, "photo", "media", "notification")
    seen = []
    receiver.notification_received.connect(seen.append)
    receiver.sio.handlers["/media-hub-notifications"]["notification_new"](
        {"item": {"id": 42, "title": "新着テスト"}}
    )
    assert seen == [{"id": 42, "title": "新着テスト"}]
