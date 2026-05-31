from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


class ReactionDialog(QDialog):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.setWindowTitle("Reactions")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(text or "No reactions"))
