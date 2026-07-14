import importlib.util
import io
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path

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
        security_module.has_view_auth = lambda *_args, **_kwargs: False
        sys.modules["app.utils.upload_security"] = security_module

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
        downloaded = client.get(payload["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(downloaded.data)) as archive:
            self.assertEqual(archive.namelist(), ["photo.jpg"])
        downloaded.close()

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
