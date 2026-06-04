from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget


class MessageWidget(QWidget):
    reply_requested = Signal(dict)
    thread_requested = Signal(dict)
    edit_requested = Signal(dict)
    delete_requested = Signal(dict)
    reaction_requested = Signal(dict, str)
    reaction_details_requested = Signal(dict, str)
    image_requested = Signal(list, int)

    def __init__(self, message: dict[str, Any], emojis: list[str], image_cache: Any) -> None:
        super().__init__()
        self.message = message
        self.emojis = emojis
        self.image_cache = image_cache
        self.setAttribute(Qt.WA_StyledBackground, True)

        is_me = bool(message.get("is_me"))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(8)

        if is_me:
            outer.addStretch(1)
            outer.addLayout(self._build_meta(is_me))
            outer.addLayout(self._build_main(is_me))
        else:
            outer.addWidget(self._avatar())
            outer.addLayout(self._build_main(is_me))
            outer.addLayout(self._build_meta(is_me))
            outer.addStretch(1)

    def _avatar(self) -> QLabel:
        label = QLabel()
        label.setFixedSize(32, 32)
        label.setObjectName("chatAvatar")
        label.setAlignment(Qt.AlignCenter)
        label.setText("・")
        avatar_url = str(self.message.get("sender_avatar_url") or "")
        path = self.image_cache.fetch(avatar_url) if avatar_url else None
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(32, 32, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                label.setText("")
        return label

    def _build_main(self, is_me: bool) -> QVBoxLayout:
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(3)

        if not is_me:
            sender = QLabel(str(self.message.get("sender_display_name") or ""))
            sender.setObjectName("chatSender")
            main.addWidget(sender, alignment=Qt.AlignLeft)

        bubble = QFrame()
        bubble.setObjectName("chatBubbleMe" if is_me else "chatBubbleOther")
        bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(12, 8, 12, 8)
        bubble_layout.setSpacing(6)

        if self.message.get("reply_to_message_id"):
            quote = QLabel(
                f"{self.message.get('reply_to_sender_display_name') or ''}\n"
                f"{self.message.get('reply_to_body_plain_excerpt') or ''}"
            )
            quote.setObjectName("chatReplyQuote")
            quote.setWordWrap(False)
            quote.setMaximumWidth(320)
            bubble_layout.addWidget(quote)

        images = self.message.get("images") or []
        if images:
            bubble_layout.addWidget(self._image_grid(images))

        body_text = str(self.message.get("body_plain") or self.message.get("body") or "")
        body = QLabel(body_text)
        body.setObjectName("chatDeletedText" if int(self.message.get("deleted_flag") or 0) == 1 else "chatBody")
        if int(self.message.get("deleted_flag") or 0) == 1:
            body.setStyleSheet("color:#7a5260; background:transparent;")
        else:
            body.setStyleSheet(f"color:{'#ffffff' if is_me else '#222222'}; background:transparent;")
        body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        body.setWordWrap(True)
        body.setMaximumWidth(520)
        bubble_layout.addWidget(body)
        main.addWidget(bubble, alignment=Qt.AlignRight if is_me else Qt.AlignLeft)

        lower = QHBoxLayout()
        lower.setContentsMargins(0, 0, 0, 0)
        lower.setSpacing(6)
        if self.message.get("edited_flag"):
            edited = QLabel("編集済み")
            edited.setObjectName("chatEditedLabel")
            lower.addWidget(edited)
        if int(self.message.get("thread_reply_count") or 0) > 0:
            thread = QPushButton(f"返信 {self.message.get('thread_reply_count')}")
            thread.setObjectName("threadSummaryButton")
            thread.clicked.connect(lambda: self.thread_requested.emit(self.message))
            lower.addWidget(thread)
        for reaction in self.message.get("reactions_summary") or []:
            emoji = str(reaction.get("emoji") or "")
            chip = QPushButton(f"{emoji} {reaction.get('count')}")
            chip.setObjectName("chatReactionChip")
            chip.clicked.connect(lambda _=False, e=emoji: self.reaction_details_requested.emit(self.message, e))
            lower.addWidget(chip)
        if lower.count():
            main.addLayout(lower)
        return main

    def _build_meta(self, is_me: bool) -> QVBoxLayout:
        meta = QVBoxLayout()
        meta.setContentsMargins(0, 0, 0, 2)
        meta.addStretch(1)
        if is_me:
            read = QLabel("既読" if self.message.get("read_count") else "")
            read.setObjectName("chatReadInline")
            meta.addWidget(read, alignment=Qt.AlignRight)
        time = QLabel(str(self.message.get("created_at_jst_time_hm") or ""))
        time.setObjectName("chatTime")
        meta.addWidget(time, alignment=Qt.AlignRight if is_me else Qt.AlignLeft)
        return meta

    def _image_grid(self, images: list[dict[str, Any]]) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container) if len(images) <= 2 else QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        rows: list[QHBoxLayout] = []
        if len(images) > 2:
            for _ in range((min(len(images), 6) + 1) // 2):
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(4)
                rows.append(row)
                layout.addLayout(row)
        for idx, image in enumerate(images[:6]):
            button = QPushButton()
            button.setObjectName("chatImageThumb")
            button.setFixedSize(150 if len(images) == 1 else 104, 112 if len(images) == 1 else 104)
            button.setIconSize(QSize(button.width() - 8, button.height() - 8))
            button.clicked.connect(lambda _=False, i=idx: self.image_requested.emit(images, i))
            path = self.image_cache.fetch(image.get("thumb_url") or image.get("url") or "")
            if path:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    button.setIcon(QIcon(pixmap))
            if rows:
                rows[idx // 2].addWidget(button)
            else:
                layout.addWidget(button)
        return container

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("返信", lambda: self.reply_requested.emit(self.message))
        if not self.message.get("dm_uuid"):
            menu.addAction("スレッド", lambda: self.thread_requested.emit(self.message))
        if self.message.get("can_edit"):
            menu.addAction("編集", lambda: self.edit_requested.emit(self.message))
        if self.message.get("can_delete"):
            menu.addAction("削除", lambda: self.delete_requested.emit(self.message))
        react_menu = menu.addMenu("リアクション")
        for emoji in self.emojis:
            react_menu.addAction(emoji, lambda checked=False, e=emoji: self.reaction_requested.emit(self.message, e))
        menu.exec(event.globalPos())
