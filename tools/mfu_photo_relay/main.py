from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import socketio
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core import (
    APP_DIR,
    NAMING_DATETIME,
    NAMING_ORIGINAL,
    NAMING_SEQUENCE,
    choose_output_path,
    load_history,
    load_settings,
    save_history,
    save_settings,
)


APP_NAME = "MFU写真転送"
APP_VERSION = "1.0.1"
TOKEN_PATH = APP_DIR / "receiver_token.bin"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_token(token: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    raw, _buffer = _blob(token.encode("utf-8"))
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(raw), APP_NAME, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        TOKEN_PATH.write_bytes(ctypes.string_at(output.pbData, output.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def load_token() -> str:
    try:
        encrypted = TOKEN_PATH.read_bytes()
        raw, _buffer = _blob(encrypted)
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(raw), None, None, None, None, 0, ctypes.byref(output)
        ):
            return ""
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)
    except (OSError, UnicodeError):
        return ""


def clear_token() -> None:
    try:
        TOKEN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def set_auto_start(enabled: bool) -> None:
    import winreg

    command = f'"{sys.executable}"'
    if not getattr(sys, "frozen", False):
        command += f' "{Path(__file__).resolve()}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, "MFUPhotoRelay", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "MFUPhotoRelay")
            except FileNotFoundError:
                pass


class ApiClient:
    def __init__(self, settings: dict, token: str) -> None:
        self.settings = settings
        self.token = token
        self.session = requests.Session()

    @property
    def base_url(self) -> str:
        return str(self.settings["base_url"]).rstrip("/")

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get_session(self) -> dict:
        response = self.session.get(
            self.base_url + "/desktop/photo-relay/api/session",
            headers=self.headers(), timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        response = self.session.post(
            self.base_url + path, headers=self.headers(), json=payload, timeout=timeout
        )
        response.raise_for_status()
        return response.json()


class ReceiverThread(QThread):
    status_changed = Signal(str, str)
    file_saved = Signal(str)
    error = Signal(str)
    authentication_failed = Signal()

    def __init__(self, settings: dict, token: str) -> None:
        super().__init__()
        self.settings = settings
        self.token = token
        self.api = ApiClient(settings, token)
        self.sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self.work: queue.Queue[dict | None] = queue.Queue()
        self.pending: set[int] = set()
        self.pending_lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self._bind()

    def _bind(self) -> None:
        @self.sio.event(namespace="/photo-relay")
        def connect():
            self.status_changed.emit("green", "MFUへリアルタイム接続中")

        @self.sio.event(namespace="/photo-relay")
        def disconnect():
            self.status_changed.emit("red", "MFUから切断されました。再接続しています")

        @self.sio.on("connect_error", namespace="/photo-relay")
        def connect_error(data):
            message = str(data or "接続できませんでした")
            if "401" in message or "unauthorized" in message.lower():
                self.authentication_failed.emit()
            self.status_changed.emit("red", message)

        @self.sio.on("photo_relay_connected", namespace="/photo-relay")
        def connected(data):
            for item in (data or {}).get("queued_files") or []:
                self.enqueue(item)

        @self.sio.on("photo_relay_queue", namespace="/photo-relay")
        def queue_snapshot(data):
            for item in (data or {}).get("files") or []:
                self.enqueue(item)

        @self.sio.on("photo_relay_file_ready", namespace="/photo-relay")
        def file_ready(data):
            if isinstance(data, dict):
                self.enqueue(data)

    def enqueue(self, payload: dict) -> None:
        file_id = int(payload.get("id") or 0)
        if not file_id:
            return
        with self.pending_lock:
            if file_id in self.pending:
                return
            self.pending.add(file_id)
        self.work.put(payload)

    def _work_loop(self) -> None:
        while True:
            payload = self.work.get()
            if payload is None:
                return
            file_id = int(payload["id"])
            try:
                self._receive(payload)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    self.authentication_failed.emit()
                self._report_failure(file_id, str(exc))
            except Exception as exc:
                self._report_failure(file_id, str(exc))
            finally:
                with self.pending_lock:
                    self.pending.discard(file_id)

    def _report_failure(self, file_id: int, message: str) -> None:
        try:
            self.api.post(f"/desktop/photo-relay/api/files/{file_id}/fail", {"error": message})
        except Exception:
            pass
        self.error.emit(message)

    def _receive(self, payload: dict) -> None:
        file_id = int(payload["id"])
        history = load_history()
        previous = history.get(str(file_id)) or {}
        previous_path = Path(previous.get("path") or "")
        if previous_path.is_file() and previous.get("sha256") == payload.get("sha256"):
            self.api.post(
                f"/desktop/photo-relay/api/files/{file_id}/ack",
                {"sha256": payload["sha256"]},
            )
            return

        target = Path(str(self.settings["target_folder"])).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        temporary = target / f".mfu-photo-relay-{file_id}.part"
        digest = hashlib.sha256()
        size = 0
        response = self.api.session.get(
            self.api.base_url + str(payload["download_path"]),
            headers=self.api.headers(), stream=True, timeout=(20, 300),
        )
        response.raise_for_status()
        try:
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            actual_hash = digest.hexdigest()
            if actual_hash != str(payload["sha256"]).lower() or size != int(payload["size_bytes"]):
                raise RuntimeError("受信ファイルの整合性確認に失敗しました。")
            result = choose_output_path(
                target,
                payload,
                naming_mode=str(self.settings.get("naming_mode")),
                sequence_digits=int(self.settings.get("sequence_digits") or 5),
                next_sequence=int(self.settings.get("next_sequence") or 1),
            )
            os.replace(temporary, result.path)
            if str(self.settings.get("naming_mode")) == NAMING_SEQUENCE:
                self.settings["next_sequence"] = result.next_sequence
                save_settings(self.settings)
            history[str(file_id)] = {"path": str(result.path), "sha256": actual_hash}
            save_history(history)
            self.api.post(
                f"/desktop/photo-relay/api/files/{file_id}/ack",
                {"sha256": actual_hash},
            )
            self.file_saved.emit(str(result.path))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def run(self) -> None:
        self.worker = threading.Thread(target=self._work_loop, daemon=True)
        self.worker.start()
        self.status_changed.emit("red", "MFUへ接続しています")
        try:
            self.sio.connect(
                self.api.base_url,
                auth={"token": self.token},
                namespaces=["/photo-relay"],
                transports=["websocket", "polling"],
                wait_timeout=20,
            )
            self.sio.wait()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.work.put(None)

    def stop(self) -> None:
        try:
            if self.sio.connected:
                self.sio.disconnect()
        finally:
            self.work.put(None)


class LoginDialog(QDialog):
    token_received = Signal(str)

    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.state = secrets.token_urlsafe(24)
        self.received_token = ""
        self.received_error = ""
        self.server: HTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.setWindowTitle("MFU写真転送 ログイン")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("ChromeでMFUへログインし、「MFU写真転送を許可」を押してください。"))
        open_button = QPushButton("Chromeでログイン")
        open_button.clicked.connect(self.open_browser)
        layout.addWidget(open_button)
        close_button = QPushButton("キャンセル")
        close_button.clicked.connect(self.reject)
        layout.addWidget(close_button)
        self._start_callback_server()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(250)

    def _start_callback_server(self) -> None:
        dialog = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                token = (query.get("token") or [""])[0]
                state = (query.get("state") or [""])[0]
                if state != dialog.state or not token:
                    dialog.received_error = "認可結果が不正です。"
                    status, body = 400, "Login failed. You may close this window."
                else:
                    dialog.received_token = token
                    status, body = 200, "MFU Photo Relay login completed. You may close this window."
                encoded = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format, *_args):
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def open_browser(self) -> None:
        if not self.server:
            QMessageBox.warning(self, APP_NAME, "ローカル認証サーバーを開始できませんでした。")
            return
        callback = f"http://127.0.0.1:{self.server.server_port}/callback"
        params = urlencode(
            {
                "callback": callback,
                "state": self.state,
                "device_uuid": self.settings["device_uuid"],
                "device_name": self.settings["device_name"],
            }
        )
        webbrowser.open(str(self.settings["base_url"]).rstrip("/") + "/desktop/photo-relay/login/start?" + params)

    def _poll(self) -> None:
        if self.received_error:
            QMessageBox.warning(self, APP_NAME, self.received_error)
            self.received_error = ""
        if self.received_token:
            token = self.received_token
            self.received_token = ""
            self.token_received.emit(token)
            self.accept()

    def done(self, result: int) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        super().done(result)


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.values = dict(settings)
        self.setWindowTitle("MFU写真転送 設定")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.device_name = QLineEdit(str(settings["device_name"]))
        form.addRow("端末名", self.device_name)
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit(str(settings["target_folder"]))
        choose = QPushButton("選択")
        choose.clicked.connect(self.choose_folder)
        folder_layout.addWidget(self.folder)
        folder_layout.addWidget(choose)
        form.addRow("保存先", folder_row)
        self.naming = QComboBox()
        self.naming.addItem("そのままの名前", NAMING_ORIGINAL)
        self.naming.addItem("yyyy-mm-dd_HHmmss（撮影日時）", NAMING_DATETIME)
        self.naming.addItem("連番", NAMING_SEQUENCE)
        self.naming.setCurrentIndex(max(0, self.naming.findData(settings["naming_mode"])))
        form.addRow("ファイル名", self.naming)
        self.digits = QSpinBox()
        self.digits.setRange(1, 12)
        self.digits.setValue(int(settings["sequence_digits"]))
        form.addRow("連番の桁数", self.digits)
        self.next_number = QSpinBox()
        self.next_number.setRange(0, 999_999_999)
        self.next_number.setValue(int(settings["next_sequence"]))
        form.addRow("次の連番", self.next_number)
        self.auto_start = QCheckBox("Windowsログイン時に自動起動")
        self.auto_start.setChecked(bool(settings.get("auto_start")))
        form.addRow("", self.auto_start)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        cancel = QPushButton("キャンセル")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.clicked.connect(self.accept)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "保存先フォルダー", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def result_values(self) -> dict:
        values = dict(self.values)
        values.update(
            {
                "device_name": self.device_name.text().strip() or "Windows PC",
                "target_folder": self.folder.text().strip(),
                "naming_mode": self.naming.currentData(),
                "sequence_digits": self.digits.value(),
                "next_sequence": self.next_number.value(),
                "auto_start": self.auto_start.isChecked(),
            }
        )
        return values


