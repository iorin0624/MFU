from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_upload_viewer_keeps_legacy_rollback_and_access_control():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'request.args.get("legacy") != "1"' in source
    assert '@app.get("/view/<uuid>/api")' in source
    assert "if not _can_access_upload_record(upload):" in source
    assert '"notice": str(message_row.get("message") or "").strip()' in source
    assert 'upload.get("mode") == "layer"' in source
    assert 'mode_row.get("enable_layer_upload_url")' in source


def test_public_upload_viewer_uses_requested_labels_without_notice_copy_button():
    component = (
        ROOT
        / "external_login_user"
        / "frontend"
        / "src"
        / "views"
        / "PublicUploadViewer.vue"
    ).read_text(encoding="utf-8")

    assert "<h2>お知らせ</h2>" in component
    assert '<details v-if="data.notice" class="notice-card">' in component
    assert '<details v-if="data.notice" class="notice-card" open>' not in component
    assert "<h2>折り返し</h2>" in component
    assert '>折り返し</a>' in component
    assert "お知らせをコピー" not in component
    assert "notice-card" in component
    assert "FILE UPLOAD" in component
    assert "SHARED ALBUM" not in component
    assert "<dt>撮影日</dt>" in component
    assert "<dt>枚数</dt>" in component
    assert "<dt>保存期間</dt>" in component
    assert " 23:59" in component
    assert "data.upload.modeLabel" not in component


def test_public_upload_viewer_build_artifacts_exist():
    output = ROOT / "static" / "external_login_vue"

    assert (output / "public-upload-viewer.js").is_file()
    assert (output / "public-upload-viewer.css").is_file()
