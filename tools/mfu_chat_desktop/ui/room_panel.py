from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from core.models import ChatTarget


class RoomPanel(QWidget):
    target_selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.targets: list[ChatTarget] = []
        self.list = QListWidget()
        self.list.itemClicked.connect(self._clicked)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)

    def set_targets(self, targets: list[ChatTarget]) -> None:
        self.targets = targets
        self.list.clear()
        for target in targets:
            suffix = f" ({target.unread_count})" if target.unread_count else ""
            item = QListWidgetItem(f"{target.title}{suffix}")
            item.setData(32, target.key)
            self.list.addItem(item)

    def _clicked(self, item: QListWidgetItem) -> None:
        key = item.data(32)
        for target in self.targets:
            if target.key == key:
                self.target_selected.emit(target)
                return
