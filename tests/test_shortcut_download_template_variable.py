from pathlib import Path

from utils.message import render_message_text, resolve_shortcut_download_url


ROOT = Path(__file__).resolve().parents[1]


def test_shortcut_variable_stays_in_upload_snapshot_and_uses_latest_url():
    snapshot = render_message_text(
        "{{title}}\nSC: {{ shortcut_download_url }}",
        {"title": "写真"},
    )
    assert snapshot == "写真\nSC: {{shortcut_download_url}}"
    assert resolve_shortcut_download_url(
        snapshot, download_url="https://example.com/shortcuts/old"
    ) == "写真\nSC: https://example.com/shortcuts/old"
    assert resolve_shortcut_download_url(
        snapshot, download_url="https://example.com/shortcuts/new"
    ) == "写真\nSC: https://example.com/shortcuts/new"


def test_missing_shortcut_url_does_not_publish_broken_link():
    assert resolve_shortcut_download_url(
        "SC: {{shortcut_download_url}}", download_url=""
    ) == "SC: "
    assert resolve_shortcut_download_url(
        "Unrelated {{value}}", download_url="https://example.com/new"
    ) == "Unrelated {{value}}"


def test_variable_is_listed_and_resolved_at_each_consumption_point():
    mode_editor = (ROOT / "templates" / "mode_edit_combined.html").read_text(encoding="utf-8")
    upload_editor = (ROOT / "templates" / "upload.html").read_text(encoding="utf-8")
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    settings_source = (ROOT / "utils" / "mobile_download.py").read_text(encoding="utf-8")

    assert '("shortcut_download_url", "ショートカット配布URL' in mode_editor
    assert "{{shortcut_download_url}}" in upload_editor
    assert 'get_shortcut_template_url()' in source
    assert 'if not settings.get("is_enabled"):' in settings_source
    assert 'message = resolve_shortcut_download_url(prepared["notification_message"])' in source
    assert 'message = resolve_shortcut_download_url(message)' in source
    assert 'body = resolve_shortcut_download_url(body)' in source
    assert '"notice": "" if reply_only_access else resolve_shortcut_download_url(' in source
    assert 'msg = resolve_shortcut_download_url(' in source
