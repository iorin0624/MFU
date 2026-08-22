from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from .repository import FEATURE_DEFINITIONS, list_discord_settings, update_discord_setting
from .service import post_discord_notification


discord_notifications_bp = Blueprint(
    "discord_notifications",
    __name__,
    url_prefix="/admin/discord-notifications",
    template_folder="templates",
)


@discord_notifications_bp.before_request
def _require_admin():
    if session.get("user") != "admin":
        abort(403)


def _valid_webhook(value: str) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    allowed_hosts = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
    return parsed.scheme == "https" and parsed.hostname in allowed_hosts and parsed.path.startswith("/api/webhooks/")


@discord_notifications_bp.get("")
@discord_notifications_bp.get("/")
def index():
    return render_template("discord_notifications/index.html", settings=list_discord_settings())


@discord_notifications_bp.post("/<feature_key>/save")
def save(feature_key: str):
    if feature_key not in FEATURE_DEFINITIONS:
        abort(404)
    webhook = (request.form.get("webhook_url") or "").strip()
    if not _valid_webhook(webhook):
        flash("Discord Webhook URLの形式が正しくありません。", "danger")
        return redirect(url_for("discord_notifications.index") + f"#{feature_key}")
    update_discord_setting(
        feature_key,
        enabled=request.form.get("enabled") == "1",
        webhook_url=webhook,
        updated_by=str(session.get("user") or "")[:128],
    )
    flash(f"{FEATURE_DEFINITIONS[feature_key]['label']}の設定を保存しました。", "success")
    return redirect(url_for("discord_notifications.index") + f"#{feature_key}")


@discord_notifications_bp.post("/<feature_key>/test")
def test(feature_key: str):
    if feature_key not in FEATURE_DEFINITIONS:
        abort(404)
    definition = FEATURE_DEFINITIONS[feature_key]
    payload = {
        "embeds": [{
            "title": "✅ Discord通知テスト",
            "color": 0x2ECC71,
            "fields": [
                {"name": "通知機能", "value": definition["label"], "inline": False},
                {"name": "送信日時", "value": datetime.now().strftime("%Y年%m月%d日 %H:%M:%S"), "inline": False},
            ],
            "footer": {"text": "この通知先は正常に利用できます。"},
        }],
        "allowed_mentions": {"parse": []},
    }
    try:
        if not post_discord_notification(feature_key, payload):
            raise RuntimeError("通知が無効、またはWebhook URLが未設定です。")
        flash(f"{definition['label']}のテスト通知を送信しました。", "success")
    except Exception as exc:
        flash(f"テスト通知に失敗しました: {exc}", "danger")
    return redirect(url_for("discord_notifications.index") + f"#{feature_key}")
