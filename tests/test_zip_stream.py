import importlib.util
import io
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask


class ZipStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]

        app_module = sys.modules.get("app") or types.ModuleType("app")
        app_module.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
        sys.modules["app"] = app_module

        utils_module = sys.modules.get("app.utils") or types.ModuleType("app.utils")
        utils_module.__path__ = [str(repo_root / "utils")]  # type: ignore[attr-defined]
        sys.modules["app.utils"] = utils_module

        security_module = types.ModuleType("app.utils.upload_security")
        security_module.resolve_upload_subpath = lambda *_args, **_kwargs: None
        security_module.fetch_upload_access_record = lambda _uuid: None
        security_module.can_access_upload_record = lambda *_args, **_kwargs: False
        security_module.can_access_upload_record_from_session = lambda *_args, **_kwargs: False
        security_module.current_upload_visibility_version = lambda *_args, **_kwargs: 0
        security_module.fetch_upload_file_record = lambda *_args, **_kwargs: None
        security_module.fetch_upload_thumbnail_source = lambda *_args, **_kwargs: None
        security_module.has_view_auth = lambda *_args, **_kwargs: False
        security_module.upload_file_is_hidden = lambda *_args, **_kwargs: False
        sys.modules["app.utils.upload_security"] = security_module

        db_module = types.ModuleType("app.utils.db")
        db_module.get_db = lambda: None
        sys.modules["app.utils.db"] = db_module

        spec = importlib.util.spec_from_file_location(
            "app.utils.zip_stream_under_test",
            repo_root / "utils" / "zip_stream.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.zip_stream = module

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.tmp_root = root / "tmp"
        self.album_root = root / "albums"
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.config.update(
            TMP_ROOT=str(self.tmp_root),
            ALBUMS_ROOT=str(self.album_root),
            ALBUMS_ROOT_HDD=str(self.album_root),
            ZIP_PROGRESS_TTL=60,
            ZIP_FILE_TTL=3600,
        )
        self.app.register_blueprint(self.zip_stream.zip_api)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_common_builder_preserves_folders_and_skips_media_recompression(self):
        source = Path(self.temp_dir.name) / "source"
        source.mkdir()
        photo = source / "photo.jpg"
        note = source / "note.txt"
        photo.write_bytes(b"jpeg-data")
        note.write_text("hello", encoding="utf-8")

        with self.app.app_context():
            output = self.zip_stream.make_zip_entries(
                [("子アルバム/photo.jpg", str(photo)), ("子アルバム/note.txt", str(note))],
                "test-zip-builder",
                download_name="親アルバム.zip",
                access={"type": "admin", "album_id": "album-1"},
            )

        self.assertIsNotNone(output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), ["子アルバム/photo.jpg", "子アルバム/note.txt"])
            self.assertEqual(archive.getinfo("子アルバム/photo.jpg").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.getinfo("子アルバム/note.txt").compress_type, zipfile.ZIP_DEFLATED)

    def test_album_prepare_and_download_require_album_session(self):
        album_id = "636d41af-7fd3-42d6-af4c-44546256acd1"
        child_id = "7c0dbe92-54cd-49db-9e67-e16314e799f5"
        album_dir = self.album_root / album_id / child_id
        album_dir.mkdir(parents=True)
        (album_dir / "photo.jpg").write_bytes(b"photo")
        path = f"albums/{album_id}/{child_id}/photo.jpg"
        client = self.app.test_client()

        denied = client.post("/api/zip-prepare", json={"paths": [path]})
        self.assertEqual(denied.status_code, 403)

        with client.session_transaction() as sess:
            sess["album_auth_ids"] = [album_id]
        prepared = client.post(
            "/api/zip-prepare",
            json={"paths": [path]},
            headers={"X-Idempotency-Key": "album-selection-test"},
        )
        self.assertEqual(prepared.status_code, 200)
        payload = prepared.get_json()
        intent = client.post("/api/zip-download-intent", json={
            "key": payload["key"],
            "intent_ref": "d254c4f9-92d5-46a4-910e-29eb8fe802f6",
        })
        self.assertEqual(intent.status_code, 200)
        downloaded = client.get(payload["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        first_bytes = downloaded.data
        with zipfile.ZipFile(io.BytesIO(first_bytes)) as archive:
            self.assertEqual(archive.namelist(), ["photo.jpg"])
        downloaded.close()
        retried = client.get(payload["download_url"])
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.data, first_bytes)
        retried.close()

    def test_selected_zip_retry_creates_separate_history_and_transfer_logs(self):
        key = "selected-zip-retry-test"
        self.tmp_root.mkdir(parents=True)
        with zipfile.ZipFile(self.tmp_root / f"{key}.zip", "w") as archive:
            archive.writestr("00001.jpg", bytes(range(256)) * 1024)
        with self.app.app_context():
            self.zip_stream._progress_write(key, {
                "status": "done",
                "access": {
                    "type": "bearer",
                    "download_history": {
                        "upload_id": 42,
                        "files": [{"file_id": 1, "filename": "photo.jpg"}],
                    },
                },
            })
        event_keys = []

        def record_attempt(**kwargs):
            event_keys.append(kwargs["event_key"])
            return len(event_keys)

        client = self.app.test_client()
        with patch.object(self.zip_stream, "record_upload_download", side_effect=record_attempt), \
             patch("app.utils.upload_download_history.mark_upload_download_status") as mark_status, \
             patch("app.utils.upload_download_history.record_upload_download_audit") as record_audit, \
             self.assertLogs("mfu.download_audit", level="INFO") as logs:
            for _ in range(2):
                response = client.get(f"/api/zip-download/{key}")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data.startswith(b"PK"))
                response.close()

        self.assertEqual(len(event_keys), 2)
        self.assertNotEqual(*event_keys)
        self.assertEqual(mark_status.call_count, 2)
        self.assertEqual(record_audit.call_count, 4)
        self.assertTrue(all(call.kwargs["upload_id"] == 42 for call in record_audit.call_args_list))
        self.assertTrue(all(call.args[1] == "completed" for call in mark_status.call_args_list))
        self.assertEqual(sum("download_started" in line for line in logs.output), 2)
        self.assertEqual(sum("stream_finished" in line for line in logs.output), 2)
        self.assertFalse(any(key in line for line in logs.output))

        with patch.object(self.zip_stream, "record_upload_download", return_value=3), \
             patch("app.utils.upload_download_history.mark_upload_download_status") as mark_status, \
             patch("app.utils.upload_download_history.record_upload_download_audit"), \
             self.assertLogs("mfu.download_audit", level="INFO") as interrupted_logs:
            response = client.get(f"/api/zip-download/{key}", buffered=False)
            next(iter(response.response))
            response.close()
        mark_status.assert_called_once_with(3, "failed")
        self.assertTrue(any('"result": "interrupted"' in line for line in interrupted_logs.output))

    def test_admin_background_job_uses_common_progress_and_download(self):
        photo = Path(self.temp_dir.name) / "admin-photo.jpg"
        photo.write_bytes(b"photo")
        with self.app.app_context():
            key = self.zip_stream.start_zip_entries_job(
                [("子アルバム/admin-photo.jpg", str(photo))],
                key="album-admin-job-test",
                download_name="管理用アルバム.zip",
                access={"type": "admin", "album_id": "album-1"},
            )

        deadline = time.time() + 5
        progress = None
        while time.time() < deadline:
            with self.app.app_context():
                progress = self.zip_stream.read_zip_progress(key)
            if progress and progress.get("status") in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual((progress or {}).get("status"), "done")

        client = self.app.test_client()
        self.assertEqual(client.get(f"/api/zip-progress?key={key}").status_code, 403)
        with client.session_transaction() as sess:
            sess["user"] = "admin"
        self.assertEqual(client.get(f"/api/zip-progress?key={key}").status_code, 200)
        response = client.get(f"/api/zip-download/{key}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("filename*=UTF-8''", response.headers.get("Content-Disposition", ""))
        response.close()


if __name__ == "__main__":
    unittest.main()
