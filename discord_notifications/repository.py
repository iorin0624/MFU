from __future__ import annotations

from collections import OrderedDict
from typing import Any

from app.utils.db import get_db


FEATURE_DEFINITIONS = OrderedDict(
    [
        ("upload_complete", {"label": "ファイルアップロード", "description": "アップロード完了、画像枚数、閲覧URL"}),
        ("upload_expiry", {"label": "アップロード期限管理", "description": "有効期限前日の案内"}),
        ("layer_reply", {"label": "加工済み写真の折り返し", "description": "加工済み写真、枚数、コメント、詳細URL"}),
        ("etc_accounting", {"label": "ETC利用証明書", "description": "新規明細、料金確定、明細削除、入出IC・時刻・走行時間"}),
        ("train_status", {"label": "鉄道運行情報", "description": "運転見合わせ、運転再開、列車遅延、運転状況、掲載終了"}),
        ("rain_alert", {"label": "雨雲解析", "description": "雨雲接近、雨の終了、解析画像"}),
        ("shipment_tracking", {"label": "配送追跡", "description": "荷物の進捗変更、配達完了"}),
        ("square_payment", {"label": "Square決済", "description": "イベント決済完了、投げ銭、Square同期異常・要確認"}),
        ("invoice_payment", {"label": "請求書決済", "description": "請求書のSquare決済完了"}),
        ("event_management", {"label": "イベント管理", "description": "カード・PayPay・銀行振込等の決済、QR受付"}),
        ("admin_login", {"label": "管理者ログイン", "description": "認証方法、日時、IPアドレス、端末情報"}),
        ("suspicious_access", {"label": "不審アクセス監視", "description": "国外IPなどからの短時間404連続アクセス"}),
        ("paypay_payout_expiry", {"label": "PayPay受取リンク管理", "description": "受取リンクが長期間未処理の場合の警告"}),
        ("instagram_login", {"label": "Instagramログイン", "description": "取得時のログイン切れ・OTP再認証要求"}),
    ]
)


def _current_admin_webhook(cur) -> str:
    cur.execute(
        "SELECT webhook_url FROM users WHERE username=%s AND webhook_url IS NOT NULL AND webhook_url<>'' LIMIT 1",
        ("admin",),
    )
    row = cur.fetchone() or {}
    if isinstance(row, dict):
        return str(row.get("webhook_url") or "").strip()
    return str(row[0] or "").strip() if row else ""


def ensure_discord_notification_schema() -> None:
    """Create and seed all feature rows with the webhook currently in use."""
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_discord_notification_settings (
                feature_key VARCHAR(64) NOT NULL PRIMARY KEY,
                enabled TINYINT(1) NOT NULL DEFAULT 1,
                webhook_url VARCHAR(1000) NOT NULL DEFAULT '',
                last_sent_at DATETIME NULL,
                last_status VARCHAR(32) NOT NULL DEFAULT '',
                last_error VARCHAR(500) NOT NULL DEFAULT '',
                updated_by VARCHAR(128) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        legacy_webhook = _current_admin_webhook(cur)
        for feature_key in FEATURE_DEFINITIONS:
            cur.execute(
                """
                INSERT IGNORE INTO mfu_discord_notification_settings
                    (feature_key, enabled, webhook_url, updated_by)
                VALUES (%s, 1, %s, 'initial_migration')
                """,
                (feature_key, legacy_webhook),
            )
        db.commit()
    finally:
        db.close()


def ensure_discord_notification_nav_item() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/admin/discord-notifications",))
        if cur.fetchone():
            return
        cur.execute(
            """
            SELECT id FROM mfu_nav_items
             WHERE parent_id IS NULL AND (label LIKE %s OR label LIKE %s)
             ORDER BY id LIMIT 1
            """,
            ("%システム系%", "%通知%"),
        )
        parent = cur.fetchone()
        parent_id = int(parent["id"]) if parent else None
        cur.execute(
            "SELECT COALESCE(MAX(order_no), 0) AS max_order FROM mfu_nav_items WHERE parent_id <=> %s",
            (parent_id,),
        )
        order_no = int((cur.fetchone() or {}).get("max_order") or 0) + 10
        cur.execute(
            """
            INSERT INTO mfu_nav_items
                (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
            VALUES (%s, %s, %s, %s, 1, NULL, 0, 0)
            """,
            (parent_id, "Discord通知設定", "/admin/discord-notifications", order_no),
        )
        db.commit()
    finally:
        db.close()


def list_discord_settings() -> list[dict[str, Any]]:
    ensure_discord_notification_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM mfu_discord_notification_settings")
        rows = {str(row["feature_key"]): row for row in (cur.fetchall() or [])}
    finally:
        db.close()
    output = []
    for key, definition in FEATURE_DEFINITIONS.items():
        row = dict(rows.get(key) or {})
        row.update(feature_key=key, **definition)
        output.append(row)
    return output


def get_discord_setting(feature_key: str) -> dict[str, Any] | None:
    if feature_key not in FEATURE_DEFINITIONS:
        return None
    ensure_discord_notification_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM mfu_discord_notification_settings WHERE feature_key=%s LIMIT 1",
            (feature_key,),
        )
        return cur.fetchone()
    finally:
        db.close()


def get_discord_webhook(feature_key: str, legacy_webhook: str | None = None) -> str:
    """Return the independent feature webhook, falling back only if DB is unavailable."""
    try:
        row = get_discord_setting(feature_key) or {}
        if not bool(row.get("enabled")):
            return ""
        return str(row.get("webhook_url") or "").strip()
    except Exception:
        return str(legacy_webhook or "").strip()


def update_discord_setting(feature_key: str, *, enabled: bool, webhook_url: str, updated_by: str) -> None:
    if feature_key not in FEATURE_DEFINITIONS:
        raise KeyError(feature_key)
    ensure_discord_notification_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE mfu_discord_notification_settings
               SET enabled=%s, webhook_url=%s, updated_by=%s
             WHERE feature_key=%s
            """,
            (1 if enabled else 0, webhook_url.strip(), updated_by[:128], feature_key),
        )
        db.commit()
    finally:
        db.close()


def record_discord_delivery(feature_key: str, *, success: bool, error: str = "") -> None:
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE mfu_discord_notification_settings
               SET last_sent_at=NOW(), last_status=%s, last_error=%s
             WHERE feature_key=%s
            """,
            ("success" if success else "error", error[:500], feature_key),
        )
        db.commit()
        db.close()
    except Exception:
        pass
