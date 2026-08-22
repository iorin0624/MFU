#!/usr/bin/env python3
"""Capture the configured river information page into a RAM-backed PNG."""

import fcntl
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


HEALTH_URL = os.environ.get("MFU_RIVER_HEALTH_URL", "http://127.0.0.1:8080/signage/river/health")
ANALYZE_URL = os.environ.get(
    "MFU_RIVER_ANALYZE_URL", "http://127.0.0.1:8080/signage/river/rain/analyze"
)
RUNTIME_DIR = Path(os.environ.get("MFU_RIVER_CAPTURE_DIR", "/run/mfu-signage"))
IMAGE_PATH = RUNTIME_DIR / "river-latest.png"
STATUS_PATH = RUNTIME_DIR / "river-status.json"
LOCK_PATH = RUNTIME_DIR / "river-capture.lock"
HOME_DIR = RUNTIME_DIR / "home"
PROFILE_DIR = HOME_DIR / "chromium-profile"
CHROMIUM = os.environ.get("MFU_RIVER_CHROMIUM", "/usr/bin/chromium")
MAGICK = os.environ.get("MFU_RIVER_MAGICK", "/usr/bin/magick")
HISTORY_DIR = Path(
    os.environ.get("MFU_RIVER_HISTORY_DIR", "/mnt/mfu/signage_archive/river")
)
HISTORY_INTERVAL_SECONDS = 60
HISTORY_FINE_RETENTION_MINUTES = max(
    5, int(os.environ.get("MFU_RIVER_HISTORY_FINE_RETENTION_MINUTES", "30"))
)
HISTORY_COARSE_INTERVAL_SECONDS = max(
    300, int(os.environ.get("MFU_RIVER_HISTORY_COARSE_INTERVAL_SECONDS", "300"))
)
HISTORY_RETENTION_HOURS = max(
    1, int(os.environ.get("MFU_RIVER_HISTORY_RETENTION_HOURS", "96"))
)
HISTORY_WEBP_QUALITY = max(
    1, min(100, int(os.environ.get("MFU_RIVER_HISTORY_WEBP_QUALITY", "82")))
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def write_status(**values):
    status = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), **values}
    temp_path = STATUS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, STATUS_PATH)


def load_config():
    request = Request(HEALTH_URL, headers={"User-Agent": "MFU-River-Capture/1.0"})
    with urlopen(request, timeout=10) as response:
        data = json.load(response)
    if not data.get("ok") or not data.get("enabled"):
        return None
    url = str(data.get("url") or "").strip()
    if not url.startswith("https://www.river.go.jp/kawabou/pc/"):
        raise RuntimeError("河川情報URLが許可されたURLではありません。")
    return url


def _history_files():
    if not HISTORY_DIR.is_dir():
        return []
    return sorted(
        path for path in HISTORY_DIR.iterdir()
        if path.is_file() and path.suffix.lower() == ".webp"
    )


def _history_timestamp(path):
    try:
        return datetime.strptime(path.stem, "%Y%m%dT%H%M%S%z")
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def prune_history(now=None):
    current = now or datetime.now().astimezone()
    cutoff = current - timedelta(hours=HISTORY_RETENTION_HOURS)
    removed = 0
    for path in _history_files():
        try:
            captured_at = _history_timestamp(path)
            if captured_at < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def compact_history(now=None):
    """Keep every minute for 30 minutes and one image per 5-minute bucket after that."""
    current = now or datetime.now().astimezone()
    fine_cutoff = current - timedelta(minutes=HISTORY_FINE_RETENTION_MINUTES)
    buckets = {}
    for path in _history_files():
        try:
            captured_at = _history_timestamp(path)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if captured_at >= fine_cutoff:
            continue
        bucket = int(captured_at.timestamp()) // HISTORY_COARSE_INTERVAL_SECONDS
        buckets.setdefault(bucket, []).append((captured_at, path))

    removed = 0
    for bucket, entries in buckets.items():
        if len(entries) <= 1:
            continue
        midpoint = (bucket + 0.5) * HISTORY_COARSE_INTERVAL_SECONDS
        keep_path = min(entries, key=lambda item: abs(item[0].timestamp() - midpoint))[1]
        for _, path in entries:
            if path == keep_path:
                continue
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
    return removed


