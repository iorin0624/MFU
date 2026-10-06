from __future__ import annotations

import ctypes
import html
import json
import os
import re
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

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
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
APP_VERSION = "2.2.0"
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
        label = QLabel("ChromeでMFUにログインし、写真転送・Media Clipboard・通知センターを一度に許可してください。")
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


SEVERITY_COLORS = {
    "info": "#2563eb",
    "success": "#16a34a",
    "warning": "#d97706",
    "error": "#dc2626",
    "critical": "#991b1b",
}


def _discord_rich_text(value: object) -> str:
    text = html.escape(str(value or ""))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a style="color:#60a5fa" href="\2">\1</a>',
        text,
    )
    return text.replace("\n", "<br>")


def _rich_label(value: object, *, color: str = "#f8fafc") -> QLabel:
    label = QLabel(_discord_rich_text(value))
    label.setTextFormat(Qt.RichText)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    label.setOpenExternalLinks(True)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{color};")
    return label


class NotificationCard(QFrame):
    def __init__(self, item: dict, *, compact: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        severity = str(item.get("severity") or "info")
        color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["info"])
        self.setObjectName("NotificationEntry")
        self.setStyleSheet(
            f"QFrame#NotificationEntry{{background:#111827;border:1px solid #374151;border-left:5px solid {color};border-radius:8px;}}"
            "QFrame#NotificationEntry QLabel{border:0;background:transparent;color:#f8fafc;} QFrame#NotificationEntry QPushButton{padding:5px 10px;}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        cards = content.get("cards") if isinstance(content.get("cards"), list) else []
        if cards and not compact:
            lead_text = str(content.get("lead_text") or "")
            if lead_text:
                layout.addWidget(_rich_label(lead_text))
            for card_data in cards[:10]:
                if not isinstance(card_data, dict):
                    continue
                card_color_value = int(card_data.get("color") or 0)
                card_color = f"#{min(card_color_value, 0xFFFFFF):06x}" if card_color_value > 0 else color
                embed = QFrame()
                embed.setObjectName("NotificationEmbed")
                embed.setStyleSheet(
                    f"QFrame#NotificationEmbed{{background:#172033;border:1px solid #374151;border-left:4px solid {card_color};border-radius:6px;}}"
                )
                embed_layout = QVBoxLayout(embed)
                embed_layout.setContentsMargins(10, 8, 10, 8)
                card_title = str(card_data.get("title") or "")
                if card_title:
                    title = _rich_label(card_title)
                    title.setStyleSheet("font-weight:700;font-size:14px;color:#f8fafc;")
                    embed_layout.addWidget(title)
                description = str(card_data.get("description") or "")
                if description:
                    embed_layout.addWidget(_rich_label(description))
                fields = card_data.get("fields") if isinstance(card_data.get("fields"), list) else []
                for row in fields[:25]:
                    if not isinstance(row, dict):
                        continue
                    field = QLabel(
                        f"<b>{html.escape(str(row.get('name') or ''))}</b><br>{_discord_rich_text(row.get('value') or '')}"
                    )
                    field.setTextFormat(Qt.RichText)
                    field.setTextInteractionFlags(Qt.TextBrowserInteraction)
                    field.setOpenExternalLinks(True)
                    field.setWordWrap(True)
                    field.setStyleSheet("color:#d1d5db;")
                    embed_layout.addWidget(field)
                footer = str(card_data.get("footer") or "")
                if footer:
                    footer_label = _rich_label(footer, color="#9ca3af")
                    footer_label.setStyleSheet("color:#9ca3af;font-size:11px;")
                    embed_layout.addWidget(footer_label)
                layout.addWidget(embed)
        else:
            title = _rich_label(str(item.get("title") or "お知らせ"))
            title.setStyleSheet("font-weight:700;font-size:14px;color:#f8fafc;")
            layout.addWidget(title)
            body = str(item.get("body") or "")
            if body:
                layout.addWidget(_rich_label(body))
            fields = content.get("fields") if isinstance(content.get("fields"), list) else []
            if fields and not compact:
                field_text = "\n".join(
                    f"{str(row.get('name') or '')}: {str(row.get('value') or '')}"
                    for row in fields[:12] if isinstance(row, dict)
                )
                layout.addWidget(_rich_label(field_text, color="#d1d5db"))
        feature_key = str(item.get("feature_key") or "general")
        feature_label = str(item.get("feature_label") or feature_key)
        meta = QLabel(f"# {feature_label}  {str(item.get('created_at') or '')}")
        meta.setStyleSheet("color:#9ca3af;font-size:11px;")
        layout.addWidget(meta)
        self.actions = QHBoxLayout()
        self.actions.addStretch(1)
        layout.addLayout(self.actions)


class NotificationPopup(QDialog):
    def __init__(self, controller, item: dict) -> None:
        super().__init__()
        self.controller = controller
        self.item = item
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFixedWidth(390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = NotificationCard(item)
        layout.addWidget(card)
        detail = QPushButton("詳細を見る")
        detail.clicked.connect(lambda: controller._open_notification(item))
        card.actions.addWidget(detail)
        controller._append_notification_actions(card, item)
        mute = QPushButton("ミュート")
        menu = QMenu(mute)
        for label, minutes in (("1時間", 60), ("3時間", 180), ("8時間", 480), ("24時間", 1440), ("3日間", 4320)):
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, value=minutes: controller._mute_notification(item, minutes=value))
        forever = menu.addAction("無期限")
        forever.triggered.connect(lambda: controller._mute_notification(item, forever=True))
        mute.setMenu(menu)
        card.actions.addWidget(mute)
        close = QPushButton("閉じる")
        close.clicked.connect(self.close)
        card.actions.addWidget(close)
        severity = str(item.get("severity") or "info")
        if severity not in {"error", "critical"}:
            QTimer.singleShot(20000 if severity == "warning" else 10000, self.close)


class NotificationCenterDialog(QDialog):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.setWindowTitle("MFU 通知センター")
        self.resize(820, 720)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("すべて", "all")
        self.mode.addItem("未読", "unread")
        self.mode.addItem("重要", "important")
        self.feature = QComboBox()
        self.feature.addItem("すべての機能", "")
        self.search = QLineEdit()
        self.search.setPlaceholderText("通知を検索")
        read_all = QPushButton("すべて既読")
        read_all.clicked.connect(controller._mark_all_notifications_read)
        self.read_channel = QPushButton("このチャンネルを既読")
        self.read_channel.clicked.connect(self._mark_current_channel_read)
        top.addWidget(self.mode)
        top.addWidget(self.feature)
        top.addWidget(self.search, 1)
        top.addWidget(self.read_channel)
        top.addWidget(read_all)
        layout.addLayout(top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QWidget()
        self.cards = QVBoxLayout(self.holder)
        self.cards.addStretch(1)
        self.scroll.setWidget(self.holder)
        layout.addWidget(self.scroll, 1)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.feature.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.refresh_features()
        self.refresh()

    def _mark_current_channel_read(self) -> None:
        channel = str(self.feature.currentData() or "all")
        self.controller._mark_notification_channel_read(channel)

    def refresh_features(self) -> None:
        selected = self.feature.currentData()
        configured = list(self.controller.notification_channels)
        configured_keys = {str(row.get("key") or "") for row in configured}
        unknown = {
            str(row.get("feature_key") or "general")
            for row in self.controller.notifications
            if str(row.get("feature_key") or "general") not in configured_keys
        }
        self.feature.blockSignals(True)
        self.feature.clear()
        self.feature.addItem("すべてのチャンネル", "")
        previous_group = ""
        for channel in configured:
            key = str(channel.get("key") or "")
            if not key or key == "other":
                continue
            group = str(channel.get("group") or "その他")
            if group != previous_group:
                self.feature.insertSeparator(self.feature.count())
                self.feature.addItem(f"── {group} ──")
                group_index = self.feature.count() - 1
                item = self.feature.model().item(group_index)
                if item is not None:
                    item.setEnabled(False)
                previous_group = group
            count = int(self.controller.notification_channel_unread.get(key) or 0)
            suffix = f"（未読{count}件）" if count else ""
            self.feature.addItem(f"# {str(channel.get('label') or key)}{suffix}", key)
        if unknown or any(str(row.get("key") or "") == "other" for row in configured):
            count = int(self.controller.notification_channel_unread.get("other") or 0)
            suffix = f"（未読{count}件）" if count else ""
            self.feature.addItem(f"# その他{suffix}", "other")
        index = self.feature.findData(selected)
        self.feature.setCurrentIndex(max(index, 0))
        self.feature.blockSignals(False)
        self.read_channel.setEnabled(bool(self.feature.currentData()))

    def refresh(self) -> None:
        while self.cards.count() > 1:
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        mode = str(self.mode.currentData() or "all")
        feature = str(self.feature.currentData() or "")
        self.read_channel.setEnabled(bool(feature))
        query = self.search.text().strip().lower()
        for row in self.controller.notifications:
            if mode == "unread" and row.get("is_read"):
                continue
            if mode == "important" and row.get("severity") not in {"warning", "error", "critical"}:
                continue
            row_feature = str(row.get("feature_key") or "general")
            if feature == "other" and row_feature in {
                str(item.get("key") or "")
                for item in self.controller.notification_channels
                if str(item.get("key") or "") != "other"
            }:
                continue
            if feature and feature != "other" and row_feature != feature:
                continue
            haystack = f"{row.get('title','')} {row.get('body','')} {row.get('feature_key','')}".lower()
            if query and query not in haystack:
                continue
            card = NotificationCard(row)
            detail = QPushButton("詳細を見る")
            detail.clicked.connect(lambda _=False, value=row: self.controller._open_notification(value))
            card.actions.addWidget(detail)
            self.controller._append_notification_actions(card, row)
            if not row.get("is_read"):
                read = QPushButton("既読")
                read.clicked.connect(lambda _=False, value=row: self.controller._mark_notification_read(value))
                card.actions.addWidget(read)
            self.cards.insertWidget(self.cards.count() - 1, card)


class MediaHubApp(clipboard.MediaClipboardApp):
    def __init__(self, qt_app: QApplication) -> None:
        clipboard.APP_NAME = APP_NAME
        super().__init__(qt_app)
        self.photo_settings = load_photo_settings()
        self.photo_receiver: ReceiverThread | None = None
        self.last_saved_path = ""
        self.notifications: list[dict] = []
        self.notification_channels: list[dict] = []
        self.notification_channel_unread: dict[str, int] = {}
        self.notification_popups: list[NotificationPopup] = []
        self.notification_center: NotificationCenterDialog | None = None
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
        if self.photo_token and self.media_token:
            self._start_receiver()
            self._disable_legacy_startups()
            if not self.notification_token:
                QTimer.singleShot(
                    1500,
                    lambda: self.show_notification(
                        "通知センターを有効にするため、タスクトレイから『3機能をまとめてログイン』を一度実行してください。",
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
        self.notification_center_action = QAction("通知センター（未読0件）", menu)
        self.notification_center_action.triggered.connect(self._open_notification_center)
        menu.addAction(self.notification_center_action)
        read_all = QAction("通知をすべて既読", menu)
        read_all.triggered.connect(self._mark_all_notifications_read)
        menu.addAction(read_all)
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
            self.notification_token,
        )
        self.photo_receiver.status_changed.connect(self._set_connection_status)
        self.photo_receiver.file_saved.connect(self._photo_saved)
        self.photo_receiver.error.connect(self._receiver_error)
        self.photo_receiver.authentication_failed.connect(lambda: self._set_connection_status("red", "再ログインが必要です"))
        self.photo_receiver.media_progress.connect(self.api.handle_websocket_progress)
        self.photo_receiver.notification_received.connect(self._notification_received)
        self.photo_receiver.notification_snapshot.connect(self._notification_snapshot)
        self.photo_receiver.notification_unread.connect(
            lambda payload: self._update_notification_count(payload.get("count"))
        )
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
            QMessageBox.information(None, APP_NAME, "写真転送、Media Clipboard、通知センターにログインしました。")
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
        self._set_connection_status("red", "未ログイン")
        QMessageBox.information(None, APP_NAME, "この端末のログイン情報を削除しました。")

    def _notification_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.notification_token}"}

    def _notification_snapshot(self, payload: dict) -> None:
        channels = payload.get("channels") if isinstance(payload, dict) else []
        if isinstance(channels, list):
            self.notification_channels = [dict(row) for row in channels if isinstance(row, dict)]
        counts = payload.get("channel_unread") if isinstance(payload, dict) else {}
        if isinstance(counts, dict):
            self.notification_channel_unread = {str(key): int(value or 0) for key, value in counts.items()}
        items = payload.get("items") if isinstance(payload, dict) else []
        for item in reversed(items or []):
            self._store_notification(item, popup=False)
        self._update_notification_count(payload.get("unread_count") if isinstance(payload, dict) else None)

    def _notification_received(self, item: dict) -> None:
        if isinstance(item, dict) and not item.get("is_read"):
            feature_key = str(item.get("feature_key") or "general")
            known = {str(row.get("key") or "") for row in self.notification_channels}
            channel_key = feature_key if feature_key in known and feature_key != "other" else "other"
            self.notification_channel_unread[channel_key] = int(self.notification_channel_unread.get(channel_key) or 0) + 1
        self._store_notification(item, popup=True)

    def _store_notification(self, item: dict, *, popup: bool) -> None:
        if not isinstance(item, dict):
            return
        notification_id = int(item.get("id") or 0)
        self.notifications = [row for row in self.notifications if int(row.get("id") or 0) != notification_id]
        stored = dict(item)
        feature_key = str(stored.get("feature_key") or "general")
        labels = {
            str(row.get("key") or ""): str(row.get("label") or "")
            for row in self.notification_channels
        }
        stored["feature_label"] = labels.get(feature_key) or ("その他" if labels else feature_key)
        self.notifications.insert(0, stored)
        self.notifications = self.notifications[:500]
        self._update_notification_count()
        if self.notification_center:
            self.notification_center.refresh_features()
            self.notification_center.refresh()
        content = stored.get("content") if isinstance(stored.get("content"), dict) else {}
        if popup and content.get("media_hub_enabled", True):
            self._show_notification_popup(stored)

    def _update_notification_count(self, explicit: int | None = None) -> None:
        unread = int(explicit) if explicit is not None else sum(1 for row in self.notifications if not row.get("is_read"))
        if hasattr(self, "notification_center_action"):
            self.notification_center_action.setText(f"通知センター（未読{unread}件）")

    def _show_notification_popup(self, item: dict) -> None:
        while len(self.notification_popups) >= 3:
            self.notification_popups[0].close()
        popup = NotificationPopup(self, item)
        self.notification_popups.append(popup)
        popup.finished.connect(lambda _=0, value=popup: self._remove_popup(value))
        self._position_popups()
        popup.show()

    def _remove_popup(self, popup: NotificationPopup) -> None:
        if popup in self.notification_popups:
            self.notification_popups.remove(popup)
        self._position_popups()

    def _position_popups(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        bottom = area.bottom() - 12
        for popup in reversed(self.notification_popups):
            popup.adjustSize()
            popup.move(area.right() - popup.width() - 12, bottom - popup.height())
            bottom -= popup.height() + 10

    def _open_notification_center(self) -> None:
        if self.notification_center is None:
            self.notification_center = NotificationCenterDialog(self)
            self.notification_center.finished.connect(lambda _=0: setattr(self, "notification_center", None))
        self.notification_center.refresh_features()
        self.notification_center.refresh()
        self.notification_center.show()
        self.notification_center.raise_()
        self.notification_center.activateWindow()

    def _open_notification(self, item: dict) -> None:
        self._mark_notification_read(item)
        target = str(item.get("target_url") or "")
        if target:
            webbrowser.open(self.api.url(target))

    def _append_notification_actions(self, card: NotificationCard, item: dict) -> None:
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        image_url = str(content.get("image_url") or "").strip()
        actions = content.get("actions") if isinstance(content.get("actions"), list) else []
        if image_url:
            image_button = QPushButton("画像")
            image_button.clicked.connect(lambda _=False, url=image_url: webbrowser.open(self.api.absolute_url(url)))
            card.actions.addWidget(image_button)
        for row in actions[:4]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            url = str(row.get("url") or "").strip()
            if not label or not url:
                continue
            button = QPushButton(label[:24])
            button.clicked.connect(lambda _=False, value=url: webbrowser.open(self.api.absolute_url(value)))
            card.actions.addWidget(button)

    def _mark_notification_read(self, item: dict) -> None:
        notification_id = int(item.get("id") or 0)
        if not notification_id or not self.notification_token:
            return
        try:
            import requests
            response = requests.post(
                self.api.url(f"/desktop/media-hub/api/notifications/{notification_id}/read"),
                headers=self._notification_headers(),
                json={},
                timeout=15,
            )
            response.raise_for_status()
            was_unread = not item.get("is_read")
            item["is_read"] = True
            if was_unread:
                feature_key = str(item.get("feature_key") or "general")
                known = {str(row.get("key") or "") for row in self.notification_channels}
                channel_key = feature_key if feature_key in known and feature_key != "other" else "other"
                self.notification_channel_unread[channel_key] = max(
                    0, int(self.notification_channel_unread.get(channel_key) or 0) - 1
                )
            self._update_notification_count()
            if self.notification_center:
                self.notification_center.refresh_features()
                self.notification_center.refresh()
        except Exception as exc:
            self.show_notification(f"通知を既読にできませんでした。\n{exc}", QSystemTrayIcon.Warning, 5000)

    def _mark_all_notifications_read(self) -> None:
        if not self.notification_token:
            return
        try:
            import requests
            response = requests.post(
                self.api.url("/desktop/media-hub/api/notifications/read-all"),
                headers=self._notification_headers(),
                json={},
                timeout=15,
            )
            response.raise_for_status()
            for item in self.notifications:
                item["is_read"] = True
            self.notification_channel_unread = {
                str(key): 0 for key in self.notification_channel_unread
            }
            self._update_notification_count(0)
            if self.notification_center:
                self.notification_center.refresh_features()
                self.notification_center.refresh()
        except Exception as exc:
            self.show_notification(f"通知を既読にできませんでした。\n{exc}", QSystemTrayIcon.Warning, 5000)

    def _mark_notification_channel_read(self, channel: str) -> None:
        if not self.notification_token or not channel or channel == "all":
            return
        try:
            import requests
            response = requests.post(
                self.api.url("/desktop/media-hub/api/notifications/read-all"),
                headers=self._notification_headers(),
                json={"channel": channel},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
            for item in self.notifications:
                feature_key = str(item.get("feature_key") or "general")
                known = {str(row.get("key") or "") for row in self.notification_channels}
                item_channel = feature_key if feature_key in known and feature_key != "other" else "other"
                if item_channel == channel:
                    item["is_read"] = True
            counts = data.get("channel_unread") if isinstance(data, dict) else {}
            if isinstance(counts, dict):
                self.notification_channel_unread = {str(key): int(value or 0) for key, value in counts.items()}
            self._update_notification_count()
            if self.notification_center:
                self.notification_center.refresh_features()
                self.notification_center.refresh()
        except Exception as exc:
            self.show_notification(f"チャンネルを既読にできませんでした。\n{exc}", QSystemTrayIcon.Warning, 5000)

    def _mute_notification(self, item: dict, *, minutes: int = 0, forever: bool = False) -> None:
        if not self.notification_token:
            return
        try:
            import requests
            response = requests.post(
                self.api.url("/desktop/media-hub/api/notifications/mute"),
                headers=self._notification_headers(),
                json={
                    "feature_key": str(item.get("feature_key") or "general"),
                    "action": "forever" if forever else "mute",
                    "minutes": minutes,
                },
                timeout=15,
            )
            response.raise_for_status()
            self.show_notification(f"{item.get('feature_key') or 'general'} の通知をミュートしました。")
        except Exception as exc:
            self.show_notification(f"ミュート設定に失敗しました。\n{exc}", QSystemTrayIcon.Warning, 5000)


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
