from __future__ import annotations

import base64
import html
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests


DEBUG_PORT = int(os.environ.get("ETC_BROWSER_DEBUG_PORT", "9224"))
ETC_BROWSER_STATE_DIR = Path(os.environ.get("ETC_BROWSER_STATE_DIR", "/mnt/mfu/tmp/etc_browser")).expanduser()
ETC_BROWSER_ROOT = Path(os.environ.get("ETC_CREDENTIALS_ROOT", "/mnt/mfu/secure/etc_accounting")).expanduser()
ETC_BROWSER_PROFILE_DIR = Path(
    os.environ.get("ETC_BROWSER_PROFILE_DIR", str(ETC_BROWSER_ROOT / "chromium_profile"))
).expanduser()
ETC_BROWSER_HOME_DIR = Path(
    os.environ.get("ETC_BROWSER_HOME_DIR", str(ETC_BROWSER_ROOT / "chromium_home"))
).expanduser()
ETC_BROWSER_VNC_PASSWORD_FILE = ETC_BROWSER_ROOT / "vnc_password.txt"
ETC_BROWSER_DISPLAY = os.environ.get("ETC_BROWSER_DISPLAY", ":97")
ETC_BROWSER_VNC_PORT = int(os.environ.get("ETC_BROWSER_VNC_PORT", "5909"))
ETC_BROWSER_NOVNC_PORT = int(os.environ.get("ETC_BROWSER_NOVNC_PORT", "6082"))
ETC_BROWSER_PUBLIC_URL = os.environ.get(
    "ETC_BROWSER_PUBLIC_URL",
    f"http://192.168.103.16:{ETC_BROWSER_NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale",
).strip()
ETC_TOP_URL = "https://www2.etc-meisai.jp/etc/R?funccode=1013000000&nextfunc=1013000000"
ETC_LIST_URL = "https://www2.etc-meisai.jp/etc/R?funccode=1013000000&nextfunc=1013200000"
ETC_PUBLIC_STATUS_URL = "https://www.etc-meisai.jp/"
ETC_MAINTENANCE_MESSAGE = "ETC側メンテナンス中です。公式サイト再開後に自動で再取得します。"
ETC_BROWSER_START_LOCK_FILE = Path(
    os.environ.get("ETC_BROWSER_START_LOCK_FILE", str(ETC_BROWSER_ROOT / "browser_start.lock"))
).expanduser()
_browser_start_thread_lock = threading.Lock()
_maintenance_cache_lock = threading.Lock()
_maintenance_cache = {"checked_at": 0.0, "active": False, "message": ""}


class ETCMaintenanceError(RuntimeError):
    """Raised when the official ETC service is temporarily under maintenance."""


def _pid_path(name: str) -> Path:
    return ETC_BROWSER_STATE_DIR / f"{name}.pid"