def archive_capture(source, now=None):
    current = now or datetime.now().astimezone()
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    filename = current.strftime("%Y%m%dT%H%M%S%z") + ".webp"
    final_path = HISTORY_DIR / filename
    temp_path = HISTORY_DIR / f".{filename}.tmp.webp"
    try:
        subprocess.run(
            [
                MAGICK,
                str(source),
                "-quality",
                str(HISTORY_WEBP_QUALITY),
                "-define",
                "webp:method=4",
                str(temp_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )
        if not temp_path.is_file() or temp_path.stat().st_size < 20_000:
            raise RuntimeError("履歴WebP画像の容量が小さすぎます。")
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, final_path)
        timestamp = current.timestamp()
        os.utime(final_path, (timestamp, timestamp))
        prune_history(current)
        compact_history(current)
        return final_path
    finally:
        temp_path.unlink(missing_ok=True)


def capture(url):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=RUNTIME_DIR) as temp_dir:
        raw_path = Path(temp_dir) / "raw.png"
        cropped_path = Path(temp_dir) / "river.png"
        environment = os.environ.copy()
        environment.update({"HOME": str(HOME_DIR), "XDG_CONFIG_HOME": str(HOME_DIR / ".config")})
        command = [
            CHROMIUM,
            "--headless",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-crash-reporter",
            "--disable-breakpad",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--window-size=1920,1223",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=10000",
            f"--user-data-dir={PROFILE_DIR}",
            f"--screenshot={raw_path}",
            url,
        ]
        completed = subprocess.run(
            command,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=False,
        )
        if completed.returncode != 0 or not raw_path.is_file():
            detail = (completed.stderr or "").strip()[-1000:]
            raise RuntimeError(f"Chromium screenshot failed rc={completed.returncode}: {detail}")
        subprocess.run(
            [MAGICK, str(raw_path), "-crop", "1920x1080+0+0", "+repage", str(cropped_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=True,
        )
        if cropped_path.stat().st_size < 50_000:
            raise RuntimeError("生成画像の容量が小さすぎます。")
        archived_path = archive_capture(cropped_path)
        os.chmod(cropped_path, 0o644)
        os.replace(cropped_path, IMAGE_PATH)
        return archived_path


def trigger_rain_analysis():
    request = Request(
        ANALYZE_URL,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MFU-River-Capture/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=25) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("雨雲解析APIがエラーを返しました。")
    return result


def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("River capture is already running")
            return 0
        try:
            url = load_config()
            if not url:
                write_status(ok=False, enabled=False, message="河川情報表示は無効です。")
                return 0
            archived_path = capture(url)
            analysis_result = None
            analysis_error = None
            try:
                analysis_result = trigger_rain_analysis()
            except Exception as exc:
                analysis_error = str(exc)
                logging.exception("Rain-cloud analysis failed after capture")
            write_status(
                ok=True,
                enabled=True,
                message="河川情報画像を更新しました。",
                image_size_bytes=IMAGE_PATH.stat().st_size,
                history_saved=bool(archived_path),
                history_interval_seconds=HISTORY_INTERVAL_SECONDS,
                history_fine_retention_minutes=HISTORY_FINE_RETENTION_MINUTES,
                history_coarse_interval_seconds=HISTORY_COARSE_INTERVAL_SECONDS,
                history_retention_hours=HISTORY_RETENTION_HOURS,
                rain_analysis_ok=bool(analysis_result),
                rain_analysis_error=analysis_error,
            )
            logging.info("River image captured bytes=%s", IMAGE_PATH.stat().st_size)
            return 0
        except Exception as exc:
            logging.exception("River image capture failed")
            write_status(ok=False, enabled=True, message=str(exc), stale_image=IMAGE_PATH.is_file())
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
