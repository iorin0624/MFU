from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mode_editor_has_separate_site_and_recipient_templates():
    template = (ROOT / "templates" / "mode_edit_combined.html").read_text(encoding="utf-8")

    assert "サイト内お知らせテンプレート" in template
    assert 'name="site_template"' in template
    assert "相手への通知文テンプレート" in template
    assert 'name="notification_template"' in template


def test_upload_completion_snapshots_and_uses_each_template_separately():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    message_source = (ROOT / "utils" / "message.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS notification_message_templates" in message_source
    assert "SELECT username, mode, template FROM message_templates" in message_source
    assert "CREATE TABLE IF NOT EXISTS upload_notification_messages" in message_source
    assert "site_message = generate_message(" in source
    assert "notification_message = generate_notification_message(" in source
    assert "REPLACE INTO messages (uuid, mode, message)" in source
    assert "REPLACE INTO upload_notification_messages (uuid, mode, message)" in source
    assert "SELECT message FROM upload_notification_messages WHERE uuid = %s" in source
    assert "base_msg = generate_notification_message(" in source
    assert "msg = generate_notification_message(" in source


def test_public_viewer_continues_to_use_site_notice_snapshot():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert '"notice": "" if reply_only_access else str(message_row.get("message") or "").strip()' in source

