from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "tools" / "mfu_photo_relay" / "core.py"
SERVER_PATH = ROOT / "utils" / "photo_relay.py"
CLIENT_PATH = ROOT / "tools" / "mfu_photo_relay" / "main.py"
SHORTCUT_PATH = ROOT / "shortcuts" / "MFU写真リアルタイム転送.base.json"


def _load_core():
    spec = importlib.util.spec_from_file_location("mfu_photo_relay_core", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_original_name_is_preserved_and_heic_becomes_jpeg(tmp_path):
    core = _load_core()
    result = core.choose_output_path(
        tmp_path,
        {
            "original_filename": "IMG_1234.HEIC",
            "mime_type": "image/jpeg",
            "converted_to_jpeg": True,
        },
        naming_mode=core.NAMING_ORIGINAL,
        sequence_digits=5,
        next_sequence=1,
    )
    assert result.path.name == "IMG_1234.jpg"


def test_datetime_name_prefers_capture_time_and_avoids_collision(tmp_path):
    core = _load_core()
    (tmp_path / "2026-09-26_143052.jpg").write_bytes(b"existing")
    result = core.choose_output_path(
        tmp_path,
        {
            "original_filename": "photo.jpg",
            "mime_type": "image/jpeg",
            "capture_at": "2026-09-26T14:30:52",
            "source_modified_at": "2025-01-01T01:02:03",
        },
        naming_mode=core.NAMING_DATETIME,
        sequence_digits=5,
        next_sequence=1,
        now=datetime(2024, 1, 1),
    )
    assert result.path.name == "2026-09-26_143052_02.jpg"


def test_datetime_name_falls_back_to_modified_time(tmp_path):
    core = _load_core()
    result = core.choose_output_path(
        tmp_path,
        {
            "original_filename": "photo.png",
            "mime_type": "image/png",
            "source_modified_at": "2026-09-25T01:02:03",
        },
        naming_mode=core.NAMING_DATETIME,
        sequence_digits=5,
        next_sequence=1,
    )
    assert result.path.name == "2026-09-25_010203.png"


def test_sequence_skips_existing_number(tmp_path):
    core = _load_core()
    (tmp_path / "00001.jpg").write_bytes(b"existing")
    result = core.choose_output_path(
        tmp_path,
        {"original_filename": "photo.jpg", "mime_type": "image/jpeg"},
        naming_mode=core.NAMING_SEQUENCE,
        sequence_digits=5,
        next_sequence=1,
    )
    assert result.path.name == "00002.jpg"
    assert result.next_sequence == 3


def test_server_uses_websocket_control_and_authenticated_https_transfer():
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert 'namespace="/photo-relay"' in source
    assert '"photo_relay_file_ready"' in source
    assert '@desktop_photo_relay_bp.get("/api/files/<int:file_id>/download")' in source
    assert '@desktop_photo_relay_bp.post("/api/files/<int:file_id>/ack")' in source
    assert '@photo_relay_api_bp.get("/jobs/<job_uuid>")' in source
    assert '@desktop_photo_relay_bp.get("/download/windows")' in source
    assert '@desktop_photo_relay_bp.get("/api/queue")' in source
    assert '"incomplete_upload"' in source
    assert '"no_files_uploaded"' in source
    assert "TOKEN_SCOPE_IOS" in source


def test_server_keeps_png_and_jpeg_and_converts_heic():
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert 'mime_type not in {"image/jpeg", "image/png", "image/heif-bmff"}' in source
    assert "convert_heif_to_jpeg" in source
    assert 'extension = ".jpg" if mime_type == "image/jpeg" else ".png"' in source


def test_relay_detects_incomplete_uploads_and_repairs_missed_events():
    server = SERVER_PATH.read_text(encoding="utf-8")
    client = CLIENT_PATH.read_text(encoding="utf-8")
    shortcut = SHORTCUT_PATH.read_text(encoding="utf-8")
    assert "expected_file_count" in shortcut
    assert '"incomplete_upload"' in server
    assert '"no_files_uploaded"' in server
    assert '@desktop_photo_relay_bp.get("/api/queue")' in server
    assert "self.stop_event.wait(30)" in client
    assert 'self.api.get("/desktop/photo-relay/api/queue")' in client


def test_receiver_ack_cannot_close_job_before_iphone_done():
    source = SERVER_PATH.read_text(encoding="utf-8")
    assert "progress[3] is not None" in source
    assert 'str(progress[2]) != "receiving"' in source
    assert "ready_at=COALESCE(ready_at, UTC_TIMESTAMP())" in source
    assert 'job.get("status") == "completed" and not job.get("ready_at")' in source
