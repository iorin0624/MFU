from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests
from PySide6.QtCore import QObject, Qt, Signal, QThread
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MFU Windows Uploader"
DEFAULT_BASE_URL = "https://mfu.iori0624.jp"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "MFU" / "MFU Windows Uploader"
CONFIG_PATH = CONFIG_DIR / "settings.json"
DEFAULT_MAGICK = Path(r"C:\Program Files\ImageMagick-7.1.2-Q16\magick.exe")


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_base_url(value: str) -> str:
    value = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if not value:
        return DEFAULT_BASE_URL
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def ymd_to_iso(value: str) -> str:
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        try:
            return dt.datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    if value:
        try:
            return dt.date.fromisoformat(value).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return dt.date.today().strftime("%Y-%m-%d")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_chrome() -> Path | None:
    roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        path = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if path.is_file():
            return path
    return None


class ApiClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = token.strip()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MFUWindowsUploader/1.0"})

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        return self.base_url + path

    def session_info(self) -> dict[str, Any]:
        response = self.session.get(self._url("/desktop/uploader/api/session"), headers=self._headers(), timeout=20)
        if response.status_code == 401:
            return {"ok": False, "authenticated": False}
        response.raise_for_status()
        return response.json()

    def modes(self) -> dict[str, Any]:
        response = self.session.get(self._url("/api/ext/up/modes"), headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def create_upload(self, title: str, date_iso: str, mode: str) -> dict[str, Any]:
        body = {"title": title, "date": date_iso, "mode": mode}
        response = self.session.post(self._url("/api/ext/up/create"), json=body, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def upload_original(self, uuid: str, path: Path) -> dict[str, Any]:
        with path.open("rb") as fh:
            files = {"file": (path.name, fh)}
            data = {"uuid": uuid}
            response = self.session.post(
                self._url("/api/ext/up/original"),
                data=data,
                files=files,
                headers=self._headers(),
                timeout=1800,
            )
        response.raise_for_status()
        return response.json()

    def upload_thumb(self, uuid: str, original_name: str, thumb_path: Path) -> dict[str, Any]:
        with thumb_path.open("rb") as fh:
            files = {"file": (thumb_path.name, fh, "image/webp")}
            data = {"uuid": uuid, "base": original_name}
            response = self.session.post(
                self._url("/api/ext/up/thumb"),
                data=data,
                files=files,
                headers=self._headers(),
                timeout=1800,
            )
        response.raise_for_status()
        return response.json()

    def done(self, uuid: str) -> dict[str, Any]:
        response = self.session.post(self._url("/api/ext/up/done"), json={"uuid": uuid}, headers=self._headers(), timeout=60)
        response.raise_for_status()
        return response.json()

    def revoke(self) -> None:
        if not self.token:
            return
        self.session.post(self._url("/desktop/uploader/api/revoke"), headers=self._headers(), timeout=20)


class LoginResult:
    def __init__(self) -> None:
        self.token = ""
        self.state = ""
        self.error = ""


def run_login_flow(base_url: str) -> str:
    state = secrets.token_urlsafe(24)
    port = find_free_port()
    callback = f"http://127.0.0.1:{port}/callback"
    result = LoginResult()
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            values = parse_qs(parsed.query)
            result.token = (values.get("token") or [""])[0]
            result.state = (values.get("state") or [""])[0]
            result.error = (values.get("error") or [""])[0]
            ok = bool(result.token and result.state == state)
            body = (
                "<html><meta charset='utf-8'><body>"
                "<h1>ログインが完了しました</h1><p>このタブを閉じてアップローダーへ戻ってください。</p>"
                "</body></html>"
                if ok
                else "<html><meta charset='utf-8'><body><h1>ログインに失敗しました</h1></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            ready.set()

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    login_url = normalize_base_url(base_url) + "/desktop/uploader/login/start?" + urlencode({"callback": callback, "state": state})
    chrome = find_chrome()
    try:
        if chrome:
            subprocess.Popen([str(chrome), login_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(login_url)
        if not ready.wait(180):
            raise RuntimeError("ログイン待機がタイムアウトしました。")
        if result.error:
            raise RuntimeError(result.error)
        if not result.token or result.state != state:
            raise RuntimeError("callback の検証に失敗しました。")
        return result.token
    finally:
        server.shutdown()
        server.server_close()


class LoginWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    def run(self) -> None:
        try:
            self.finished.emit(run_login_flow(self.base_url))
        except Exception as exc:
            self.failed.emit(str(exc))


class UploadWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        api: ApiClient,
        files: list[Path],
        title: str,
        date_iso: str,
        mode: str,
        magick: Path,
        thumb_size: int,
        thumb_quality: int,
        thumb_method: int,
        parallel: int,
        skip_thumbs: bool,
    ) -> None:
        super().__init__()
        self.api = api
        self.files = files
        self.title = title
        self.date_iso = date_iso
        self.mode = mode
        self.magick = magick
        self.thumb_size = thumb_size
        self.thumb_quality = thumb_quality
        self.thumb_method = thumb_method
        self.parallel = max(1, parallel)
        self.skip_thumbs = skip_thumbs

    def _convert_one(self, source: Path, out_dir: Path) -> tuple[Path, Path | None, str]:
        out_path = out_dir / f"{source.stem}.webp"
        args = []
        if source.suffix.lower() in {".jpg", ".jpeg"}:
            args.extend(["-define", "jpeg:size=2048x2048"])
        args.extend(
            [
                "-limit",
                "thread",
                "0",
                str(source),
                "-filter",
                "box",
                "-resize",
                f"{self.thumb_size}x{self.thumb_size}>",
                "-strip",
                "-quality",
                str(self.thumb_quality),
                "-define",
                f"webp:method={self.thumb_method}",
                str(out_path),
            ]
        )
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.run([str(self.magick), *args], capture_output=True, text=True, startupinfo=startupinfo)
        if proc.returncode != 0 or not out_path.is_file():
            return source, None, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return source, out_path, ""

    def _make_thumbs(self, temp_root: Path) -> list[tuple[Path, Path]]:
        out_dir = temp_root / "thumb"
        out_dir.mkdir(parents=True, exist_ok=True)
        made: list[tuple[Path, Path]] = []
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            futures = [pool.submit(self._convert_one, path, out_dir) for path in self.files]
            done = 0
            for future in as_completed(futures):
                source, thumb, error = future.result()
                done += 1
                if thumb:
                    made.append((source, thumb))
                    self.log.emit(f"サムネ生成 OK: {source.name}")
                else:
                    self.log.emit(f"サムネ生成 NG: {source.name} {error}")
                self.progress.emit(min(82, 70 + int(12 * done / max(1, len(futures)))))
        return made

    def run(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="mfu_uploader_"))
        try:
            self.log.emit("アップロード枠を作成しています...")
            slot = self.api.create_upload(self.title, self.date_iso, self.mode)
            if not slot.get("ok"):
                raise RuntimeError(str(slot))
            uuid = str(slot.get("uuid") or "")
            password = str(slot.get("password") or "")
            self.log.emit(f"UUID: {uuid}")
            self.progress.emit(10)

            for index, path in enumerate(self.files, 1):
                self.log.emit(f"原本送信 ({index}/{len(self.files)}): {path.name}")
                self.api.upload_original(uuid, path)
                self.progress.emit(min(70, 10 + int(60 * index / max(1, len(self.files)))))

            if self.skip_thumbs:
                self.log.emit("サムネ送信をスキップしました。")
            elif not self.magick.is_file():
                self.log.emit(f"magick.exe が見つからないためサムネをスキップしました: {self.magick}")
            else:
                thumbs = self._make_thumbs(temp_root)
                for index, (source, thumb) in enumerate(thumbs, 1):
                    self.log.emit(f"サムネ送信 ({index}/{len(thumbs)}): {thumb.name}")
                    self.api.upload_thumb(uuid, source.name, thumb)
                    self.progress.emit(min(95, 82 + int(13 * index / max(1, len(thumbs)))))

            self.log.emit("完了通知を送信しています...")
            self.api.done(uuid)
            self.progress.emit(100)
            self.finished.emit({"uuid": uuid, "password": password})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.files: list[Path] = []
        self.login_thread: QThread | None = None
        self.login_worker: LoginWorker | None = None
        self.upload_thread: QThread | None = None
        self.upload_worker: UploadWorker | None = None
        self.setWindowTitle(APP_NAME)
        self.resize(920, 720)

        self.base_url = QLineEdit(normalize_base_url(str(self.config.get("base_url") or DEFAULT_BASE_URL)))
        self.user_label = QLabel("未ログイン")
        self.login_btn = QPushButton("Chromeでログイン")
        self.logout_btn = QPushButton("ログアウト")
        self.refresh_btn = QPushButton("モード取得")
        self.mode_box = QComboBox()
        self.title_edit = QLineEdit("Windows Upload")
        self.date_edit = QLineEdit(dt.date.today().strftime("%Y%m%d"))
        self.magick_edit = QLineEdit(str(self.config.get("magick_path") or DEFAULT_MAGICK))
        self.browse_magick_btn = QPushButton("参照")
        self.file_label = QLabel("未選択")
        self.pick_btn = QPushButton("ファイル選択")
        self.run_btn = QPushButton("アップロード開始")
        self.progress = QProgressBar()
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)

        self.thumb_size = QSpinBox()
        self.thumb_size.setRange(64, 4096)
        self.thumb_size.setValue(int(self.config.get("thumb_size") or 250))
        self.thumb_quality = QSpinBox()
        self.thumb_quality.setRange(1, 100)
        self.thumb_quality.setValue(int(self.config.get("thumb_quality") or 75))
        self.thumb_method = QSpinBox()
        self.thumb_method.setRange(0, 6)
        self.thumb_method.setValue(int(self.config.get("thumb_method") or 3))
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 64)
        self.parallel.setValue(int(self.config.get("parallel") or min(8, (os.cpu_count() or 4))))
        self.skip_thumbs = QCheckBox("サムネ送信をスキップ")
        self.skip_thumbs.setChecked(bool(self.config.get("skip_thumbs") or False))

        self._build_ui()
        self._connect()
        self._apply_token_state()
        self.check_session(load_modes=False)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        auth = QGroupBox("接続")
        auth_layout = QGridLayout(auth)
        auth_layout.addWidget(QLabel("API Base URL"), 0, 0)
        auth_layout.addWidget(self.base_url, 0, 1, 1, 4)
        auth_layout.addWidget(QLabel("ログイン"), 1, 0)
        auth_layout.addWidget(self.user_label, 1, 1)
        auth_layout.addWidget(self.login_btn, 1, 2)
        auth_layout.addWidget(self.logout_btn, 1, 3)
        auth_layout.addWidget(self.refresh_btn, 1, 4)
        layout.addWidget(auth)

        info = QGroupBox("アップロード")
        form = QFormLayout(info)
        form.addRow("Mode", self.mode_box)
        form.addRow("タイトル", self.title_edit)
        form.addRow("撮影日 (yyyymmdd / yyyy-mm-dd)", self.date_edit)
        layout.addWidget(info)

        thumbs = QGroupBox("サムネ生成")
        thumb_grid = QGridLayout(thumbs)
        thumb_grid.addWidget(QLabel("magick.exe"), 0, 0)
        thumb_grid.addWidget(self.magick_edit, 0, 1, 1, 5)
        thumb_grid.addWidget(self.browse_magick_btn, 0, 6)
        thumb_grid.addWidget(QLabel("サイズ(px)"), 1, 0)
        thumb_grid.addWidget(self.thumb_size, 1, 1)
        thumb_grid.addWidget(QLabel("品質"), 1, 2)
        thumb_grid.addWidget(self.thumb_quality, 1, 3)
        thumb_grid.addWidget(QLabel("method"), 1, 4)
        thumb_grid.addWidget(self.thumb_method, 1, 5)
        thumb_grid.addWidget(QLabel("並列数"), 2, 0)
        thumb_grid.addWidget(self.parallel, 2, 1)
        thumb_grid.addWidget(self.skip_thumbs, 2, 2, 1, 3)
        layout.addWidget(thumbs)

        files = QHBoxLayout()
        files.addWidget(self.pick_btn)
        files.addWidget(self.file_label, 1)
        layout.addLayout(files)

        run = QHBoxLayout()
        run.addWidget(self.run_btn)
        run.addWidget(self.progress, 1)
        layout.addLayout(run)
        layout.addWidget(self.log_box, 1)

    def _connect(self) -> None:
        self.login_btn.clicked.connect(self.login)
        self.logout_btn.clicked.connect(self.logout)
        self.refresh_btn.clicked.connect(lambda: self.check_session(load_modes=True))
        self.pick_btn.clicked.connect(self.pick_files)
        self.run_btn.clicked.connect(self.start_upload)
        self.browse_magick_btn.clicked.connect(self.pick_magick)
        self.base_url.editingFinished.connect(self.save_settings)
        self.magick_edit.editingFinished.connect(self.save_settings)

    def api(self) -> ApiClient:
        return ApiClient(self.base_url.text(), str(self.config.get("api_token") or ""))

    def log(self, message: str) -> None:
        self.log_box.appendPlainText(f"[{_now()}] {message}")

    def save_settings(self) -> None:
        self.config.update(
            {
                "base_url": normalize_base_url(self.base_url.text()),
                "magick_path": self.magick_edit.text().strip(),
                "thumb_size": self.thumb_size.value(),
                "thumb_quality": self.thumb_quality.value(),
                "thumb_method": self.thumb_method.value(),
                "parallel": self.parallel.value(),
                "skip_thumbs": self.skip_thumbs.isChecked(),
            }
        )
        self.base_url.setText(self.config["base_url"])
        save_config(self.config)

    def _apply_token_state(self) -> None:
        logged_in = bool(self.config.get("api_token"))
        self.logout_btn.setEnabled(logged_in)
        self.refresh_btn.setEnabled(logged_in)
        self.run_btn.setEnabled(logged_in)

    def check_session(self, load_modes: bool) -> None:
        if not self.config.get("api_token"):
            self.user_label.setText("未ログイン")
            self._apply_token_state()
            return
        try:
            info = self.api().session_info()
            if info.get("authenticated"):
                self.user_label.setText(str(info.get("username") or "ログイン済み"))
                self._apply_token_state()
                if load_modes:
                    self.load_modes()
            else:
                self.config.pop("api_token", None)
                save_config(self.config)
                self.user_label.setText("未ログイン")
                self._apply_token_state()
        except Exception as exc:
            self.log(f"セッション確認に失敗: {exc}")
            self._apply_token_state()

    def login(self) -> None:
        self.save_settings()
        self.login_btn.setEnabled(False)
        self.log("Chromeでログイン許可画面を開きます...")
        self.login_thread = QThread(self)
        worker = LoginWorker(self.base_url.text())
        self.login_worker = worker
        worker.moveToThread(self.login_thread)
        self.login_thread.started.connect(worker.run)
        worker.finished.connect(self._login_finished)
        worker.failed.connect(self._login_failed)
        worker.finished.connect(self.login_thread.quit)
        worker.failed.connect(self.login_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.finished.connect(lambda _: setattr(self, "login_worker", None))
        worker.failed.connect(lambda _: setattr(self, "login_worker", None))
        self.login_thread.finished.connect(self.login_thread.deleteLater)
        self.login_thread.start()

    def _login_finished(self, token: str) -> None:
        self.config["api_token"] = token
        save_config(self.config)
        self.login_btn.setEnabled(True)
        self.log("ログインしました。")
        self.check_session(load_modes=True)

    def _login_failed(self, error: str) -> None:
        self.login_btn.setEnabled(True)
        QMessageBox.warning(self, APP_NAME, f"ログインに失敗しました。\n{error}")
        self.log(f"ログイン失敗: {error}")

    def logout(self) -> None:
        try:
            self.api().revoke()
        except Exception:
            pass
        self.config.pop("api_token", None)
        save_config(self.config)
        self.mode_box.clear()
        self.user_label.setText("未ログイン")
        self._apply_token_state()
        self.log("ログアウトしました。")

    def load_modes(self) -> None:
        try:
            data = self.api().modes()
            self.mode_box.clear()
            default_mode = str(data.get("default_mode") or "")
            default_index = 0
            for index, row in enumerate(data.get("modes") or []):
                mode = str(row.get("mode") or "")
                label = str(row.get("label") or mode)
                if not mode:
                    continue
                self.mode_box.addItem(label, mode)
                if mode == default_mode:
                    default_index = index
            if self.mode_box.count():
                self.mode_box.setCurrentIndex(min(default_index, self.mode_box.count() - 1))
            self.log(f"モード取得: {self.mode_box.count()}件")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"モード取得に失敗しました。\n{exc}")
            self.log(f"モード取得失敗: {exc}")

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "アップロードする画像を選択",
            "",
            "Images (*.jpg *.jpeg *.png *.heic *.webp *.tif *.tiff);;All files (*.*)",
        )
        if not paths:
            return
        self.files = [Path(p) for p in paths]
        self.file_label.setText(f"{len(self.files)} ファイル選択")
        self.log(f"ファイル選択: {len(self.files)}件")

    def pick_magick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "magick.exe を選択", str(DEFAULT_MAGICK), "magick.exe (magick.exe);;All files (*.*)")
        if path:
            self.magick_edit.setText(path)
            self.save_settings()

    def start_upload(self) -> None:
        self.save_settings()
        if not self.config.get("api_token"):
            QMessageBox.information(self, APP_NAME, "先にログインしてください。")
            return
        if not self.files:
            QMessageBox.information(self, APP_NAME, "アップロードするファイルを選択してください。")
            return
        mode = str(self.mode_box.currentData() or "")
        if not mode:
            QMessageBox.information(self, APP_NAME, "モードを取得して選択してください。")
            return

        self.run_btn.setEnabled(False)
        self.progress.setValue(0)
        self.upload_thread = QThread(self)
        worker = UploadWorker(
            api=self.api(),
            files=self.files,
            title=self.title_edit.text().strip() or "Windows Upload",
            date_iso=ymd_to_iso(self.date_edit.text()),
            mode=mode,
            magick=Path(self.magick_edit.text().strip()),
            thumb_size=self.thumb_size.value(),
            thumb_quality=self.thumb_quality.value(),
            thumb_method=self.thumb_method.value(),
            parallel=self.parallel.value(),
            skip_thumbs=self.skip_thumbs.isChecked(),
        )
        self.upload_worker = worker
        worker.moveToThread(self.upload_thread)
        self.upload_thread.started.connect(worker.run)
        worker.log.connect(self.log)
        worker.progress.connect(self.progress.setValue)
        worker.finished.connect(self._upload_finished)
        worker.failed.connect(self._upload_failed)
        worker.finished.connect(self.upload_thread.quit)
        worker.failed.connect(self.upload_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.finished.connect(lambda _: setattr(self, "upload_worker", None))
        worker.failed.connect(lambda _: setattr(self, "upload_worker", None))
        self.upload_thread.finished.connect(self.upload_thread.deleteLater)
        self.upload_thread.start()

    def _upload_finished(self, result: dict) -> None:
        self.run_btn.setEnabled(True)
        uuid = result.get("uuid", "")
        password = result.get("password", "")
        self.log("DONE")
        QMessageBox.information(self, APP_NAME, f"完了しました。\nUUID: {uuid}\nPW: {password or '-'}")
        self.title_edit.setText("Windows Upload")
        self.date_edit.setText(dt.date.today().strftime("%Y%m%d"))
        self.files = []
        self.file_label.setText("未選択")
        self.progress.setValue(0)

    def _upload_failed(self, error: str) -> None:
        self.run_btn.setEnabled(True)
        self.log(f"ERROR: {error}")
        QMessageBox.warning(self, APP_NAME, f"アップロードに失敗しました。\n{error}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
