from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.models import ChatTarget
from ui.image_viewer import ImageViewer
from ui.message_widgets import MessageWidget


class ChatView(QWidget):
    send_text = Signal(str)
    send_images = Signal(list, str)
    react = Signal(dict, str)
    edit = Signal(dict)
    delete = Signal(dict)
    thread = Signal(dict)
    search_requested = Signal(str)

    def __init__(self, image_cache: Any) -> None:
        super().__init__()
        self.image_cache = image_cache
        self.target: ChatTarget | None = None
        self.emojis = ["💕", "👍", "😆", "😭", "😢", "🫶"]
        self.messages: list[dict[str, Any]] = []
        self.uploads: list[str] = []
        self.reply_to: dict[str, Any] | None = None
        self.enter_sends = False

        layout = QVBoxLayout(self)
        self.title = QLabel("MFU Chat")
        self.title.setStyleSheet("font-weight:600; font-size:18px;")
        layout.addWidget(self.title)
        self.reply_label = QLabel("")
        self.reply_label.hide()
        layout.addWidget(self.reply_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.message_box = QWidget()
        self.message_layout = QVBoxLayout(self.message_box)
        self.message_layout.addStretch(1)
        self.scroll.setWidget(self.message_box)
        layout.addWidget(self.scroll, 1)

        self.upload_list = QListWidget()
        self.upload_list.setMaximumHeight(70)
        self.upload_list.hide()
        layout.addWidget(self.upload_list)

        controls = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setMaximumHeight(90)
        self.pick_image = QPushButton("Image")
        self.pick_image.clicked.connect(self.pick_images)
        self.send = QPushButton("Send")
        self.send.clicked.connect(self._send)
        self.search = QPushButton("Search")
        self.search.clicked.connect(self._search)
        controls.addWidget(self.input, 1)
        controls.addWidget(self.pick_image)
        controls.addWidget(self.search)
        controls.addWidget(self.send)
        layout.addLayout(controls)

    def set_target(self, target: ChatTarget, messages: list[dict[str, Any]], emojis: list[str]) -> None:
        self.target = target
        self.emojis = emojis or self.emojis
        self.title.setText(target.title)
        self.messages = list(messages)
        self.render()

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.render()

    def update_message(self, message: dict[str, Any]) -> None:
        message_id = int(message.get("id") or message.get("message_id") or 0)
        self.messages = [message if int(m.get("id") or 0) == message_id else m for m in self.messages]
        self.render()

    def mark_deleted(self, payload: dict[str, Any]) -> None:
        message_id = int(payload.get("message_id") or 0)
        for message in self.messages:
            if int(message.get("id") or 0) == message_id:
                message["deleted_flag"] = 1
                message["body_plain"] = message.get("deleted_text") or "Deleted"
                message["body"] = ""
        self.render()

    def update_reactions(self, payload: dict[str, Any]) -> None:
        message_id = int(payload.get("message_id") or 0)
        for message in self.messages:
            if int(message.get("id") or 0) == message_id:
                message["reactions_summary"] = payload.get("reactions") or []
        self.render()

    def render(self) -> None:
        while self.message_layout.count() > 1:
            item = self.message_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        last_date = ""
        for message in self.messages:
            date_label = str(message.get("created_at_jst_date_label") or "")
            if date_label and date_label != last_date:
                divider = QLabel(date_label)
                divider.setStyleSheet("color:#667; margin:10px;")
                self.message_layout.insertWidget(self.message_layout.count() - 1, divider)
                last_date = date_label
            widget = MessageWidget(message, self.emojis)
            widget.reply_requested.connect(self._reply)
            widget.thread_requested.connect(self.thread.emit)
            widget.edit_requested.connect(self.edit.emit)
            widget.delete_requested.connect(self.delete.emit)
            widget.reaction_requested.connect(self.react.emit)
            widget.image_requested.connect(lambda images, idx: ImageViewer(images, idx, self.image_cache).exec())
            self.message_layout.insertWidget(self.message_layout.count() - 1, widget)

    def _reply(self, message: dict[str, Any]) -> None:
        self.reply_to = message
        self.reply_label.setText(f"Replying to {message.get('sender_display_name')}: {message.get('body_plain_excerpt')}")
        self.reply_label.show()

    def pick_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select images", "", "Images (*.png *.jpg *.jpeg *.webp *.heic *.heif)")
        if not files:
            return
        if len(files) + len(self.uploads) > 6:
            QMessageBox.warning(self, "Too many images", "Maximum 6 images.")
            return
        for path in files:
            if path.lower().endswith((".heic", ".heif")):
                QMessageBox.warning(self, "Unsupported", "HEIC/HEIF is not supported. Please send JPEG/PNG/WEBP.")
                continue
            if os.path.getsize(path) > 20 * 1024 * 1024:
                QMessageBox.warning(self, "Too large", f"{os.path.basename(path)} exceeds 20MB.")
                continue
            self.uploads.append(path)
            QListWidgetItem(os.path.basename(path), self.upload_list)
        self.upload_list.setVisible(bool(self.uploads))

    def _send(self) -> None:
        body = self.input.toPlainText().strip()
        if not body and not self.uploads:
            return
        if len(body) > 2000:
            QMessageBox.warning(self, "Too long", "Message must be at most 2000 characters.")
            return
        self.send.setEnabled(False)
        if self.uploads:
            self.send_images.emit(list(self.uploads), body)
        else:
            self.send_text.emit(body)
        self.input.clear()
        self.uploads.clear()
        self.upload_list.clear()
        self.upload_list.hide()
        self.reply_to = None
        self.reply_label.hide()
        self.send.setEnabled(True)

    def _search(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.search_requested.emit(text)
