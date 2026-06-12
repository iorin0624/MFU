from __future__ import annotations

from datetime import datetime

from app.utils.db import get_db


def now_ts() -> datetime:
    return datetime.now()


def ensure_freee_api_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS freee_oauth_tokens (
            id INT AUTO_INCREMENT PRIMARY KEY,
            provider VARCHAR(32) NOT NULL DEFAULT 'freee',
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uniq_provider (provider)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS freee_common_settings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            company_id BIGINT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS freee_integration_settings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            integration_key VARCHAR(64) NOT NULL,
            account_item_id BIGINT NULL,
            tax_code INT NULL,
            tax_code_8 INT NULL,
            tax_code_nontax INT NULL,
            deal_payment_mode VARCHAR(32) NOT NULL DEFAULT 'settled',
            walletable_type VARCHAR(64) NULL,
            walletable_id BIGINT NULL,
            partner_id BIGINT NULL,
            partner_code VARCHAR(191) NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uniq_freee_integration_key (integration_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    if not _column_exists(cur, "freee_integration_settings", "tax_code_8"):
        cur.execute(
            "ALTER TABLE freee_integration_settings ADD COLUMN tax_code_8 INT NULL AFTER tax_code"
        )
    if not _column_exists(cur, "freee_integration_settings", "tax_code_nontax"):
        cur.execute(
            "ALTER TABLE freee_integration_settings ADD COLUMN tax_code_nontax INT NULL AFTER tax_code_8"
        )
    _migrate_legacy_freee_settings(cur)
    db.commit()
    if close_db:
        db.close()


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        (table_name,),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row.get("count") or 0) > 0
    return int(row[0] or 0) > 0


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row.get("count") or 0) > 0
    return int(row[0] or 0) > 0


def _has_rows(cur, table_name: str) -> bool:
    cur.execute(f"SELECT COUNT(*) AS count FROM {table_name}")
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row.get("count") or 0) > 0
    return int(row[0] or 0) > 0


def _migrate_legacy_freee_settings(cur) -> None:
    if not _table_exists(cur, "freee_accounting_settings"):
        return
    cur.execute(
        """
        SELECT company_id, partner_id, partner_code, account_item_id, tax_code,
               walletable_type, walletable_id, deal_payment_mode
        FROM freee_accounting_settings
        ORDER BY id ASC
        LIMIT 1
        """
    )
    legacy = cur.fetchone()
    if not legacy:
        return
    if not isinstance(legacy, dict):
        keys = (
            "company_id",
            "partner_id",
            "partner_code",
            "account_item_id",
            "tax_code",
            "walletable_type",
            "walletable_id",
            "deal_payment_mode",
        )
        legacy = dict(zip(keys, legacy))
    now = now_ts()
    if legacy.get("company_id") and not _has_rows(cur, "freee_common_settings"):
        cur.execute(
            """
            INSERT INTO freee_common_settings (company_id, created_at, updated_at)
            VALUES (%s, %s, %s)
            """,
            (legacy.get("company_id"), now, now),
        )
    cur.execute(
        "SELECT id FROM freee_integration_settings WHERE integration_key = 'uber' LIMIT 1"
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO freee_integration_settings (
            integration_key, account_item_id, tax_code, deal_payment_mode,
            walletable_type, walletable_id, partner_id, partner_code,
            created_at, updated_at
        ) VALUES ('uber', %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            legacy.get("account_item_id"),
            legacy.get("tax_code"),
            legacy.get("deal_payment_mode") or "settled",
            legacy.get("walletable_type"),
            legacy.get("walletable_id"),
            legacy.get("partner_id"),
            legacy.get("partner_code"),
            now,
            now,
        ),
    )
