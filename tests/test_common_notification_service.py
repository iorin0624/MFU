from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "utils" / "notification_service.py"
NOTIFICATIONS = ROOT / "external_login_user" / "notifications.py"
DISCORD = ROOT / "discord_notifications" / "service.py"
SENDER = ROOT / "tools" / "mfu_notification_sender" / "mfu_notify.py"
BRIDGE = ROOT / "tools" / "mfu_notification_sender" / "mfu_notification_bridge.py"
RPI_MIGRATION = ROOT / "tools" / "mfu_notification_sender" / "migrate_raspberry_pi_discord.py"
FREEPBX_MIGRATION = ROOT / "tools" / "mfu_notification_sender" / "migrate_freepbx_discord.py"


def test_common_notification_schema_keeps_rich_content_and_muted_history():
    source = NOTIFICATIONS.read_text(encoding="utf-8")
    for column in ("feature_key", "severity", "topic_key", "content_json", "source_id", "muted_at"):
        assert column in source
    assert "read_at" in source
    assert "if inserted and muted_at is None" in source
    common = SERVICE.read_text(encoding="utf-8")
    assert '"cards": [row for row in (cards or [])' in common
    assert '"lead_text": str(lead_text or "")' in common


def test_external_ingress_supports_native_and_discord_compatible_requests():
    source = SERVICE.read_text(encoding="utf-8")
    assert '"/api/notification-ingress/discord/<token>"' in source
    assert '"/api/notification-ingress/v1/events"' in source
    assert "_source_rate_allowed" in source
    assert "mfu_notification_ingress_audit" in source


def test_common_notification_admin_pages_are_added_to_system_navigation():
    source = SERVICE.read_text(encoding="utf-8")
    assert '("外部通知元", "/admin/notification-sources")' in source
    assert '("共通通知設定", "/admin/notification-settings")' in source
    assert "UPDATE mfu_nav_items SET parent_id=%s" in source


def test_discord_same_site_absolute_links_are_normalized_for_common_notifications():
    source = SERVICE.read_text(encoding="utf-8")
    assert "def _common_notification_target_url(" in source
    assert '== "mfu.iori0624.jp"' in source
    assert 'return "/mfu-notifications"' in source


def test_existing_discord_delivery_uses_common_dispatcher_without_creating_a_loop():
    service = DISCORD.read_text(encoding="utf-8")
    common = SERVICE.read_text(encoding="utf-8")
    assert "dispatch_discord_notification" in service
    assert "def dispatch_discord_notification(" in common
    assert "mirror=False" in common


def test_legacy_feature_senders_use_common_notification_delivery():
    paths = (
        "utils/upload_notifications.py",
        "etc_accounting/notifications.py",
        "shipment_tracking/services.py",
        "bank_account/routes.py",
        "invoice/services.py",
        "payment/__init__.py",
        "signage/train_alert.py",
        "signage/rain_alert.py",
        "utils/admin_auth.py",
        "utils/logs.py",
    )
    for relative in paths:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert (
            "post_discord_notification" in source
            or "dispatch_discord_notification" in source
            or "mirror_discord_payload_best_effort" in source
        ), relative


def test_feature_notification_tests_are_labeled_as_common_notifications():
    paths = (
        "discord_notifications/templates/discord_notifications/index.html",
        "etc_accounting/templates/etc_accounting/index.html",
        "shipment_tracking/template/admin/shipment_tracking/detail.html",
        "signage/templates/signage/train_alert_settings.html",
        "records/templates/records/uber/list.html",
    )
    for relative in paths:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "共通通知テスト" in source, relative


def test_linux_sender_has_durable_retry_queue():
    source = SENDER.read_text(encoding="utf-8")
    assert "sqlite3" in source
    assert "pending_notifications" in source
    assert 'parser.add_argument("--drain"' in source