def _read_pid(name: str) -> int:
    try:
        return int(_pid_path(name).read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        command_end = stat_text.rfind(")")
        if command_end >= 0 and stat_text[command_end + 2:command_end + 3] == "Z":
            return False
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        pass
    return True


def _process_cmdline(pid: int) -> str:
    if not _process_alive(pid):
        return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _process_matches(
    pid: int,
    required_tokens: tuple[str, ...],
    forbidden_tokens: tuple[str, ...] = (),
) -> bool:
    command = _process_cmdline(pid)
    return (
        bool(command)
        and all(str(token) in command for token in required_tokens)
        and not any(str(token) in command for token in forbidden_tokens)
    )


def _matching_processes(
    required_tokens: tuple[str, ...],
    forbidden_tokens: tuple[str, ...] = (),
) -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    try:
        entries = proc_root.iterdir()
    except OSError:
        return []
    matches = []
    for entry in entries:
        if entry.name.isdigit() and _process_matches(int(entry.name), required_tokens, forbidden_tokens):
            matches.append(int(entry.name))
    return sorted(matches)


def _tcp_listener_pid(port: int, candidate_pids: list[int]) -> int:
    socket_inodes: set[str] = set()
    wanted_port = f"{int(port):04X}"
    for table_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table_path.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            if fields[1].rsplit(":", 1)[-1].upper() == wanted_port:
                socket_inodes.add(fields[9])
    for pid in candidate_pids:
        try:
            descriptors = Path(f"/proc/{pid}/fd").iterdir()
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in socket_inodes:
                return pid
    return 0


def _component_tokens(name: str) -> tuple[str, ...]:
    if name == "xvfb":
        return ("Xvfb", ETC_BROWSER_DISPLAY)
    if name == "chromium":
        return (
            "chromium",
            f"--user-data-dir={ETC_BROWSER_PROFILE_DIR}",
            f"--remote-debugging-port={DEBUG_PORT}",
        )
    if name == "x11vnc":
        return ("x11vnc", "-rfbport", str(ETC_BROWSER_VNC_PORT))
    if name == "novnc":
        return ("websockify", f"0.0.0.0:{ETC_BROWSER_NOVNC_PORT}", f"127.0.0.1:{ETC_BROWSER_VNC_PORT}")
    return ()


def _component_forbidden_tokens(name: str) -> tuple[str, ...]:
    return ("--type=",) if name == "chromium" else ()


def _write_pid(name: str, pid: int) -> None:
    ETC_BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _pid_path(name)
    try:
        path.write_text(str(pid), encoding="utf-8")
    except PermissionError:
        path.unlink(missing_ok=True)
        path.write_text(str(pid), encoding="utf-8")


def _resolve_component_pid(name: str) -> int:
    tokens = _component_tokens(name)
    forbidden = _component_forbidden_tokens(name)
    pid = _read_pid(name)
    if tokens and _process_matches(pid, tokens, forbidden):
        return pid
    if pid:
        _pid_path(name).unlink(missing_ok=True)
    matches = _matching_processes(tokens, forbidden) if tokens else []
    if not matches:
        return 0
    pid = matches[0]
    _write_pid(name, pid)
    return pid


def _start_process(name: str, command: list[str], *, env: dict | None = None) -> tuple[int, bool]:
    pid = _resolve_component_pid(name)
    if pid:
        return pid, False
    ETC_BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = (ETC_BROWSER_STATE_DIR / f"{name}.log").open("ab")
    process = subprocess.Popen(
        command,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env or os.environ.copy(),
        start_new_session=True,
    )
    log_fp.close()
    _write_pid(name, process.pid)
    return process.pid, True


def _terminate_pid(pid: int, timeout: float = 3.0) -> None:
    if not _process_alive(pid):
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.time() + timeout
    while time.time() < deadline and _process_alive(pid):
        time.sleep(0.1)
    if _process_alive(pid):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _browser_debug_ready() -> bool:
    try:
        response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2)
        return bool(response.ok and response.json().get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _running_browser_pid() -> int:
    matches = _matching_processes(_component_tokens("chromium"), _component_forbidden_tokens("chromium"))
    if not matches:
        _pid_path("chromium").unlink(missing_ok=True)
        return 0
    pid = _tcp_listener_pid(DEBUG_PORT, matches) or matches[0]
    _write_pid("chromium", pid)
    return pid


def _remove_duplicate_browser_processes(keep_pid: int) -> None:
    for pid in _matching_processes(_component_tokens("chromium"), _component_forbidden_tokens("chromium")):
        if pid != keep_pid:
            _terminate_pid(pid)


@contextmanager
def _browser_start_lock():
    ETC_BROWSER_ROOT.mkdir(parents=True, exist_ok=True)
    ETC_BROWSER_START_LOCK_FILE.touch(mode=0o600, exist_ok=True)
    try:
        os.chmod(ETC_BROWSER_START_LOCK_FILE, 0o600)
    except OSError:
        pass
    with _browser_start_thread_lock, ETC_BROWSER_START_LOCK_FILE.open("r+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _wait_browser_debug(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2)
            if response.ok and response.json().get("webSocketDebuggerUrl"):
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"ETC用Chromiumを起動できませんでした: {last_error}")


def _vnc_password() -> str:
    try:
        password = ETC_BROWSER_VNC_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        password = ""
    if not password:
        password = secrets.token_urlsafe(9)
        ETC_BROWSER_ROOT.mkdir(parents=True, exist_ok=True)
        ETC_BROWSER_VNC_PASSWORD_FILE.write_text(password, encoding="utf-8")
        os.chmod(ETC_BROWSER_VNC_PASSWORD_FILE, 0o600)
    return password


def _start_etc_browser_locked() -> dict:
    for path in (
        ETC_BROWSER_STATE_DIR,
        ETC_BROWSER_ROOT,
        ETC_BROWSER_PROFILE_DIR,
        ETC_BROWSER_HOME_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
    browser_config_dir = ETC_BROWSER_HOME_DIR / ".config"
    browser_cache_dir = ETC_BROWSER_HOME_DIR / ".cache"
    browser_data_dir = ETC_BROWSER_HOME_DIR / ".local" / "share"
    for path in (browser_config_dir, browser_cache_dir, browser_data_dir):
        path.mkdir(parents=True, exist_ok=True)
    for path in (ETC_BROWSER_ROOT, ETC_BROWSER_PROFILE_DIR, ETC_BROWSER_HOME_DIR):
        os.chmod(path, 0o700)

    vnc_password = _vnc_password()
    debug_ready = _browser_debug_ready()
    chromium_pid = _running_browser_pid() if debug_ready else 0
    if debug_ready and not chromium_pid:
        raise RuntimeError("ETC用ではないChromiumがデバッグポートを使用しています。")

    xvfb_pid, xvfb_started = _start_process(
        "xvfb",
        ["Xvfb", ETC_BROWSER_DISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
    )
    env = os.environ.copy()
    env["DISPLAY"] = ETC_BROWSER_DISPLAY
    env["HOME"] = str(ETC_BROWSER_HOME_DIR)
    env["XDG_CONFIG_HOME"] = str(browser_config_dir)
    env["XDG_CACHE_HOME"] = str(browser_cache_dir)
    env["XDG_DATA_HOME"] = str(browser_data_dir)
    chromium_pid = chromium_pid or _resolve_component_pid("chromium")
    if chromium_pid and not _browser_debug_ready():
        try:
            _wait_browser_debug(timeout=8.0)
        except RuntimeError:
            _terminate_pid(chromium_pid)
            _pid_path("chromium").unlink(missing_ok=True)
            chromium_pid = 0

    chromium_started = False
    if not _browser_debug_ready():
        chromium_pid, chromium_started = _start_process(
            "chromium",
            [
                "chromium",
                f"--user-data-dir={ETC_BROWSER_PROFILE_DIR}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-pdf-extension",
                "--disable-crash-reporter",
                "--disable-breakpad",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                "--window-size=1280,900",
                f"--remote-debugging-port={DEBUG_PORT}",
                "--remote-allow-origins=*",
                ETC_TOP_URL,
            ],
            env=env,
        )
    else:
        chromium_pid = _running_browser_pid()
        if not chromium_pid:
            raise RuntimeError("ETC用ではないChromiumがデバッグポートを使用しています。")

    try:
        _wait_browser_debug()
    except Exception:
        if chromium_started:
            _terminate_pid(chromium_pid)
            _pid_path("chromium").unlink(missing_ok=True)
        if xvfb_started:
            _terminate_pid(xvfb_pid)
            _pid_path("xvfb").unlink(missing_ok=True)
        raise

    _remove_duplicate_browser_processes(chromium_pid)
    _write_pid("chromium", chromium_pid)
    _start_process(
        "x11vnc",
        [
            "x11vnc",
            "-display",
            ETC_BROWSER_DISPLAY,
            "-localhost",
            "-forever",
            "-shared",
            "-passwdfile",
            str(ETC_BROWSER_VNC_PASSWORD_FILE),
            "-rfbport",
            str(ETC_BROWSER_VNC_PORT),
        ],
        env=env,
    )
    _start_process(
        "novnc",
        [
            "websockify",
            "--web=/usr/share/novnc",
            f"0.0.0.0:{ETC_BROWSER_NOVNC_PORT}",
            f"127.0.0.1:{ETC_BROWSER_VNC_PORT}",
        ],
    )
    public_url = ETC_BROWSER_PUBLIC_URL
    if "password=" not in public_url:
        separator = "&" if "?" in public_url else "?"
        public_url = f"{public_url}{separator}{urlencode({'password': vnc_password})}"
    return {
        "running": bool(_running_browser_pid() and _browser_debug_ready()),
        "url": public_url,
        "vncPassword": vnc_password,
        "pids": {name: _read_pid(name) for name in ("xvfb", "chromium", "x11vnc", "novnc")},
    }


def _start_etc_browser() -> dict:
    with _browser_start_lock():
        return _start_etc_browser_locked()


def ensure_shared_browser() -> dict:
    """Start the ETC-only browser kept separate from Instagram."""
    return _start_etc_browser()


def etc_maintenance_status(*, force: bool = False) -> dict:
    now = time.monotonic()
    with _maintenance_cache_lock:
        if not force and now - float(_maintenance_cache["checked_at"]) < 60:
            return dict(_maintenance_cache)
    active = False
    message = ""
    try:
        response = requests.get(ETC_PUBLIC_STATUS_URL, timeout=5)
        text = response.text or ""
        active = bool(
            response.ok
            and (
                "メンテナンスに伴うサービス一時停止" in text
                or ("メンテナンス作業のため" in text and "利用の停止" in text)
            )
        )
        if active:
            plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text))).strip()
            until_match = re.search(r"([0-9０-９]+月[0-9０-９]+日[^。]{0,30}時頃まで)", plain_text)
            message = ETC_MAINTENANCE_MESSAGE
            if until_match:
                message = f"ETC側メンテナンス中です（{until_match.group(1)}）。再開後に自動で再取得します。"
    except Exception:
        pass
    result = {"checked_at": now, "active": active, "message": message}
    with _maintenance_cache_lock:
        _maintenance_cache.update(result)
    return dict(result)


