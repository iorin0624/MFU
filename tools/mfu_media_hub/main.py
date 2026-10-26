from __future__ import annotations

import ctypes
import html
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from ctypes import wintypes
from datetime import datetime, timezone
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
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

from tools.mfu_media_clipboard import main as clipboard
from tools.mfu_media_hub.card_links import notification_card_url as _notification_card_url
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
APP_VERSION = "2.2.8"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MFU" / APP_NAME
TOKEN_PATH = APP_DIR / "tokens.bin"


def _notification_action_expired(action: dict) -> bool:
    if action.get("expired"):
        return True
    raw = str(action.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _au_pay_otp_action(item: dict) -> dict | None:
    if str(item.get("topic_key") or "") != "au-pay-otp":
        return None
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    actions = content.get("actions") if isinstance(content.get("actions"), list) else []
    return next(
        (action for action in actions if isinstance(action, dict) and str(action.get("type") or "").lower() == "copy"),
        None,
    )


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


def _notification_link_label(text: str, url: str, *, bold: bool = False) -> QLabel:
    weight = "font-weight:700;" if bold else ""
    label = QLabel(f'<a href="{html.escape(url, quote=True)}" style="color:#60a5fa;{weight}">{html.escape(text)}</a>')
    label.setTextFormat(Qt.RichText)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    label.setOpenExternalLinks(True)
    label.setWordWrap(True)
    return label


def _add_notification_fields(layout: QVBoxLayout, fields: object, *, columns: int = 2) -> None:
    if not isinstance(fields, list) or not fields:
        return
    column_count = 1 if columns <= 1 else 2
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(8)
    for column in range(column_count):
        grid.setColumnStretch(column, 1)
    grid_row = 0
    grid_column = 0
    for row in fields[:25]:
        if not isinstance(row, dict):
            continue
        inline = bool(row.get("inline")) and column_count > 1
        if not inline and grid_column:
            grid_row += 1
            grid_column = 0
        field = QFrame()
        field.setObjectName("NotificationField")
        field.setStyleSheet(
            "QFrame#NotificationField{background:#111827;border:1px solid #374151;border-radius:6px;}"
        )
        field_layout = QVBoxLayout(field)
        field_layout.setContentsMargins(9, 7, 9, 7)
        field_layout.setSpacing(2)
        name = QLabel(f"<b>{html.escape(str(row.get('name') or ''))}</b>")
        name.setTextFormat(Qt.RichText)
        name.setStyleSheet("color:#9ca3af;font-size:12px;")
        field_layout.addWidget(name)
        value = _rich_label(row.get("value") or "", color="#f8fafc")
        field_layout.addWidget(value)
        if inline:
            grid.addWidget(field, grid_row, grid_column)
            grid_column += 1
            if grid_column >= column_count:
                grid_row += 1
                grid_column = 0
        else:
            grid.addWidget(field, grid_row, 0, 1, column_count)
            grid_row += 1
    layout.addLayout(grid)


class NotificationCard(QFrame):
    def __init__(self, item: dict, *, compact: bool = False, field_columns: int = 2, parent=None) -> None:
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
                card_url = _notification_card_url(card_data.get("url"))
                card_title = str(card_data.get("title") or "")
                if card_title:
                    title = _notification_link_label(card_title, card_url, bold=True) if card_url else _rich_label(card_title)
                    title.setStyleSheet("font-weight:700;font-size:14px;color:#f8fafc;")
                    embed_layout.addWidget(title)
                description = str(card_data.get("description") or "")
                is_link_prompt = bool(card_url and re.fullmatch(r".{1,80}(?:を開く|を確認する)", description.strip()))
                if description:
                    embed_layout.addWidget(
                        _notification_link_label(description.strip(), card_url)
                        if is_link_prompt else _rich_label(description)
                    )
                if card_url and not card_title and (not description or not is_link_prompt):
                    embed_layout.addWidget(_notification_link_label("リンクを開く", card_url))
                fields = card_data.get("fields") if isinstance(card_data.get("fields"), list) else []
                _add_notification_fields(embed_layout, fields, columns=field_columns)
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
                _add_notification_fields(layout, fields, columns=field_columns)
        self.otp_value_label = None
        otp_action = _au_pay_otp_action(item)
        if otp_action is not None:
            otp_value = str(otp_action.get("value") or "")
            if _notification_action_expired(otp_action) or not re.fullmatch(r"\d{6,10}", otp_value):
                otp_value = "期限切れ"
            field = QFrame()
            field.setObjectName("NotificationField")
            field.setStyleSheet("QFrame#NotificationField{background:#111827;border:1px solid #374151;border-radius:6px;}")
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(9, 7, 9, 7)
            name = QLabel("ワンタイムパスワード")
            name.setStyleSheet("color:#9ca3af;font-size:12px;")
            field_layout.addWidget(name)
            self.otp_value_label = QLabel(otp_value)
            self.otp_value_label.setStyleSheet("color:#f8fafc;font-size:19px;font-weight:700;")
            field_layout.addWidget(self.otp_value_label)
            layout.addWidget(field)
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
        # Use an ordinary top-level window so popup actions remain interactive
        # even while the tray-only application is in the background.
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFixedWidth(390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = NotificationCard(item, field_columns=1)
        layout.addWidget(card)
        web = QPushButton("Webで開く")
        web.clicked.connect(self._open_web)
        card.actions.addWidget(web)
        if _notification_card_url(item.get("target_url")):
            detail = QPushButton("詳細を見る")
            detail.clicked.connect(self._open_detail)
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

    def _open_web(self) -> None:
        logging.info("notification popup action clicked: web")
        self.controller._open_notification_center()

    def _open_detail(self) -> None:
        logging.info("notification popup action clicked: detail")
        self.controller._open_notification(self.item)


class MediaHubApp(clipboard.MediaClipboardApp):
    unread_count_loaded = Signal(int)

    def __init__(self, qt_app: QApplication) -> None:
        clipboard.APP_NAME = APP_NAME
        super().__init__(qt_app)
        self.photo_settings = load_photo_settings()
        self.photo_receiver: ReceiverThread | None = None
        self.last_saved_path = ""
        self.notification_popups: list[NotificationPopup] = []
        self._recent_popup_ids: list[int] = []
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
                        "通知ポップアップとWeb通知センターの未読数を利用するには、タスクトレイから『3機能をまとめてログイン』を一度実行してください。",
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
            self.notification_token,
        )
        self.photo_receiver.status_changed.connect(self._set_connection_status)
        self.photo_receiver.file_saved.connect(self._photo_saved)
        self.photo_receiver.error.connect(self._receiver_error)
        self.photo_receiver.authentication_failed.connect(lambda: self._set_connection_status("red", "再ログインが必要です"))
        self.photo_receiver.media_progress.connect(self.api.handle_websocket_progress)
        self.photo_receiver.notification_received.connect(self._notification_received)
        self.photo_receiver.notification_unread.connect(self._notification_unread)
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
            QMessageBox.information(None, APP_NAME, "写真転送、Media Clipboard、通知ポップアップとWeb通知センターにログインしました。")
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
        for popup in list(self.notification_popups):
            popup.close()
        self._update_notification_count(0)
        self._set_connection_status("red", "未ログイン")
        QMessageBox.information(None, APP_NAME, "この端末のログイン情報を削除しました。")

    def _notification_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.notification_token}"}

    def _notification_unread(self, payload: dict) -> None:
        if isinstance(payload, dict) and "count" in payload:
            self._update_notification_count(max(0, int(payload.get("count") or 0)))

    def _notification_received(self, item: dict) -> None:
        # Only new events reach this handler. Historical lists are never sent
        # to popup-only Windows clients.
        notification_id = int(item.get("id") or 0)
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        if content.get("media_hub_enabled", True) and notification_id not in self._recent_popup_ids:
            if notification_id:
                self._recent_popup_ids.append(notification_id)
                self._recent_popup_ids = self._recent_popup_ids[-200:]
            self._show_notification_popup(item)
        QTimer.singleShot(500, self._poll_notification_count)

    def _show_notification_popup(self, item: dict) -> None:
        while len(self.notification_popups) >= 3:
            self.notification_popups[0].close()
        popup = NotificationPopup(self, item)
        self.notification_popups.append(popup)
        popup.finished.connect(lambda _=0, value=popup: self._remove_popup(value))
        popup.show()
        self._position_popups()

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

    def _open_notification(self, item: dict) -> None:
        target = _notification_card_url(item.get("target_url"))
        if target and webbrowser.open(target):
            self._mark_notification_read(item)

    def _mark_notification_read(self, item: dict) -> None:
        notification_id = int(item.get("id") or 0)
        token = self.notification_token
        if not notification_id or not token:
            return

        def mark_read() -> None:
            try:
                import requests
                requests.post(
                    self.api.url(f"/desktop/media-hub/api/notifications/{notification_id}/read"),
                    headers={"Authorization": f"Bearer {token}"},
                    json={},
                    timeout=8,
                ).raise_for_status()
            except Exception:
                logging.warning("notification read acknowledgement failed", exc_info=True)

        threading.Thread(target=mark_read, daemon=True).start()
        QTimer.singleShot(1500, self._poll_notification_count)

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
            action_type = str(row.get("type") or "link").strip().lower()
            if action_type == "copy":
                value = str(row.get("value") or "")
                if not label:
                    continue
                expired = _notification_action_expired(row) or not value
                button = QPushButton("期限切れ" if expired else label[:24])
                button.setEnabled(not expired)
                button.clicked.connect(
                    lambda _=False, copy_action=row: self._copy_notification_value(copy_action)
                )
                card.actions.addWidget(button)
                raw_expiry = str(row.get("expires_at") or "").strip()
                if not expired and raw_expiry:
                    try:
                        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        remaining_ms = int((expires_at - datetime.now(timezone.utc)).total_seconds() * 1000)
                        timer = QTimer(card)
                        timer.setSingleShot(True)
                        timer.timeout.connect(lambda b=button, c=card: self._expire_copy_action_ui(b, c))
                        timer.start(max(1, min(remaining_ms, 2147483647)))
                    except ValueError:
                        pass
                continue
            url = str(row.get("url") or "").strip()
            if not label or not url:
                continue
            button = QPushButton(label[:24])
            button.clicked.connect(lambda _=False, value=url: webbrowser.open(self.api.absolute_url(value)))
            card.actions.addWidget(button)

    @staticmethod
    def _expire_copy_action_ui(button: QPushButton, card: NotificationCard) -> None:
        button.setEnabled(False)
        button.setText("期限切れ")
        if card.otp_value_label is not None:
            card.otp_value_label.setText("期限切れ")

    def _copy_notification_value(self, action: dict) -> None:
        if _notification_action_expired(action):
            self.show_notification("ワンタイムパスワードの有効期限が切れています。")
            return
        value = str(action.get("value") or "")
        if not value:
            return
        QApplication.clipboard().setText(value)
        self.show_notification("ワンタイムパスワードをコピーしました。")

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
