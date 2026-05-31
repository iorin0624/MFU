from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QFormLayout, QLineEdit, QMessageBox, QPushButton, QVBoxLayout


class LoginWindow(QDialog):
    login_requested = Signal(str, str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MFU Chat Login")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.save_password = QCheckBox("Save password with Windows Credential Manager")
        self.submit = QPushButton("Login")
        self.submit.clicked.connect(self._submit)
        form = QFormLayout()
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save_password)
        layout.addWidget(self.submit)

    def _submit(self) -> None:
        self.login_requested.emit(self.username.text().strip(), self.password.text(), self.save_password.isChecked())

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "Login failed", text)