def _raise_if_official_maintenance() -> None:
    status = etc_maintenance_status()
    if status["active"]:
        raise ETCMaintenanceError(status["message"] or ETC_MAINTENANCE_MESSAGE)


def _browser_websocket_url() -> str:
    response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=5)
    response.raise_for_status()
    value = response.json().get("webSocketDebuggerUrl")
    if not value:
        raise RuntimeError("Chromiumのデバッグ接続先を取得できません。")
    return str(value)


def cdp_call(method: str, params: dict | None = None) -> dict:
    import websocket

    ws = websocket.create_connection(_browser_websocket_url(), timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") != 1:
                continue
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message.get("result") or {}
    finally:
        ws.close()


def open_etc_login_tab() -> dict:
    browser = ensure_shared_browser()
    target = find_etc_target(create=True)
    cdp_call("Target.activateTarget", {"targetId": target["id"]})
    return browser


def _page_targets() -> list[dict]:
    return [row for row in _all_targets() if row.get("type") == "page"]


def _all_targets() -> list[dict]:
    response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5)
    response.raise_for_status()
    return [row for row in response.json() if row.get("webSocketDebuggerUrl")]


def find_etc_target(*, create: bool = False) -> dict:
    ensure_shared_browser()
    targets = [row for row in _page_targets() if "etc-meisai.jp" in str(row.get("url") or "")]
    if not targets and create:
        created = cdp_call("Target.createTarget", {"url": ETC_TOP_URL})
        target_id = created.get("targetId")
        deadline = time.time() + 10
        while time.time() < deadline:
            targets = [row for row in _page_targets() if row.get("id") == target_id]
            if targets:
                break
            time.sleep(0.2)
    if not targets:
        raise RuntimeError("ETCログインタブがありません。ETCログイン画面を開いてください。")
    selected = targets[0]
    for duplicate in targets[1:]:
        try:
            cdp_call("Target.closeTarget", {"targetId": duplicate["id"]})
        except Exception:
            pass
    return selected


