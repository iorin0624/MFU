from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


class ImageViewer(QDialog):
    def __init__(self, images: list[dict[str, Any]], index: int, cache: Any) -> None:
        super().__init__()
        self.images = images
        self.index = index
        self.cache = cache
        self.setWindowTitle("Image")
        self.resize(900, 700)
        self.label = QLabel(alignment=Qt.AlignCenter)
        QVBoxLayout(self).addWidget(self.label)
        self._render()

    def _render(self) -> None:
        image = self.images[self.index]
        path = self.cache.fetch(image.get("url") or image.get("thumb_url") or "")
        if path:
            pixmap = QPixmap(path)
            self.label.setPixmap(pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
        elif event.key() == Qt.Key_Right and self.index < len(self.images) - 1:
            self.index += 1
            self._render()
        elif event.key() == Qt.Key_Left and self.index > 0:
            self.index -= 1
            self._render()
