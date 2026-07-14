from __future__ import annotations

import datetime as dt
import hashlib
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
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtGui import QImageReader, QImageWriter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MFU Uploader"
DEFAULT_BASE_URL = "https://mfu.iori0624.jp"
DEFAULT_LAN_BASE_URL = "http://192.168.103.16:8080"
USER_AGENT = "MFUUploader/1.1"
DEFAULT_EXTENSIONS = ".jpg,.jpeg,.png,.webp,.heic,.tif,.tiff"


def app_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "MFU" / "MFU Uploader"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MFU" / "MFU Uploader"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "MFU" / "MFU Uploader"


CONFIG_DIR = app_config_dir()
CONFIG_PATH = CONFIG_DIR / "settings.json"
LEGACY_WINDOWS_CONFIG_PATH = (
    Path(os.environ.get("APPDATA", str(Path.home()))) / "MFU" / "MFU Windows Uploader" / "settings.json"
)


def default_magick_path() -> Path:
    candidates: list[Path] = []
    found = shutil.which("magick")
    if found:
        candidates.append(Path(found))
    if sys.platform == "win32":
        candidates.extend(
            [
                Path(r"C:\Program Files\ImageMagick-7.1.2-Q16\magick.exe"),
                Path(r"C:\Program Files\ImageMagick-7.1.1-Q16-HDRI\magick.exe"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend([Path("/opt/homebrew/bin/magick"), Path("/usr/local/bin/magick")])
    else:
        candidates.append(Path("/usr/bin/magick"))
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


DEFAULT_MAGICK = default_magick_path()


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH
    if not path.is_file() and sys.platform == "win32" and LEGACY_WINDOWS_CONFIG_PATH.is_file():
        path = LEGACY_WINDOWS_CONFIG_PATH
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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


def parse_extensions(value: str) -> set[str]:
    result: set[str] = set()
    for raw in (value or DEFAULT_EXTENSIONS).replace(";", ",").split(","):
        ext = raw.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        result.add(ext)
    return result or parse_extensions(DEFAULT_EXTENSIONS)


def collect_uploadable_paths(paths: list[Path], extensions: set[str]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            if path.is_dir():
                candidates = [p for p in path.rglob("*") if p.is_file()]
            elif path.is_file():
                candidates = [path]
            else:
                candidates = []
        except OSError:
            candidates = []
        for candidate in candidates:
            if candidate.suffix.lower() not in extensions:
                continue
            key = str(candidate.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
    return result


def wait_file_stable(path: Path, timeout: float = 30.0, interval: float = 0.7) -> bool:
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        if size > 0 and size == last_size:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0
            last_size = size
        time.sleep(interval)
    return False


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def retryable_upload_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in {408, 422, 429} or exc.response.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, RuntimeError))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_chrome() -> Path | None:
    if sys.platform == "darwin":
        for path in (Path("/Applications/Google Chrome.app"), Path.home() / "Applications" / "Google Chrome.app"):
            if path.exists():
                return path
        return None
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


def open_login_url(url: str) -> None:
    chrome = find_chrome()
    if sys.platform == "darwin" and chrome:
        subprocess.Popen(["open", "-a", str(chrome), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if sys.platform == "win32" and chrome:
        subprocess.Popen([str(chrome), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    webbrowser.open(url)


class ApiClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = token.strip()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        return self.base_url + path

    def clone(self) -> "ApiClient":
        return ApiClient(self.base_url, self.token)

    def probe_authenticated(self, timeout: float = 2.0) -> bool:
        response = self.session.get(
            self._url("/desktop/uploader/api/session"),
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code == 401:
            return False
        response.raise_for_status()
        return bool((response.json() or {}).get("authenticated"))

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
        response = self.session.post(
            self._url("/api/ext/up/create"),
            json={"title": title, "date": date_iso, "mode": mode},
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def upload_original(
        self,
        uuid: str,
        path: Path,
        *,
        client_file_id: str = "",
        sha256: str = "",
        file_size: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"uuid": uuid}
        if client_file_id:
            data.update(
                {
                    "client_file_id": client_file_id,
                    "sha256": sha256,
                    "file_size": str(file_size if file_size is not None else path.stat().st_size),
                }
            )
        with path.open("rb") as fh:
            response = self.session.post(
                self._url("/api/ext/up/original"),
                data=data,
                files={"file": (path.name, fh)},
                headers=self._headers(),
                timeout=1800,
            )
        response.raise_for_status()
        return response.json()

    def upload_thumb(self, uuid: str, original_name: str, thumb_path: Path) -> dict[str, Any]:
        with thumb_path.open("rb") as fh:
            response = self.session.post(
                self._url("/api/ext/up/thumb"),
                data={"uuid": uuid, "base": original_name},
                files={"file": (thumb_path.name, fh, "image/webp")},
                headers=self._headers(),
                timeout=1800,
            )
        response.raise_for_status()
        return response.json()

    def done(self, uuid: str) -> dict[str, Any]:
        response = self.session.post(self._url("/api/ext/up/done"), json={"uuid": uuid}, headers=self._headers(), timeout=60)
        response.raise_for_status()
        return response.json()

    def reconcile_thumbnails(self, uuid: str) -> dict[str, Any]:
        response = self.session.post(
            self._url("/api/ext/up/reconcile-thumbnails"),
            json={"uuid": uuid},
            headers=self._headers(),
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def revoke(self) -> None:
        if self.token:
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
            if ok:
                body = (
                    "<html><meta charset='utf-8'><body>"
                    "<h1>ログインが完了しました</h1>"
                    "<p>このタブを閉じてアップローダーへ戻ってください。</p>"
                    "</body></html>"
                )
            else:
                body = "<html><meta charset='utf-8'><body><h1>ログインに失敗しました</h1></body></html>"
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
    try:
        open_login_url(login_url)
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


class SessionWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, base_url: str, token: str, load_modes: bool) -> None:
        super().__init__()
        self.base_url = base_url
        self.token = token
        self.load_modes = load_modes

    def run(self) -> None:
        try:
            api = ApiClient(self.base_url, self.token)
            info = api.session_info()
            result: dict[str, Any] = {"session": info}
            if info.get("authenticated") and self.load_modes:
                try:
                    result["modes"] = api.modes()
                except Exception as exc:
                    result["modes_error"] = str(exc)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class UploadBase:
    def __init__(
        self,
        api: ApiClient,
        magick: Path,
        thumb_size: int,
        thumb_quality: int,
        thumb_method: int,
        parallel: int,
        skip_thumbs: bool,
    ) -> None:
        self.api = api
        self.magick = magick
        self.thumb_size = thumb_size
        self.thumb_quality = thumb_quality
        self.thumb_method = thumb_method
        self.parallel = max(1, parallel)
        self.skip_thumbs = skip_thumbs

    def _upload_original_with_retry(
        self,
        uuid: str,
        path: Path,
        client_file_id: str,
    ) -> dict[str, Any]:
        sha256, size = file_sha256(path)
        api = self.api.clone()
        last_error: Exception | None = None
        for attempt, delay in enumerate((0, 1, 3), 1):
            if delay:
                time.sleep(delay)
            try:
                result = api.upload_original(
                    uuid,
                    path,
                    client_file_id=client_file_id,
                    sha256=sha256,
                    file_size=size,
                )
                if not result.get("ok"):
                    raise RuntimeError(str(result))
                if str(result.get("sha256") or "").lower() != sha256:
                    raise RuntimeError("サーバーのSHA-256応答が一致しません。")
                if int(result.get("file_size") or -1) != size:
                    raise RuntimeError("サーバーのファイルサイズ応答が一致しません。")
                result["attempts"] = attempt
                result["client_file_id"] = client_file_id
                return result
            except Exception as exc:
                last_error = exc
                if attempt >= 3 or not retryable_upload_error(exc):
                    break
        raise RuntimeError(f"原本送信に失敗（3回まで試行）: {last_error}")

    def _convert_one(self, source: Path, out_dir: Path) -> tuple[Path, Path | None, str]:
        source_key = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:12]
        out_path = out_dir / f"{source.stem}_{source_key}.webp"
        if source.suffix.lower() in {".jpg", ".jpeg"}:
            reader = QImageReader(str(source))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                return source, None, reader.errorString() or "JPEGの読み込みに失敗しました。"
            if image.width() > self.thumb_size or image.height() > self.thumb_size:
                image = image.scaled(
                    self.thumb_size,
                    self.thumb_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            writer = QImageWriter(str(out_path), b"webp")
            writer.setQuality(self.thumb_quality)
            if not writer.write(image) or not out_path.is_file():
                return source, None, writer.errorString() or "WebPの書き出しに失敗しました。"
            return source, out_path, ""

        if not self.magick.is_file():
            return source, None, f"magick が見つかりません: {self.magick}"

        args: list[str] = []
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

    def upload_one(self, uuid: str, path: Path, temp_root: Path) -> str:
        original_result = self._upload_original_with_retry(uuid, path, secrets.token_hex(16))
        remote_name = str(original_result.get("saved") or path.name)
        if self.skip_thumbs:
            return "クライアントのサムネ送信はスキップ設定です。"
        if path.suffix.lower() not in {".jpg", ".jpeg"} and not self.magick.is_file():
            return f"magick が見つかりません: {self.magick}"
        out_dir = temp_root / "thumb"
        out_dir.mkdir(parents=True, exist_ok=True)
        _, thumb, error = self._convert_one(path, out_dir)
        if not thumb:
            return f"サムネ生成失敗: {error}"
        try:
            self.api.upload_thumb(uuid, remote_name, thumb)
        except Exception as exc:
            return f"サムネ送信失敗: {exc}"
        return ""


class UploadWorker(QObject):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)
    incomplete = Signal(dict)
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
        upload_parallel: int,
        skip_thumbs: bool,
        existing_uuid: str = "",
        existing_password: str = "",
        transfer_ids: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.files = files
        self.title = title
        self.date_iso = date_iso
        self.mode = mode
        self.upload_parallel = max(1, min(8, upload_parallel))
        self.existing_uuid = existing_uuid
        self.existing_password = existing_password
        supplied_ids = transfer_ids or {}
        self.transfer_ids = {
            str(path): supplied_ids.get(str(path)) or secrets.token_hex(16)
            for path in files
        }
        self.converter = UploadBase(
            api,
            magick,
            thumb_size,
            thumb_quality,
            thumb_method,
            parallel,
            skip_thumbs,
        )

    def _collect_thumbs(self, futures) -> list[tuple[Path, Path]]:
        made: list[tuple[Path, Path]] = []
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

    def _upload_original_task(self, uuid: str, path: Path) -> dict[str, Any]:
        client_file_id = self.transfer_ids[str(path)]
        self.log.emit(f"SHA-256計算・原本送信: {path.name}")
        try:
            result = self.converter._upload_original_with_retry(uuid, path, client_file_id)
            return {
                "ok": True,
                "path": path,
                "client_file_id": client_file_id,
                "saved": str(result.get("saved") or path.name),
                "attempts": int(result.get("attempts") or 1),
                "already_uploaded": bool(result.get("already_uploaded")),
            }
        except Exception as exc:
            return {
                "ok": False,
                "path": path,
                "client_file_id": client_file_id,
                "error": str(exc),
            }

    def _upload_originals_parallel(self, uuid: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        successes: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.upload_parallel) as pool:
            futures = [pool.submit(self._upload_original_task, uuid, path) for path in self.files]
            done_count = 0
            for future in as_completed(futures):
                result = future.result()
                done_count += 1
                path = result["path"]
                if result.get("ok"):
                    successes[str(path)] = result
                    retry_label = "・既存確認" if result.get("already_uploaded") else ""
                    self.log.emit(
                        f"原本送信 OK ({done_count}/{len(futures)}): {path.name} "
                        f"試行{result.get('attempts', 1)}回{retry_label}"
                    )
                else:
                    failures.append(result)
                    self.log.emit(
                        f"原本送信 NG ({done_count}/{len(futures)}): {path.name} {result.get('error')}"
                    )
                self.progress.emit(min(70, 10 + int(60 * done_count / max(1, len(futures)))))
        return successes, failures

    def _upload_thumbs_parallel(
        self,
        uuid: str,
        thumbs: list[tuple[Path, Path]],
        originals: dict[str, dict[str, Any]],
    ) -> None:
        targets = [(source, thumb, originals[str(source)]) for source, thumb in thumbs if str(source) in originals]
        if not targets:
            return

        def send(target):
            source, thumb, original = target
            api = self.converter.api.clone()
            api.upload_thumb(uuid, original["saved"], thumb)
            return source, thumb

        with ThreadPoolExecutor(max_workers=self.upload_parallel) as pool:
            future_map = {pool.submit(send, target): target for target in targets}
            done_count = 0
            for future in as_completed(future_map):
                done_count += 1
                source, thumb, _ = future_map[future]
                try:
                    future.result()
                    self.log.emit(f"サムネ送信 OK ({done_count}/{len(targets)}): {thumb.name}")
                except Exception as exc:
                    self.log.emit(f"サムネ送信 NG: {source.name} {exc}")
                self.progress.emit(min(95, 82 + int(13 * done_count / max(1, len(targets)))))

    def run(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="mfu_uploader_"))
        try:
            if self.existing_uuid:
                uuid = self.existing_uuid
                password = self.existing_password
                self.log.emit(f"失敗ファイルのみ再送します: UUID {uuid}")
            else:
                self.log.emit("アップロード枠を作成しています...")
                slot = self.converter.api.create_upload(self.title, self.date_iso, self.mode)
                if not slot.get("ok"):
                    raise RuntimeError(str(slot))
                uuid = str(slot.get("uuid") or "")
                password = str(slot.get("password") or "")
            self.log.emit(f"UUID: {uuid}")
            self.progress.emit(10)

            if self.converter.skip_thumbs:
                originals, failures = self._upload_originals_parallel(uuid)
                thumbs: list[tuple[Path, Path]] = []
                self.log.emit("サムネ送信をスキップしました。")
            else:
                out_dir = temp_root / "thumb"
                out_dir.mkdir(parents=True, exist_ok=True)
                self.log.emit(
                    f"原本送信(並列{self.upload_parallel})とサムネ生成"
                    f"(並列{self.converter.parallel})を同時開始します..."
                )
                with ThreadPoolExecutor(max_workers=self.converter.parallel) as thumb_pool:
                    thumb_futures = [
                        thumb_pool.submit(self.converter._convert_one, path, out_dir)
                        for path in self.files
                    ]
                    originals, failures = self._upload_originals_parallel(uuid)
                    thumbs = self._collect_thumbs(thumb_futures)
                self._upload_thumbs_parallel(uuid, thumbs, originals)

            if failures:
                self.incomplete.emit({
                    "uuid": uuid,
                    "password": password,
                    "failed": [
                        {
                            "path": str(item["path"]),
                            "client_file_id": item["client_file_id"],
                            "error": item.get("error") or "",
                        }
                        for item in failures
                    ],
                })
                return

            self.log.emit("完了通知を送信しています...")
            done_result = self.converter.api.done(uuid)
            self.progress.emit(100)
            self.finished.emit({
                "uuid": uuid,
                "password": password,
                "completion_url": str(done_result.get("completion_url") or ""),
            })
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


class MonitorSignals(QObject):
    log = Signal(str)
    status = Signal(str)
    uploaded = Signal(int)
    first_notified = Signal(str)
    failed = Signal(str)
    stopped = Signal()


class MonitorWorker(UploadBase):
    def __init__(
        self,
        api: ApiClient,
        folder: Path,
        extensions: set[str],
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
        super().__init__(api, magick, thumb_size, thumb_quality, thumb_method, parallel, skip_thumbs)
        self.signals = MonitorSignals()
        self.folder = folder
        self.extensions = extensions
        self.title = title
        self.date_iso = date_iso
        self.mode = mode
        self._stop = threading.Event()
        self._queue: list[Path] = []
        self._queued_keys: set[str] = set()
        self._uploaded_keys: set[str] = set()
        self._lock = threading.Lock()
        self._count = 0

    def stop(self) -> None:
        self._stop.set()

    def enqueue_external(self, paths: list[Path]) -> None:
        with self._lock:
            for path in collect_uploadable_paths(paths, self.extensions):
                key = str(path.resolve()).lower()
                if key in self._queued_keys or key in self._uploaded_keys:
                    continue
                self._queued_keys.add(key)
                self._queue.append(path)
                self.signals.log.emit(f"追加: {path.name}")

    def _scan(self) -> None:
        with self._lock:
            for path in collect_uploadable_paths([self.folder], self.extensions):
                key = str(path.resolve()).lower()
                if key in self._queued_keys or key in self._uploaded_keys:
                    continue
                self._queued_keys.add(key)
                self._queue.append(path)
                self.signals.log.emit(f"検出: {path.name}")

    def run(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="mfu_monitor_"))
        try:
            self.signals.log.emit("リアルタイム送信用のアップロード枠を作成しています...")
            slot = self.api.create_upload(self.title, self.date_iso, self.mode)
            if not slot.get("ok"):
                raise RuntimeError(str(slot))
            uuid = str(slot.get("uuid") or "")
            self.signals.status.emit(uuid)
            self.signals.log.emit(f"UUID: {uuid}")
            notified = False

            while not self._stop.is_set():
                self._scan()
                with self._lock:
                    path = self._queue.pop(0) if self._queue else None
                if path is None:
                    time.sleep(1.0)
                    continue
                key = str(path.resolve()).lower()
                if key in self._uploaded_keys:
                    continue
                if not wait_file_stable(path):
                    self.signals.log.emit(f"書き込み完了待ちタイムアウト: {path.name}")
                    continue
                try:
                    self.signals.log.emit(f"送信: {path.name}")
                    thumb_error = self.upload_one(uuid, path, temp_root)
                    self._uploaded_keys.add(key)
                    self._count += 1
                    self.signals.uploaded.emit(self._count)
                    if thumb_error:
                        self.signals.log.emit(f"クライアントサムネ未送信: {path.name} {thumb_error}")
                    if not notified:
                        self.signals.log.emit("最初の1ファイル通知を送信しています...")
                        self.api.done(uuid)
                        notified = True
                        self.signals.first_notified.emit(uuid)
                    else:
                        result = self.api.reconcile_thumbnails(uuid)
                        if result.get("queued"):
                            self.signals.log.emit(
                                f"サーバーサムネ補完を依頼: 不足{result.get('missing_count', 0)}件"
                            )
                except Exception as exc:
                    self.signals.log.emit(f"送信失敗: {path.name} {exc}")

            if uuid:
                try:
                    result = self.api.reconcile_thumbnails(uuid)
                    self.signals.log.emit(
                        f"監視停止時のサムネ確認: 不足{result.get('missing_count', 0)}件"
                    )
                except Exception as exc:
                    self.signals.log.emit(f"監視停止時のサムネ確認失敗: {exc}")
            self.signals.log.emit("リアルタイム送信を停止しました。終了時通知は送信しません。")
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            self.signals.stopped.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.files: list[Path] = []
        self.login_thread: QThread | None = None
        self.login_worker: LoginWorker | None = None
        self.session_thread: QThread | None = None
        self.session_worker: SessionWorker | None = None
        self.upload_thread: QThread | None = None
        self.upload_worker: UploadWorker | None = None
        self.retry_context: dict[str, Any] | None = None
        self.monitor_thread: QThread | None = None
        self.monitor_py_thread: threading.Thread | None = None
        self.monitor_worker: MonitorWorker | None = None
        self.realtime_uuid = ""
        self.setWindowTitle(APP_NAME)
        self.resize(980, 780)
        self.setAcceptDrops(True)

        self.base_url = QLineEdit(normalize_base_url(str(self.config.get("base_url") or DEFAULT_BASE_URL)))
        self.upload_route_box = QComboBox()
        self.upload_route_box.addItem("自動（LAN優先）", "auto")
        self.upload_route_box.addItem("公開URL", "public")
        self.upload_route_box.addItem("LAN直接", "lan")
        route_index = self.upload_route_box.findData(str(self.config.get("upload_route") or "auto"))
        self.upload_route_box.setCurrentIndex(max(0, route_index))
        self.lan_base_url = QLineEdit(
            normalize_base_url(str(self.config.get("lan_base_url") or DEFAULT_LAN_BASE_URL))
        )
        self.user_label = QLabel("未ログイン")
        self.login_btn = QPushButton("Chromeでログイン")
        self.logout_btn = QPushButton("ログアウト")
        self.refresh_btn = QPushButton("モード取得")
        self.mode_box = QComboBox()
        self.title_edit = QLineEdit("MFU Upload")
        self.date_edit = QLineEdit(dt.date.today().strftime("%Y%m%d"))
        self.magick_edit = QLineEdit(str(self.config.get("magick_path") or DEFAULT_MAGICK))
        self.browse_magick_btn = QPushButton("参照")
        self.file_label = QLabel("未選択")
        self.pick_btn = QPushButton("ファイル選択")
        self.run_btn = QPushButton("アップロード開始")
        self.retry_btn = QPushButton("失敗ファイルのみ再送")
        self.retry_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)

        self.batch_radio = QRadioButton("一括アップロード")
        self.realtime_radio = QRadioButton("リアルタイム送信")
        self.batch_radio.setChecked(True)
        self.send_mode_group = QButtonGroup(self)
        self.send_mode_group.addButton(self.batch_radio)
        self.send_mode_group.addButton(self.realtime_radio)

        self.watch_folder_edit = QLineEdit(str(self.config.get("watch_folder") or ""))
        self.watch_browse_btn = QPushButton("参照")
        self.extensions_edit = QLineEdit(str(self.config.get("extensions") or DEFAULT_EXTENSIONS))
        self.watch_start_btn = QPushButton("監視開始")
        self.watch_stop_btn = QPushButton("監視停止")
        self.watch_stop_btn.setEnabled(False)
        self.current_uuid_label = QLabel("-")
        self.uploaded_count_label = QLabel("0")

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
        self.upload_parallel = QSpinBox()
        self.upload_parallel.setRange(1, 8)
        self.upload_parallel.setValue(int(self.config.get("upload_parallel") or 4))
        self.skip_thumbs = QCheckBox("サムネ送信をスキップ")
        self.skip_thumbs.setChecked(bool(self.config.get("skip_thumbs") or False))

        self._build_ui()
        self._connect()
        self._apply_token_state()
        self.check_session(load_modes=True)

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
        auth_layout.addWidget(QLabel("送信経路"), 2, 0)
        auth_layout.addWidget(self.upload_route_box, 2, 1)
        auth_layout.addWidget(QLabel("LAN API URL"), 2, 2)
        auth_layout.addWidget(self.lan_base_url, 2, 3, 1, 2)
        layout.addWidget(auth)

        info = QGroupBox("アップロード設定")
        form = QFormLayout(info)
        form.addRow("Mode", self.mode_box)
        form.addRow("タイトル", self.title_edit)
        form.addRow("撮影日 (yyyymmdd / yyyy-mm-dd)", self.date_edit)
        layout.addWidget(info)

        mode_box = QGroupBox("送信モード")
        mode_layout = QHBoxLayout(mode_box)
        mode_layout.addWidget(self.batch_radio)
        mode_layout.addWidget(self.realtime_radio)
        mode_layout.addStretch(1)
        layout.addWidget(mode_box)

        realtime = QGroupBox("リアルタイム送信")
        rt = QGridLayout(realtime)
        rt.addWidget(QLabel("監視フォルダ"), 0, 0)
        rt.addWidget(self.watch_folder_edit, 0, 1, 1, 4)
        rt.addWidget(self.watch_browse_btn, 0, 5)
        rt.addWidget(QLabel("拡張子"), 1, 0)
        rt.addWidget(self.extensions_edit, 1, 1, 1, 4)
        rt.addWidget(self.watch_start_btn, 1, 5)
        rt.addWidget(QLabel("現在UUID"), 2, 0)
        rt.addWidget(self.current_uuid_label, 2, 1, 1, 2)
        rt.addWidget(QLabel("送信済み"), 2, 3)
        rt.addWidget(self.uploaded_count_label, 2, 4)
        rt.addWidget(self.watch_stop_btn, 2, 5)
        layout.addWidget(realtime)

        thumbs = QGroupBox("サムネ生成")
        thumb_grid = QGridLayout(thumbs)
        thumb_grid.addWidget(QLabel("magick"), 0, 0)
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
        thumb_grid.addWidget(QLabel("送信並列数"), 2, 5)
        thumb_grid.addWidget(self.upload_parallel, 2, 6)
        layout.addWidget(thumbs)

        files = QHBoxLayout()
        files.addWidget(self.pick_btn)
        files.addWidget(self.file_label, 1)
        layout.addLayout(files)

        run = QHBoxLayout()
        run.addWidget(self.run_btn)
        run.addWidget(self.retry_btn)
        run.addWidget(self.progress, 1)
        layout.addLayout(run)
        layout.addWidget(self.log_box, 1)

    def _connect(self) -> None:
        self.login_btn.clicked.connect(self.login)
        self.logout_btn.clicked.connect(self.logout)
        self.refresh_btn.clicked.connect(lambda: self.check_session(load_modes=True))
        self.pick_btn.clicked.connect(self.pick_files)
        self.run_btn.clicked.connect(self.start_upload)
        self.retry_btn.clicked.connect(self.retry_failed_upload)
        self.browse_magick_btn.clicked.connect(self.pick_magick)
        self.watch_browse_btn.clicked.connect(self.pick_watch_folder)
        self.watch_start_btn.clicked.connect(self.start_monitor)
        self.watch_stop_btn.clicked.connect(self.stop_monitor)
        self.batch_radio.toggled.connect(self._update_send_mode_ui)
        self.base_url.editingFinished.connect(self.save_settings)
        self.lan_base_url.editingFinished.connect(self.save_settings)
        self.upload_route_box.currentIndexChanged.connect(self.save_settings)
        self.magick_edit.editingFinished.connect(self.save_settings)
        self.watch_folder_edit.editingFinished.connect(self.save_settings)
        self.extensions_edit.editingFinished.connect(self.save_settings)

    def api(self) -> ApiClient:
        return ApiClient(self.base_url.text(), str(self.config.get("api_token") or ""))

    def upload_api(self) -> ApiClient:
        route = str(self.upload_route_box.currentData() or "auto")
        public_api = self.api()
        if route == "public":
            self.log(f"送信経路: 公開URL ({public_api.base_url})")
            return public_api

        lan_api = ApiClient(self.lan_base_url.text(), str(self.config.get("api_token") or ""))
        try:
            if not lan_api.probe_authenticated(timeout=2.0):
                raise RuntimeError("LANサーバーで認証できません。")
            self.log(f"送信経路: LAN直接 ({lan_api.base_url})")
            return lan_api
        except Exception as exc:
            if route == "lan":
                raise RuntimeError(f"LAN直接接続に失敗しました: {exc}") from exc
            self.log(f"LAN直接を利用できないため公開URLへ切り替えます: {exc}")
            self.log(f"送信経路: 公開URL ({public_api.base_url})")
            return public_api

    def log(self, message: str) -> None:
        self.log_box.appendPlainText(f"[{_now()}] {message}")

    def save_settings(self) -> None:
        self.config.update(
            {
                "base_url": normalize_base_url(self.base_url.text()),
                "upload_route": str(self.upload_route_box.currentData() or "auto"),
                "lan_base_url": normalize_base_url(self.lan_base_url.text()),
                "magick_path": self.magick_edit.text().strip(),
                "thumb_size": self.thumb_size.value(),
                "thumb_quality": self.thumb_quality.value(),
                "thumb_method": self.thumb_method.value(),
                "parallel": self.parallel.value(),
                "upload_parallel": self.upload_parallel.value(),
                "skip_thumbs": self.skip_thumbs.isChecked(),
                "watch_folder": self.watch_folder_edit.text().strip(),
                "extensions": self.extensions_edit.text().strip() or DEFAULT_EXTENSIONS,
            }
        )
        self.base_url.setText(self.config["base_url"])
        self.lan_base_url.setText(self.config["lan_base_url"])
        save_config(self.config)

    def _apply_token_state(self) -> None:
        logged_in = bool(self.config.get("api_token"))
        monitoring = self.monitor_worker is not None
        self.logout_btn.setEnabled(logged_in and not monitoring)
        self.refresh_btn.setEnabled(logged_in and not monitoring)
        self.run_btn.setEnabled(logged_in and not monitoring)
        self.retry_btn.setEnabled(logged_in and self.retry_context is not None and not monitoring and self.upload_worker is None)
        self.watch_start_btn.setEnabled(logged_in and self.realtime_radio.isChecked() and not monitoring)
        self.watch_stop_btn.setEnabled(monitoring)
        self._update_send_mode_ui()

    def _update_send_mode_ui(self) -> None:
        realtime = self.realtime_radio.isChecked()
        monitoring = self.monitor_worker is not None
        self.pick_btn.setEnabled(not realtime and not monitoring)
        self.run_btn.setEnabled(bool(self.config.get("api_token")) and not realtime and not monitoring)
        self.retry_btn.setEnabled(
            bool(self.config.get("api_token"))
            and self.retry_context is not None
            and not realtime
            and not monitoring
            and self.upload_worker is None
        )
        self.watch_folder_edit.setEnabled(realtime and not monitoring)
        self.watch_browse_btn.setEnabled(realtime and not monitoring)
        self.extensions_edit.setEnabled(realtime and not monitoring)
        self.watch_start_btn.setEnabled(
            bool(self.config.get("api_token")) and realtime and not monitoring
        )
        self.watch_start_btn.setToolTip("リアルタイム送信を選択すると監視を開始できます。")

    def check_session(self, load_modes: bool) -> None:
        if not self.config.get("api_token"):
            self.user_label.setText("未ログイン")
            self._apply_token_state()
            return
        if self.session_thread is not None:
            return
        self.refresh_btn.setEnabled(False)
        self.session_thread = QThread(self)
        worker = SessionWorker(
            self.base_url.text(),
            str(self.config.get("api_token") or ""),
            load_modes,
        )
        self.session_worker = worker
        worker.moveToThread(self.session_thread)
        self.session_thread.started.connect(worker.run)
        worker.finished.connect(self._session_finished)
        worker.failed.connect(self._session_failed)
        worker.finished.connect(self.session_thread.quit)
        worker.failed.connect(self.session_thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.finished.connect(lambda _: setattr(self, "session_worker", None))
        worker.failed.connect(lambda _: setattr(self, "session_worker", None))
        self.session_thread.finished.connect(self._session_thread_finished)
        self.session_thread.finished.connect(self.session_thread.deleteLater)
        self.session_thread.start()

    def _session_finished(self, result: dict[str, Any]) -> None:
        info = result.get("session") or {}
        if info.get("authenticated"):
            self.user_label.setText(str(info.get("username") or "ログイン済み"))
            if "modes" in result:
                self._apply_modes(result.get("modes") or {})
            if result.get("modes_error"):
                self.log(f"モード取得失敗: {result['modes_error']}")
        else:
            self.config.pop("api_token", None)
            save_config(self.config)
            self.mode_box.clear()
            self.user_label.setText("未ログイン")
        self._apply_token_state()

    def _session_failed(self, error: str) -> None:
        self.log(f"セッション確認に失敗: {error}")

    def _session_thread_finished(self) -> None:
        self.session_thread = None
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
        self.check_session(load_modes=True)

    def _apply_modes(self, data: dict[str, Any]) -> None:
        try:
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
            self.log(f"モード取得失敗: {exc}")

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "アップロードする画像を選択",
            "",
            "Images (*.jpg *.jpeg *.png *.heic *.webp *.tif *.tiff);;All files (*.*)",
        )
        if paths:
            self.add_batch_files([Path(p) for p in paths])

    def add_batch_files(self, paths: list[Path]) -> None:
        exts = parse_extensions(self.extensions_edit.text())
        additions = collect_uploadable_paths(paths, exts)
        known = {str(p.resolve()).lower() for p in self.files if p.exists()}
        for path in additions:
            key = str(path.resolve()).lower()
            if key not in known:
                known.add(key)
                self.files.append(path)
        self.file_label.setText(f"{len(self.files)} ファイル選択")
        self.log(f"ファイル追加: {len(additions)}件")

    def pick_magick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "magick を選択", str(DEFAULT_MAGICK), "magick (magick.exe magick);;All files (*.*)")
        if path:
            self.magick_edit.setText(path)
            self.save_settings()

    def pick_watch_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "監視フォルダを選択", self.watch_folder_edit.text() or str(Path.home()))
        if folder:
            self.watch_folder_edit.setText(folder)
            self.save_settings()

    def _validate_upload_settings(self) -> str | None:
        if not self.config.get("api_token"):
            return "先にログインしてください。"
        if not str(self.mode_box.currentData() or ""):
            return "モードを取得して選択してください。"
        return None

    def start_upload(self) -> None:
        self.save_settings()
        error = self._validate_upload_settings()
        if error:
            QMessageBox.information(self, APP_NAME, error)
            return
        if not self.files:
            QMessageBox.information(self, APP_NAME, "アップロードするファイルを選択してください。")
            return

        self.retry_context = None
        self.retry_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log("アップロード開始処理を開始します...")
        try:
            self.upload_thread = QThread(self)
            worker = UploadWorker(
                api=self.upload_api(),
                files=list(self.files),
                title=self.title_edit.text().strip() or "MFU Upload",
                date_iso=ymd_to_iso(self.date_edit.text()),
                mode=str(self.mode_box.currentData() or ""),
                magick=Path(self.magick_edit.text().strip()),
                thumb_size=self.thumb_size.value(),
                thumb_quality=self.thumb_quality.value(),
                thumb_method=self.thumb_method.value(),
                parallel=self.parallel.value(),
                upload_parallel=self.upload_parallel.value(),
                skip_thumbs=self.skip_thumbs.isChecked(),
            )
            self.upload_worker = worker
            worker.moveToThread(self.upload_thread)
            self.upload_thread.started.connect(worker.run)
            worker.log.connect(self.log)
            worker.progress.connect(self.progress.setValue)
            worker.finished.connect(self._upload_finished)
            worker.incomplete.connect(self._upload_incomplete)
            worker.failed.connect(self._upload_failed)
            worker.finished.connect(self.upload_thread.quit)
            worker.incomplete.connect(self.upload_thread.quit)
            worker.failed.connect(self.upload_thread.quit)
            worker.finished.connect(worker.deleteLater)
            worker.incomplete.connect(worker.deleteLater)
            worker.failed.connect(worker.deleteLater)
            worker.finished.connect(lambda _: setattr(self, "upload_worker", None))
            worker.incomplete.connect(lambda _: setattr(self, "upload_worker", None))
            worker.failed.connect(lambda _: setattr(self, "upload_worker", None))
            self.upload_thread.finished.connect(self._upload_thread_finished)
            self.upload_thread.finished.connect(self.upload_thread.deleteLater)
            self.upload_thread.start()
        except Exception as exc:
            self.upload_thread = None
            self.upload_worker = None
            self._upload_failed(str(exc))

    def _upload_thread_finished(self) -> None:
        self.upload_thread = None
        self._apply_token_state()

    def _upload_finished(self, result: dict) -> None:
        self.retry_context = None
        self.retry_btn.setEnabled(False)
        self.run_btn.setEnabled(True)
        uuid = result.get("uuid", "")
        password = result.get("password", "")
        completion_url = str(result.get("completion_url") or "").strip()
        self.log("DONE")
        if completion_url:
            try:
                open_login_url(completion_url)
                self.log("メール送信用のWeb完了画面を開きました。")
            except Exception as exc:
                self.log(f"Web完了画面を開けませんでした: {exc}")
        QMessageBox.information(self, APP_NAME, f"完了しました。\nUUID: {uuid}\nPW: {password or '-'}")
        self.title_edit.setText("MFU Upload")
        self.date_edit.setText(dt.date.today().strftime("%Y%m%d"))
        self.files = []
        self.file_label.setText("未選択")
        self.progress.setValue(0)
        self._apply_token_state()

    def _upload_incomplete(self, result: dict[str, Any]) -> None:
        self.run_btn.setEnabled(True)
        self.retry_context = result
        failed = result.get("failed") or []
        self.log(f"原本送信が未完了: {len(failed)}件。/done は送信していません。")
        for item in failed:
            self.log(f"再送対象: {Path(str(item.get('path') or '')).name} {item.get('error') or ''}")
        self._apply_token_state()
        QMessageBox.warning(
            self,
            APP_NAME,
            f"{len(failed)}件の原本を3回試行しましたが送信できませんでした。\n"
            "通信を確認後、「失敗ファイルのみ再送」を押してください。",
        )

    def retry_failed_upload(self) -> None:
        context = self.retry_context or {}
        failed = context.get("failed") or []
        files = [Path(str(item.get("path") or "")) for item in failed]
        missing = [path for path in files if not path.is_file()]
        if missing:
            QMessageBox.warning(self, APP_NAME, f"再送対象が見つかりません: {missing[0]}")
            return
        transfer_ids = {
            str(Path(str(item.get("path") or ""))): str(item.get("client_file_id") or "")
            for item in failed
        }
        self.retry_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log(f"失敗ファイルのみ再送します: {len(files)}件")
        try:
            self.upload_thread = QThread(self)
            worker = UploadWorker(
                api=self.upload_api(),
                files=files,
                title=self.title_edit.text().strip() or "MFU Upload",
                date_iso=ymd_to_iso(self.date_edit.text()),
                mode=str(self.mode_box.currentData() or ""),
                magick=Path(self.magick_edit.text().strip()),
                thumb_size=self.thumb_size.value(),
                thumb_quality=self.thumb_quality.value(),
                thumb_method=self.thumb_method.value(),
                parallel=self.parallel.value(),
                upload_parallel=self.upload_parallel.value(),
                skip_thumbs=self.skip_thumbs.isChecked(),
                existing_uuid=str(context.get("uuid") or ""),
                existing_password=str(context.get("password") or ""),
                transfer_ids=transfer_ids,
            )
            self.upload_worker = worker
            worker.moveToThread(self.upload_thread)
            self.upload_thread.started.connect(worker.run)
            worker.log.connect(self.log)
            worker.progress.connect(self.progress.setValue)
            worker.finished.connect(self._upload_finished)
            worker.incomplete.connect(self._upload_incomplete)
            worker.failed.connect(self._upload_failed)
            worker.finished.connect(self.upload_thread.quit)
            worker.incomplete.connect(self.upload_thread.quit)
            worker.failed.connect(self.upload_thread.quit)
            worker.finished.connect(worker.deleteLater)
            worker.incomplete.connect(worker.deleteLater)
            worker.failed.connect(worker.deleteLater)
            worker.finished.connect(lambda _: setattr(self, "upload_worker", None))
            worker.incomplete.connect(lambda _: setattr(self, "upload_worker", None))
            worker.failed.connect(lambda _: setattr(self, "upload_worker", None))
            self.upload_thread.finished.connect(self._upload_thread_finished)
            self.upload_thread.finished.connect(self.upload_thread.deleteLater)
            self.upload_thread.start()
        except Exception as exc:
            self.upload_thread = None
            self.upload_worker = None
            self._upload_failed(str(exc))

    def _upload_failed(self, error: str) -> None:
        self.run_btn.setEnabled(True)
        self.log(f"ERROR: {error}")
        QMessageBox.warning(self, APP_NAME, f"アップロードに失敗しました。\n{error}")
        self._apply_token_state()

    def start_monitor(self) -> None:
        try:
            if not self.realtime_radio.isChecked():
                return
            self.log("監視開始処理を開始します...")
            self.save_settings()
            error = self._validate_upload_settings()
            if error:
                self.log(f"監視開始できません: {error}")
                QMessageBox.information(self, APP_NAME, error)
                return
            folder = Path(self.watch_folder_edit.text().strip())
            self.log(f"監視フォルダ確認: {folder}")
            if not folder.is_dir():
                self.log("監視開始できません: 監視フォルダが見つかりません。")
                QMessageBox.information(self, APP_NAME, "監視フォルダを選択してください。")
                return
            extensions = parse_extensions(self.extensions_edit.text())
            self.log(f"監視対象拡張子: {', '.join(sorted(extensions))}")
            self.realtime_uuid = ""
            self.current_uuid_label.setText("-")
            self.uploaded_count_label.setText("0")
            worker = MonitorWorker(
                api=self.upload_api(),
                folder=folder,
                extensions=extensions,
                title=self.title_edit.text().strip() or "MFU Upload",
                date_iso=ymd_to_iso(self.date_edit.text()),
                mode=str(self.mode_box.currentData() or ""),
                magick=Path(self.magick_edit.text().strip()),
                thumb_size=self.thumb_size.value(),
                thumb_quality=self.thumb_quality.value(),
                thumb_method=self.thumb_method.value(),
                parallel=self.parallel.value(),
                skip_thumbs=self.skip_thumbs.isChecked(),
            )
            self.monitor_worker = worker
            worker.signals.log.connect(self.log)
            worker.signals.status.connect(self._monitor_uuid)
            worker.signals.uploaded.connect(lambda count: self.uploaded_count_label.setText(str(count)))
            worker.signals.first_notified.connect(lambda uuid: self.log(f"最初の1ファイル通知済み: {uuid}"))
            worker.signals.failed.connect(self._monitor_failed)
            worker.signals.stopped.connect(self._monitor_stopped)
            self.monitor_py_thread = threading.Thread(target=worker.run, name="MFURealtimeMonitor", daemon=True)
            self.monitor_py_thread.start()
            self.log(f"監視開始: {folder}")
            self._apply_token_state()
        except Exception as exc:
            self.log(f"監視開始エラー: {exc}")
            QMessageBox.warning(self, APP_NAME, f"監視開始に失敗しました。\n{exc}")
            self.monitor_worker = None
            self.monitor_py_thread = None
            self._apply_token_state()

    def stop_monitor(self) -> None:
        if self.monitor_worker:
            self.log("監視停止を要求しました。")
            self.monitor_worker.stop()

    def _monitor_uuid(self, uuid: str) -> None:
        self.realtime_uuid = uuid
        self.current_uuid_label.setText(uuid)

    def _monitor_failed(self, error: str) -> None:
        self.log(f"リアルタイム送信エラー: {error}")
        QMessageBox.warning(self, APP_NAME, f"リアルタイム送信に失敗しました。\n{error}")

    def _monitor_stopped(self) -> None:
        self.monitor_worker = None
        self.monitor_thread = None
        self.monitor_py_thread = None
        self._apply_token_state()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            return
        if self.monitor_worker:
            self.monitor_worker.enqueue_external(paths)
            self.log("D&Dファイルをリアルタイム送信キューに追加しました。")
        else:
            self.batch_radio.setChecked(True)
            self.add_batch_files(paths)
            self.log("D&Dファイルを一括アップロードに追加しました。")
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.monitor_worker:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "リアルタイム送信中です。監視を停止して終了しますか？\n終了時通知は送信しません。",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.monitor_worker.stop()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