def set_target_window_name(target_id: str, name: str) -> None:
    import websocket

    deadline = time.time() + 10
    target = None
    while time.time() < deadline:
        target = next((row for row in _page_targets() if row.get("id") == target_id), None)
        if target:
            break
        time.sleep(0.2)
    if not target:
        raise RuntimeError("ETC利用証明書用タブを作成できません。")
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": f"window.name = {json.dumps(name)}", "returnByValue": True},
                }
            )
        )
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == 1:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return
    finally:
        ws.close()


class ETCTargetPage:
    def __init__(self, *, create: bool = False):
        import websocket

        self.target = find_etc_target(create=create)
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=15)
        self.message_id = 0

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.message_id += 1
        message_id = self.message_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message.get("result") or {}

    def evaluate(self, expression: str, *, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        ).get("result") or {}
        if result.get("subtype") == "error":
            raise RuntimeError(str(result.get("description") or "Chromium JavaScript error"))
        return result.get("value")

    def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("ETC画面の読み込みがタイムアウトしました。")

    def wait_navigation(self, marker: str, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                changed = self.evaluate(f"window.__mfuEtcMarker !== {json.dumps(marker)}")
                ready = self.evaluate("document.readyState") == "complete"
                if changed and ready:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("ETC画面の遷移がタイムアウトしました。")

    def navigate(self, url: str) -> None:
        marker = uuid.uuid4().hex
        self.evaluate(f"window.__mfuEtcMarker = {json.dumps(marker)}")
        self.call("Page.navigate", {"url": url})
        self.wait_navigation(marker)

    def is_logged_in(self) -> bool:
        self.wait_ready()
        return bool(self.evaluate("document.body && document.body.innerText.includes('ログアウト')"))

    def _raise_if_maintenance_page(self) -> None:
        current_url = str(self.evaluate("location.href") or "")
        if current_url.startswith("chrome-error://"):
            _raise_if_official_maintenance()

    def login_with_credentials(self, login_id: str, password: str) -> None:
        self.navigate(ETC_TOP_URL)
        self._raise_if_maintenance_page()
        if self.is_logged_in():
            return
        marker = uuid.uuid4().hex
        self.evaluate(f"window.__mfuEtcMarker = {json.dumps(marker)}")
        submitted = self.evaluate(
            f"""
            (() => {{
              const form = document.querySelector('form[name="frm"]');
              const loginId = form && form.querySelector('input[name="risLoginId"]');
              const password = form && form.querySelector('input[name="risPassword"]');
              if (!form || !loginId || !password || typeof window.submitPage !== 'function') return false;
              loginId.value = {json.dumps(login_id)};
              password.value = {json.dumps(password)};
              for (const input of [loginId, password]) {{
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
              }}
              window.submitPage('frm', '/etc/R?funccode=1013000000&nextfunc=1013000000');
              return true;
            }})()
            """
        )
        if not submitted:
            _raise_if_official_maintenance()
            raise RuntimeError("ETCログイン画面の入力欄が見つかりません。")
        self.wait_navigation(marker)
        if not self.is_logged_in():
            try:
                self.evaluate(
                    "for (const input of document.querySelectorAll('input[name=\"risLoginId\"], input[name=\"risPassword\"]')) input.value = ''"
                )
            except Exception:
                pass
            raise RuntimeError("ETC自動ログインに失敗しました。ユーザーIDまたはパスワードを確認してください。")

    def ensure_logged_in(self) -> None:
        self.navigate(ETC_TOP_URL)
        self._raise_if_maintenance_page()
        if self.is_logged_in():
            return
        from .credentials import auto_login_failure, clear_login_failure, load_credentials, record_login_failure

        credentials = load_credentials()
        if not credentials:
            raise RuntimeError("ETC自動ログイン情報が未設定です。管理画面から登録してください。")
        if auto_login_failure():
            raise RuntimeError("ETC自動ログインは前回失敗したため停止中です。管理画面で認証情報を再保存してください。")
        try:
            self.login_with_credentials(credentials["login_id"], credentials["password"])
        except RuntimeError as exc:
            if "ユーザーIDまたはパスワード" in str(exc):
                record_login_failure()
            raise
        clear_login_failure()

    def html(self) -> str:
        self.wait_ready()
        return str(self.evaluate("document.documentElement.outerHTML") or "")

    def _submit(self, path: str) -> None:
        path_json = json.dumps(path)
        marker = uuid.uuid4().hex
        self.evaluate(f"window.__mfuEtcMarker = {json.dumps(marker)}")
        submitted = self.evaluate(
            f"""
            (() => {{
              if (typeof window.submitPage !== 'function') return false;
              const form = document.querySelector('form[name="frm"]');
              if (!form) return false;
              form.target = '_self';
              window.submitPage('frm', {path_json});
              return true;
            }})()
            """
        )
        if not submitted:
            raise RuntimeError("ETC画面の送信フォームが見つかりません。")
        self.wait_navigation(marker)

    def open_statement_month(self, statement_month: str) -> None:
        self.ensure_logged_in()
        has_statement = bool(self.evaluate("document.querySelector('input[name=\"hakkoMeisai\"]')"))
        if not has_statement:
            marker = uuid.uuid4().hex
            self.evaluate(f"window.__mfuEtcMarker = {json.dumps(marker)}")
            clicked = self.evaluate(
                """
                (() => {
                  const link = [...document.querySelectorAll('a')].find(
                    node => (node.textContent || '').includes('利用明細の表示')
                  );
                  if (!link) return false;
                  link.click();
                  return true;
                })()
                """
            )
            if not clicked:
                raise RuntimeError("ETCの利用明細画面を開けません。トップページからやり直してください。")
            self.wait_navigation(marker)
        self._submit(f"/etc/R?funccode=1013000000&nextfunc=1013200000&taisyoYM={statement_month}")

    def go_to_page(self, page_number: int) -> None:
        self.evaluate(
            """
            (() => {
              for (const input of document.querySelectorAll('input[name="hakkoMeisai"]')) {
                if (input.type === 'checkbox') input.checked = false;
                else input.disabled = true;
              }
            })()
            """
        )
        self._submit(f"/etc/R?funccode=1013000000&nextfunc=1013100000&pageNo={int(page_number)}")

    def download_pdf(self, transaction_key: str, form_token: str) -> bytes:
        key_json = json.dumps(transaction_key)
        token_json = json.dumps(form_token)
        before_targets = {row.get("id") for row in _all_targets()}
        try:
            button_rect = self.evaluate(
                f"""
                (() => {{
                  const form = document.querySelector('form[name="frm"]');
                  if (!form) return null;
                  for (const checkbox of form.querySelectorAll('input[type="checkbox"][name="hakkoMeisai"]')) {{
                    checkbox.checked = checkbox.value === {key_json};
                  }}
                  for (const hidden of form.querySelectorAll('input[name="hakkoMeisai"]:not([type="checkbox"])')) {{
                    hidden.disabled = true;
                  }}
                  const token = form.querySelector('input[name="p"]');
                  if (token) token.value = {token_json};
                  const button = [...document.querySelectorAll('input[type="button"]')].find(
                    node => node.value === '利用証明書発行'
                  );
                  if (!button) return null;
                  button.scrollIntoView({{block: 'center', inline: 'center'}});
                  const rect = button.getBoundingClientRect();
                  return {{x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}};
                }})()
                """
            )
            if not isinstance(button_rect, dict):
                raise RuntimeError("ETC利用証明書の発行ボタンが見つかりません。")
            click = {"x": float(button_rect["x"]), "y": float(button_rect["y"]), "button": "left", "clickCount": 1}
            self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **click})
            self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **click})

            viewer_target = None
            deadline = time.time() + 30
            while time.time() < deadline:
                viewer_target = next(
                    (
                        row
                        for row in _all_targets()
                        if row.get("id") not in before_targets
                        and row.get("type") == "iframe"
                        and "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/" in str(row.get("url") or "")
                    ),
                    None,
                )
                if viewer_target:
                    break
                time.sleep(0.2)
            if not viewer_target:
                raise RuntimeError("ETC利用証明書のPDFビューアーが開きませんでした。")

            import websocket

            viewer_ws = websocket.create_connection(viewer_target["webSocketDebuggerUrl"], timeout=60)
            try:
                viewer_ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": """
                                (async () => {
                                  const deadline = Date.now() + 30000;
                                  let viewer = null;
                                  while (Date.now() < deadline) {
                                    viewer = document.querySelector('#viewer');
                                    if (viewer && viewer.currentController && viewer.loadState_ === 'success') break;
                                    await new Promise(resolve => setTimeout(resolve, 100));
                                  }
                                  if (!viewer || !viewer.currentController) {
                                    throw new Error('PDF viewer is not ready');
                                  }
                                  const result = await viewer.currentController.save('EDITED');
                                  if (!result || !result.dataToSave) {
                                    throw new Error('PDF data is unavailable');
                                  }
                                  const bytes = new Uint8Array(result.dataToSave);
                                  let binary = '';
                                  for (let offset = 0; offset < bytes.length; offset += 32768) {
                                    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
                                  }
                                  return btoa(binary);
                                })()
                                """,
                                "returnByValue": True,
                                "awaitPromise": True,
                            },
                        }
                    )
                )
                while True:
                    message = json.loads(viewer_ws.recv())
                    if message.get("id") != 1:
                        continue
                    if message.get("error"):
                        raise RuntimeError(str(message["error"]))
                    result = message.get("result", {}).get("result", {})
                    if result.get("subtype") == "error":
                        raise RuntimeError(str(result.get("description") or "PDF viewer error"))
                    encoded = result.get("value")
                    if not encoded:
                        raise RuntimeError("ETC利用証明書PDFの内容を取得できませんでした。")
                    content = base64.b64decode(encoded)
                    break
            finally:
                viewer_ws.close()

            if not content.startswith(b"%PDF-"):
                raise RuntimeError("取得したETC利用証明書がPDFではありません。")
            return content
        finally:
            for target in _page_targets():
                if target.get("id") not in before_targets and target.get("id") != self.target.get("id"):
                    try:
                        cdp_call("Target.closeTarget", {"targetId": target.get("id")})
                    except Exception:
                        pass


def requests_session_from_browser() -> requests.Session:
    ensure_shared_browser()
    cookies = cdp_call("Storage.getCookies").get("cookies") or []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            "Accept-Language": "ja,en;q=0.5",
        }
    )
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if "etc-meisai.jp" not in domain:
            continue
        session.cookies.set(
            str(cookie.get("name") or ""),
            str(cookie.get("value") or ""),
            domain=domain,
            path=str(cookie.get("path") or "/"),
        )
    return session


def same_origin_url(path: str) -> str:
    parsed = urlparse(ETC_LIST_URL)
    return f"{parsed.scheme}://{parsed.netloc}{path}"
