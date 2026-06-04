from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QFormLayout, QLineEdit, QMessageBox, QPushButton, QVBoxLayout


class LoginWindow(QDialog):
    login_requested = Signal(str, str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MFUチャット ログイン")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.save_password = QCheckBox("Windows資格情報マネージャーにパスワードを保存")
        self.submit = QPushButton("ログイン")
        self.submit.clicked.connect(self._submit)
        form = QFormLayout()
        form.addRow("ユーザー名", self.username)
        form.addRow("パスワード", self.password)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.save_password)
        layout.addWidget(self.submit)

    def _submit(self) -> None:
        self.login_requested.emit(self.username.text().strip(), self.password.text(), self.save_password.isChecked())

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "ログイン失敗", text)
