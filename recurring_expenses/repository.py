from __future__ import annotations

import calendar
from datetime import date, datetime
from threading import Lock

from app.utils.db import get_db


_SCHEMA_LOCK = Lock()
_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_expense_masters (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(191) NOT NULL,
                    link_url VARCHAR(2048) NULL,
                    freee_memo VARCHAR(255) NULL,
                    allow_skip TINYINT(1) NOT NULL DEFAULT 0,
                    amount_mode VARCHAR(16) NOT NULL DEFAULT 'variable',
                    default_amount INT NULL,
                    due_day TINYINT UNSIGNED NOT NULL DEFAULT 1,
                    frequency_months TINYINT UNSIGNED NOT NULL DEFAULT 1,
                    start_month CHAR(7) NOT NULL,
                    end_month CHAR(7) NULL,
                    account_item_id BIGINT NOT NULL,
                    item_id BIGINT NULL,
                    partner_id BIGINT NULL,
                    tax_code INT NOT NULL,
                    payment_mode VARCHAR(16) NOT NULL DEFAULT 'settled',
                    walletable_type VARCHAR(32) NULL,
                    walletable_id BIGINT NULL,
                    registration_mode VARCHAR(16) NOT NULL DEFAULT 'create',
                    receipt_required TINYINT(1) NOT NULL DEFAULT 0,
                    notes TEXT NULL,
                    order_no INT NOT NULL DEFAULT 0,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX ix_recurring_expense_master_active (is_active, order_no)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_expense_months (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    master_id BIGINT NOT NULL,
                    target_month CHAR(7) NOT NULL,
                    issue_date DATE NOT NULL,
                    actual_amount INT NULL,
                    freee_memo VARCHAR(255) NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    existing_deal_id BIGINT NULL,
                    freee_deal_id BIGINT NULL,
                    freee_synced_amount INT NULL,
                    freee_synced_issue_date DATE NULL,
                    freee_synced_memo VARCHAR(255) NULL,
                    freee_error TEXT NULL,
                    registered_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_recurring_expense_month (master_id, target_month),
                    INDEX ix_recurring_expense_month_status (target_month, status),
                    CONSTRAINT fk_recurring_expense_month_master
                      FOREIGN KEY (master_id) REFERENCES recurring_expense_masters(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute("SHOW COLUMNS FROM recurring_expense_masters LIKE 'link_url'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE recurring_expense_masters "
                    "ADD COLUMN link_url VARCHAR(2048) NULL AFTER name"
                )
            for column_name, definition in (
                ("freee_memo", "VARCHAR(255) NULL AFTER link_url"),
                ("allow_skip", "TINYINT(1) NOT NULL DEFAULT 0 AFTER freee_memo"),
            ):
                cur.execute(f"SHOW COLUMNS FROM recurring_expense_masters LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE recurring_expense_masters ADD COLUMN {column_name} {definition}"
                    )
            for column_name, definition in (
                ("freee_synced_amount", "INT NULL AFTER freee_deal_id"),
                ("freee_synced_issue_date", "DATE NULL AFTER freee_synced_amount"),
                ("freee_memo", "VARCHAR(255) NULL AFTER actual_amount"),
                ("freee_synced_memo", "VARCHAR(255) NULL AFTER freee_synced_issue_date"),
            ):
                cur.execute(f"SHOW COLUMNS FROM recurring_expense_months LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE recurring_expense_months ADD COLUMN {column_name} {definition}")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_expense_attachments (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    month_id BIGINT NOT NULL,
                    original_name VARCHAR(255) NOT NULL,
                    stored_name VARCHAR(255) NOT NULL,
                    file_path TEXT NOT NULL,
                    mime_type VARCHAR(96) NOT NULL,
                    file_size BIGINT NOT NULL,
                    sha256 CHAR(64) NOT NULL,
                    freee_receipt_id BIGINT NULL,
                    freee_error TEXT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_recurring_expense_attachment (month_id, sha256),
                    INDEX ix_recurring_expense_attachment_month (month_id, id),
                    CONSTRAINT fk_recurring_expense_attachment_month
                      FOREIGN KEY (month_id) REFERENCES recurring_expense_months(id)
                      ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
            _SCHEMA_READY = True
        finally:
            db.close()


def _month_index(value: str) -> int:
    year, month = (int(part) for part in value.split("-", 1))
    return year * 12 + month - 1


def _is_due(master: dict, target_month: str) -> bool:
    if target_month < master["start_month"]:
        return False
    if master.get("end_month") and target_month > master["end_month"]:
        return False
    return (_month_index(target_month) - _month_index(master["start_month"])) % int(master["frequency_months"]) == 0


def ensure_month(target_month: str) -> None:
    ensure_schema()
    year, month = (int(part) for part in target_month.split("-", 1))
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET status=CASE WHEN freee_deal_id IS NULL THEN 'error' ELSE 'pending_update' END,
                freee_error='前回のfreee登録処理が中断されました。再実行してください。',
                updated_at=%s
            WHERE status='registering' AND updated_at < DATE_SUB(%s, INTERVAL 30 MINUTE)
            """,
            (datetime.now(), datetime.now()),
        )
        cur.execute("SELECT * FROM recurring_expense_masters WHERE is_active=1")
        masters = cur.fetchall()
        now = datetime.now()
        for master in masters:
            if not _is_due(master, target_month):
                continue
            day = min(max(int(master["due_day"]), 1), calendar.monthrange(year, month)[1])
            amount = int(master["default_amount"]) if master["amount_mode"] == "fixed" and master.get("default_amount") is not None else None
            cur.execute(
                """
                INSERT IGNORE INTO recurring_expense_months
                    (master_id, target_month, issue_date, actual_amount, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                """,
                (master["id"], target_month, date(year, month, day), amount, now, now),
            )
        db.commit()
    finally:
        db.close()


def list_masters(*, include_inactive: bool = True) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        where = "" if include_inactive else "WHERE is_active=1"
        cur.execute(f"SELECT * FROM recurring_expense_masters {where} ORDER BY order_no, id")
        return cur.fetchall()
    finally:
        db.close()


def get_master(master_id: int) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM recurring_expense_masters WHERE id=%s", (master_id,))
        return cur.fetchone()
    finally:
        db.close()


def save_master(values: dict, master_id: int | None = None) -> int:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        now = datetime.now()
        columns = (
            "name", "link_url", "allow_skip", "amount_mode", "default_amount", "due_day", "frequency_months", "start_month",
            "end_month", "account_item_id", "item_id", "partner_id", "tax_code", "payment_mode",
            "walletable_type", "walletable_id", "registration_mode", "receipt_required", "notes",
            "order_no", "is_active",
        )
        params = tuple(values.get(column) for column in columns)
        if master_id:
            assignments = ", ".join(f"{column}=%s" for column in columns)
            cur.execute(
                f"UPDATE recurring_expense_masters SET {assignments}, updated_at=%s WHERE id=%s",
                (*params, now, master_id),
            )
            # Month rows are generated ahead of time.  When the start month is
            # moved forward, remove only untouched rows; registered/manual rows
            # and rows with attachments remain in the audit trail but are hidden
            # by list_month_items once they are outside the current schedule.
            cur.execute(
                """
                DELETE m FROM recurring_expense_months m
                LEFT JOIN recurring_expense_attachments a ON a.month_id=m.id
                WHERE m.master_id=%s AND m.target_month < %s
                  AND m.freee_deal_id IS NULL
                  AND m.status NOT IN ('registered','manual','registering')
                  AND a.id IS NULL
                """,
                (master_id, values["start_month"]),
            )
            if values.get("end_month"):
                cur.execute(
                    """
                    DELETE m FROM recurring_expense_months m
                    LEFT JOIN recurring_expense_attachments a ON a.month_id=m.id
                    WHERE m.master_id=%s AND m.target_month > %s
                      AND m.freee_deal_id IS NULL
                      AND m.status NOT IN ('registered','manual','registering')
                      AND a.id IS NULL
                    """,
                    (master_id, values["end_month"]),
                )
            if not values.get("allow_skip"):
                cur.execute(
                    """
                    UPDATE recurring_expense_months
                    SET status='pending', updated_at=%s
                    WHERE master_id=%s AND status='excluded' AND freee_deal_id IS NULL
                    """,
                    (now, master_id),
                )
            result = master_id
        else:
            placeholders = ", ".join(["%s"] * len(columns))
            cur.execute(
                f"INSERT INTO recurring_expense_masters ({', '.join(columns)}, created_at, updated_at) VALUES ({placeholders}, %s, %s)",
                (*params, now, now),
            )
            result = int(cur.lastrowid)
        db.commit()
        return result
    finally:
        db.close()


def set_master_active(master_id: int, active: bool) -> None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE recurring_expense_masters SET is_active=%s, updated_at=%s WHERE id=%s",
            (1 if active else 0, datetime.now(), master_id),
        )
        db.commit()
    finally:
        db.close()


def list_month_items(target_month: str) -> list[dict]:
    ensure_month(target_month)
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT m.*, x.name, x.link_url, x.allow_skip,
                   x.amount_mode, x.default_amount, x.receipt_required,
                   x.account_item_id, x.item_id, x.partner_id, x.tax_code,
                   x.payment_mode, x.walletable_type, x.walletable_id,
                   x.registration_mode, x.notes, x.start_month, x.end_month,
                   x.frequency_months,
                   COUNT(a.id) AS attachment_count,
                   SUM(CASE WHEN a.freee_receipt_id IS NOT NULL THEN 1 ELSE 0 END) AS uploaded_attachment_count
            FROM recurring_expense_months m
            JOIN recurring_expense_masters x ON x.id=m.master_id
            LEFT JOIN recurring_expense_attachments a ON a.month_id=m.id
            WHERE m.target_month=%s
            GROUP BY m.id
            ORDER BY x.order_no, x.id
            """,
            (target_month,),
        )
        return [row for row in cur.fetchall() if _is_due(row, target_month)]
    finally:
        db.close()


def get_month_item(month_id: int) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT m.*, x.name, x.allow_skip, x.receipt_required,
                   x.account_item_id, x.item_id, x.partner_id,
                   x.tax_code, x.payment_mode, x.walletable_type, x.walletable_id,
                   x.registration_mode, x.notes
            FROM recurring_expense_months m
            JOIN recurring_expense_masters x ON x.id=m.master_id
            WHERE m.id=%s
            """,
            (month_id,),
        )
        return cur.fetchone()
    finally:
        db.close()


def update_month_item(
    month_id: int,
    *,
    issue_date: date,
    actual_amount: int | None,
    freee_memo: str | None,
    status: str,
    existing_deal_id: int | None,
) -> None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET issue_date=%s, actual_amount=%s, freee_memo=%s,
                status=CASE
                    WHEN freee_deal_id IS NOT NULL
                         AND (NOT (freee_synced_amount <=> %s)
                              OR NOT (freee_synced_issue_date <=> %s)
                              OR NOT (freee_synced_memo <=> %s))
                      THEN 'pending_update'
                    WHEN freee_deal_id IS NOT NULL THEN 'registered'
                    ELSE %s
                END,
                existing_deal_id=%s,
                freee_error=NULL, updated_at=%s
            WHERE id=%s
            """,
            (
                issue_date, actual_amount, freee_memo,
                actual_amount, issue_date, freee_memo,
                status, existing_deal_id, datetime.now(), month_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def set_registration(month_id: int, *, status: str, deal_id: int | None = None, error: str | None = None) -> None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET status=%s, freee_deal_id=COALESCE(%s, freee_deal_id), freee_error=%s,
                freee_synced_amount=CASE WHEN %s='registered' THEN actual_amount ELSE freee_synced_amount END,
                freee_synced_issue_date=CASE WHEN %s='registered' THEN issue_date ELSE freee_synced_issue_date END,
                freee_synced_memo=CASE WHEN %s='registered' THEN freee_memo ELSE freee_synced_memo END,
                registered_at=CASE WHEN %s IN ('registered','manual') THEN %s ELSE registered_at END,
                updated_at=%s
            WHERE id=%s
            """,
            (status, deal_id, error, status, status, status, status, datetime.now(), datetime.now(), month_id),
        )
        db.commit()
    finally:
        db.close()


def clear_registration(month_id: int) -> None:
    """Return a deleted freee registration to the local pending state."""
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET status='pending', freee_deal_id=NULL,
                freee_synced_amount=NULL, freee_synced_issue_date=NULL,
                freee_synced_memo=NULL,
                freee_error=NULL, registered_at=NULL, updated_at=%s
            WHERE id=%s
            """,
            (datetime.now(), month_id),
        )
        db.commit()
    finally:
        db.close()


def claim_registration(month_id: int) -> bool:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET status='registering', freee_error=NULL, updated_at=%s
            WHERE id=%s
              AND (freee_deal_id IS NULL OR status='pending_update')
              AND status NOT IN ('registering','excluded','manual')
            """,
            (datetime.now(), month_id),
        )
        claimed = cur.rowcount == 1
        db.commit()
        return claimed
    finally:
        db.close()


def list_attachments(month_id: int) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM recurring_expense_attachments WHERE month_id=%s ORDER BY id", (month_id,))
        return cur.fetchall()
    finally:
        db.close()


def get_attachment(attachment_id: int) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM recurring_expense_attachments WHERE id=%s", (attachment_id,))
        return cur.fetchone()
    finally:
        db.close()


def add_attachment(month_id: int, values: dict) -> int:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        now = datetime.now()
        cur.execute(
            """
            INSERT INTO recurring_expense_attachments
                (month_id, original_name, stored_name, file_path, mime_type, file_size, sha256, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (month_id, values["original_name"], values["stored_name"], values["file_path"], values["mime_type"], values["file_size"], values["sha256"], now, now),
        )
        attachment_id = int(cur.lastrowid)
        cur.execute(
            """
            UPDATE recurring_expense_months
            SET status=CASE WHEN status='registered' THEN 'pending_update' ELSE status END,
                updated_at=%s
            WHERE id=%s
            """,
            (now, month_id),
        )
        db.commit()
        return attachment_id
    finally:
        db.close()


def update_attachment_freee(attachment_id: int, receipt_id: int | None, error: str | None = None) -> None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE recurring_expense_attachments SET freee_receipt_id=%s, freee_error=%s, updated_at=%s WHERE id=%s",
            (receipt_id, error, datetime.now(), attachment_id),
        )
        db.commit()
    finally:
        db.close()


def delete_attachment(attachment_id: int) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT a.*, m.freee_deal_id
            FROM recurring_expense_attachments a
            JOIN recurring_expense_months m ON m.id=a.month_id
            WHERE a.id=%s FOR UPDATE
            """,
            (attachment_id,),
        )
        row = cur.fetchone()
        if row and (not row.get("freee_receipt_id") or not row.get("freee_deal_id")):
            cur.execute("DELETE FROM recurring_expense_attachments WHERE id=%s", (attachment_id,))
            db.commit()
            return row
        db.rollback()
        return None
    finally:
        db.close()


def delete_month_attachments(month_id: int) -> list[dict]:
    """Delete MFU-side attachments after the linked freee deal is gone."""
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT freee_deal_id FROM recurring_expense_months WHERE id=%s FOR UPDATE",
            (month_id,),
        )
        month = cur.fetchone()
        if not month or month.get("freee_deal_id"):
            db.rollback()
            return []
        cur.execute(
            "SELECT * FROM recurring_expense_attachments WHERE month_id=%s FOR UPDATE",
            (month_id,),
        )
        rows = cur.fetchall()
        if rows:
            cur.execute(
                "DELETE FROM recurring_expense_attachments WHERE month_id=%s",
                (month_id,),
            )
        db.commit()
        return rows
    finally:
        db.close()


def ensure_nav_item() -> None:
    from app.utils.feature_access import ensure_feature_access_schema

    ensure_feature_access_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO mfu_features (feature_key, label, description, is_enabled_global)
            VALUES (%s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE feature_key=feature_key
            """,
            ("recurring_expenses_admin", "定期経費", "毎月発生する経費とfreee登録の管理"),
        )
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/recurring-expenses/",))
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (NULL, %s, %s, 58, 1, %s, 0, 0)
                """,
                ("定期経費", "/recurring-expenses/", "recurring_expenses_admin"),
            )
        db.commit()
    finally:
        db.close()
