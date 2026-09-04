from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import requests


UBER_ACTIVITIES_URL = "https://drivers.uber.com/earnings/activities"
DEBUG_PORT = int(os.environ.get("UBER_BROWSER_DEBUG_PORT", "9225"))
STATE_DIR = Path(os.environ.get("UBER_BROWSER_STATE_DIR", "/mnt/mfu/tmp/uber_browser"))
SECURE_ROOT = Path(os.environ.get("UBER_BROWSER_ROOT", "/mnt/mfu/secure/uber"))
PROFILE_DIR = Path(os.environ.get("UBER_BROWSER_PROFILE_DIR", str(SECURE_ROOT / "chromium_profile")))
HOME_DIR = Path(os.environ.get("UBER_BROWSER_HOME_DIR", str(SECURE_ROOT / "chromium_home")))
LOCK_FILE = Path(os.environ.get("UBER_BROWSER_LOCK_FILE", str(SECURE_ROOT / "browser.lock")))
DISPLAY = os.environ.get("UBER_BROWSER_DISPLAY", ":96")
VNC_PORT = int(os.environ.get("UBER_BROWSER_VNC_PORT", "5910"))
NOVNC_PORT = int(os.environ.get("UBER_BROWSER_NOVNC_PORT", "6083"))
PUBLIC_URL = os.environ.get(
    "UBER_BROWSER_PUBLIC_URL",
    f"http://192.168.103.16:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale",
).strip()
_thread_lock = threading.Lock()


class UberAuthenticationRequired(RuntimeError):
    pass


def _pid_path(name: str) -> Path:
    return STATE_DIR / f"{name}.pid"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(name: str) -> int:
    try:
        pid = int(_pid_path(name).read_text(encoding="utf-8").strip())
        return pid if _alive(pid) else 0
    except Exception:
        return 0


