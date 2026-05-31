from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QMainWindow, QMessageBox, QPushButton, QSplitter, QTabWidget, QToolBar, QVBoxLayout, QWidget

from core.models import ChatTarget
from ui.chat_view import ChatView
from ui.dm_panel import DmPanel
from ui.room_panel import RoomPanel
from ui.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    refresh_requested = Signal()
    target_selected = Signal(object)
    settings_saved = Signal(dict)

    def __init__(self, image_cache: Any, settings: dict) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("MFU Chat Desktop")
        self.resize(settings.get("width", 1200), settings.get("height", 760))
        self.event_panel = RoomPanel()
        self.dm_panel = DmPanel()
        self.chat_view = ChatView(image_cache)
        self.event_panel.target_selected.connect(self.target_selected.emit)
        self.dm_panel.target_selected.connect(self.target_selected.emit)

        tabs = QTabWidget()
        tabs.addTab(self.event_panel, "Events")
        tabs.addTab(self.dm_panel, "DM")
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.refresh = QPushButton("Refresh")
        self.refresh.clicked.connect(self.refresh_requested.emit)
        left_layout.addWidget(self.refresh)
        left_layout.addWidget(tabs, 1)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.chat_view)
        splitter.setSizes([320, 880])
        self.setCentralWidget(splitter)

        toolbar = QToolBar()
        self.addToolBar(toolbar)
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self._settings)
        toolbar.addAction(settings_action)

    def apply_bootstrap(self, data: dict[str, Any]) -> None:
        events = []
        for event in data.get("accessible_events") or []:
            events.append(
                ChatTarget(
                    kind="event",
                    title=str(event.get("title") or f"Event {event.get('id')}"),
                    event_id=int(event.get("id") or 0),
                    unread_count=int(event.get("unread_count") or 0),
                    raw=event,
                )
            )
        dms = []
        for dm in data.get("dm_inbox") or []:
            if not dm.get("dm_uuid"):
                continue
            title = str(dm.get("peer_display_name") or dm.get("peer_actor_key") or dm.get("dm_uuid"))
            last = str(dm.get("last_message") or "")
            if last:
                title = f"{title} - {last[:24]}"
            dms.append(ChatTarget(kind="dm", title=title, dm_uuid=str(dm.get("dm_uuid")), unread_count=int(dm.get("unread_count") or 0), raw=dm))
        self.event_panel.set_targets(events)
        self.dm_panel.set_targets(dms)
        total = sum(t.unread_count for t in events + dms)
        self.setWindowTitle(f"MFU Chat Desktop ({total})" if total else "MFU Chat Desktop")

    def show_snapshot(self, target: ChatTarget, snapshot: dict[str, Any], emojis: list[str]) -> None:
        if target.kind == "event":
            room = snapshot.get("active_room") or {}
            target.room_id = room.get("room_id")
            target.title = f"{(snapshot.get('event') or {}).get('title', target.title)} / {room.get('room_name', '')}"
        else:
            target.title = snapshot.get("peer_display_name") or target.title
        self.chat_view.set_target(target, snapshot.get("messages") or [], emojis)

    def ask_text(self, title: str, label: str, text: str = "") -> str | None:
        value, ok = QInputDialog.getMultiLineText(self, title, label, text)
        return value if ok else None

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "MFU Chat Desktop", text)

    def _settings(self) -> None:
        dialog = SettingsDialog(self.settings)
        if dialog.exec():
            self.settings.update(dialog.values())
            self.settings_saved.emit(self.settings)
