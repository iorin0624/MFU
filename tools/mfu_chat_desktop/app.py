from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from core.api_client import ApiClient, ApiError
from core.image_cache import ImageCache
from core.logger import get_logger
from core.models import ChatTarget
from core.notifications import Notifier
from core.session_store import SessionStore
from core.socket_client import SocketClient
from ui.login_window import LoginWindow
from ui.main_window import MainWindow


class DesktopApp:
    def __init__(self, qt_app: QApplication) -> None:
        self.qt_app = qt_app
        self.store = SessionStore()
        self.settings = self.store.load_settings()
        self.api = ApiClient(self.store)
        self.socket = SocketClient(self.api)
        self.notifier = Notifier()
        self.logger = get_logger()
        self.image_cache = ImageCache(self.api.session, self.api.base_url)
        self.login_window: LoginWindow | None = None
        self.main_window: MainWindow | None = None
        self.current_target: ChatTarget | None = None
        self.bootstrap_data: dict[str, Any] = {}
        self._bind_socket()
        self.tray = QSystemTrayIcon()
        self.tray.setToolTip("MFU Chat Desktop")
        tray_menu = QMenu()
        tray_menu.addAction("Open", self._show_main)
        tray_menu.addAction("Reconnect", self._reconnect)
        tray_menu.addAction("Toggle notifications", self._toggle_notifications)
        tray_menu.addAction("Quit", self.qt_app.quit)
        self.tray.setContextMenu(tray_menu)
        self.tray.show()

    def start(self) -> None:
        try:
            data = self.api.check_session()
        except Exception:
            data = {"authenticated": False}
        if data.get("authenticated"):
            self._open_main()
        else:
            self._open_login()

    def _open_login(self) -> None:
        self.login_window = LoginWindow()
        self.login_window.login_requested.connect(self._login)
        self.login_window.show()

    def _login(self, username: str, password: str, save_password: bool) -> None:
        if not self.login_window:
            return
        try:
            self.api.login(username, password)
            if save_password:
                self.store.save_password(username, password)
            self.login_window.accept()
            self._open_main()
        except ApiError as exc:
            self.login_window.show_error(str(exc))

    def _open_main(self) -> None:
        self.main_window = MainWindow(self.image_cache, self.settings)
        self.main_window.refresh_requested.connect(self.refresh)
        self.main_window.target_selected.connect(self.open_target)
        self.main_window.settings_saved.connect(self._save_settings)
        self.main_window.chat_view.send_text.connect(self.send_text)
        self.main_window.chat_view.send_images.connect(self.send_images)
        self.main_window.chat_view.react.connect(self.react)
        self.main_window.chat_view.edit.connect(self.edit_message)
        self.main_window.chat_view.delete.connect(self.delete_message)
        self.main_window.chat_view.thread.connect(self.open_thread)
        self.main_window.chat_view.search_requested.connect(self.search)
        self.main_window.show()
        self.refresh()
        QTimer.singleShot(100, self._reconnect)

    def refresh(self) -> None:
        try:
            self.bootstrap_data = self.api.bootstrap()
            self.notifier.enabled = self.settings.get("notifications_enabled", True)
            if self.main_window:
                self.main_window.apply_bootstrap(self.bootstrap_data)
        except Exception as exc:
            self._error(exc)

    def open_target(self, target: ChatTarget) -> None:
        try:
            self.current_target = target
            if target.kind == "dm" and target.dm_uuid:
                snapshot = self.api.dm_snapshot(target.dm_uuid)
                self.socket.join_dm(target.dm_uuid)
            else:
                snapshot = self.api.event_snapshot(int(target.event_id or 0), target.room_id)
                active_room = snapshot.get("active_room") or {}
                self.socket.join_event(int(target.event_id or 0), active_room.get("room_id"))
            if self.main_window:
                self.main_window.show_snapshot(target, snapshot, self.bootstrap_data.get("reaction_emojis") or [])
            self._mark_seen_latest()
        except Exception as exc:
            self._error(exc)

    def send_text(self, body: str) -> None:
        target = self.current_target
        if not target:
            return
        try:
            if target.kind == "dm" and target.dm_uuid:
                self.socket.send_dm(target.dm_uuid, body)
            elif target.event_id and target.room_id:
                self.socket.send_event(target.event_id, target.room_id, body)
        except Exception as exc:
            self._error(exc)

    def send_images(self, files: list[str], body: str) -> None:
        target = self.current_target
        if not target:
            return
        try:
            if target.kind == "dm":
                data = self.api.upload_images(files, body, dm_uuid=target.dm_uuid)
            else:
                data = self.api.upload_images(files, body, event_id=target.event_id, room_id=target.room_id)
            if self.main_window:
                self.main_window.chat_view.append_message(data.get("message") or {})
        except Exception as exc:
            self._error(exc)

    def react(self, message: dict, emoji: str) -> None:
        target = self.current_target
        if not target:
            return
        if target.kind == "dm" and target.dm_uuid:
            self.socket.react_dm(target.dm_uuid, int(message.get("id") or 0), emoji)
        elif target.event_id and target.room_id:
            self.socket.react_event(target.event_id, target.room_id, int(message.get("id") or 0), emoji)

    def edit_message(self, message: dict) -> None:
        if not self.current_target or not self.main_window:
            return
        text = self.main_window.ask_text("Edit message", "Body", str(message.get("body_plain") or ""))
        if text is None:
            return
        try:
            self.api.edit_message(self.current_target, int(message.get("id") or 0), text)
        except Exception as exc:
            self._error(exc)

    def delete_message(self, message: dict) -> None:
        if not self.current_target:
            return
        try:
            self.api.delete_message(self.current_target, int(message.get("id") or 0))
        except Exception as exc:
            self._error(exc)

    def search(self, query: str) -> None:
        target = self.current_target
        if not target or not self.main_window:
            return
        try:
            if target.kind == "dm" and target.dm_uuid:
                data = self.api.search_dm(target.dm_uuid, query)
            else:
                data = self.api.search_event(int(target.event_id or 0), str(target.room_id or ""), query)
            count = len(data.get("results") or [])
            self.main_window.show_error(f"{count} results")
        except Exception as exc:
            self._error(exc)

    def open_thread(self, message: dict) -> None:
        target = self.current_target
        if not target or target.kind == "dm" or not self.main_window:
            return
        try:
            root_id = int(message.get("thread_root_id") or message.get("id") or 0)
            data = self.api.thread_messages(int(target.event_id or 0), str(target.room_id or ""), root_id)
            lines = [f"Root: {(data.get('root') or {}).get('body_plain', '')}"]
            for reply in data.get("replies") or []:
                lines.append(f"{reply.get('sender_display_name')}: {reply.get('body_plain')}")
            reply_body = self.main_window.ask_text("Thread", "\n".join(lines) + "\n\nReply")
            if reply_body:
                self.socket.send_event(int(target.event_id or 0), str(target.room_id or ""), reply_body, reply_to=int(message.get("id") or root_id), thread_root_id=root_id)
        except Exception as exc:
            self._error(exc)

    def _bind_socket(self) -> None:
        self.socket.on("chat_message", self._socket_message)
        self.socket.on("chat_error", lambda data: self._error(RuntimeError(str((data or {}).get("error") or data))))
        self.socket.on("chat_reaction_update", lambda data: self.main_window and self.main_window.chat_view.update_reactions(data or {}))
        self.socket.on("chat_delete_update", lambda data: self.main_window and self.main_window.chat_view.mark_deleted(data or {}))
        self.socket.on("chat_edit_update", lambda data: self.main_window and self.main_window.chat_view.update_message(data or {}))
        self.socket.on("disconnect", lambda _=None: self.logger.info("socket disconnected"))

    def _socket_message(self, message: dict | None) -> None:
        if not message or not self.main_window:
            return
        current = self.current_target
        is_current = False
        if current and current.kind == "dm":
            is_current = str(message.get("dm_uuid") or "") == str(current.dm_uuid or "")
        elif current:
            is_current = int(message.get("event_id") or 0) == int(current.event_id or 0) and str(message.get("room_id") or current.room_id or "") == str(current.room_id or "")
        if is_current:
            self.main_window.chat_view.append_message(message)
            self._mark_seen_latest()
        elif not message.get("is_me"):
            self.notifier.show_message(message)
            self.refresh()

    def _mark_seen_latest(self) -> None:
        if not self.current_target or not self.main_window:
            return
        messages = self.main_window.chat_view.messages
        if not messages:
            return
        last_id = int(messages[-1].get("id") or 0)
        if last_id <= 0:
            return
        if self.current_target.kind == "dm" and self.current_target.dm_uuid:
            self.socket.seen_dm(self.current_target.dm_uuid, last_id)
        elif self.current_target.event_id and self.current_target.room_id:
            self.socket.seen_event(self.current_target.event_id, self.current_target.room_id, last_id)

    def _reconnect(self) -> None:
        try:
            self.socket.disconnect()
            self.socket.connect()
            if self.current_target:
                self.open_target(self.current_target)
            else:
                self.refresh()
        except Exception as exc:
            self.logger.info("socket reconnect failed: %s", exc)

    def _show_main(self) -> None:
        if self.main_window:
            self.main_window.showNormal()
            self.main_window.activateWindow()

    def _toggle_notifications(self) -> None:
        self.notifier.enabled = not self.notifier.enabled
        self.settings["notifications_enabled"] = self.notifier.enabled
        self._save_settings(self.settings)

    def _save_settings(self, settings: dict) -> None:
        self.settings = settings
        self.store.save_settings(settings)

    def _error(self, exc: Exception) -> None:
        self.logger.exception("desktop error: %s", exc)
        if self.main_window:
            self.main_window.show_error(str(exc))
