from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.ios_upload_images import looks_like_heif
from utils.uploader_auth import _format_utc_as_jst


def test_format_uploader_token_timestamp_as_jst():
    expected = "2026年08月27日 12:04:05"
    assert _format_utc_as_jst(datetime(2026, 8, 27, 3, 4, 5)) == expected
    assert (
        _format_utc_as_jst(datetime(2026, 8, 27, 3, 4, 5, tzinfo=timezone.utc))
        == expected
    )
    assert _format_utc_as_jst(None) == ""


def test_heif_detection_uses_file_signature_not_only_extension():
    assert looks_like_heif(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    assert looks_like_heif(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 64)
    assert not looks_like_heif(b"\xff\xd8\xff" + b"\x00" * 64)


def test_ios_api_is_scoped_and_reuses_upload_completion_pipeline():
    auth_source = (ROOT / "utils" / "uploader_auth.py").read_text(encoding="utf-8")
    api_source = (ROOT / "utils" / "ext_api_uploads.py").read_text(encoding="utf-8")
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'TOKEN_SCOPE_IOS = "ios_shortcut_upload"' in auth_source
    assert 'url_prefix="/api/ios-upload/v1"' in api_source
    assert '@ios_up.route("/config", methods=["GET"])' in api_source
    assert '@ios_up.route("/create", methods=["POST"])' in api_source
    assert '@ios_up.route("/original", methods=["POST"])' in api_source
    assert '@ios_up.route("/done", methods=["POST"])' in api_source
    assert "convert_heif_to_jpeg" in api_source
    assert "background_thumb_and_notify" in api_source
    assert '"message": str(prepared.get("message") or "")' in api_source
    assert "app.register_blueprint(ios_up)" in app_source


def test_ios_api_key_admin_template_parses_and_warns_key_is_one_time_only():
    source = (ROOT / "templates" / "admin_ios_shortcut_upload.html").read_text(
        encoding="utf-8"
    )
    assert source.count("{% block") == source.count("{% endblock %}")
    assert "今回だけ表示されるAPIキー" in source
    assert "APIキーを無効化" in source
    assert 'name="csrf_token"' in source
