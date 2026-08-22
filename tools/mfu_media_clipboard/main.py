from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookiejar import MozillaCookieJar
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
import webbrowser

import requests
import websocket
from dotenv import load_dotenv
from platformdirs import user_config_dir


def _bootstrap_log(message: str) -> None:
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        log_dir = base / "MFU" / "MFU Media Clipboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "mfu_media_clipboard_bootstrap.log").open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")
    except Exception:
        pass


def _windows_drive_type(path: Path) -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        root = str(path.anchor or path.drive or "")
        if root.startswith("\\\\"):
            root = "\\\\"
        elif root and not root.endswith("\\"):
            root += "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except Exception:
        return 0


def _is_remote_runtime_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\"):
        return True
    return _windows_drive_type(path) == 4  # DRIVE_REMOTE


def _maybe_relaunch_from_local_runtime() -> None:
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("MFU_MEDIA_CLIPBOARD_LOCAL_RUNTIME") == "1":
        return

    source_exe = Path(sys.executable).resolve()
    source_dir = source_exe.parent
    if not _is_remote_runtime_path(source_exe):
        return

    local_base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    runtime_root = local_base / "MFU" / "MFU Media Clipboard" / "runtime"
    runtime_id = f"runtime_{int(source_exe.stat().st_mtime)}_{source_exe.stat().st_size}"
    target_dir = runtime_root / runtime_id
    target_exe = target_dir / source_exe.name

    try:
        runtime_root.mkdir(parents=True, exist_ok=True)
        if not target_exe.exists():
            _bootstrap_log(f"copy runtime from {source_dir} to {target_dir}")
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        env = os.environ.copy()
        env["MFU_MEDIA_CLIPBOARD_LOCAL_RUNTIME"] = "1"
        subprocess.Popen([str(target_exe), *sys.argv[1:]], cwd=str(target_dir), env=env)
        _bootstrap_log(f"relaunched local runtime: {target_exe}")
        os._exit(0)
    except Exception as exc:
        _bootstrap_log(f"local runtime relaunch failed: {exc!r}")


_maybe_relaunch_from_local_runtime()

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)


load_dotenv()

APP_NAME = "MFU Media Clipboard"
DEFAULT_BASE_URL = os.getenv("MFU_BASE_URL", "https://mfu.iori0624.jp").rstrip("/")
CONFIG_DIR = Path(user_config_dir(APP_NAME, "MFU"))
SETTINGS_PATH = CONFIG_DIR / "settings.json"
LOG_PATH = CONFIG_DIR / "mfu_media_clipboard.log"
COOKIE_PATH = CONFIG_DIR / "cookies.txt"
WEB_PROFILE_DIR = CONFIG_DIR / "web_profile"
WEB_CACHE_DIR = CONFIG_DIR / "web_cache"
EXTERNAL_BROWSER_PROFILE_DIR = CONFIG_DIR / "external_browser_profile"
STARTUP_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "MFUMediaClipboard"
URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:"
    r"(?:instagram\.com/(?:[^/?#]+/)?(?:p|reel|tv)/[A-Za-z0-9_-]+/?[^ \r\n\t]*)|"
    r"(?:instagram\.com/stories/[A-Za-z0-9._]+(?:/\d+)?/?[^ \r\n\t]*)|"
    r"(?:threads\.(?:com|net)/(?:@[^/?#\s]+/post|t)/[A-Za-z0-9_-]+/?[^ \r\n\t]*)|"
    r"(?:(?:x|twitter)\.com/[^/?#\s]+/status/\d+[^ \r\n\t]*)"
    r")",
    re.IGNORECASE,
)
MAX_BATCH_URLS = 20
URL_TRAILING_PUNCTUATION = ".,;:!?)]}>\u3001\u3002\uff01\uff1f\u3011\u300d\u300f"
FOLDER_RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def extract_media_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(str(text or "")):
        url = match.group(0).rstrip(URL_TRAILING_PUNCTUATION)
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def folder_sort_key(value: str) -> tuple[int, int, int, str]:
    if value == "":
        return (0, 0, 0, "")
    match = FOLDER_RANGE_PATTERN.match(value)
    if match:
        return (1, int(match.group(1)), int(match.group(2)), value.casefold())
    return (2, 0, 0, value.casefold())


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        args = [str(Path(sys.executable).resolve())]
    else:
        args = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve())]
    return subprocess.list2cmdline(args)


def _startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
        return bool(str(value).strip())
    except FileNotFoundError:
        return False


def _set_startup_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise OSError("Windowsのスタートアップ登録はWindows上でのみ利用できます。")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_PATH) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE_NAME)
            except FileNotFoundError:
                pass


class ApiError(RuntimeError):
    pass


