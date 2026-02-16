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
                updated_at DATETIME NULL
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
            INSERT INTO mfu_payout_paypay (id, paypay_send_id, paypay_link, is_active, updated_at)
            VALUES (1, NULL, NULL, 1, NOW())
            ON DUPLICATE KEY UPDATE id = id
            """
        )
        db.commit()
    finally:
        cursor.close()
        db.close()
