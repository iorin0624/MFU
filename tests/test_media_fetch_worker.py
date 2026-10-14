from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fcntl")

from app.image_viewer import media_fetch_worker, routes


def _job_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(routes, "PREVIEW_ROOT", tmp_path)
    monkeypatch.setattr(routes, "MEDIA_FETCH_QUEUE_ROOT", tmp_path / "queue")
    monkeypatch.setattr(routes, "MEDIA_FETCH_HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(routes, "VIDEO_JOB_ROOT", tmp_path / "video")
    monkeypatch.setattr(routes, "_ensure_cache_roots", lambda: [path.mkdir(exist_ok=True) for path in (tmp_path, tmp_path / "queue", tmp_path / "video")])


def test_image_fetch_is_queued_without_starting_in_web_worker(monkeypatch, tmp_path):
    _job_paths(monkeypatch, tmp_path)
    with patch.object(routes, "_media_image_items") as fetch:
        job_id = routes._start_instagram_fetch_job(
            "DeMen9fpFyt", "instagram", "https://www.instagram.com/p/DeMen9fpFyt/",
            owner_username="alice",
        )
    fetch.assert_not_called()
    assert (tmp_path / "queue" / f"image_{job_id}.job").is_file()
    assert routes._read_instagram_job_from_disk(job_id)["status"] == "pending"


def test_worker_processes_queued_image_and_keeps_result(monkeypatch, tmp_path):
    _job_paths(monkeypatch, tmp_path)
    job_id = routes._start_instagram_fetch_job(
        "DeMen9fpFyt", "instagram", "https://www.instagram.com/p/DeMen9fpFyt/",
        owner_username="alice",
    )
    marker = tmp_path / "queue" / f"image_{job_id}.job"
    def complete(*args, **kwargs):
        routes._set_instagram_job(job_id, {"status": "done", "images": [{"index": 1}]})
    with patch.object(routes, "_start_instagram_fetch_job", side_effect=complete):
        media_fetch_worker._process_marker(marker)
    assert not marker.exists()
    job = routes._read_instagram_job_from_disk(job_id)
    assert job["status"] == "done"
    assert job["images"] == [{"index": 1}]


def test_worker_retries_interrupted_job_once(monkeypatch, tmp_path):
    _job_paths(monkeypatch, tmp_path)
    job_id = routes._start_instagram_fetch_job(
        "DeMen9fpFyt", "instagram", "https://www.instagram.com/p/DeMen9fpFyt/",
        owner_username="alice",
    )
    marker = tmp_path / "queue" / f"image_{job_id}.job"
    routes._set_instagram_job(job_id, {"status": "downloading"})
    with patch.object(routes, "_start_instagram_fetch_job", side_effect=RuntimeError("crashed")):
        media_fetch_worker._process_marker(marker)
    assert not marker.exists()
    job = routes._read_instagram_job_from_disk(job_id)
    assert job["recoveries"] == 1
    assert job["status"] == "error"


def test_video_status_reads_worker_result_from_disk(monkeypatch, tmp_path):
    _job_paths(monkeypatch, tmp_path)
    job_id = "a" * 32
    routes._write_video_job(job_id, {"status": "done", "videos": [{"index": 1}]})
    with routes._video_jobs_lock:
        routes._video_jobs[job_id] = {"status": "pending"}
    assert routes._read_video_job(job_id)["status"] == "done"


def test_worker_availability_requires_recent_heartbeat(monkeypatch, tmp_path):
    _job_paths(monkeypatch, tmp_path)
    assert not routes._media_fetch_worker_available()
    routes.MEDIA_FETCH_HEARTBEAT.touch()
    assert routes._media_fetch_worker_available()
    old = time.time() - 20
    routes.os.utime(routes.MEDIA_FETCH_HEARTBEAT, (old, old))
    assert not routes._media_fetch_worker_available()
