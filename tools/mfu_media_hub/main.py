from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

from tools.mfu_media_clipboard import main as clipboard
from tools.mfu_photo_relay.core import load_settings as load_photo_settings, save_settings as save_photo_settings
from tools.mfu_photo_relay.main import (
    ApiClient as PhotoApiClient,
    ReceiverThread,
    SettingsDialog,
    clear_token as clear_legacy_photo_token,
    load_token as load_legacy_photo_token,
    set_auto_start as set_legacy_photo_startup,
)


APP_NAME = "MFU Media Hub"
APP_VERSION = "2.2.7"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MFU" / APP_NAME
TOKEN_PATH = APP_DIR / "tokens.bin"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def save_tokens(photo_token: str, media_token: str, notification_token: str = "") -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "photo_token": photo_token,
        "media_token": media_token,
        "notification_token": notification_token,
    }).encode("utf-8")
    raw, _buffer = _blob(payload)
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(raw), APP_NAME, None, None, None, 0, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        temporary = TOKEN_PATH.with_suffix(".tmp")
        temporary.write_bytes(ctypes.string_at(output.pbData, output.cbData))
        os.replace(temporary, TOKEN_PATH)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def load_tokens() -> dict[str, str]:
    try:
        encrypted = TOKEN_PATH.read_bytes()
        raw, _buffer = _blob(encrypted)
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(raw), None, None, None, None, 0, ctypes.byref(output)):
            return {}
        try:
            data = json.loads(ctypes.string_at(output.pbData, output.cbData).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
    except (OSError, ValueError, UnicodeError):
        return {}


class HubLoginDialog(QDialog):
    tokens_received = Signal(str, str, str)

    def __init__(self, api, photo_settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.photo_settings = photo_settings
        self.state = secrets.token_urlsafe(24)
        self.received: dict[str, str] = {}
        self.server: HTTPServer | None = None
        self.setWindowTitle(f"{APP_NAME} ログイン")
        layout = QVBoxLayout(self)
        label = QLabel("ChromeでMFUにログインし、写真転送・Media Clipboard・Web通知センターの未読数を一度に許可してください。")
        label.setWordWrap(True)
        layout.addWidget(label)
        button = QPushButton("Chromeでログイン")
        button.clicked.connect(self.open_browser)
        layout.addWidget(button)
        cancel = QPushButton("キャンセル")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)
        self._start_server()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(250)
        QTimer.singleShot(150, self.open_browser)

    def _start_server(self) -> None:
        dialog = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                values = parse_qs(parsed.query)
                state = (values.get("state") or [""])[0]
                photo = (values.get("photo_token") or [""])[0]
                media = (values.get("media_token") or [""])[0]
                notification = (values.get("notification_token") or [""])[0]
                ok = parsed.path == "/callback" and state == dialog.state and bool(photo and media and notification)
                if ok:
                    dialog.received = {
                        "photo_token": photo,
                        "media_token": media,
                        "notification_token": notification,
                    }
                body = ("MFU Media Hub login completed." if ok else "MFU Media Hub login failed.").encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def open_browser(self) -> None:
        if not self.server:
            return
        callback_url = f"http://127.0.0.1:{self.server.server_port}/callback"
        query = urlencode({
            "callback": callback_url,
            "state": self.state,
            "device_uuid": self.photo_settings["device_uuid"],
            "device_name": self.photo_settings["device_name"],
        })
        webbrowser.open(self.api.url(f"/desktop/media-hub/login/start?{query}"))

    def _poll(self) -> None:
        if not self.received:
            return
        self.tokens_received.emit(
            self.received["photo_token"],
            self.received["media_token"],
            self.received["notification_token"],
        )
        self.accept()

    def done(self, result: int) -> None:
        self.timer.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        super().done(result)


class MediaHubApp(clipboard.MediaClipboardApp):
    unread_count_loaded = Signal(int)

    def __init__(self, qt_app: QApplication) -> None:
        clipboard.APP_NAME = APP_NAME
        super().__init__(qt_app)
        self.photo_settings = load_photo_settings()
        self.photo_receiver: ReceiverThread | None = None
        self.last_saved_path = ""
        self._notification_count_inflight = False
        tokens = load_tokens()
        if not tokens:
            legacy_photo = load_legacy_photo_token()
            legacy_media = self.api.token
            if legacy_photo and legacy_media:
                save_tokens(legacy_photo, legacy_media)
                tokens = {"photo_token": legacy_photo, "media_token": legacy_media}
        self.photo_token = str(tokens.get("photo_token") or "")
        self.media_token = str(tokens.get("media_token") or "")
        self.notification_token = str(tokens.get("notification_token") or "")
        if self.media_token:
            self.api.set_token(self.media_token, persist=False)
        if self.photo_token and self.media_token and not self.notification_token:
            self._upgrade_notification_token()
        self._extend_menu()
        self.unread_count_loaded.connect(self._update_notification_count)
        self.notification_count_timer = QTimer(self)
        self.notification_count_timer.setInterval(15000)
        self.notification_count_timer.timeout.connect(self._poll_notification_count)
        self.notification_count_timer.start()
        QTimer.singleShot(1000, self._poll_notification_count)
        if self.photo_token and self.media_token:
            self._start_receiver()
            self._disable_legacy_startups()
            if not self.notification_token:
                QTimer.singleShot(
                    1500,
                    lambda: self.show_notification(
                        "Web通知センターの未読数を表示するには、タスクトレイから『3機能をまとめてログイン』を一度実行してください。",
                        QSystemTrayIcon.Warning,
                        10000,
                    ),
                )
        else:
            self._set_connection_status("red", "未ログイン")

    def _upgrade_notification_token(self) -> None:
        try:
            import requests
            response = requests.post(
                self.api.url("/desktop/media-hub/api/notification-token"),
                headers={"Authorization": f"Bearer {self.media_token}"},
                json={
                    "device_uuid": self.photo_settings.get("device_uuid"),
                    "device_name": self.photo_settings.get("device_name") or "Windows PC",
                },
                timeout=15,
            )
            response.raise_for_status()
            token = str((response.json() or {}).get("notification_token") or "")
            if token:
                self.notification_token = token
                save_tokens(self.photo_token, self.media_token, token)
        except Exception:
            # The existing two functions remain available; the tray warning
            # guides the user to re-login if automatic migration is unavailable.
            pass

    def _extend_menu(self) -> None:
        menu = self.tray.contextMenu()
        menu.addSeparator()
        self.connection_action = QAction("🔴 未ログイン", menu)
        self.connection_action.setEnabled(False)
        menu.addAction(self.connection_action)
        self.notification_center_action = QAction("Web通知センター（未読0件）", menu)
        self.notification_center_action.triggered.connect(self._open_notification_center)
        menu.addAction(self.notification_center_action)
        settings = QAction("写真転送設定", menu)
        settings.triggered.connect(self._open_photo_settings)
        menu.addAction(settings)
        login = QAction("3機能をまとめてログイン", menu)
        login.triggered.connect(self._open_login)
        menu.addAction(login)
        version = QAction(f"{APP_NAME} {APP_VERSION}", menu)
        version.setEnabled(False)
        menu.addAction(version)

    def _set_connection_status(self, color: str, detail: str) -> None:
        icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(color, "🔴")
        self.connection_action.setText(f"{icon} {detail}")
        if not self._batch_active():
            self.tray.setToolTip(f"{APP_NAME}\n{detail}")

    def _start_receiver(self) -> None:
        if self.photo_receiver and self.photo_receiver.isRunning():
            self.photo_receiver.stop()
            self.photo_receiver.wait(3000)
        self.photo_receiver = ReceiverThread(
            self.photo_settings,
            self.photo_token,
            self.media_token,
        )
        self.photo_receiver.status_changed.connect(self._set_connection_status)
        self.photo_receiver.file_saved.connect(self._photo_saved)
        self.photo_receiver.error.connect(self._receiver_error)
        self.photo_receiver.authentication_failed.connect(lambda: self._set_connection_status("red", "再ログインが必要です"))
        self.photo_receiver.media_progress.connect(self.api.handle_websocket_progress)
        self.api.websocket_rpc = self.photo_receiver.media_call
        self.photo_receiver.start()

    def _photo_saved(self, path: str) -> None:
        self.last_saved_path = path
        self.show_notification(
            f"写真を保存しました。\n{path}\nクリックして保存先を開きます。",
            QSystemTrayIcon.Information,
            8000,
            action=lambda value=path: self._open_saved_folder(value),
        )

    @staticmethod
    def _open_saved_folder(path: str) -> None:
        folder = str(Path(path).parent)
        if os.name == "nt":
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder])

    def _receiver_error(self, message: str) -> None:
        self.show_notification(f"通信または写真転送に失敗しました。\n{message}", QSystemTrayIcon.Warning, 8000)

    def _open_login(self) -> None:
        dialog = HubLoginDialog(self.api, self.photo_settings)
        dialog.tokens_received.connect(self._finish_hub_login)
        dialog.exec()

    def _finish_hub_login(self, photo_token: str, media_token: str, notification_token: str) -> None:
        try:
            save_tokens(photo_token, media_token, notification_token)
            self.photo_token, self.media_token = photo_token, media_token
            self.notification_token = notification_token
            self.api.set_token(media_token, persist=False)
            details = PhotoApiClient(self.photo_settings, photo_token).get_session()
            self.photo_settings["device_name"] = details.get("device_name") or self.photo_settings["device_name"]
            save_photo_settings(self.photo_settings)
            self._start_receiver()
            self._disable_legacy_startups()
            self._poll_notification_count()
            QMessageBox.information(None, APP_NAME, "写真転送、Media Clipboard、Web通知センターの未読数にログインしました。")
        except Exception as exc:
            QMessageBox.warning(None, APP_NAME, f"ログイン情報を保存できませんでした。\n{exc}")

    def _open_photo_settings(self) -> None:
        dialog = SettingsDialog(self.photo_settings)
        if dialog.exec() != QDialog.Accepted:
            return
        self.photo_settings = dialog.result_values()
        save_photo_settings(self.photo_settings)
        clipboard._set_startup_enabled(bool(self.photo_settings.get("auto_start")))
        self.startup_action.blockSignals(True)
        self.startup_action.setChecked(bool(self.photo_settings.get("auto_start")))
        self.startup_action.blockSignals(False)
        set_legacy_photo_startup(False)
        if self.photo_token:
            try:
                PhotoApiClient(self.photo_settings, self.photo_token).post(
                    "/desktop/photo-relay/api/device", {"device_name": self.photo_settings["device_name"]}
                )
            finally:
                self._start_receiver()

    def _disable_legacy_startups(self) -> None:
        try:
            set_legacy_photo_startup(False)
        except Exception:
            pass
        try:
            import winreg
            enabled = clipboard._startup_enabled()
            if not enabled:
                return
            if getattr(sys, "frozen", False):
                command = subprocess.list2cmdline([str(Path(sys.executable).resolve())])
            else:
                command = subprocess.list2cmdline([str(Path(sys.executable).resolve()), str(Path(__file__).resolve())])
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, clipboard.STARTUP_REGISTRY_PATH) as key:
                winreg.SetValueEx(key, clipboard.STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
        except Exception:
            pass

    def _logout(self) -> None:
        if self.photo_receiver:
            self.photo_receiver.stop()
            self.photo_receiver.wait(3000)
            self.photo_receiver = None
        for token, path in (
            (self.photo_token, "/desktop/photo-relay/api/revoke"),
            (self.media_token, "/desktop/media-clipboard/api/revoke"),
            (self.notification_token, "/desktop/media-hub/api/notifications/revoke"),
        ):
            if not token:
                continue
            try:
                import requests
                requests.post(self.api.url(path), headers={"Authorization": f"Bearer {token}"}, json={}, timeout=15)
            except Exception:
                pass
        TOKEN_PATH.unlink(missing_ok=True)
        clear_legacy_photo_token()
        self.api.clear_cookies()
        self.photo_token = self.media_token = self.notification_token = ""
        self.api.websocket_rpc = None
        self._update_notification_count(0)
        self._set_connection_status("red", "未ログイン")
        QMessageBox.information(None, APP_NAME, "この端末のログイン情報を削除しました。")

    def _poll_notification_count(self) -> None:
        if not self.notification_token or self._notification_count_inflight:
            return
        self._notification_count_inflight = True
        token = self.notification_token
        threading.Thread(target=self._fetch_notification_count, args=(token,), daemon=True).start()

    def _fetch_notification_count(self, token: str) -> None:
        try:
            import requests
            response = requests.get(
                self.api.url("/desktop/media-hub/api/notifications/unread-count"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            response.raise_for_status()
            count = max(0, int((response.json() or {}).get("unread_count") or 0))
            if token == self.notification_token:
                self.unread_count_loaded.emit(count)
        except Exception:
            logging.warning("notification unread count request failed", exc_info=True)
        finally:
            self._notification_count_inflight = False

    def _update_notification_count(self, count: int) -> None:
        self.notification_center_action.setText(f"Web通知センター（未読{count}件）")

    def _open_notification_center(self) -> None:
        self._poll_notification_count()
        if not webbrowser.open(self.api.url("/mfu-notifications")):
            self.show_notification("Web通知センターを開けませんでした。", QSystemTrayIcon.Warning, 5000)


def main() -> int:
    clipboard.setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "タスクトレイが利用できません。")
        return 1
    controller = MediaHubApp(app)
    app._mfu_media_hub_controller = controller
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
