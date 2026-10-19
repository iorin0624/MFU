import ast
import io
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "__init__.py").read_text(encoding="utf-8")
VIEWER_SOURCE = (
    ROOT / "external_login_user" / "frontend" / "src" / "views" / "PublicUploadViewer.vue"
).read_text(encoding="utf-8")


def test_append_window_closes_at_expiry_without_extending_existing_upload():
    module = ast.parse(APP_SOURCE)
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_upload_is_open_for_append"
    )
    namespace = {"datetime": datetime, "date_cls": date, "timedelta": timedelta, "timezone": timezone}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "__init__.py", "exec"), namespace)
    is_open = namespace["_upload_is_open_for_append"]
    today = datetime.now(timezone(timedelta(hours=9))).date()

    assert is_open({"expire_at": today, "upload_deleted_at": None})
    assert not is_open({"expire_at": today - timedelta(days=1), "upload_deleted_at": None})
    assert not is_open({"expire_at": today + timedelta(days=1), "upload_deleted_at": datetime.now()})


def test_append_is_owner_only_and_rebuilds_download_and_thumbnail_state():
    route = APP_SOURCE.split('@app.post("/view/<uuid>/append")', 1)[1].split(
        '@app.route("/upload/<path:subpath>")', 1
    )[0]
    assert "if not is_upload_owner(upload):" in route
    assert "if not _upload_is_open_for_append(upload):" in route
    assert 'request.files.get("photo")' in route
    assert "validate_upload_file(" in route
    assert "tempfile.NamedTemporaryFile(" in route
    assert "os.link(staged_path, candidate)" in route
    assert '"INSERT INTO files (upload_id, filename) VALUES (%s, %s)"' in route
    assert "visibility_version=visibility_version+1" in route
    assert "_purge_upload_zip_cache(uuid)" in route
    assert 'enqueue_thumb_job("upload", uuid, "thumb")' in route
    assert "background_thumb_and_notify" not in route
    assert "UPDATE uploads SET expire_at" not in route


def test_append_panel_tracks_progress_and_keeps_failed_files_for_retry():
    assert 'v-if="data.permissions.manageVisibility && !data.replyOnly" id="append"' in VIEWER_SOURCE
    assert "appendOneFile(uuid, file" in VIEWER_SOURCE
    assert "appendFiles.value = failed" in VIEWER_SOURCE
    assert "await load()" in VIEWER_SOURCE
    assert "URL・保存期限は変わらず、相手への通知も送信しません" in VIEWER_SOURCE


def test_append_saves_new_name_without_overwriting_existing_file():
    module = ast.parse(APP_SOURCE)
    functions = [
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_upload_is_open_for_append", "append_upload_file"}
    ]
    fake_app = SimpleNamespace(
        post=lambda _route: lambda function: function,
        logger=SimpleNamespace(
            info=lambda *args: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args: None,
        ),
    )
    upload = {
        "id": 7,
        "username": "owner",
        "expire_at": date.today() + timedelta(days=5),
        "upload_deleted_at": None,
        "generate_thumbnails": True,
    }
    rows = [{"filename": "photo.jpg"}]
    statements = []
    queued = []
    purged = []
    reject_insert = {"enabled": False}

    class FakeCursor:
        def execute(self, sql, params):
            statements.append((sql, params))
            if reject_insert["enabled"] and "INSERT INTO files" in sql:
                raise RuntimeError("test-only database failure")
            self.sql = sql

        def fetchall(self):
            return rows

        def fetchone(self):
            return upload

        def close(self):
            pass

    class FakeDb:
        def cursor(self, **_kwargs):
            return FakeCursor()

        def commit(self):
            statements.append(("COMMIT", ()))

        def rollback(self):
            statements.append(("ROLLBACK", ()))

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as directory:
        original_dir = Path(directory) / "original"
        original_dir.mkdir()
        (original_dir / "photo.jpg").write_bytes(b"existing")
        request = SimpleNamespace(
            files={
                "photo": SimpleNamespace(
                    filename="photo.jpg", mimetype="image/jpeg", stream=io.BytesIO(b"new-photo")
                )
            }
        )
        namespace = {
            "app": fake_app,
            "request": request,
            "session": {"user": "owner"},
            "current_app": SimpleNamespace(config={}),
            "jsonify": lambda value: value,
            "_UUID32_RE": re.compile(r"^[0-9a-f]{32}$"),
            "_get_upload_access_record": lambda _uuid: upload,
            "is_upload_owner": lambda item: item["username"] == "owner",
            "_upload_dirs": lambda _uuid: (directory, str(original_dir), str(Path(directory) / "thumb")),
            "get_db": FakeDb,
            "sanitize_filename": lambda filename, _used: filename,
            "detect_mime_from_bytes": lambda _data: "image/jpeg",
            "validate_upload_file": lambda **_kwargs: (True, "ok"),
            "DEFAULT_ALLOWED_EXTENSIONS": {".jpg": "image/jpeg"},
            "_purge_upload_zip_cache": lambda uuid: purged.append(uuid),
            "enqueue_thumb_job": lambda *args: queued.append(args),
            "date_cls": date,
            "datetime": datetime,
            "timedelta": timedelta,
            "timezone": timezone,
            "os": os,
            "shutil": shutil,
            "tempfile": tempfile,
            "Path": Path,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "__init__.py", "exec"), namespace)
        uuid = "a" * 32
        result = namespace["append_upload_file"](uuid)
        assert result == {"ok": True, "saved": "photo_2.jpg", "thumbnailStatus": "queued"}
        assert (original_dir / "photo.jpg").read_bytes() == b"existing"
        assert (original_dir / "photo_2.jpg").read_bytes() == b"new-photo"
        assert list(original_dir.glob(".append-*.tmp")) == []
        assert queued == [("upload", uuid, "thumb")]
        assert purged == [uuid]
        assert any("INSERT INTO files" in sql and params == (7, "photo_2.jpg") for sql, params in statements)
        assert any("visibility_version=visibility_version+1" in sql for sql, _params in statements)

        request.files["photo"].filename = "photo.png"
        request.files["photo"].stream = io.BytesIO(b"other-extension")
        assert namespace["append_upload_file"](uuid)["saved"] == "photo_3.png"
        assert (original_dir / "photo_3.png").read_bytes() == b"other-extension"
        request.files["photo"].filename = "photo.jpg"

        namespace["session"]["user"] = "other"
        assert namespace["append_upload_file"](uuid)[1] == 403
        assert sorted(path.name for path in original_dir.iterdir()) == ["photo.jpg", "photo_2.jpg", "photo_3.png"]

        namespace["session"]["user"] = "owner"
        upload["expire_at"] = date.today() - timedelta(days=1)
        assert namespace["append_upload_file"](uuid)[1] == 410

        upload["expire_at"] = date.today() + timedelta(days=5)
        request.files["photo"].stream = io.BytesIO(b"should-rollback")
        reject_insert["enabled"] = True
        assert namespace["append_upload_file"](uuid)[1] == 500
        assert sorted(path.name for path in original_dir.iterdir()) == ["photo.jpg", "photo_2.jpg", "photo_3.png"]
