"""Media Hub's server-scoped GPU classification worker."""

from __future__ import annotations

import json
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import requests


_DLL_LAUNCH_LOCK = threading.Lock()


def _gpu_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONIOENCODING"):
        env.pop(key, None)
    for key in list(env):
        if key.startswith("_PYI_") or key.startswith("PYINSTALLER_"):
            env.pop(key, None)
    runtime_root = runtime_python().parent.parent
    base_python = ""
    try:
        for line in (runtime_root / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("home ="):
                base_python = line.partition("=")[2].strip()
                break
    except OSError:
        pass
    windows = Path(env.get("SystemRoot") or r"C:\Windows")
    # Do not pass the frozen app's Python 3.10 or Qt directories to Python 3.13.
    env["PATH"] = os.pathsep.join(filter(None, (
        str(runtime_python().parent), base_python,
        str(windows / "System32"), str(windows),
    )))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_gpu(source: Path) -> dict:
    # Running the script *inside* PyInstaller's _internal directory puts its
    # Python 3.10 extension modules on Python 3.13's import path. Copy this
    # tiny script beside the temporary preview instead.
    isolated_script = source.parent / "gpu_classifier.py"
    isolated_script.write_bytes(classifier_script().read_bytes())
    command = [str(runtime_python()), str(isolated_script), str(source)]
    kwargs = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace",
        "env": _gpu_environment(),
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    def launch_and_wait() -> tuple[str, int]:
        child = subprocess.Popen(command, **kwargs)
        try:
            stdout, _stderr = child.communicate(timeout=360)
            return stdout, child.returncode
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise RuntimeError("GPU分類がタイムアウトしました。")

    if getattr(sys, "frozen", False):
        # PyInstaller points the process-wide DLL search path at _internal.
        # A separate Python installation must not inherit its python310.dll.
        with _DLL_LAUNCH_LOCK:
            ctypes.windll.kernel32.SetDllDirectoryW(None)
            try:
                stdout, return_code = launch_and_wait()
            finally:
                ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))
    else:
        stdout, return_code = launch_and_wait()
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("GPU分類プログラムから結果を取得できませんでした。") from exc
    if return_code or not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "GPU分類に失敗しました。"))
    return result["suggestion"]


def runtime_python() -> Path:
    return Path(os.environ.get("MFU_MEDIA_HUB_GPU_PYTHON") or (
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MFU" / "MediaHubGPU"
        / "Scripts" / "python.exe"
    ))


def classifier_script() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "gpu_classifier.py"
    return Path(__file__).with_name("gpu_classifier.py")


class StyleWorker:
    def __init__(self, base_url: str, token: str, stop_event: threading.Event,
                 report_error) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.stop_event = stop_event
        self.report_error = report_error
        self.wake = threading.Event()
        self.session = requests.Session()
        self.thread: threading.Thread | None = None
        self.runtime_warned = False

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="mfu-style-gpu", daemon=True)
        self.thread.start()

    def notify(self) -> None:
        self.wake.set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _post(self, path: str, payload: dict) -> dict:
        response = self.session.post(
            self.base_url + path, headers=self._headers(), json=payload, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _process(self, job: dict) -> None:
        job_id = str(job["id"])
        image_path = str(job.get("imagePath") or "")
        if not image_path.startswith("/image_viewer/api/entries/style-jobs/"):
            raise ValueError("分類画像のパスが不正です。")
        response = self.session.get(
            self.base_url + image_path, headers=self._headers(), timeout=60,
        )
        response.raise_for_status()
        if len(response.content) > 5 * 1024 * 1024:
            raise ValueError("分類用画像が大きすぎます。")
        with tempfile.TemporaryDirectory(prefix="mfu-style-") as temp_dir:
            source = Path(temp_dir) / "preview.jpg"
            source.write_bytes(response.content)
            suggestion = _run_gpu(source)
            self._post(f"/image_viewer/api/entries/style-jobs/{job_id}/result", {
                "suggestion": suggestion,
            })

    def run(self) -> None:
        while not self.stop_event.is_set():
            python = runtime_python()
            script = classifier_script()
            if not python.is_file() or not script.is_file():
                if not self.runtime_warned:
                    self.report_error("GPU分類ランタイムが見つかりません。設定手順を確認してください。")
                    self.runtime_warned = True
                self.wake.wait(60)
                self.wake.clear()
                continue
            self.runtime_warned = False
            try:
                result = self._post("/image_viewer/api/entries/style-jobs/claim", {})
                job = result.get("job")
                if job:
                    job_id = str(job["id"])
                    try:
                        self._process(job)
                    except Exception as exc:
                        self.report_error(f"GPU分類に失敗しました: {exc}")
                        try:
                            self._post(f"/image_viewer/api/entries/style-jobs/{job_id}/result", {
                                "error": str(exc)[:300],
                            })
                        except Exception:
                            pass
                    continue
            except Exception:
                # A disconnected server is expected; the WebSocket reconnect and
                # periodic poll repair missed events without changing job state.
                pass
            self.wake.wait(30)
            self.wake.clear()
