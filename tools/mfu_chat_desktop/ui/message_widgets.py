from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget


class MessageWidget(QWidget):
    reply_requested = Signal(dict)
    thread_requested = Signal(dict)
    edit_requested = Signal(dict)
    delete_requested = Signal(dict)
    reaction_requested = Signal(dict, str)
    image_requested = Signal(list, int)

    def __init__(self, message: dict[str, Any], emojis: list[str]) -> None:
        super().__init__()
        self.message = message
        self.emojis = emojis
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        header = QLabel(f"{message.get('sender_display_name', '')}  {message.get('created_at_jst_time_hm', '')}")
        header.setStyleSheet("color:#667; font-size:11px;")
        body = QLabel(str(message.get("body_plain") or ""))
        body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        body.setWordWrap(True)
        body.setStyleSheet("padding:8px; border-radius:8px; background:%s;" % ("#dff4ff" if message.get("is_me") else "#f3f4f6"))
        root.addWidget(header)
        root.addWidget(body)
        if message.get("edited_flag"):
            edited = QLabel("edited")
            edited.setStyleSheet("color:#889; font-size:10px;")
            root.addWidget(edited)
        images = message.get("images") or []
        if images:
            row = QHBoxLayout()
            for idx, image in enumerate(images[:6]):
                btn = QPushButton("image")
                btn.clicked.connect(lambda _=False, i=idx: self.image_requested.emit(images, i))
                row.addWidget(btn)
            root.addLayout(row)
        reactions = message.get("reactions_summary") or []
        if reactions:
            label = QLabel(" ".join(f"{r.get('emoji')} {r.get('count')}" for r in reactions))
            root.addWidget(label)
        if int(message.get("thread_reply_count") or 0) > 0:
            thread = QPushButton(f"Replies {message.get('thread_reply_count')}")
            thread.clicked.connect(lambda: self.thread_requested.emit(self.message))
            root.addWidget(thread)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("Reply", lambda: self.reply_requested.emit(self.message))
        menu.addAction("Thread", lambda: self.thread_requested.emit(self.message))
        if self.message.get("can_edit"):
            menu.addAction("Edit", lambda: self.edit_requested.emit(self.message))
        if self.message.get("can_delete"):
            menu.addAction("Delete", lambda: self.delete_requested.emit(self.message))
        react_menu = menu.addMenu("Reaction")
        for emoji in self.emojis:
            react_menu.addAction(emoji, lambda checked=False, e=emoji: self.reaction_requested.emit(self.message, e))
        menu.exec(event.globalPos())
