from pathlib import Path

from flask import Flask, session

from app.utils.upload_security import (
    AUTH_EMAIL_OTP,
    AUTH_NONE,
    AUTH_PASSWORD,
    DEFAULT_ALLOWED_EXTENSIONS,
    can_access_upload_record,
    detect_mime_from_bytes,
    grant_view_auth,
    has_view_auth,
    normalize_upload_auth_method,
)


ROOT = Path(__file__).resolve().parents[1]


def test_auth_methods_are_mutually_exclusive_and_legacy_password_is_preserved():
    assert normalize_upload_auth_method("none", require_password=True) == AUTH_NONE
    assert normalize_upload_auth_method("password") == AUTH_PASSWORD
    assert normalize_upload_auth_method("email_otp") == AUTH_EMAIL_OTP
    assert normalize_upload_auth_method(None, require_password=True) == AUTH_PASSWORD


def test_email_recipient_change_invalidates_an_existing_view_session():
    app = Flask(__name__)
    app.secret_key = "upload-email-otp-test"

    with app.test_request_context("/"):
        grant_view_auth("a" * 32, auth_version=3)
        assert has_view_auth("a" * 32, auth_version=3) is True
        assert has_view_auth("a" * 32, auth_version=4) is False


def test_email_otp_upload_needs_a_grant_but_owner_and_admin_keep_management_access():
    app = Flask(__name__)
    app.secret_key = "upload-email-otp-test"
    upload = {"uuid": "b" * 32, "username": "photographer", "auth_method": "email_otp"}

    with app.test_request_context("/"):
        assert can_access_upload_record(upload, has_view_auth_func=lambda _upload: False) is False
        grant_view_auth(upload["uuid"], auth_version=0)
        assert can_access_upload_record(
            upload,
            has_view_auth_func=lambda item: has_view_auth(item["uuid"], 0),
        ) is True

    with app.test_request_context("/"):
        session["user"] = "photographer"
        assert can_access_upload_record(upload, has_view_auth_func=lambda _upload: False) is True

    with app.test_request_context("/"):
        session["user"] = "admin"
        assert can_access_upload_record(upload, has_view_auth_func=lambda _upload: False) is True


def test_expanded_upload_signatures_are_detected_from_bytes():
    assert detect_mime_from_bytes(b"PK\x03\x04" + b"\0" * 32) == "application/zip"
    assert detect_mime_from_bytes(b"\0\0\0\x18ftypheic" + b"\0" * 32) == "image/heif-bmff"
    assert detect_mime_from_bytes(b"\0\0\0\x18ftypcrx " + b"\0" * 32) == "image/x-canon-cr3"
    assert detect_mime_from_bytes(b"II*\0" + b"\0" * 4 + b"CR\x02\0") == "image/x-canon-cr2"
    assert detect_mime_from_bytes(b"MM\0*" + b"\0" * 32) == "image/tiff-raw"

    for extension in (".heic", ".heif", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".zip"):
        assert extension in DEFAULT_ALLOWED_EXTENSIONS


def test_email_otp_routes_and_download_only_frontend_are_present():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "view_email_otp.html").read_text(encoding="utf-8")

    assert '@app.post("/view/<uuid>/otp/send")' in source
    assert '@app.post("/view/<uuid>/otp/verify")' in source
    assert "replace_upload_otp_recipient(int(upload_row[\"id\"]), to_email)" in source
    assert '"view_download_only.html"' in source
    assert "download_zip_for_upload" in source
    download_template = (ROOT / "templates" / "view_download_only.html").read_text(encoding="utf-8")
    assert "すべてのファイルをZIPでダウンロード" in download_template
    assert "autocomplete=\"one-time-code\"" in template
    assert "name=\"csrf_token\"" in template
