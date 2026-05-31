from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


class RoomMemberDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Room members")
        QVBoxLayout(self).addWidget(QLabel("Room member editing uses the existing room member API."))
