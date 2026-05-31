from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout


class SettingsDialog(QDialog):
    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.setWindowTitle("Settings")
        self.base_url = QLineEdit(settings.get("base_url", ""))
        self.notifications = QCheckBox("Notifications")
        self.notifications.setChecked(settings.get("notifications_enabled", True))
        self.enter_sends = QCheckBox("Enter sends message")
        self.enter_sends.setChecked(settings.get("enter_sends", False))
        form = QFormLayout()
        form.addRow("Base URL", self.base_url)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.notifications)
        layout.addWidget(self.enter_sends)
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        layout.addWidget(ok)

    def values(self) -> dict:
        return {
            "base_url": self.base_url.text().strip(),
            "notifications_enabled": self.notifications.isChecked(),
            "enter_sends": self.enter_sends.isChecked(),
        }