class PhotoRelayController:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.settings = load_settings()
        self.token = load_token()
        self.receiver: ReceiverThread | None = None
        self.tray = QSystemTrayIcon(
            app.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon), app
        )
        self.tray.setToolTip(APP_NAME)
        menu = self._build_menu()
        self.tray.setContextMenu(menu)
        self.tray.show()
        if self.token:
            self.start_receiver()
        else:
            self.set_status("red", "未ログイン")

    def _build_menu(self):
        from PySide6.QtWidgets import QMenu

        menu = QMenu()
        self.status_action = QAction("🔴 未ログイン", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        version = QAction(f"バージョン {APP_VERSION}", menu)
        version.setEnabled(False)
        menu.addAction(version)
        login = QAction("MFUへログイン", menu)
        login.triggered.connect(self.login)
        menu.addAction(login)
        check = QAction("ログイン確認", menu)
        check.triggered.connect(self.check_login)
        menu.addAction(check)
        settings = QAction("設定", menu)
        settings.triggered.connect(self.open_settings)
        menu.addAction(settings)
        menu.addSeparator()
        logout = QAction("ログアウト", menu)
        logout.triggered.connect(lambda: self.logout(False))
        menu.addAction(logout)
        unregister = QAction("この端末の登録解除", menu)
        unregister.triggered.connect(lambda: self.logout(True))
        menu.addAction(unregister)
        menu.addSeparator()
        exit_action = QAction("終了", menu)
        exit_action.triggered.connect(self.quit)
        menu.addAction(exit_action)
        return menu

    def set_status(self, color: str, detail: str) -> None:
        icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(color, "🔴")
        self.status_action.setText(f"{icon} {detail}")
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}\n{detail}")

    def login(self) -> None:
        dialog = LoginDialog(self.settings)
        dialog.token_received.connect(self.finish_login)
        dialog.exec()

    def finish_login(self, token: str) -> None:
        try:
            client = ApiClient(self.settings, token)
            details = client.get_session()
            protect_token(token)
            self.token = token
            self.settings["device_name"] = details.get("device_name") or self.settings["device_name"]
            save_settings(self.settings)
            self.start_receiver()
            QMessageBox.information(None, APP_NAME, "MFUへログインしました。")
        except Exception as exc:
            QMessageBox.warning(None, APP_NAME, f"ログイン確認に失敗しました。\n{exc}")

    def check_login(self) -> None:
        if not self.token:
            QMessageBox.information(None, APP_NAME, "MFUへログインしていません。")
            return
        try:
            details = ApiClient(self.settings, self.token).get_session()
            QMessageBox.information(None, APP_NAME, f"{details['username']} としてログイン済みです。")
        except Exception as exc:
            QMessageBox.warning(None, APP_NAME, f"ログインを確認できません。\n{exc}")

    def start_receiver(self) -> None:
        if self.receiver and self.receiver.isRunning():
            self.receiver.stop()
            self.receiver.wait(3000)
        self.receiver = ReceiverThread(self.settings, self.token)
        self.receiver.status_changed.connect(self.set_status)
        self.receiver.file_saved.connect(self.on_saved)
        self.receiver.error.connect(self.on_error)
        self.receiver.authentication_failed.connect(self.on_auth_failed)
        self.receiver.start()

    def on_saved(self, path: str) -> None:
        self.tray.showMessage(APP_NAME, f"写真を保存しました。\n{path}", QSystemTrayIcon.MessageIcon.Information, 5000)

    def on_error(self, message: str) -> None:
        self.tray.showMessage(APP_NAME, f"写真転送に失敗しました。\n{message}", QSystemTrayIcon.MessageIcon.Warning, 8000)

    def on_auth_failed(self) -> None:
        self.set_status("red", "再ログインが必要です")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        old_name = self.settings.get("device_name")
        self.settings = dialog.result_values()
        save_settings(self.settings)
        set_auto_start(bool(self.settings.get("auto_start")))
        if self.token and old_name != self.settings.get("device_name"):
            try:
                ApiClient(self.settings, self.token).post(
                    "/desktop/photo-relay/api/device",
                    {"device_name": self.settings["device_name"]},
                )
            except Exception as exc:
                QMessageBox.warning(None, APP_NAME, f"端末名をサーバーへ反映できませんでした。\n{exc}")
        if self.token:
            self.start_receiver()

    def logout(self, unregister: bool) -> None:
        if not self.token:
            return
        if unregister and QMessageBox.question(
            None, APP_NAME, "この端末の登録を解除しますか？待機中の写真は受信できなくなります。"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            ApiClient(self.settings, self.token).post(
                "/desktop/photo-relay/api/revoke", {"unregister": unregister}
            )
        except Exception:
            pass
        if self.receiver:
            self.receiver.stop()
            self.receiver.wait(3000)
            self.receiver = None
        clear_token()
        self.token = ""
        self.set_status("red", "未ログイン")

    def quit(self) -> None:
        if self.receiver:
            self.receiver.stop()
            self.receiver.wait(3000)
        self.tray.hide()
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    controller = PhotoRelayController(app)
    app._photo_relay_controller = controller
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
