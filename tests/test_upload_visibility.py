from pathlib import Path

from flask import Flask, session

from app.utils.upload_security import (
    can_preview_upload_file,
    current_upload_visibility_version,
    is_upload_owner,
    upload_file_is_hidden,
)


ROOT = Path(__file__).resolve().parents[1]


def test_only_recorded_uploader_can_manage_or_preview_hidden_file():
    app = Flask(__name__)
    app.secret_key = "upload-visibility-test"
    upload = {"username": "photographer", "visibility_version": 4}
    hidden_file = {"id": 10, "is_hidden": 1}
    public_file = {"id": 11, "is_hidden": 0}

    with app.test_request_context("/"):
        session["user"] = "photographer"
        assert is_upload_owner(upload) is True
        assert can_preview_upload_file(upload, hidden_file) is True
        assert can_preview_upload_file(upload, public_file) is True

    with app.test_request_context("/"):
        session["user"] = "admin"
        assert is_upload_owner(upload) is False
        assert can_preview_upload_file(upload, hidden_file) is False
        assert can_preview_upload_file(upload, public_file) is True

    with app.test_request_context("/"):
        assert is_upload_owner(upload) is False
        assert can_preview_upload_file(upload, hidden_file) is False


def test_visibility_values_and_versions_are_normalized():
    assert upload_file_is_hidden({"is_hidden": 1}) is True
    assert upload_file_is_hidden({"is_hidden": "true"}) is True
    assert upload_file_is_hidden({"is_hidden": 0}) is False
    assert current_upload_visibility_version({"visibility_version": "7"}) == 7
    assert current_upload_visibility_version({"visibility_version": "invalid"}) == 0


def test_view_template_has_separate_management_mode_and_confirmations():
    template = (ROOT / "templates" / "view.html").read_text(encoding="utf-8")
    assert "公開状態を管理" in template
    assert "visibility-managing" in template
    assert "選択した${fileIds.length}枚を非公開にします" in template
    assert "閲覧者が表示・ダウンロードできるようになります" in template
    assert "/visibility`" in template
    assert "event.shiftKey" in template
    assert "lightbox-visibility-switch" in template
    assert "role=\"switch\"" in template
    assert "lightboxAfterShow" in template
    assert "lightboxAfterHide" in template
    assert "onChange: (currentIndex, imagesCount)" in template


def test_download_paths_recheck_current_visibility():
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    zip_source = (ROOT / "utils" / "zip_stream.py").read_text(encoding="utf-8")
    mobile_source = (ROOT / "utils" / "mobile_download.py").read_text(encoding="utf-8")

    assert "AND is_hidden=0 ORDER BY filename ASC" in app_source
    assert "visibility_versions" in zip_source
    assert "upload_file_is_hidden(file_row)" in zip_source
    assert "SELECT filename FROM files WHERE upload_id=%s AND is_hidden=0" in mobile_source
    assert "or upload_file_is_hidden(file_row)" in mobile_source