def setup_logging() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )

    def _excepthook(exc_type, exc, tb):
        logging.exception("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook


class ApiClient:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.base_url = self._load_base_url()
        self.token = self._load_token()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"{APP_NAME}/1.0"})
        self.session.cookies = self._cookie_jar()

    def _load_base_url(self) -> str:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            value = str(data.get("base_url") or "").rstrip("/")
            return value or DEFAULT_BASE_URL
        except Exception:
            return DEFAULT_BASE_URL

    def _load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self, data: dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_token(self) -> str:
        return str(self._load_settings().get("api_token") or "")

    def get_setting(self, key: str, default: str = "") -> str:
        return str(self._load_settings().get(key) or default)

    def set_setting(self, key: str, value: str) -> None:
        settings = self._load_settings()
        settings[key] = value
        self._save_settings(settings)

    def set_token(self, token: str) -> None:
        self.token = token.strip()
        settings = self._load_settings()
        settings["base_url"] = self.base_url
        settings["api_token"] = self.token
        self._save_settings(settings)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _cookie_jar(self) -> MozillaCookieJar:
        jar = MozillaCookieJar(str(COOKIE_PATH))
        if COOKIE_PATH.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        return jar

    def save_cookies(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.session.cookies.save(ignore_discard=True, ignore_expires=True)

    def replace_cookies(self, cookies: list[Cookie]) -> None:
        jar = MozillaCookieJar(str(COOKIE_PATH))
        for cookie in cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=True)
        self.session.cookies = jar

    def clear_cookies(self) -> None:
        self.session.cookies.clear()
        self.token = ""
        settings = self._load_settings()
        settings.pop("api_token", None)
        self._save_settings(settings)
        try:
            COOKIE_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    def url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def absolute_url(self, value: str) -> str:
        return urljoin(self.base_url + "/", value)

    def _json(self, response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"HTTP {response.status_code}: JSONを読み取れませんでした") from exc
        if response.status_code >= 400 or data.get("ok") is False:
            raise ApiError(str(data.get("error") or f"HTTP {response.status_code}"))
        return data

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        res = self.session.get(
            self.url(path),
            params={k: v for k, v in params.items() if v is not None},
            headers=self._headers(),
            timeout=30,
        )
        return self._json(res)

    def post_json(self, path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
        res = self.session.post(self.url(path), json=payload, headers=self._headers(), timeout=timeout)
        return self._json(res)

    def ensure_login(self) -> None:
        data = self.get("/desktop/media-clipboard/api/session")
        if not data.get("authenticated"):
            raise ApiError("MFUにログインしていません。トレイメニューの「ログイン」からログインしてください。")

    def image_next_number(self, folder: str) -> int:
        data = self.get("/image_viewer/api/instagram/next-number", folder=folder)
        return int(data.get("nextNumber") or 1)

    def start_instagram_browser(self) -> dict[str, Any]:
        return self.post_json("/image_viewer/api/instagram/browser/start", {}, timeout=90)

    def folders(self) -> list[str]:
        data = self.get("/image_viewer/api/images")
        folders = data.get("folders") or []
        values = [str(folder) for folder in folders if isinstance(folder, str)]
        values.append("")
        return sorted(set(values), key=folder_sort_key)

    def fetch_images(self, source_url: str, progress: Signal) -> dict[str, Any]:
        data = self.post_json("/image_viewer/api/instagram/fetch", {"url": source_url})
        job_id = str(data.get("jobId") or "")
        if not job_id:
            return data
        result = self._poll_job(f"/image_viewer/api/instagram/jobs/{job_id}", progress)
        result["jobId"] = job_id
        return result

    def fetch_videos(self, source_url: str, progress: Signal) -> dict[str, Any]:
        data = self.post_json("/image_viewer/api/video/fetch", {"url": source_url})
        job_id = str(data.get("jobId") or "")
        if not job_id:
            return data
        result = self._poll_job(f"/image_viewer/api/video/jobs/{job_id}", progress)
        result["jobId"] = job_id
        return result

    def fetch_video_frames(self, source_url: str, progress: Signal) -> dict[str, Any]:
        data = self.post_json("/image_viewer/api/video/frames/fetch", {"url": source_url})
        job_id = str(data.get("jobId") or "")
        if not job_id:
            return data
        result = self._poll_job(f"/image_viewer/api/instagram/jobs/{job_id}", progress)
        result["jobId"] = job_id
        return result

    def _poll_job(
        self,
        path: str,
        progress: Signal,
        *,
        attempts: int = 180,
        action_label: str = "取得中",
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for _ in range(attempts):
            data = self.get(path)
            last = data
            status = str(data.get("status") or "")
            progress.emit(self._progress_text(data, action_label=action_label))
            if status in {"done", "error", "cancelled", "login_required"}:
                return data
            time.sleep(1)
        raise ApiError("取得がタイムアウトしました。")

    def _progress_text(self, data: dict[str, Any], *, action_label: str = "取得中") -> str:
        total = int(data.get("total") or 0)
        processed = int(data.get("processed") or 0)
        downloaded = int(data.get("downloaded") or 0)
        failed = int(data.get("failed") or 0)
        if total:
            if downloaded or failed:
                return f"{action_label}... {processed}/{total}  成功:{downloaded}  失敗:{failed}"
            return f"{action_label}... {processed}/{total}"
        return f"{action_label}..."

    def save_images(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post_json("/image_viewer/api/instagram/save", payload, timeout=180)

    def save_videos(self, payload: dict[str, Any], progress: Signal) -> dict[str, Any]:
        data = self.post_json("/image_viewer/api/video/save-async", payload, timeout=30)
        save_job_id = str(data.get("saveJobId") or "")
        if not save_job_id:
            return data
        return self._poll_job(
            f"/image_viewer/api/video/save-jobs/{save_job_id}",
            progress,
            attempts=1800,
            action_label="動画を保存中",
        )

    def download_bytes(self, url: str) -> bytes:
        response = self.session.get(self.absolute_url(url), headers=self._headers(), timeout=45)
        response.raise_for_status()
        return response.content


class WorkerSignals(QObject):
    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)


class FetchWorker(threading.Thread):
    def __init__(self, api: ApiClient, source_url: str, kinds: list[str]) -> None:
        super().__init__(daemon=True)
        self.api = api
        self.source_url = source_url
        self.kinds = kinds
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.api.ensure_login()
            result: dict[str, Any] = {"images": None, "videos": None, "errors": []}
            if "images" in self.kinds:
                self.signals.progress.emit("画像を取得しています...")
                try:
                    result["images"] = self.api.fetch_images(self.source_url, self.signals.progress)
                    if result["images"].get("loginRequired"):
                        result["errors"].append(
                            str(result["images"].get("error") or "Instagramの再ログインが必要です。")
                        )
                except Exception as exc:
                    result["errors"].append(f"画像: {exc}")
            if "videos" in self.kinds:
                self.signals.progress.emit("動画を取得しています...")
                try:
                    result["videos"] = self.api.fetch_videos(self.source_url, self.signals.progress)
                    if result["videos"].get("loginRequired"):
                        result["errors"].append(
                            str(result["videos"].get("error") or "Instagramの再ログインが必要です。")
                        )
                except Exception as exc:
                    result["errors"].append(f"動画: {exc}")
            if "video_frames" in self.kinds:
                self.signals.progress.emit("動画を写真に変換しています...")
                try:
                    result["images"] = self.api.fetch_video_frames(
                        self.source_url,
                        self.signals.progress,
                    )
                    if result["images"].get("loginRequired"):
                        result["errors"].append(
                            str(result["images"].get("error") or "Instagramの再ログインが必要です。")
                        )
                except Exception as exc:
                    result["errors"].append(f"動画→写真: {exc}")
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class VideoSaveWorker(threading.Thread):
    def __init__(self, api: ApiClient, payload: dict[str, Any]) -> None:
        super().__init__(daemon=True)
        self.api = api
        self.payload = payload
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.api.save_videos(self.payload, self.signals.progress)
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class ThumbSignals(QObject):
    loaded = Signal(int, bytes)


class ThumbWorker(threading.Thread):
    def __init__(self, api: ApiClient, index: int, url: str, signals: ThumbSignals) -> None:
        super().__init__(daemon=True)
        self.api = api
        self.index = index
        self.url = url
        self.signals = signals

    def run(self) -> None:
        try:
            self.signals.loaded.emit(self.index, self.api.download_bytes(self.url))
        except Exception:
            self.signals.loaded.emit(self.index, b"")


def _qt_cookie_to_cookie(qt_cookie: QNetworkCookie, base_host: str) -> Cookie:
    name = bytes(qt_cookie.name()).decode("utf-8", errors="ignore")
    value = bytes(qt_cookie.value()).decode("utf-8", errors="ignore")
    domain = qt_cookie.domain() or base_host
    path = qt_cookie.path() or "/"
    expires_dt = qt_cookie.expirationDate()
    expires = expires_dt.toSecsSinceEpoch() if expires_dt.isValid() else None
    initial_dot = domain.startswith(".")
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(qt_cookie.domain()),
        domain_initial_dot=initial_dot,
        path=path,
        path_specified=bool(qt_cookie.path()),
        secure=qt_cookie.isSecure(),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if qt_cookie.isHttpOnly() else {},
        rfc2109=False,
    )


def _dict_cookie_to_cookie(data: dict[str, Any], base_host: str) -> Cookie:
    domain = str(data.get("domain") or base_host)
    path = str(data.get("path") or "/")
    expires_value = data.get("expires")
    expires = int(expires_value) if isinstance(expires_value, (int, float)) and expires_value > 0 else None
    return Cookie(
        version=0,
        name=str(data.get("name") or ""),
        value=str(data.get("value") or ""),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(data.get("domain")),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=bool(data.get("path")),
        secure=bool(data.get("secure")),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if data.get("httpOnly") else {},
        rfc2109=False,
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _browser_candidates() -> list[Path]:
    program_files = [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")]
    relative = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    ]
    paths: list[Path] = []
    for root in program_files:
        if not root:
            continue
        for rel in relative:
            paths.append(Path(root) / rel)
    return paths


def _find_browser_exe() -> Path | None:
    for path in _browser_candidates():
        if path.is_file():
            return path
    return None


def _find_chrome_exe() -> Path | None:
    roots = [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")]
    for root in roots:
        if not root:
            continue
        path = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if path.is_file():
            return path
    return None


def _devtools_json(port: int, path: str) -> Any:
    response = requests.get(f"http://127.0.0.1:{port}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def _wait_devtools(port: int) -> None:
    last_error: Exception | None = None
    for _ in range(50):
        try:
            _devtools_json(port, "/json/version")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"ブラウザのデバッグポートに接続できませんでした: {last_error}")


def _external_browser_cookies(port: int, base_host: str) -> list[Cookie]:
    targets = _devtools_json(port, "/json")
    page_ws = ""
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            target_url = str(target.get("url") or "")
            if target.get("type") == "page" and base_host in target_url:
                page_ws = str(target.get("webSocketDebuggerUrl") or "")
                break
        if not page_ws:
            for target in targets:
                if isinstance(target, dict) and target.get("type") == "page":
                    page_ws = str(target.get("webSocketDebuggerUrl") or "")
                    break
    if not page_ws:
        raise RuntimeError("ブラウザのログインタブが見つかりませんでした。")
    ws = websocket.create_connection(page_ws, timeout=8)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            message = json.loads(ws.recv())
            if message.get("id") == 2:
                rows = ((message.get("result") or {}).get("cookies") or [])
                return [
                    _dict_cookie_to_cookie(row, base_host)
                    for row in rows
                    if isinstance(row, dict) and row.get("name")
                ]
    finally:
        ws.close()
    raise RuntimeError("ブラウザからCookieを取得できませんでした。")


class BrowserLoginDialog(QDialog):
    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.cookies: dict[tuple[str, str, str], QNetworkCookie] = {}
        self.base_host = urlparse(api.base_url).hostname or "mfu.iori0624.jp"
        self.external_browser_process: subprocess.Popen | None = None
        self.external_browser_port: int | None = None
        self.setWindowTitle("MFU ログイン")
        self.resize(980, 760)

        WEB_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        WEB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("mfu-media-clipboard-login", self)
        self.profile.setPersistentStoragePath(str(WEB_PROFILE_DIR))
        self.profile.setCachePath(str(WEB_CACHE_DIR))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.profile.cookieStore().cookieAdded.connect(self._cookie_added)
        self.profile.cookieStore().cookieRemoved.connect(self._cookie_removed)
        self.profile.cookieStore().loadAllCookies()

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.status = QLabel("ログインしてください。パスキーもこの画面で利用できます。")
        toolbar.addWidget(self.status, 1)
        reload_btn = QPushButton("再読み込み")
        reload_btn.clicked.connect(lambda: self.view.reload())
        toolbar.addWidget(reload_btn)
        external_btn = QPushButton("Edge/Chromeでログイン")
        external_btn.clicked.connect(self._open_external_browser)
        toolbar.addWidget(external_btn)
        done_btn = QPushButton("ログイン完了")
        done_btn.clicked.connect(self._finish_login)
        toolbar.addWidget(done_btn)
        layout.addLayout(toolbar)

        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        self.view.urlChanged.connect(self._url_changed)
        self.view.loadFinished.connect(self._load_finished)
        layout.addWidget(self.view, 1)

        target = f"/login?next={quote('/image_viewer/', safe='')}"
        self.view.load(QUrl(self.api.url(target)))

    def _cookie_key(self, cookie: QNetworkCookie) -> tuple[str, str, str]:
        name = bytes(cookie.name()).decode("utf-8", errors="ignore")
        return (cookie.domain() or self.base_host, cookie.path() or "/", name)

    def _cookie_added(self, cookie: QNetworkCookie) -> None:
        self.cookies[self._cookie_key(cookie)] = QNetworkCookie(cookie)

    def _cookie_removed(self, cookie: QNetworkCookie) -> None:
        self.cookies.pop(self._cookie_key(cookie), None)

    def _url_changed(self, url: QUrl) -> None:
        self.status.setText(url.toString())

    def _load_finished(self, ok: bool) -> None:
        if not ok:
            self.status.setText(
                "内蔵ブラウザでページを読み込めませんでした。"
                "「Edge/Chromeでログイン」を試してください。"
                f" URL: {self.view.url().toString()}"
            )
            return
        current = self.view.url().toString()
        if "/image_viewer" in current or "/upload" in current:
            self.status.setText("ログイン済みの可能性があります。「ログイン完了」を押してください。")

    def _login_url(self) -> str:
        return self.api.url(f"/login?next={quote('/image_viewer/', safe='')}")

    def _open_external_browser(self) -> None:
        browser = _find_browser_exe()
        if not browser:
            QMessageBox.warning(self, APP_NAME, "Edge または Chrome が見つかりませんでした。")
            return
        if self.external_browser_process and self.external_browser_process.poll() is None:
            QMessageBox.information(self, APP_NAME, "すでに外部ブラウザを起動しています。ログイン後に「ログイン完了」を押してください。")
            return
        EXTERNAL_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.external_browser_port = _find_free_port()
        args = [
            str(browser),
            f"--remote-debugging-port={self.external_browser_port}",
            f"--user-data-dir={EXTERNAL_BROWSER_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            self._login_url(),
        ]
        try:
            self.external_browser_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _wait_devtools(self.external_browser_port)
        except Exception as exc:
            self.external_browser_process = None
            self.external_browser_port = None
            QMessageBox.warning(self, APP_NAME, f"外部ブラウザを起動できませんでした。\n{exc}")
            return
        self.status.setText("Edge/Chromeでログインしてください。完了後にこの画面の「ログイン完了」を押してください。")

    def _finish_login(self) -> None:
        cookies = []
        now = int(time.time())
        for qt_cookie in self.cookies.values():
            cookie = _qt_cookie_to_cookie(qt_cookie, self.base_host)
            if cookie.expires is not None and cookie.expires < now:
                continue
            cookies.append(cookie)
        external_error = ""
        if self.external_browser_port:
            try:
                external_cookies = _external_browser_cookies(self.external_browser_port, self.base_host)
                cookies.extend(external_cookies)
            except Exception as exc:
                external_error = str(exc)
        if not cookies:
            detail = f"\n\n外部ブラウザ: {external_error}" if external_error else ""
            QMessageBox.warning(self, APP_NAME, "Cookieを取得できませんでした。ログイン後にもう一度押してください。" + detail)
            return
        self.api.replace_cookies(cookies)
        try:
            self.api.ensure_login()
        except Exception as exc:
            detail = f"\n\n外部ブラウザ: {external_error}" if external_error else ""
            QMessageBox.warning(self, APP_NAME, f"ログイン確認に失敗しました。\n{exc}{detail}")
            return
        QMessageBox.information(self, APP_NAME, "ログイン状態を保存しました。")
        self.accept()

    def closeEvent(self, event) -> None:
        if self.external_browser_process and self.external_browser_process.poll() is None:
            try:
                self.external_browser_process.terminate()
            except Exception:
                pass
        super().closeEvent(event)


@dataclass
class MediaCard:
    widget: QWidget
    item: dict[str, Any]
    checkbox: QCheckBox
    thumb: QLabel


class ImageCardWidget(QWidget):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        child = self.childAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and not isinstance(child, QCheckBox):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ImageSelectionDialog(QDialog):
    CARD_WIDTH = 184
    THUMB_SIZE = 156

    def __init__(
        self,
        api: ApiClient,
        job: dict[str, Any],
        parent: QWidget | None = None,
        *,
        show_completion_message: bool = True,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.job = job
        self.show_completion_message = show_completion_message
        self.save_result: dict[str, int] | None = None
        self.save_error = ""
        self.items = [x for x in (job.get("images") or []) if isinstance(x, dict)]
        self.cards: list[MediaCard] = []
        self.thumb_signals = ThumbSignals()
        self.thumb_signals.loaded.connect(self._apply_thumb)

        self.setWindowTitle("画像を選択して保存")
        self.resize(980, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.folder_edit = QComboBox()
        self.folder_edit.setEditable(True)
        self._load_folder_options(self.api.get_setting("last_image_folder"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, 999999)
        self.start_spin.setValue(1)
        self.digits_spin = QSpinBox()
        self.digits_spin.setRange(1, 6)
        self.digits_spin.setValue(3)
        form.addRow("保存先フォルダー", self.folder_edit)
        form.addRow("開始番号", self.start_spin)
        form.addRow("桁数", self.digits_spin)
        layout.addLayout(form)

        actions = QHBoxLayout()
        for label, handler in (
            ("全選択", self._select_all),
            ("全解除", self._clear_all),
            ("反転", self._invert),
            ("次番号を再取得", self._refresh_next_number),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            actions.addWidget(btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self.grid = QGridLayout(holder)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("選択画像を保存")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.status = QLabel("")
        layout.addWidget(self.status)

        self.folder_edit.activated.connect(lambda _=None: self._refresh_next_number())
        line_edit = self.folder_edit.lineEdit()
        if line_edit:
            line_edit.editingFinished.connect(self._refresh_next_number)
        self._render()
        self._refresh_next_number()

    def _render(self) -> None:
        if not self.items:
            self.grid.addWidget(QLabel("画像が見つかりませんでした。"), 0, 0)
            return
        for pos, item in enumerate(self.items):
            card = ImageCardWidget()
            card.setObjectName("mediaCard")
            card.setFixedWidth(self.CARD_WIDTH)
            card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            card.setCursor(Qt.PointingHandCursor)
            box = QVBoxLayout(card)
            box.setContentsMargins(8, 8, 8, 8)
            box.setSpacing(8)
            thumb = QLabel("Loading...")
            thumb.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet("border:1px solid #4b5563; background:transparent;")
            checkbox = QCheckBox(str(item.get("filename") or item.get("index") or pos))
            checkbox.setChecked(True)
            checkbox.setToolTip(checkbox.text())
            box.addWidget(thumb)
            box.addWidget(checkbox)
            self.cards.append(MediaCard(widget=card, item=item, checkbox=checkbox, thumb=thumb))
            card.clicked.connect(lambda checkbox=checkbox: checkbox.setChecked(not checkbox.isChecked()))
            checkbox.toggled.connect(lambda _=False, card=card: self._apply_card_style(card))
            self._apply_card_style(card)
            preview_url = str(item.get("previewUrl") or item.get("url") or "")
            if preview_url:
                ThumbWorker(self.api, pos, preview_url, self.thumb_signals).start()
        self._reflow_cards()

    def _apply_card_style(self, card: QWidget) -> None:
        checkbox = card.findChild(QCheckBox)
        selected = bool(checkbox and checkbox.isChecked())
        card.setProperty("selected", selected)
        card.setStyleSheet(
            """
            QWidget#mediaCard {
                border: 3px solid #4b5563;
                border-radius: 6px;
                background: #1f2933;
            }
            QWidget#mediaCard[selected="true"] {
                border-color: #38bdf8;
                background: #0f3a55;
            }
            QWidget#mediaCard[selected="false"] {
                border-color: #4b5563;
                background: #1f2933;
            }
            QWidget#mediaCard QCheckBox {
                color: #f8fafc;
                font-weight: 600;
            }
            """
        )

    def _reflow_cards(self) -> None:
        while self.grid.takeAt(0):
            pass
        if not self.cards:
            return
        viewport_width = max(self.CARD_WIDTH, self.scroll.viewport().width() - 20)
        columns = max(1, viewport_width // (self.CARD_WIDTH + self.grid.spacing()))
        for pos, card in enumerate(self.cards):
            self.grid.addWidget(card.widget, pos // columns, pos % columns, Qt.AlignTop | Qt.AlignLeft)
        self.grid.setColumnStretch(columns, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.cards:
            QTimer.singleShot(0, self._reflow_cards)

    def _apply_thumb(self, index: int, data: bytes) -> None:
        if index < 0 or index >= len(self.cards):
            return
        label = self.cards[index].thumb
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            label.setText("Previewなし")

    def _load_folder_options(self, current: str) -> None:
        self.folder_edit.blockSignals(True)
        self.folder_edit.clear()
        try:
            folders = self.api.folders()
        except Exception:
            folders = [""]
        for folder in folders:
            self.folder_edit.addItem("(ルート)" if folder == "" else folder, folder)
        if current:
            match_index = self.folder_edit.findData(current)
            if match_index < 0:
                self.folder_edit.insertItem(0, current, current)
                match_index = 0
            self.folder_edit.setCurrentIndex(match_index)
        else:
            self.folder_edit.setCurrentIndex(max(0, self.folder_edit.findData("")))
        self.folder_edit.blockSignals(False)

    def _folder_text(self) -> str:
        data = self.folder_edit.currentData()
        if isinstance(data, str) and self.folder_edit.currentText() in {"(ルート)", data}:
            return data
        return self.folder_edit.currentText().strip()

    def _selected_indexes(self) -> list[int]:
        selected: list[int] = []
        for card in self.cards:
            if card.checkbox.isChecked():
                selected.append(int(card.item.get("index") or 0))
        return selected

    def _select_all(self) -> None:
        for card in self.cards:
            card.checkbox.setChecked(True)

    def _clear_all(self) -> None:
        for card in self.cards:
            card.checkbox.setChecked(False)

    def _invert(self) -> None:
        for card in self.cards:
            card.checkbox.setChecked(not card.checkbox.isChecked())

    def _refresh_next_number(self) -> None:
        try:
            self.start_spin.setValue(self.api.image_next_number(self._folder_text()))
            self.status.setText("")
        except Exception as exc:
            self.status.setText(f"次番号を取得できませんでした: {exc}")

    def _save(self) -> None:
        selected = self._selected_indexes()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "保存する画像を選択してください。")
            return
        self.status.setText("保存中...")
        QApplication.processEvents()
        try:
            data = self.api.save_images(
                {
                    "shortcode": self.job.get("shortcode") or "",
                    "jobId": self.job.get("jobId") or "",
                    "images": self.items,
                    "selected": selected,
                    "folder": self._folder_text(),
                    "startNumber": self.start_spin.value(),
                    "digits": self.digits_spin.value(),
                }
            )
            saved = len(data.get("saved") or [])
            duplicates = len(data.get("duplicates") or [])
            errors = len(data.get("errors") or [])
            self.save_result = {"saved": saved, "duplicates": duplicates, "errors": errors}
            self.save_error = ""
            self.api.set_setting("last_image_folder", self._folder_text())
            if self.show_completion_message:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    f"画像を保存しました。\n保存: {saved}件\n重複: {duplicates}件\n失敗: {errors}件",
                )
            self.accept()
        except Exception as exc:
            self.save_error = str(exc)
            QMessageBox.critical(self, APP_NAME, f"画像保存に失敗しました。\n{exc}")
            self.status.setText("")


class VideoSelectionDialog(QDialog):
    def __init__(
        self,
        api: ApiClient,
        job: dict[str, Any],
        parent: QWidget | None = None,
        *,
        show_completion_message: bool = True,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.job = job
        self.show_completion_message = show_completion_message
        self.save_result: dict[str, int] | None = None
        self.save_error = ""
        self.items = [x for x in (job.get("videos") or []) if isinstance(x, dict)]
        self.checkboxes: list[tuple[dict[str, Any], QCheckBox]] = []
        self.save_worker: VideoSaveWorker | None = None
        self.save_progress: QProgressDialog | None = None
        self.pending_save_folder = ""

        self.setWindowTitle("動画を選択して保存")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.folder_edit = QComboBox()
        self.folder_edit.setEditable(True)
        self._load_folder_options(self.api.get_setting("last_video_folder", "video"))
        form.addRow("保存先フォルダー", self.folder_edit)
        layout.addLayout(form)

        actions = QHBoxLayout()
        for label, handler in (("全選択", self._select_all), ("全解除", self._clear_all), ("反転", self._invert)):
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            actions.addWidget(btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        list_layout = QVBoxLayout(holder)
        if not self.items:
            list_layout.addWidget(QLabel("動画が見つかりませんでした。"))
        for item in self.items:
            checkbox = QCheckBox(str(item.get("filename") or item.get("index") or "video"))
            checkbox.setChecked(True)
            list_layout.addWidget(checkbox)
            self.checkboxes.append((item, checkbox))
        list_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("選択動画を保存")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.buttons = buttons
        layout.addWidget(buttons)

    def _select_all(self) -> None:
        for _, checkbox in self.checkboxes:
            checkbox.setChecked(True)

    def _clear_all(self) -> None:
        for _, checkbox in self.checkboxes:
            checkbox.setChecked(False)

    def _invert(self) -> None:
        for _, checkbox in self.checkboxes:
            checkbox.setChecked(not checkbox.isChecked())

    def _selected_indexes(self) -> list[int]:
        return [int(item.get("index") or 0) for item, checkbox in self.checkboxes if checkbox.isChecked()]

    def _load_folder_options(self, current: str) -> None:
        self.folder_edit.clear()
        try:
            folders = self.api.folders()
        except Exception:
            folders = [""]
        for folder in folders:
            self.folder_edit.addItem("(ルート)" if folder == "" else folder, folder)
        match_index = self.folder_edit.findData(current)
        if match_index < 0 and current:
            self.folder_edit.insertItem(0, current, current)
            match_index = 0
        self.folder_edit.setCurrentIndex(max(0, match_index))

    def _folder_text(self) -> str:
        data = self.folder_edit.currentData()
        if isinstance(data, str) and self.folder_edit.currentText() in {"(ルート)", data}:
            return data
        return self.folder_edit.currentText().strip()

    def _save(self) -> None:
        selected = self._selected_indexes()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "保存する動画を選択してください。")
            return
        self.pending_save_folder = self._folder_text()
        payload = {
            "jobId": self.job.get("jobId") or "",
            "videos": self.items,
            "selected": selected,
            "folder": self.pending_save_folder,
        }
        self.buttons.setEnabled(False)
        self.save_progress = QProgressDialog("動画保存を開始しています...", "", 0, 0, self)
        self.save_progress.setWindowTitle(APP_NAME)
        self.save_progress.setCancelButton(None)
        self.save_progress.setWindowModality(Qt.WindowModal)
        self.save_progress.show()
        self.save_worker = VideoSaveWorker(self.api, payload)
        self.save_worker.signals.progress.connect(self._save_progress_changed)
        self.save_worker.signals.finished.connect(self._save_finished)
        self.save_worker.signals.failed.connect(self._save_failed)
        self.save_worker.start()

    def _save_progress_changed(self, message: str) -> None:
        if self.save_progress:
            self.save_progress.setLabelText(message)

    def _finish_save_progress(self) -> None:
        if self.save_progress:
            self.save_progress.close()
            self.save_progress.deleteLater()
            self.save_progress = None
        self.buttons.setEnabled(True)

    def _save_finished(self, data: dict[str, Any]) -> None:
        self._finish_save_progress()
        saved = len(data.get("saved") or [])
        duplicates = len(data.get("duplicates") or [])
        errors = len(data.get("errors") or [])
        self.save_result = {"saved": saved, "duplicates": duplicates, "errors": errors}
        self.save_error = ""
        self.api.set_setting("last_video_folder", self.pending_save_folder)
        if self.show_completion_message:
            QMessageBox.information(
                self,
                APP_NAME,
                f"動画を保存しました。\n保存: {saved}件\n重複: {duplicates}件\n失敗: {errors}件",
            )
        self.accept()

    def _save_failed(self, error: str) -> None:
        self._finish_save_progress()
        self.save_error = str(error)
        QMessageBox.critical(self, APP_NAME, f"動画保存に失敗しました。\n{error}")


class ManualUrlDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("URLを指定")
        self.resize(700, 380)
        layout = QVBoxLayout(self)
        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText(
            "Instagramの投稿・ストーリー / Threads / X のURLを1行ずつ入力してください。\n\n"
            "https://www.instagram.com/reel/...\n"
            "https://www.instagram.com/stories/username/...\n"
            "https://www.threads.com/@user/post/...\n"
            "https://x.com/.../status/..."
        )
        layout.addWidget(QLabel(f"Instagram / Threads / X のURL（最大{MAX_BATCH_URLS}件）"))
        layout.addWidget(self.url_edit)
        self.url_count = QLabel("有効なURL: 0件")
        self.url_count.setStyleSheet("color:#64748b;")
        layout.addWidget(self.url_count)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("取得へ進む")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setEnabled(False)
        self.url_edit.textChanged.connect(self._update_url_count)

    def _update_url_count(self) -> None:
        count = len(self.urls())
        over_limit = count > MAX_BATCH_URLS
        self.url_count.setText(
            f"有効なURL: {count}件"
            + (f"（{MAX_BATCH_URLS}件以下に分けてください）" if over_limit else "")
        )
        self.url_count.setStyleSheet(f"color:{'#dc3545' if over_limit else '#64748b'};")
        self.ok_button.setEnabled(0 < count <= MAX_BATCH_URLS)

    def urls(self) -> list[str]:
        return extract_media_urls(self.url_edit.toPlainText())

    def url(self) -> str:
        urls = self.urls()
        return urls[0] if urls else ""


class BrowserLoginDialog(QDialog):
    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.state = secrets.token_urlsafe(18)
        self.server: HTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.received_token = ""
        self.received_error = ""

        self.setWindowTitle("MFU ログイン")
        self.resize(560, 220)
        layout = QVBoxLayout(self)
        self.status = QLabel(
            "普段使っているChromeでMFUの認可画面を開きます。\n"
            "ログイン後、「MFU Media Clipboard を許可しますか？」で許可してください。"
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        open_btn = QPushButton("Chromeでログイン")
        open_btn.clicked.connect(self._open_browser)
        buttons.addWidget(open_btn)
        done_btn = QPushButton("閉じる")
        done_btn.clicked.connect(self.reject)
        buttons.addWidget(done_btn)
        layout.addLayout(buttons)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(300)
        self.poll_timer.timeout.connect(self._poll_callback)
        self._start_callback_server()
        QTimer.singleShot(150, self._open_browser)

    def _start_callback_server(self) -> None:
        dialog = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                token = (qs.get("token") or [""])[0]
                state = (qs.get("state") or [""])[0]
                if state != dialog.state or not token:
                    dialog.received_error = "認可レスポンスが不正です。もう一度ログインしてください。"
                    body = "MFU Media Clipboard authorization failed. You can close this tab."
                else:
                    dialog.received_token = token
                    body = "MFU Media Clipboard authorization completed. You can close this tab."
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.poll_timer.start()

    def _callback_url(self) -> str:
        if not self.server:
            return ""
        port = int(self.server.server_address[1])
        return f"http://127.0.0.1:{port}/callback"

    def _open_browser(self) -> None:
        callback = self._callback_url()
        if not callback:
            QMessageBox.warning(self, APP_NAME, "ローカルcallbackサーバーを開始できませんでした。")
            return
        params = urlencode({"callback": callback, "state": self.state})
        url = self.api.url(f"/desktop/media-clipboard/login/start?{params}")
        self.status.setText("Chromeで認可画面を開きました。許可後、この画面は自動的に閉じます。")
        chrome = _find_chrome_exe()
        if chrome:
            subprocess.Popen([str(chrome), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)

    def _poll_callback(self) -> None:
        if self.received_error:
            self.poll_timer.stop()
            QMessageBox.warning(self, APP_NAME, self.received_error)
            self.received_error = ""
            return
        if not self.received_token:
            return
        token = self.received_token
        self.received_token = ""
        self.api.set_token(token)
        try:
            self.api.ensure_login()
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"トークン確認に失敗しました。\n{exc}")
            return
        self.poll_timer.stop()
        QMessageBox.information(self, APP_NAME, "ログイン状態を保存しました。")
        self.accept()

    def closeEvent(self, event) -> None:
        self.poll_timer.stop()
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        super().closeEvent(event)


class MediaClipboardApp(QObject):
    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self.qt_app = qt_app
        self.api = ApiClient()
        self.pending_urls: list[str] = []
        self.last_seen_urls: tuple[str, ...] = ()
        self.worker: FetchWorker | None = None
        self.progress: QProgressDialog | None = None
        self.batch_urls: list[str] = []
        self.batch_kinds: list[str] = []
        self.batch_results: list[dict[str, Any]] = []
        self.batch_index = 0
        self.batch_cancel_requested = False
        self.current_url = ""
        self.last_timer_tick = time.monotonic()
        self.resume_recovery_running = False

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(qt_app.style().standardIcon(QStyle.SP_DriveNetIcon))
        self.tray.setToolTip(APP_NAME)
        self.tray.messageClicked.connect(self._open_pending_confirmation)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setContextMenu(self._build_menu())
        self.tray.show()

        self.clipboard = qt_app.clipboard()
        self.clipboard.dataChanged.connect(self._safe_check_clipboard)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._timer_tick)
        self.timer.start()
        QTimer.singleShot(250, self._safe_check_clipboard)
        self.tray.showMessage(APP_NAME, "クリップボード監視を開始しました。", QSystemTrayIcon.Information, 2500)
        logging.info("application started")

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        manual = QAction("URLを指定して取得", self)
        manual.triggered.connect(self._open_manual_url)
        menu.addAction(manual)
        login = QAction("ログイン", self)
        login.triggered.connect(self._open_login)
        menu.addAction(login)
        check = QAction("ログイン確認", self)
        check.triggered.connect(self._show_login_status)
        menu.addAction(check)
        instagram_vnc = QAction("Instagramログイン（VNC）", self)
        instagram_vnc.triggered.connect(self._open_instagram_vnc)
        menu.addAction(instagram_vnc)
        logout = QAction("ログアウト", self)
        logout.triggered.connect(self._logout)
        menu.addAction(logout)
        menu.addSeparator()
        self.startup_action = QAction("Windowsログイン時に起動", self)
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(_startup_enabled())
        self.startup_action.toggled.connect(self._toggle_startup)
        menu.addAction(self.startup_action)
        menu.addSeparator()
        quit_action = QAction("終了", self)
        quit_action.triggered.connect(self.qt_app.quit)
        menu.addAction(quit_action)
        return menu

    def _toggle_startup(self, enabled: bool) -> None:
        try:
            _set_startup_enabled(enabled)
        except Exception as exc:
            self.startup_action.blockSignals(True)
            self.startup_action.setChecked(not enabled)
            self.startup_action.blockSignals(False)
            QMessageBox.warning(None, APP_NAME, f"スタートアップ設定の更新に失敗しました。\n{exc}")
            return
        state = "登録" if enabled else "解除"
        self.tray.showMessage(APP_NAME, f"Windowsのスタートアップを{state}しました。", QSystemTrayIcon.Information, 2500)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            if self.pending_urls:
                self._open_pending_confirmation()
            else:
                self._open_manual_url()

    def _timer_tick(self) -> None:
        now = time.monotonic()
        gap = now - self.last_timer_tick
        self.last_timer_tick = now
        if gap >= 20:
            self._recover_after_resume(gap)
        self._safe_check_clipboard()

    def _recover_after_resume(self, gap_seconds: float) -> None:
        if self.resume_recovery_running:
            return
        self.resume_recovery_running = True
        logging.info("resume recovery scheduled after timer gap %.1fs", gap_seconds)
        QTimer.singleShot(1500, self._finish_resume_recovery)

    def _finish_resume_recovery(self) -> None:
        try:
            self.clipboard = self.qt_app.clipboard()
            if not self.timer.isActive():
                self.timer.start()
            if not self.tray.isVisible():
                self.tray.show()
            try:
                self.clipboard.dataChanged.connect(self._safe_check_clipboard, Qt.UniqueConnection)
            except Exception:
                pass
            self.tray.showMessage(APP_NAME, "スリープ復帰後、監視を再開しました。", QSystemTrayIcon.Information, 2500)
            logging.info("resume recovery completed")
            QTimer.singleShot(1000, self._safe_check_clipboard)
        except Exception:
            logging.exception("resume recovery failed")
        finally:
            self.resume_recovery_running = False

    def _safe_check_clipboard(self) -> None:
        try:
            self._check_clipboard()
        except Exception:
            logging.exception("clipboard check failed")

    def _check_clipboard(self) -> None:
        text = (self.clipboard.text() or "").strip()
        urls = extract_media_urls(text)
        if not urls:
            return
        fingerprint = tuple(urls)
        if fingerprint == self.last_seen_urls:
            return
        self.last_seen_urls = fingerprint
        if self._batch_active():
            self.tray.showMessage(
                APP_NAME,
                "URLを検出しましたが、現在は複数URLを取得中です。\n完了後にもう一度コピーしてください。",
                QSystemTrayIcon.Warning,
                8000,
            )
            return
        self.pending_urls = urls[:MAX_BATCH_URLS]
        omitted = len(urls) - len(self.pending_urls)
        message = f"Instagram / Threads / X のURLを{len(self.pending_urls)}件検出しました。"
        if omitted:
            message += f"\n上限を超えた{omitted}件は対象外です。"
        message += "\nクリックして取得確認を開きます。"
        self.tray.showMessage(
            APP_NAME,
            message,
            QSystemTrayIcon.Information,
            10000,
        )

    def _open_manual_url(self) -> None:
        dialog = ManualUrlDialog()
        if dialog.exec() != QDialog.Accepted:
            return
        urls = dialog.urls()
        if not urls:
            QMessageBox.warning(None, APP_NAME, "Instagram / Threads / X の投稿URLを入力してください。")
            return
        if len(urls) > MAX_BATCH_URLS:
            QMessageBox.warning(None, APP_NAME, f"URLは1度に{MAX_BATCH_URLS}件までです。")
            return
        self.pending_urls = urls
        self._open_pending_confirmation()

    def _show_login_status(self) -> None:
        try:
            self.api.ensure_login()
            QMessageBox.information(None, APP_NAME, "MFUにログイン済みです。")
        except Exception as exc:
            if QMessageBox.question(None, APP_NAME, f"{exc}\n\nログイン画面を開きますか？") == QMessageBox.Yes:
                self._open_login()

    def _open_login(self) -> None:
        BrowserLoginDialog(self.api).exec()

    def _open_instagram_vnc(self) -> None:
        try:
            self.api.ensure_login()
            data = self.api.start_instagram_browser()
            target_url = str(data.get("url") or "")
            if not target_url:
                raise ApiError("VNCのURLを取得できませんでした。")
            chrome = _find_chrome_exe()
            if chrome:
                subprocess.Popen([str(chrome), target_url])
            else:
                webbrowser.open(target_url)
            state = str(data.get("state") or "")
            if state == "otp_required":
                self.tray.showMessage(APP_NAME, "InstagramのOTPをVNC画面で入力してください。", QSystemTrayIcon.Warning, 8000)
            elif state == "logged_in":
                self.tray.showMessage(APP_NAME, "Instagramはログイン済みです。", QSystemTrayIcon.Information, 4000)
        except Exception as exc:
            QMessageBox.warning(None, APP_NAME, f"Instagram VNCを開けませんでした。\n{exc}")

    def _logout(self) -> None:
        if self.api.token:
            try:
                self.api.post_json("/desktop/media-clipboard/api/revoke", {}, timeout=15)
            except Exception:
                pass
        self.api.clear_cookies()
        for path in (WEB_PROFILE_DIR, WEB_CACHE_DIR, EXTERNAL_BROWSER_PROFILE_DIR):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        QMessageBox.information(None, APP_NAME, "このアプリのログイン状態を削除しました。")

    def _open_pending_confirmation(self) -> None:
        if not self.pending_urls:
            return
        urls = list(self.pending_urls)
        box = QMessageBox()
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Question)
        if len(urls) == 1:
            box.setText("このURLからメディアを取得しますか？")
            box.setInformativeText(urls[0])
        else:
            box.setText(f"{len(urls)}件のURLを上から順に取得しますか？")
            preview = "\n".join(urls[:5])
            if len(urls) > 5:
                preview += f"\n…他 {len(urls) - 5}件"
            box.setInformativeText(preview)
            box.setDetailedText("\n".join(urls))
        images_btn = box.addButton("画像", QMessageBox.AcceptRole)
        videos_btn = box.addButton("動画", QMessageBox.AcceptRole)
        frames_btn = box.addButton("動画を写真で取得", QMessageBox.AcceptRole)
        both_btn = box.addButton("画像と動画", QMessageBox.AcceptRole)
        box.addButton("キャンセル", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == images_btn:
            self._start_batch(urls, ["images"])
        elif clicked == videos_btn:
            self._start_batch(urls, ["videos"])
        elif clicked == frames_btn:
            self._start_batch(urls, ["video_frames"])
        elif clicked == both_btn:
            self._start_batch(urls, ["images", "videos"])

    def _start_fetch(self, source_url: str, kinds: list[str]) -> None:
        self._start_batch([source_url], kinds)

    def _batch_active(self) -> bool:
        return bool(self.batch_urls) or bool(self.worker and self.worker.is_alive())

    def _start_batch(self, urls: list[str], kinds: list[str]) -> None:
        urls = list(dict.fromkeys(urls))
        if not urls:
            return
        if len(urls) > MAX_BATCH_URLS:
            QMessageBox.warning(None, APP_NAME, f"URLは1度に{MAX_BATCH_URLS}件までです。")
            return
        if self._batch_active():
            QMessageBox.information(None, APP_NAME, "現在取得中です。完了してから再度実行してください。")
            return
        self.pending_urls = []
        self.batch_urls = urls
        self.batch_kinds = list(kinds)
        self.batch_results = []
        self.batch_index = 0
        self.batch_cancel_requested = False
        self.current_url = ""
        self.progress = QProgressDialog("取得を開始しています...", "残りを中止", 0, 0)
        self.progress.setWindowTitle(APP_NAME)
        self.progress.setWindowModality(Qt.NonModal)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.progress.canceled.connect(self._cancel_batch)
        self.progress.show()
        self._start_next_batch_item()

    def _start_next_batch_item(self) -> None:
        if self.batch_cancel_requested or self.batch_index >= len(self.batch_urls):
            self._finish_batch()
            return
        self.current_url = self.batch_urls[self.batch_index]
        if self.progress:
            self.progress.setLabelText(
                f"{self.batch_index + 1}/{len(self.batch_urls)} URLの取得を開始しています..."
            )
            if not self.progress.isVisible():
                self.progress.show()
        self.worker = FetchWorker(self.api, self.current_url, self.batch_kinds)
        self.worker.signals.progress.connect(self._set_progress_text)
        self.worker.signals.finished.connect(self._batch_fetch_finished)
        self.worker.signals.failed.connect(self._batch_fetch_failed)
        self.worker.start()

    def _set_progress_text(self, text: str) -> None:
        if self.progress:
            self.progress.setLabelText(f"{self.batch_index + 1}/{len(self.batch_urls)}  {text}")

    def _cancel_batch(self) -> None:
        self.batch_cancel_requested = True
        if self.progress:
            self.progress.setLabelText("現在のURLを完了後、残りの取得を中止します。")

    def _fetch_failed(self, message: str) -> None:
        self._batch_fetch_failed(message)

    def _fetch_finished(self, result: dict[str, Any]) -> None:
        self._batch_fetch_finished(result)

    def _batch_fetch_failed(self, message: str) -> None:
        self.batch_results.append(
            {
                "url": self.current_url,
                "shown": False,
                "saved": 0,
                "duplicates": 0,
                "skipped": 0,
                "empty": False,
                "errors": [str(message)],
            }
        )
        if "ログイン" in message:
            self.batch_cancel_requested = True
        self.worker = None
        self.batch_index += 1
        QTimer.singleShot(0, self._start_next_batch_item)

    def _batch_fetch_finished(self, result: dict[str, Any]) -> None:
        if self.progress:
            self.progress.hide()
        outcome = self._show_selection_dialogs(result)
        outcome["url"] = self.current_url
        self.batch_results.append(outcome)
        self.worker = None
        self.batch_index += 1
        QTimer.singleShot(0, self._start_next_batch_item)

    def _show_selection_dialogs(self, result: dict[str, Any]) -> dict[str, Any]:
        shown = False
        saved = 0
        duplicates = 0
        skipped = 0
        errors = [str(value) for value in (result.get("errors") or [])]
        show_completion_message = len(self.batch_urls) == 1
        image_job = result.get("images")
        if isinstance(image_job, dict) and image_job.get("images"):
            shown = True
            dialog = ImageSelectionDialog(
                self.api,
                image_job,
                show_completion_message=show_completion_message,
            )
            accepted = dialog.exec() == QDialog.Accepted
            if accepted and dialog.save_result:
                saved += int(dialog.save_result.get("saved") or 0)
                duplicates += int(dialog.save_result.get("duplicates") or 0)
                failed = int(dialog.save_result.get("errors") or 0)
                if failed:
                    errors.append(f"画像保存: {failed}件失敗")
            else:
                skipped += 1
                if dialog.save_error:
                    errors.append(f"画像保存: {dialog.save_error}")
        video_job = result.get("videos")
        if isinstance(video_job, dict) and video_job.get("videos"):
            shown = True
            dialog = VideoSelectionDialog(
                self.api,
                video_job,
                show_completion_message=show_completion_message,
            )
            accepted = dialog.exec() == QDialog.Accepted
            if accepted and dialog.save_result:
                saved += int(dialog.save_result.get("saved") or 0)
                duplicates += int(dialog.save_result.get("duplicates") or 0)
                failed = int(dialog.save_result.get("errors") or 0)
                if failed:
                    errors.append(f"動画保存: {failed}件失敗")
            else:
                skipped += 1
                if dialog.save_error:
                    errors.append(f"動画保存: {dialog.save_error}")
        return {
            "shown": shown,
            "saved": saved,
            "duplicates": duplicates,
            "skipped": skipped,
            "empty": not shown and not errors,
            "errors": errors,
        }

    def _finish_batch(self) -> None:
        total_urls = len(self.batch_urls)
        kinds = list(self.batch_kinds)
        results = list(self.batch_results)
        cancelled_count = max(0, total_urls - len(results))
        if self.progress:
            self.progress.close()
            self.progress.deleteLater()
            self.progress = None

        self.worker = None
        self.batch_urls = []
        self.batch_kinds = []
        self.batch_results = []
        self.batch_index = 0
        self.batch_cancel_requested = False
        self.current_url = ""

        if total_urls == 1:
            self._show_single_result(results[0] if results else None)
            return
        self._show_batch_summary(results, cancelled_count, kinds)

    def _show_single_result(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        errors = result.get("errors") or []
        if not result.get("shown"):
            text = "取得できるメディアが見つかりませんでした。"
            if errors:
                text += "\n\n" + "\n".join(str(value) for value in errors)
            QMessageBox.warning(None, APP_NAME, text)
        elif errors:
            QMessageBox.warning(
                None,
                APP_NAME,
                "一部の取得または保存に失敗しました。\n\n"
                + "\n".join(str(value) for value in errors),
            )
        if any("ログイン" in str(value) for value in errors):
            if QMessageBox.question(None, APP_NAME, "ログイン画面を開きますか？") == QMessageBox.Yes:
                self._open_login()

    def _show_batch_summary(
        self,
        results: list[dict[str, Any]],
        cancelled_count: int,
        kinds: list[str],
    ) -> None:
        saved = sum(int(row.get("saved") or 0) for row in results)
        duplicates = sum(int(row.get("duplicates") or 0) for row in results)
        skipped = sum(1 for row in results if row.get("skipped") and not row.get("saved"))
        failed_urls = [str(row.get("url") or "") for row in results if row.get("errors")]
        empty = sum(1 for row in results if row.get("empty"))

        box = QMessageBox()
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Information if not failed_urls else QMessageBox.Warning)
        box.setText("複数URLの取得が完了しました。")
        box.setInformativeText(
            f"保存: {saved}件\n重複: {duplicates}件\nスキップ: {skipped}件\n"
            f"メディアなし: {empty}件\n取得・保存失敗: {len(failed_urls)}件"
            + (f"\n中止した残りURL: {cancelled_count}件" if cancelled_count else "")
        )
        details: list[str] = []
        for index, row in enumerate(results, start=1):
            errors = [str(value) for value in (row.get("errors") or [])]
            if errors and (row.get("saved") or row.get("duplicates")):
                status = "⚠ 一部失敗"
            elif errors:
                status = "❌ 失敗"
            elif row.get("saved") or row.get("duplicates"):
                status = "✅ 完了"
            elif row.get("skipped"):
                status = "⏭ スキップ"
            else:
                status = "ℹ メディアなし"
            detail = f"{index}. {status}\n{row.get('url') or ''}"
            detail += f"\n保存:{int(row.get('saved') or 0)}  重複:{int(row.get('duplicates') or 0)}"
            if errors:
                detail += "\n" + "\n".join(errors)
            details.append(detail)
        if cancelled_count:
            details.append(f"未処理: {cancelled_count}件（ユーザーが中止）")
        box.setDetailedText("\n\n".join(details))
        retry_button = None
        if failed_urls:
            retry_button = box.addButton("失敗したURLだけ再試行", QMessageBox.ActionRole)
        box.addButton("閉じる", QMessageBox.AcceptRole)
        box.exec()
        if retry_button is not None and box.clickedButton() == retry_button:
            QTimer.singleShot(0, lambda urls=failed_urls, values=kinds: self._start_batch(urls, values))


def main() -> int:
    setup_logging()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "タスクトレイが利用できません。")
        return 1
    controller = MediaClipboardApp(qt_app)
    qt_app._mfu_media_clipboard_controller = controller
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
