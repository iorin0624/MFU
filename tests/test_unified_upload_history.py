from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_upload_history_is_a_grouped_card_view():
    template = (ROOT / "templates" / "upload_list.html").read_text(encoding="utf-8")

    assert "アップロードファイルあり" in template
    assert "折り返し受付のみ" in template
    assert 'class="upload-history-card"' in template
    assert "閲覧保護タイプ" in template
    assert "view_upload_download_history" in template
    assert "layer_upload_detail" in template
    assert "アップロードファイル削除" in template
    assert "折り返し削除" in template
    assert "両方削除" in template


def test_legacy_layer_list_redirects_to_the_unified_view():
    source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")

    route_start = source.index('def layer_upload_list():')
    route_end = source.index('@upload_history_bp.route("/layer_upload_list/<uuid>")')
    route_source = source[route_start:route_end]
    assert 'url_for("upload_history.upload_list", group="reply-only")' in route_source
    assert 'render_template("layer_upload_list.html"' not in route_source


def test_unified_view_preserves_scoped_and_combined_deletion():
    source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")

    assert 'def upload_delete(uuid):' in source
    assert 'def layer_upload_delete(uuid):' in source
    assert 'def upload_delete_all(uuid):' in source
    assert 'require_admin_passkey(f"upload_delete_all:{uuid}")' in source
    assert 'delete_normal_upload(' in source
    assert 'delete_layer_replies(upload["id"]' in source
    assert "DELETE FROM uploads" not in source


def test_protection_labels_cover_every_public_auth_mode():
    source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")

    for label in ("PW", "アクセストークン", "メールOTP認証", "なし"):
        assert label in source


def test_reply_summary_includes_batches_and_file_count():
    source = (ROOT / "utils" / "layer_reply_store.py").read_text(encoding="utf-8")
    history_source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")

    assert "COUNT(DISTINCT reply.id) AS folder_count" in source
    assert "COUNT(file.id) AS reply_file_count" in source
    assert '"reply_file_count"' in source
    assert "AS reply_summary ON reply_summary.upload_id=upload.id" in history_source
    assert 'row = {**upload, **_layer_summary' not in history_source
