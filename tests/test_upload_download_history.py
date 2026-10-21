from pathlib import Path
import importlib.util
import json
import sys
import types
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def test_history_entry_is_in_upload_list_and_history_uses_base_template():
    view_template = (ROOT / "templates" / "view.html").read_text(encoding="utf-8")
    upload_list_template = (
        ROOT / "templates" / "upload_list.html"
    ).read_text(encoding="utf-8")
    history_template = (
        ROOT / "templates" / "view_download_history.html"
    ).read_text(encoding="utf-8")
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert "view_upload_download_history" not in view_template
    assert "view_upload_download_history" in upload_list_template
    assert '{% extends "base.html" %}' in history_template
    assert "upload_history.upload_list" in history_template
    assert "IP：{{ event.ip_address }}" in history_template
    assert "event.requested_at_jst" in history_template
    assert '@app.get("/view/<uuid>/download-history")' in app_source
    assert "if not is_upload_owner(upload):" in app_source


def test_selected_zip_records_file_ids_at_actual_download():
    source = (ROOT / "utils" / "zip_stream.py").read_text(encoding="utf-8")

    assert '"download_history"' in source
    assert 'event_key=f"selected-zip:{safe_key}:{attempt_ref}"' in source
    assert 'download_kind="selected_zip"' in source
    assert "api_zip_download" in source


def test_normal_upload_deletion_purges_history_without_layer_hook():
    source = (ROOT / "utils" / "upload_deletion.py").read_text(encoding="utf-8")
    layer_source = (ROOT / "utils" / "layer_reply.py").read_text(encoding="utf-8")

    assert "purge_upload_download_history" in source
    assert "deleted_download_history" in source
    assert "purge_upload_download_history" not in layer_source


def test_history_schema_cascades_only_with_normal_upload_parent():
    source = (
        ROOT / "utils" / "upload_download_history.py"
    ).read_text(encoding="utf-8")

    assert "FOREIGN KEY (upload_id) REFERENCES uploads (id)" in source
    assert "ON DELETE CASCADE" in source
    assert "layer_upload" not in source.replace(
        "Layer reply uploads are not recorded here.", ""
    )


def _load_history_module():
    app_module = types.ModuleType("app")
    app_module.__path__ = [str(ROOT)]
    utils_module = types.ModuleType("app.utils")
    utils_module.__path__ = [str(ROOT / "utils")]
    db_module = types.ModuleType("app.utils.db")
    db_module.get_db = lambda: None
    spec = importlib.util.spec_from_file_location(
        "app.utils.upload_download_history_audit_test",
        ROOT / "utils" / "upload_download_history.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "app": app_module, "app.utils": utils_module, "app.utils.db": db_module,
    }):
        spec.loader.exec_module(module)
    return module


def test_audit_persists_only_allowlisted_fields_without_download_key():
    module = _load_history_module()
    module._schema_ready = True
    calls = []

    class FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    with patch.object(module, "get_db", return_value=FakeConnection()):
        module.log_download_audit(
            "download_started", upload_id=42, job_ref="1234567890abcdef",
            attempt_ref="attempt-1", http_status=200, archive_bytes=123,
            key="secret-download-key", token="secret-token",
        )
    assert len(calls) == 1
    sql, params = calls[0]
    assert "upload_download_audit_events" in sql
    assert params[:3] == (42, "1234567890abcdef", "download_started")
    details = json.loads(params[3])
    assert details == {
        "job_ref": "1234567890abcdef", "attempt_ref": "attempt-1",
        "http_status": 200, "archive_bytes": 123,
    }


def test_audit_query_is_scoped_to_upload_and_paged():
    module = _load_history_module()
    module._schema_ready = True
    calls = []

    class FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchone(self):
            return {"total": 51}

        def fetchall(self):
            return [{"id": 7, "event_name": "stream_finished",
                     "details_json": '{"result":"interrupted","bytes_yielded":12}',
                     "created_at_jst": None}]

    class FakeConnection:
        def cursor(self, **kwargs):
            return FakeCursor()

        def close(self):
            pass

    with patch.object(module, "get_db", return_value=FakeConnection()):
        result = module.list_upload_download_audit(upload_id=42, page=2)
    assert result["total"] == 51
    assert result["page"] == 2
    assert result["events"][0]["label"] == "ZIP配信が終了"
    assert result["events"][0]["details"] == [
        ("配信結果", "interrupted"), ("送信量（バイト）", 12),
    ]
    assert calls[0][1] == (42,)
    assert calls[1][1] == (42, 50, 50)
    assert all("WHERE upload_id=%s" in sql for sql, _ in calls)


def test_expired_zip_lookup_uses_only_hashed_job_reference():
    module = _load_history_module()
    module._schema_ready = True
    calls = []

    class FakeCursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchone(self):
            return {"upload_id": 42}

    class FakeConnection:
        def cursor(self, **kwargs):
            return FakeCursor()

        def close(self):
            pass

    with patch.object(module, "get_db", return_value=FakeConnection()):
        assert module.find_upload_id_for_zip_job("1234567890abcdef") == 42
        assert module.find_upload_id_for_zip_job("not-a-hash") is None
    assert len(calls) == 1
    assert calls[0][1] == ("1234567890abcdef",)


def test_audit_storage_failure_does_not_interrupt_download_logging():
    module = _load_history_module()
    module._schema_ready = True
    with patch.object(module, "get_db", side_effect=RuntimeError("database offline")):
        module.log_download_audit("stream_finished", upload_id=42, result="interrupted")


def test_history_page_exposes_owner_only_audit_tab():
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "view_download_history.html").read_text(encoding="utf-8")
    assert "if not is_upload_owner(upload):" in app_source
    assert "list_upload_download_audit(upload_id=int(upload[\"id\"]), page=page)" in app_source
    assert "view='logs'" in template
    assert "audit_history.events" in template
