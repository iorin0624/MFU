from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class BrowserAutomationBusy(RuntimeError):
    pass


@contextmanager
def browser_automation_lock(owner: str, *, wait_seconds: float = 0):
    """Serialize browser automation jobs that must not run concurrently."""
    import fcntl

    path = Path(os.getenv("MFU_BROWSER_AUTOMATION_LOCK_FILE", "/mnt/mfu/secure/browser_automation.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    with path.open("r+", encoding="utf-8") as fp:
        while True:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BrowserAutomationBusy("別のブラウザ取得処理が実行中です。") from exc
                time.sleep(1)
        try:
            fp.seek(0)
            fp.truncate()
            fp.write(f"{owner} pid={os.getpid()}\n")
            fp.flush()
            yield
        finally:
            fp.seek(0)
            fp.truncate()
            fp.flush()
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
