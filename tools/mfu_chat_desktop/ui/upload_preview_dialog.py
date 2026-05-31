from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


class UploadPreviewDialog(QDialog):
    def __init__(self, files: list[str]) -> None:
        super().__init__()
        self.setWindowTitle("Upload preview")
        layout = QVBoxLayout(self)
        for path in files:
            layout.addWidget(QLabel(path))
