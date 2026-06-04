from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
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


class MessageInput(QTextEdit):
    send_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.enter_sends = False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            has_ctrl = bool(event.modifiers() & Qt.ControlModifier)
            if (self.enter_sends and not has_ctrl) or (not self.enter_sends and has_ctrl):
                self.send_requested.emit()
                return
        super().keyPressEvent(event)


class ChatView(QWidget):
    send_text = Signal(str, object)
    send_images = Signal(list, str, object)
    react = Signal(dict, str)
    reaction_details = Signal(dict, str)
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
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title = QLabel("MFUチャット")
        self.title.setObjectName("chatTitle")
        self.jump_unread = QPushButton("未読へ")
        self.jump_unread.clicked.connect(self.scroll_to_unread)
        self.jump_latest = QPushButton("最新へ")
        self.jump_latest.clicked.connect(self.scroll_to_bottom)
        header.addWidget(self.title, 1)
        header.addWidget(self.jump_unread)
        header.addWidget(self.jump_latest)
        layout.addLayout(header)

        self.reply_label = QLabel("")
        self.reply_label.setObjectName("replyPreview")
        self.reply_label.hide()
        layout.addWidget(self.reply_label)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatTimeline")
        self.scroll.setWidgetResizable(True)
        self.message_box = QWidget()
        self.message_box.setObjectName("chatMessages")
        self.message_layout = QVBoxLayout(self.message_box)
        self.message_layout.setContentsMargins(12, 12, 12, 12)
        self.message_layout.setSpacing(0)
        self.message_layout.addStretch(1)
        self.scroll.setWidget(self.message_box)
        layout.addWidget(self.scroll, 1)

        self.typing_label = QLabel("")
        self.typing_label.setObjectName("typingIndicator")
        self.typing_label.hide()
        layout.addWidget(self.typing_label)

        self.upload_list = QListWidget()
        self.upload_list.setMaximumHeight(70)
        self.upload_list.hide()
        layout.addWidget(self.upload_list)

        controls = QHBoxLayout()
        self.input = MessageInput()
        self.input.setMaximumHeight(90)
        self.input.send_requested.connect(self._send)
        self.pick_image = QPushButton("画像")
        self.pick_image.clicked.connect(self.pick_images)
        self.search = QPushButton("検索")
        self.search.clicked.connect(self._search)
        self.send = QPushButton("送信")
        self.send.clicked.connect(self._send)
        controls.addWidget(self.input, 1)
        controls.addWidget(self.pick_image)
        controls.addWidget(self.search)
        controls.addWidget(self.send)
        layout.addLayout(controls)

    def set_target(self, target: ChatTarget, messages: list[dict[str, Any]], emojis: list[str]) -> None:
        self.target = target
        self.emojis = emojis or self.emojis
        self.input.enter_sends = self.enter_sends
        self.title.setText(target.title)
        self.messages = list(messages)
        self.render()

    def append_message(self, message: dict[str, Any]) -> None:
        if not message:
            return
        message_id = int(message.get("id") or 0)
        if message_id and any(int(m.get("id") or 0) == message_id for m in self.messages):
            self.update_message(message)
            return
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
                message["body_plain"] = message.get("deleted_text") or "このメッセージは削除されました"
                message["body"] = ""
                message["images"] = []
        self.render()

    def update_reactions(self, payload: dict[str, Any]) -> None:
        message_id = int(payload.get("message_id") or 0)
        for message in self.messages:
            if int(message.get("id") or 0) == message_id:
                message["reactions_summary"] = payload.get("reactions") or []
        self.render()

    def update_typing(self, payload: dict[str, Any]) -> None:
        if not payload or payload.get("is_typing") is False:
            self.typing_label.hide()
            self.typing_label.setText("")
            return
        name = str(payload.get("display_name") or "相手")
        self.typing_label.setText(f"{name}が入力中…")
        self.typing_label.show()

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
                divider.setObjectName("chatDateDivider")
                self.message_layout.insertWidget(self.message_layout.count() - 1, divider, alignment=Qt.AlignHCenter)
                last_date = date_label
            widget = MessageWidget(message, self.emojis, self.image_cache)
            widget.reply_requested.connect(self._reply)
            widget.thread_requested.connect(self.thread.emit)
            widget.edit_requested.connect(self.edit.emit)
            widget.delete_requested.connect(self.delete.emit)
            widget.reaction_requested.connect(self.react.emit)
            widget.reaction_details_requested.connect(self.reaction_details.emit)
            widget.image_requested.connect(lambda images, idx: ImageViewer(images, idx, self.image_cache).exec())
            self.message_layout.insertWidget(self.message_layout.count() - 1, widget)
        self.scroll_to_bottom()

    def scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def scroll_to_unread(self) -> None:
        self.scroll_to_bottom()

    def _reply(self, message: dict[str, Any]) -> None:
        self.reply_to = message
        self.reply_label.setText(f"返信先: {message.get('sender_display_name')}: {message.get('body_plain_excerpt')}")
        self.reply_label.show()

    def pick_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "画像を選択", "", "画像 (*.png *.jpg *.jpeg *.webp *.heic *.heif)")
        if not files:
            return
        if len(files) + len(self.uploads) > 6:
            QMessageBox.warning(self, "画像が多すぎます", "画像は最大6枚まで送信できます。")
            return
        for path in files:
            if path.lower().endswith((".heic", ".heif")):
                QMessageBox.warning(self, "非対応形式", "HEIC/HEIFには対応していません。JPEG/PNG/WEBPを送信してください。")
                continue
            if os.path.getsize(path) > 20 * 1024 * 1024:
                QMessageBox.warning(self, "画像が大きすぎます", f"{os.path.basename(path)} は20MBを超えています。")
                continue
            self.uploads.append(path)
            QListWidgetItem(os.path.basename(path), self.upload_list)
        self.upload_list.setVisible(bool(self.uploads))

    def _send(self) -> None:
        body = self.input.toPlainText().strip()
        if not body and not self.uploads:
            return
        if len(body) > 2000:
            QMessageBox.warning(self, "本文が長すぎます", "メッセージは2000文字以内で入力してください。")
            return
        self.send.setEnabled(False)
        if self.uploads:
            self.send_images.emit(list(self.uploads), body, self.reply_to)
        else:
            self.send_text.emit(body, self.reply_to)
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
