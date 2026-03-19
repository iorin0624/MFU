from __future__ import annotations

from app.utils.db import get_db



def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return cursor.fetchone() is not None



def ensure_bank_account_schema() -> None:
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_payout_settings (
                id INT NOT NULL PRIMARY KEY,
                payout_password_hash VARCHAR(255) NULL,
                payout_password_version INT NOT NULL DEFAULT 1,
                account_holder_name VARCHAR(100) NULL,
                updated_at DATETIME NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        if not _column_exists(cursor, "mfu_payout_settings", "account_holder_name"):
            cursor.execute(
                """
                ALTER TABLE mfu_payout_settings
                ADD COLUMN account_holder_name VARCHAR(100) NULL
                AFTER payout_password_version
                """
            )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_payout_account (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                label VARCHAR(100) NULL,
                bank_name VARCHAR(100) NOT NULL,
                branch_name VARCHAR(100) NULL,
                account_number VARCHAR(32) NOT NULL,
                is_active TINYINT NOT NULL DEFAULT 1,
                sort_order INT NOT NULL DEFAULT 0,
                created_at DATETIME NULL,
                updated_at DATETIME NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_payout_paypay (
                id INT NOT NULL PRIMARY KEY,
                paypay_send_id VARCHAR(100) NULL,
                paypay_link VARCHAR(255) NULL,
                is_active TINYINT NOT NULL DEFAULT 1,
                paypay_link_saved_at DATETIME NULL,
                paypay_link_expired_notified_at DATETIME NULL,
                updated_at DATETIME NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        if not _column_exists(cursor, "mfu_payout_paypay", "paypay_link_saved_at"):
            cursor.execute(
                """
                ALTER TABLE mfu_payout_paypay
                ADD COLUMN paypay_link_saved_at DATETIME NULL
                AFTER is_active
                """
            )
        if not _column_exists(cursor, "mfu_payout_paypay", "paypay_link_expired_notified_at"):
            cursor.execute(
                """
                ALTER TABLE mfu_payout_paypay
                ADD COLUMN paypay_link_expired_notified_at DATETIME NULL
                AFTER paypay_link_saved_at
                """
            )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_payout_access_token (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                token_hash VARCHAR(255) NOT NULL,
                token_prefix VARCHAR(32) NOT NULL,
                token_suffix VARCHAR(32) NOT NULL,
                memo VARCHAR(255) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                access_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                last_accessed_at DATETIME NULL,
                last_access_ip VARCHAR(64) NULL,
                issued_via VARCHAR(32) NOT NULL DEFAULT 'admin_ui',
                issued_by_app VARCHAR(64) NULL,
                created_by_admin VARCHAR(64) NULL,
                expires_at DATETIME NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                INDEX idx_mfu_payout_access_token_active (is_active),
                INDEX idx_mfu_payout_access_token_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_payout_token_api_client (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                app_name VARCHAR(64) NOT NULL,
                api_key_hash VARCHAR(255) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                last_used_at DATETIME NULL,
                last_used_ip VARCHAR(64) NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uq_mfu_payout_token_api_client_app_name (app_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            INSERT INTO mfu_payout_settings (
                id, payout_password_hash, payout_password_version, account_holder_name, updated_at
            )
            VALUES (1, NULL, 1, NULL, NOW())
            ON DUPLICATE KEY UPDATE id = id
            """
        )
        cursor.execute(
            """
            INSERT INTO mfu_payout_paypay (
                id,
                paypay_send_id,
                paypay_link,
                is_active,
                paypay_link_saved_at,
                paypay_link_expired_notified_at,
                updated_at
            )
            VALUES (1, NULL, NULL, 1, NULL, NULL, NOW())
            ON DUPLICATE KEY UPDATE id = id
            """
        )
        db.commit()
    finally:
        cursor.close()
        db.close()