def test_raspberry_pi_bridge_preserves_discord_payloads_and_retries():
    source = BRIDGE.read_text(encoding="utf-8")
    assert "ThreadingHTTPServer" in source
    assert 'removeprefix("/discord/")' in source
    assert "pending_discord_payloads" in source
    assert "bridge.drain()" in source
    assert "os.chmod(path, 0o600)" in source


def test_raspberry_pi_bridge_converts_discord_text_attachments_to_cards():
    spec = importlib.util.spec_from_file_location("mfu_notification_bridge", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    boundary = "----mfu-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload_json"\r\n'
        "Content-Type: application/json\r\n\r\n"
        '{"content":"Debian自動更新の詳細"}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files[0]"; filename="updates.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "host-a: 3 packages updated\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    payloads = module.parse_discord_payloads(f"multipart/form-data; boundary={boundary}", body)
    assert payloads[0]["content"] == "Debian自動更新の詳細"
    assert payloads[0]["embeds"][0]["title"] == "📎 updates.txt"
    assert "3 packages updated" in payloads[0]["embeds"][0]["description"]


def test_web_notifications_follow_windows_card_layout():
    source = (ROOT / "external_login_user" / "template" / "notifications.html").read_text(encoding="utf-8")
    assert "const hasCards = Array.isArray(content.cards)" in source
    assert "${hasCards ? '' :" in source
    assert '<div class="notification-actions"><button' in source
    assert ".notification-field { background:#fff; border:1px solid #e4e9f1;" in source
    assert ".notification-field:not(.is-inline) { grid-column:1/-1; }" in source
    assert "white-space:pre-wrap; overflow-wrap:anywhere;" in source


def test_raspberry_pi_notification_features_are_registered():
    repository = (ROOT / "discord_notifications" / "repository.py").read_text(encoding="utf-8")
    for feature in (
        "earthquake_early_warning", "ichihara_disaster_radio", "chiba_police_incidents",
        "mail_summary", "mail_spam_report", "debian_updates", "backup_status",
    ):
        assert feature in repository


def test_raspberry_pi_migration_covers_all_active_discord_producers():
    source = RPI_MIGRATION.read_text(encoding="utf-8")
    for feature in (
        "earthquake_early_warning", "ichihara_disaster_radio", "chiba_police_incidents",
        "mail_summary", "mail_spam_report", "debian_updates", "backup_status",
    ):
        assert f'"{feature}"' in source
    assert '127.0.0.1' in source
    assert 'parsed.port == 8765' in source


def test_freepbx_migration_separates_calls_and_voicemail():
    source = FREEPBX_MIGRATION.read_text(encoding="utf-8")
    assert "/discord/freepbx_calls" in source
    assert "/discord/voicemail_transcription" in source
    assert "MFU_VOICEMAIL_NOTIFICATION_WEBHOOK_URL" in source


def test_discord_ingress_preserves_cards_lead_text_and_link_buttons():
    source = SERVICE.read_text(encoding="utf-8")
    assert 'cards=event.get("cards")' in source
    assert 'lead_text=str(event.get("lead_text")' in source
    assert 'payload.get("components")' in source
    assert 'params={"with_components": "true"}' in source


def test_content_only_mail_summary_is_not_duplicated_across_title_body_and_lead():
    from app.utils.notification_service import discord_payload_to_event

    summary = "差出人: sender@example.com\n件名: ご利用のお知らせ\n------\n800円の利用がありました。"
    event = discord_payload_to_event("mail_summary", {"content": summary})

    assert event["title"] == "メール要約"
    assert event["description"] == summary
    assert event["lead_text"] == ""


def test_embed_content_remains_a_lead_without_replacing_the_card_title():
    from app.utils.notification_service import discord_payload_to_event

    event = discord_payload_to_event(
        "etc_accounting",
        {
            "content": "ETC定期取得のテスト通知です",
            "embeds": [{"title": "ETC通知カード", "description": "2件取得"}],
        },
    )

    assert event["title"] == "ETC通知カード"
    assert event["description"] == "2件取得"
    assert event["lead_text"] == "ETC定期取得のテスト通知です"
