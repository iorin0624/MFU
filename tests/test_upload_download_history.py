from pathlib import Path


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
    assert 'event_key=f"selected-zip:{safe_key}"' in source
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