def _write_pid(name: str, pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _pid_path(name).write_text(str(pid), encoding="utf-8")


def _start(name: str, command: list[str], env: dict | None = None) -> int:
    pid = _read_pid(name)
    if pid:
        return pid
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = (STATE_DIR / f"{name}.log").open("ab")
    process = subprocess.Popen(
        command,
        env=env or os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    _write_pid(name, process.pid)
    return process.pid


def _debug_ready() -> bool:
    try:
        response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
        return response.ok and bool(response.json().get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _password() -> str:
    path = SECURE_ROOT / "vnc_password.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except Exception:
        value = ""
    if not value:
        value = secrets.token_urlsafe(12)
        path.write_text(value, encoding="utf-8")
        os.chmod(path, 0o600)
    return value


@contextmanager
def uber_browser_lock(blocking: bool = True):
    import fcntl

    SECURE_ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(mode=0o600, exist_ok=True)
    with _thread_lock, LOCK_FILE.open("r+", encoding="utf-8") as fp:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fp.fileno(), flags)
        except BlockingIOError as exc:
            raise RuntimeError("別のUber取得処理が実行中です。") from exc
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def ensure_uber_browser() -> dict:
    for path in (STATE_DIR, SECURE_ROOT, PROFILE_DIR, HOME_DIR):
        path.mkdir(parents=True, exist_ok=True)
    for path in (SECURE_ROOT, PROFILE_DIR, HOME_DIR):
        os.chmod(path, 0o700)
    config = HOME_DIR / ".config"
    cache = HOME_DIR / ".cache"
    data = HOME_DIR / ".local" / "share"
    for path in (config, cache, data):
        path.mkdir(parents=True, exist_ok=True)
    password = _password()
    _start("xvfb", ["Xvfb", DISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
    env = os.environ.copy()
    env.update({"DISPLAY": DISPLAY, "HOME": str(HOME_DIR), "XDG_CONFIG_HOME": str(config), "XDG_CACHE_HOME": str(cache), "XDG_DATA_HOME": str(data)})
    if not _debug_ready():
        old_pid = _read_pid("chromium")
        if old_pid:
            try:
                os.killpg(old_pid, signal.SIGTERM)
            except OSError:
                pass
            _pid_path("chromium").unlink(missing_ok=True)
        _start(
            "chromium",
            [
                "chromium", f"--user-data-dir={PROFILE_DIR}", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                "--window-size=1280,900", f"--remote-debugging-port={DEBUG_PORT}",
                "--remote-allow-origins=*", UBER_ACTIVITIES_URL,
            ],
            env,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not _debug_ready():
            time.sleep(0.25)
        if not _debug_ready():
            raise RuntimeError("Uber用Chromeを起動できませんでした。")
    _start("x11vnc", ["x11vnc", "-display", DISPLAY, "-localhost", "-forever", "-shared", "-passwdfile", str(SECURE_ROOT / "vnc_password.txt"), "-rfbport", str(VNC_PORT)], env)
    _start("novnc", ["websockify", "--web=/usr/share/novnc", f"0.0.0.0:{NOVNC_PORT}", f"127.0.0.1:{VNC_PORT}"])
    url = PUBLIC_URL
    if "password=" not in url:
        url += ("&" if "?" in url else "?") + urlencode({"password": password})
    return {"running": True, "url": url}


def _browser_ws_url() -> str:
    ensure_uber_browser()
    response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=5)
    response.raise_for_status()
    return str(response.json()["webSocketDebuggerUrl"])


def _browser_call(method: str, params: dict | None = None) -> dict:
    import websocket

    ws = websocket.create_connection(_browser_ws_url(), timeout=15)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == 1:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
    finally:
        ws.close()


def _targets() -> list[dict]:
    response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5)
    response.raise_for_status()
    return [row for row in response.json() if row.get("type") == "page" and row.get("webSocketDebuggerUrl")]


def _find_target(create: bool = True) -> dict:
    ensure_uber_browser()
    matches = [row for row in _targets() if "drivers.uber.com" in str(row.get("url") or "")]
    if not matches and create:
        target_id = _browser_call("Target.createTarget", {"url": UBER_ACTIVITIES_URL}).get("targetId")
        deadline = time.time() + 10
        while time.time() < deadline:
            matches = [row for row in _targets() if row.get("id") == target_id]
            if matches:
                break
            time.sleep(0.2)
    if not matches:
        raise RuntimeError("Uber用Chromeタブを開けませんでした。")
    return matches[0]


def open_uber_login_tab() -> dict:
    result = ensure_uber_browser()
    target = _find_target()
    _browser_call("Target.activateTarget", {"targetId": target["id"]})
    return result


class UberPage:
    def __init__(self, target: dict | None = None):
        import websocket

        self.target = target or _find_target()
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=30)
        self._id = 0

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message_id = self._id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == message_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}).get("result") or {}
        if result.get("subtype") == "error":
            raise RuntimeError(str(result.get("description") or "Uber Chrome JavaScript error"))
        return result.get("value")

    def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("Uber画面の読み込みがタイムアウトしました。")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.wait_ready()
        time.sleep(1)

    def trusted_click(self, element_expression: str) -> bool:
        point = self.evaluate(
            f"""
            (() => {{
              const element = ({element_expression});
              if (!element) return null;
              const rect = element.getBoundingClientRect();
              if (!rect.width || !rect.height) return null;
              return {{x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}};
            }})()
            """
        )
        if not point:
            return False
        params = {"x": float(point["x"]), "y": float(point["y"]), "button": "left", "clickCount": 1}
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **params})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **params})
        return True

    def ensure_logged_in(self) -> None:
        self.navigate(UBER_ACTIVITIES_URL)
        current = str(self.evaluate("location.href") or "")
        available = bool(self.evaluate("!!document.querySelector('input[aria-label=\"Select a date range.\"]')"))
        if not available or "login" in current.lower() or "auth" in current.lower():
            raise UberAuthenticationRequired("Uberへの再ログインが必要です。")

    @staticmethod
    def _month_label(target: date) -> str:
        return target.strftime("%B")

    @staticmethod
    def _ordinal(day: int) -> str:
        if 10 <= day % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suffix}"

    def _calendar_month(self) -> tuple[int, int]:
        value = self.evaluate("(() => { const m=document.querySelector('[aria-label^=\"Month,\"]'); const y=document.querySelector('[aria-label^=\"Year,\"]'); return m&&y ? [m.getAttribute('aria-label').split(',')[1].trim(), Number(y.textContent.trim())] : null; })()")
        if not value:
            raise RuntimeError("Uberの期間選択カレンダーを確認できません。")
        month = time.strptime(value[0], "%B").tm_mon
        return int(value[1]), month

    def _move_calendar(self, target: date) -> None:
        for _ in range(240):
            year, month = self._calendar_month()
            delta = (target.year * 12 + target.month) - (year * 12 + month)
            if delta == 0:
                return
            label = "Next month." if delta > 0 else "Previous month."
            clicked = self.trusted_click(f"document.querySelector('[aria-label={json.dumps(label)}]')")
            if not clicked:
                raise RuntimeError("Uberのカレンダーを移動できません。")
            time.sleep(0.1)
        raise RuntimeError("指定期間がUberの取得可能範囲を超えています。")

    def _click_date(self, target: date) -> None:
        label_part = f"{self._month_label(target)} {self._ordinal(target.day)} {target.year}"
        clicked = self.trusted_click(
            f"[...document.querySelectorAll('[aria-label]')].find(e => e.getAttribute('aria-label').includes({json.dumps(label_part)}) && !e.getAttribute('aria-label').includes('unavailable'))"
        )
        if not clicked:
            raise RuntimeError(f"Uberで{target:%Y-%m-%d}を選択できません。")

    def select_range(self, date_from: date, date_to: date) -> None:
        self.navigate(UBER_ACTIVITIES_URL)
        opened = self.trusted_click("document.querySelector('input[aria-label=\"Select a date range.\"]')")
        if not opened:
            raise UberAuthenticationRequired("Uberへの再ログインが必要です。")
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.evaluate("!!document.querySelector('[aria-label=\"Calendar.\"]')"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Uberの期間選択カレンダーを開けません。")
        self._move_calendar(date_from)
        self._click_date(date_from)
        # Uber automatically selects the end of the search window one week
        # after the clicked date, so clicking date_to would advance another week.
        _ = date_to
        if self.evaluate("!!document.querySelector('[aria-label=\"Calendar.\"]')"):
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape"})
            self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape"})
        time.sleep(1.5)

    def load_all(self) -> None:
        button_expression = "[...document.querySelectorAll('button')].find(x => !x.disabled && ((x.innerText||'').includes('さらに読み込む') || /load more/i.test(x.innerText||'')))"
        for _ in range(200):
            before = int(self.evaluate("document.querySelectorAll('table tbody tr').length") or 0)
            scrolled = self.evaluate(
                f"(() => {{ const button=({button_expression}); if (!button) return false; button.scrollIntoView({{block:'center'}}); return true; }})()"
            )
            if not scrolled:
                return
            time.sleep(0.15)
            if not self.trusted_click(button_expression):
                raise RuntimeError("Uberの「Load More」を押せませんでした。")

            deadline = time.time() + 10
            while time.time() < deadline:
                after = int(self.evaluate("document.querySelectorAll('table tbody tr').length") or 0)
                has_more = bool(self.evaluate(f"!!({button_expression})"))
                if after > before or not has_more:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Uberの「Load More」を押しても明細が増えませんでした。")
        raise RuntimeError("Uber明細の追加読み込み回数が上限を超えました。")

    def list_rows(self) -> list[dict]:
        return self.evaluate("""
        (() => [...document.querySelectorAll('table tbody tr')].map(row => {
          const cells=[...row.querySelectorAll('td')]; const link=row.querySelector('a[href*="/earnings/"]');
          if (!link || cells.length < 3) return null;
          const dateParts=(cells[1].innerText||'').split(/\\n+/).map(x=>x.trim()).filter(Boolean);
          return {typeText:(cells[0].innerText||'').trim(), dateText:dateParts[0]||'', timeText:dateParts[1]||'', amountText:(cells[2].innerText||'').trim(), url:link.href};
        }).filter(Boolean))()
        """) or []


def read_detail(detail_url: str) -> str:
    target_id = _browser_call("Target.createTarget", {"url": detail_url}).get("targetId")
    deadline = time.time() + 10
    target = None
    while time.time() < deadline:
        target = next((row for row in _targets() if row.get("id") == target_id), None)
        if target:
            break
        time.sleep(0.2)
    if not target:
        raise RuntimeError("Uber明細タブを開けませんでした。")
    try:
        with UberPage(target) as page:
            page.wait_ready()
            time.sleep(0.7)
            current = str(page.evaluate("location.href") or "")
            if "login" in current.lower() or "auth" in current.lower():
                raise UberAuthenticationRequired("Uberへの再ログインが必要です。")
            return str(page.evaluate("document.body ? document.body.innerText : ''") or "")
    finally:
        try:
            _browser_call("Target.closeTarget", {"targetId": target_id})
        except Exception:
            pass
