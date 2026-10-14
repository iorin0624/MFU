"""Run image/video URL fetch jobs outside the web server's reload lifecycle."""

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path

from app import app

from . import routes


LOG = logging.getLogger(__name__)
JOB_NAME = re.compile(r"^(image|video)_([0-9a-f]{32})\.job$")
TERMINAL = {"done", "error", "cancelled", "login_required"}
STOP = threading.Event()


def _heartbeat() -> None:
    while not STOP.is_set():
        try:
            routes._ensure_cache_roots()
            routes.MEDIA_FETCH_HEARTBEAT.touch()
        except OSError:
            LOG.exception("media fetch heartbeat failed")
        STOP.wait(2)


def _process_marker(marker: Path) -> None:
    match = JOB_NAME.fullmatch(marker.name)
    if not match:
        LOG.warning("ignoring invalid media fetch marker: %s", marker.name)
        marker.unlink(missing_ok=True)
        return
    kind, job_id = match.groups()
    read_job = routes._read_instagram_job_from_disk if kind == "image" else routes._read_video_job
    set_job = routes._set_instagram_job if kind == "image" else routes._set_video_job
    job = read_job(job_id)
    if not job:
        marker.unlink(missing_ok=True)
        return
    status = str(job.get("status") or "")
    if status in TERMINAL:
        marker.unlink(missing_ok=True)
        return
    if status not in {"pending", "running", "downloading"}:
        set_job(job_id, {"status": "error", "error": "取得ジョブの状態が不正です。"})
        marker.unlink(missing_ok=True)
        return

    # A running marker at service startup belongs to a process that exited.
    # Retry once, then surface a terminal error rather than polling forever.
    if status != "pending":
        recoveries = int(job.get("recoveries") or 0)
        if recoveries >= 1:
            set_job(job_id, {"status": "error", "error": "画像取得サービスが繰り返し停止しました。再度取得してください。"})
            marker.unlink(missing_ok=True)
            return
        set_job(job_id, {"status": "pending", "recoveries": recoveries + 1, "error": ""})
        job = read_job(job_id)

    source = str(job.get("source") or "")
    source_url = str(job.get("source_url") or "")
    identifier = str(job.get("shortcode" if kind == "image" else "identifier") or "")
    owner = str(job.get("owner_username") or "")
    parsed_source, parsed_identifier = routes._extract_media_identifier(source_url)
    if not owner or (parsed_source, parsed_identifier) != (source, identifier):
        set_job(job_id, {"status": "error", "error": "取得ジョブのURLが不正です。"})
        marker.unlink(missing_ok=True)
        return

    set_job(job_id, {"status": "running", "worker_pid": os.getpid(), "started_at": time.time()})
    try:
        if kind == "image":
            routes._start_instagram_fetch_job(
                identifier, source, source_url,
                video_frames_only=bool(job.get("video_frames_only")),
                owner_username=owner, existing_job_id=job_id,
            )
        else:
            routes._start_video_fetch_job(
                identifier, source, source_url,
                owner_username=owner, existing_job_id=job_id,
            )
    except Exception as exc:
        LOG.exception("media fetch job crashed: %s", marker.name)
        set_job(job_id, {"status": "error", "error": str(exc) or "取得中に予期しないエラーが発生しました。"})
    finally:
        result = read_job(job_id)
        if result.get("status") in TERMINAL:
            marker.unlink(missing_ok=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    routes._ensure_cache_roots()
    lock_path = routes.PREVIEW_ROOT / "_media_fetch_worker.lock"
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("another media fetch worker is already running")
            return 1

        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        heartbeat = threading.Thread(target=_heartbeat, daemon=True, name="media-fetch-heartbeat")
        heartbeat.start()
        LOG.info("media fetch worker started")
        try:
            with app.app_context():
                while not STOP.is_set():
                    try:
                        markers = sorted(
                            routes.MEDIA_FETCH_QUEUE_ROOT.glob("*.job"),
                            key=lambda path: (path.stat().st_mtime, path.name),
                        )
                        if markers:
                            _process_marker(markers[0])
                        else:
                            STOP.wait(0.5)
                    except Exception:
                        LOG.exception("media fetch queue iteration failed")
                        STOP.wait(1)
        finally:
            STOP.set()
            heartbeat.join(timeout=3)
            routes.MEDIA_FETCH_HEARTBEAT.unlink(missing_ok=True)
            LOG.info("media fetch worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
