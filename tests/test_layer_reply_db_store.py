from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_layer_reply_routes_have_no_json_dependency():
    source = (ROOT / "utils" / "layer_reply.py").read_text(encoding="utf-8")

    assert "info.json" not in source
    assert "json.load" not in source
    assert "json.dump" not in source
    assert "create_layer_reply(" in source
    assert "get_layer_reply(reply_uuid)" in source
    assert "create_zip" not in source
    assert "zip_path" not in source
    assert source.index("create_layer_reply(") < source.index("send_discord_upload_notification(")


def test_layer_reply_schema_is_normalized_and_cascades():
    source = (ROOT / "utils" / "layer_reply_store.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS layer_upload_replies" in source
    assert "CREATE TABLE IF NOT EXISTS layer_upload_reply_files" in source
    assert "FOREIGN KEY (upload_id) REFERENCES uploads (id)" in source
    assert "FOREIGN KEY (reply_id) REFERENCES layer_upload_replies (id)" in source
    assert source.count("ON DELETE CASCADE") >= 2


def test_detail_page_displays_comment_and_posted_at():
    template = (ROOT / "templates" / "layer_upload_detail.html").read_text(encoding="utf-8")

    assert "group.comment" in template
    assert "（コメントなし）" in template
    assert "group.posted_at.strftime" in template
    assert 'style="white-space: pre-wrap;' in template
    assert "UUID一括ZIPダウンロード" in template
    assert "このアップロードをZIP" in template
    assert "data-reply-uuid" in template
    assert "layer-upload-detail.js" in template
    assert "{% block body_extra %}" in template


def test_history_and_file_authorization_use_database_records():
    source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")

    assert "list_layer_reply_groups(upload_id)" in source
    assert "get_layer_reply_summary(upload_id)" in source
    assert "layer_reply_file_exists(" in source
    assert "delete_layer_replies(upload[\"id\"]" in source
    assert "def layer_upload_zip_prepare" in source
    assert "start_zip_entries_job(" in source
    assert '"type": "layer_upload"' in source


def test_layer_zip_download_is_owner_scoped_and_on_demand():
    zip_source = (ROOT / "utils" / "zip_stream.py").read_text(encoding="utf-8")
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "layer-upload-detail.js").read_text(encoding="utf-8")

    assert 'if access_type == "layer_upload":' in zip_source
    assert 'username == "admin" or username == owner' in zip_source
    assert '"/layer_upload_list/",' in app_source
    assert 'method: "POST"' in script
    assert "reply_uuid: replyUuid" in script
    assert "MFUZipDownload.waitUntilReady" in script


def test_migration_requires_verified_backup_before_json_removal():
    source = (
        ROOT / "scripts" / "migrate_layer_reply_json_to_db.py"
    ).read_text(encoding="utf-8")

    assert "_verify_existing(source, stored)" in source
    assert "tarfile.open(backup_path" in source
    assert "if len(archived) != len(sources)" in source
    assert 'source["info_path"].unlink()' in source
