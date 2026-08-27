from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock

from app.utils.db import get_db

from .invoice_issuers import INVOICE_ISSUERS, canonical_issuer_name


_SCHEMA_LOCK = Lock()
_SCHEMA_READY = False
LOCK_NAME = "mfu_etc_certificate_refresh"


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
                CREATE TABLE IF NOT EXISTS etc_freee_records (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    transaction_key VARCHAR(128) NOT NULL,
                    statement_month CHAR(6) NOT NULL,
                    used_at DATETIME NOT NULL,
                    entry_at DATETIME NULL,
                    exit_at DATETIME NULL,
                    entry_ic VARCHAR(191) NULL,
                    exit_ic VARCHAR(191) NULL,
                    amount INT NOT NULL,
                    vehicle_type VARCHAR(64) NULL,
                    vehicle_number VARCHAR(64) NULL,
                    card_mask VARCHAR(64) NULL,
                    redemption_amount INT NULL,
                    postpaid_amount INT NULL,
                    remarks VARCHAR(255) NULL,
                    source_json LONGTEXT NULL,
                    source_state VARCHAR(16) NOT NULL DEFAULT 'present',
                    source_missing_count INT NOT NULL DEFAULT 0,
                    source_missing_since DATETIME NULL,
                    source_deleted_at DATETIME NULL,
                    source_last_seen_at DATETIME NULL,
                    pdf_path TEXT NULL,
                    pdf_sha256 CHAR(64) NULL,
                    invoice_registration_number CHAR(14) NULL,
                    invoice_issuer_name VARCHAR(255) NULL,
                    tollgate_operator_name VARCHAR(255) NULL,
                    tollgate_road_name VARCHAR(255) NULL,
                    tollgate_matched_name VARCHAR(191) NULL,
                    tollgate_match_status VARCHAR(32) NULL,
                    tollgate_reference_updated_at DATETIME NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    freee_receipt_id BIGINT NULL,
                    freee_deal_id BIGINT NULL,
                    freee_error TEXT NULL,
                    fetched_at DATETIME NULL,
                    registered_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_etc_transaction_key (transaction_key),
                    INDEX ix_etc_month_used (statement_month, used_at),
                    INDEX ix_etc_status_used (status, used_at),
                    INDEX ix_etc_source_state_used (source_state, used_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute("SHOW COLUMNS FROM etc_freee_records LIKE 'invoice_registration_number'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE etc_freee_records ADD COLUMN invoice_registration_number CHAR(14) NULL AFTER pdf_sha256")
            cur.execute("SHOW COLUMNS FROM etc_freee_records LIKE 'invoice_issuer_name'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE etc_freee_records ADD COLUMN invoice_issuer_name VARCHAR(255) NULL AFTER invoice_registration_number")
            route_time_columns = (
                ("entry_at", "DATETIME NULL AFTER used_at"),
                ("exit_at", "DATETIME NULL AFTER entry_at"),
            )
            for column_name, column_definition in route_time_columns:
                cur.execute(f"SHOW COLUMNS FROM etc_freee_records LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE etc_freee_records ADD COLUMN {column_name} {column_definition}"
                    )
            payment_columns = (
                ("vehicle_number", "VARCHAR(64) NULL AFTER vehicle_type"),
                ("redemption_amount", "INT NULL AFTER card_mask"),
                ("postpaid_amount", "INT NULL AFTER redemption_amount"),
            )
            for column_name, column_definition in payment_columns:
                cur.execute(f"SHOW COLUMNS FROM etc_freee_records LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE etc_freee_records ADD COLUMN {column_name} {column_definition}"
                    )
            source_state_columns = (
                ("source_state", "VARCHAR(16) NOT NULL DEFAULT 'present' AFTER source_json"),
                ("source_missing_count", "INT NOT NULL DEFAULT 0 AFTER source_state"),
                ("source_missing_since", "DATETIME NULL AFTER source_missing_count"),
                ("source_deleted_at", "DATETIME NULL AFTER source_missing_since"),
                ("source_last_seen_at", "DATETIME NULL AFTER source_deleted_at"),
            )
            for column_name, column_definition in source_state_columns:
                cur.execute(f"SHOW COLUMNS FROM etc_freee_records LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE etc_freee_records ADD COLUMN {column_name} {column_definition}"
                    )
            tollgate_columns = (
                ("tollgate_operator_name", "VARCHAR(255) NULL AFTER invoice_issuer_name"),
                ("tollgate_road_name", "VARCHAR(255) NULL AFTER tollgate_operator_name"),
                ("tollgate_matched_name", "VARCHAR(191) NULL AFTER tollgate_road_name"),
                ("tollgate_match_status", "VARCHAR(32) NULL AFTER tollgate_matched_name"),
                ("tollgate_reference_updated_at", "DATETIME NULL AFTER tollgate_match_status"),
            )
            for column_name, column_definition in tollgate_columns:
                cur.execute(f"SHOW COLUMNS FROM etc_freee_records LIKE '{column_name}'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE etc_freee_records ADD COLUMN {column_name} {column_definition}"
                    )
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.statistics
                WHERE table_schema=DATABASE()
                  AND table_name='etc_freee_records'
                  AND index_name='ix_etc_operator_used'
                """
            )
            if int((cur.fetchone() or [0])[0] or 0) == 0:
                cur.execute(
                    "ALTER TABLE etc_freee_records "
                    "ADD INDEX ix_etc_operator_used (tollgate_operator_name, used_at)"
                )
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.statistics
                WHERE table_schema=DATABASE()
                  AND table_name='etc_freee_records'
                  AND index_name='ix_etc_source_state_used'
                """
            )
            if int((cur.fetchone() or [0])[0] or 0) == 0:
                cur.execute(
                    "ALTER TABLE etc_freee_records "
                    "ADD INDEX ix_etc_source_state_used (source_state, used_at)"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_registration_mappings (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    company_id BIGINT NOT NULL,
                    registration_number CHAR(14) NOT NULL,
                    partner_id BIGINT NOT NULL,
                    partner_name VARCHAR(255) NULL,
                    item_id BIGINT NOT NULL,
                    item_name VARCHAR(255) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_etc_registration_company (company_id, registration_number),
                    INDEX ix_etc_registration_number (registration_number)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_fetch_runs (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    statement_month CHAR(6) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'running',
                    found_count INT NOT NULL DEFAULT 0,
                    downloaded_count INT NOT NULL DEFAULT 0,
                    skipped_count INT NOT NULL DEFAULT 0,
                    error_text TEXT NULL,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME NULL,
                    INDEX ix_etc_fetch_started (started_at),
                    INDEX ix_etc_fetch_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_freee_batch_jobs (
                    id CHAR(32) NOT NULL PRIMARY KEY,
                    requested_by VARCHAR(191) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'queued',
                    total_count INT NOT NULL DEFAULT 0,
                    success_count INT NOT NULL DEFAULT 0,
                    failure_count INT NOT NULL DEFAULT 0,
                    skipped_count INT NOT NULL DEFAULT 0,
                    total_amount INT NOT NULL DEFAULT 0,
                    error_text TEXT NULL,
                    created_at DATETIME NOT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX ix_etc_batch_created (created_at),
                    INDEX ix_etc_batch_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_freee_batch_job_items (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    job_id CHAR(32) NOT NULL,
                    record_id BIGINT NOT NULL,
                    sequence_no INT NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'queued',
                    freee_deal_id BIGINT NULL,
                    error_text TEXT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_etc_batch_record (job_id, record_id),
                    INDEX ix_etc_batch_item_status (job_id, status),
                    INDEX ix_etc_batch_item_record (record_id),
                    CONSTRAINT fk_etc_batch_job FOREIGN KEY (job_id)
                        REFERENCES etc_freee_batch_jobs(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_record_notifications (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    record_id BIGINT NOT NULL,
                    notification_kind VARCHAR(32) NOT NULL DEFAULT 'new_record',
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    attempt_count INT NOT NULL DEFAULT 0,
                    last_error TEXT NULL,
                    created_at DATETIME NOT NULL,
                    sent_at DATETIME NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_etc_record_notification (record_id, notification_kind),
                    INDEX ix_etc_notification_status (status, updated_at),
                    INDEX ix_etc_notification_record (record_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_fetch_automation_state (
                    id TINYINT NOT NULL PRIMARY KEY,
                    last_completed_at DATETIME NULL,
                    last_status VARCHAR(32) NULL,
                    updated_at DATETIME NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_tollgate_reference (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    operator_name VARCHAR(255) NOT NULL,
                    road_name VARCHAR(255) NOT NULL,
                    tollgate_name VARCHAR(191) NOT NULL,
                    tollgate_reading VARCHAR(191) NULL,
                    normalized_name VARCHAR(191) NOT NULL,
                    source_row INT NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX ix_etc_tollgate_normalized (normalized_name),
                    INDEX ix_etc_tollgate_operator (operator_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS etc_tollgate_reference_state (
                    id TINYINT NOT NULL PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    source_sha256 CHAR(64) NULL,
                    source_etag VARCHAR(255) NULL,
                    source_last_modified VARCHAR(255) NULL,
                    row_count INT NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL,
                    error_text TEXT NULL,
                    checked_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.executemany(
                """
                UPDATE etc_freee_records
                SET invoice_issuer_name=%s
                WHERE invoice_registration_number=%s
                  AND COALESCE(invoice_issuer_name, '') <> %s
                """,
                [(name, number, name) for number, name in INVOICE_ISSUERS],
            )
            db.commit()
            _SCHEMA_READY = True
        finally:
            db.close()


def ensure_nav_item() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/etc-accounting/",))
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                INSERT INTO mfu_nav_items
                    (parent_id, label, url, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
                VALUES (NULL, %s, %s, %s, 1, NULL, 0, 0)
                """,
                ("ETC利用証明書", "/etc-accounting/", 845),
            )
        db.commit()
    finally:
        db.close()


def acquire_fetch_lock():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT GET_LOCK(%s, 0)", (LOCK_NAME,))
    row = cur.fetchone()
    if not row or int(row[0] or 0) != 1:
        db.close()
        return None
    return db


def release_fetch_lock(db) -> None:
    if db is None:
        return
    try:
        cur = db.cursor()
        cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    finally:
        db.close()


def start_run(statement_month: str) -> int:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO etc_fetch_runs (statement_month, status, started_at) VALUES (%s, 'running', %s)",
            (statement_month, datetime.now()),
        )
        run_id = int(cur.lastrowid)
        db.commit()
        return run_id
    finally:
        db.close()


def finish_run(run_id: int, *, status: str, found: int = 0, downloaded: int = 0, skipped: int = 0, error: str | None = None) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE etc_fetch_runs
            SET status=%s, found_count=%s, downloaded_count=%s, skipped_count=%s,
                error_text=%s, finished_at=%s
            WHERE id=%s
            """,
            (status, found, downloaded, skipped, (error or "")[:2000] or None, datetime.now(), run_id),
        )
        db.commit()
    finally:
        db.close()


def _remarks_are_provisional(value: object) -> bool:
    return "確認中" in str(value or "").replace(" ", "")


def _queue_record_notification(cur, record_id: int, notification_kind: str, now: datetime) -> None:
    cur.execute(
        """
        INSERT INTO etc_record_notifications (
            record_id, notification_kind, status, attempt_count,
            last_error, created_at, sent_at, updated_at
        ) VALUES (%s, %s, 'pending', 0, NULL, %s, NULL, %s)
        ON DUPLICATE KEY UPDATE
            status='pending', attempt_count=0, last_error=NULL,
            sent_at=NULL, updated_at=VALUES(updated_at)
        """,
        (int(record_id), str(notification_kind), now, now),
    )


def _find_provisional_rekey_candidate(cur, record: dict) -> dict | None:
    """Find one unregistered provisional row whose ETC-side key was replaced."""
    cur.execute(
        """
        SELECT *
        FROM etc_freee_records
        WHERE statement_month=%s
          AND used_at=%s
          AND COALESCE(entry_ic, '')=%s
          AND COALESCE(exit_ic, '')=%s
          AND COALESCE(card_mask, '')=%s
          AND COALESCE(vehicle_type, '')=%s
          AND status='pending'
          AND freee_deal_id IS NULL
          AND freee_receipt_id IS NULL
          AND remarks LIKE %s
        ORDER BY id
        LIMIT 2
        """,
        (
            record["statement_month"],
            record["used_at"],
            str(record.get("entry_ic") or ""),
            str(record.get("exit_ic") or ""),
            str(record.get("card_mask") or ""),
            str(record.get("vehicle_type") or ""),
            "%\u78ba\u8a8d\u4e2d%",
        ),
    )
    candidates = cur.fetchall()
    return candidates[0] if len(candidates) == 1 else None


def upsert_record(record: dict) -> dict:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        now = datetime.now()
        cur.execute("SELECT * FROM etc_freee_records WHERE transaction_key=%s", (record["transaction_key"],))
        previous = cur.fetchone()
        if previous is None:
            previous = _find_provisional_rekey_candidate(cur, record)
            if previous is not None:
                cur.execute(
                    "UPDATE etc_freee_records SET transaction_key=%s WHERE id=%s",
                    (record["transaction_key"], int(previous["id"])),
                )
        cur.execute(
            """
            INSERT INTO etc_freee_records (
                transaction_key, statement_month, used_at, entry_at, exit_at,
                entry_ic, exit_ic, amount,
                vehicle_type, vehicle_number, card_mask,
                redemption_amount, postpaid_amount,
                remarks, source_json, source_last_seen_at,
                status, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)
            ON DUPLICATE KEY UPDATE
                statement_month=VALUES(statement_month), used_at=VALUES(used_at),
                entry_at=VALUES(entry_at), exit_at=VALUES(exit_at),
                entry_ic=VALUES(entry_ic), exit_ic=VALUES(exit_ic), amount=VALUES(amount),
                vehicle_type=VALUES(vehicle_type), vehicle_number=VALUES(vehicle_number),
                card_mask=VALUES(card_mask),
                redemption_amount=VALUES(redemption_amount), postpaid_amount=VALUES(postpaid_amount),
                remarks=VALUES(remarks), source_json=VALUES(source_json),
                source_state='present', source_missing_count=0,
                source_missing_since=NULL, source_deleted_at=NULL,
                source_last_seen_at=VALUES(source_last_seen_at), updated_at=VALUES(updated_at)
            """,
            (
                record["transaction_key"], record["statement_month"], record["used_at"],
                record.get("entry_at"), record.get("exit_at"),
                record.get("entry_ic"), record.get("exit_ic"), int(record.get("amount") or 0),
                record.get("vehicle_type"), record.get("vehicle_number"), record.get("card_mask"),
                record.get("redemption_amount"), record.get("postpaid_amount"), record.get("remarks"),
                json.dumps(record, ensure_ascii=False, default=str), now, now, now,
            ),
        )
        cur.execute("SELECT * FROM etc_freee_records WHERE transaction_key=%s", (record["transaction_key"],))
        row = cur.fetchone()
        is_new = previous is None
        if previous:
            compared_fields = (
                "used_at", "entry_at", "exit_at", "entry_ic", "exit_ic",
                "amount", "vehicle_type", "vehicle_number", "card_mask",
                "redemption_amount", "postpaid_amount", "remarks",
            )
            row["_details_changed"] = any(previous.get(key) != row.get(key) for key in compared_fields)
            pdf_fields = (
                "used_at", "entry_at", "exit_at", "entry_ic", "exit_ic",
                "amount", "vehicle_type", "card_mask", "remarks",
            )
            row["_pdf_refresh_required"] = any(previous.get(key) != row.get(key) for key in pdf_fields)
        else:
            row["_details_changed"] = False
            row["_pdf_refresh_required"] = True
        row["_is_new"] = is_new
        row["_became_final"] = bool(
            previous
            and _remarks_are_provisional(previous.get("remarks"))
            and not _remarks_are_provisional(row.get("remarks"))
        )
        row["_restored"] = bool(previous and previous.get("source_state") != "present")
        if is_new:
            _queue_record_notification(cur, int(row["id"]), "new_record", now)
        elif row["_became_final"]:
            cur.execute(
                """
                UPDATE etc_freee_records
                SET invoice_registration_number=NULL, invoice_issuer_name=NULL
                WHERE id=%s
                """,
                (int(row["id"]),),
            )
            row["invoice_registration_number"] = None
            row["invoice_issuer_name"] = None
            _queue_record_notification(cur, int(row["id"]), "finalized", now)
        db.commit()
        return row
    finally:
        db.close()


def reconcile_source_records(
    statement_month: str,
    seen_transaction_keys: set[str],
    *,
    missing_threshold: int = 2,
) -> dict:
    """Soft-delete records absent from consecutive successful full-month fetches."""
    ensure_schema()
    threshold = max(1, int(missing_threshold))
    seen = {str(key or "").strip() for key in seen_transaction_keys if key}
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, transaction_key, source_state, source_missing_count
              FROM etc_freee_records
             WHERE statement_month=%s
            """,
            (statement_month,),
        )
        rows = cur.fetchall() or []
        missing_updates = []
        newly_deleted = 0
        newly_deleted_ids: list[int] = []
        already_deleted = 0
        for row in rows:
            if str(row.get("transaction_key") or "") in seen:
                continue
            old_count = int(row.get("source_missing_count") or 0)
            new_count = old_count + 1
            was_deleted = row.get("source_state") == "deleted"
            will_delete = was_deleted or new_count >= threshold
            if was_deleted:
                already_deleted += 1
            elif will_delete:
                newly_deleted += 1
                newly_deleted_ids.append(int(row["id"]))
            missing_updates.append(
                (
                    "deleted" if will_delete else "missing",
                    new_count,
                    now,
                    now if will_delete else None,
                    int(row["id"]),
                )
            )
        if missing_updates:
            cur.executemany(
                """
                UPDATE etc_freee_records
                   SET source_state=%s,
                       source_missing_count=%s,
                       source_missing_since=COALESCE(source_missing_since, %s),
                       source_deleted_at=COALESCE(source_deleted_at, %s)
                 WHERE id=%s
                """,
                missing_updates,
            )
        for record_id in newly_deleted_ids:
            _queue_record_notification(cur, record_id, "source_deleted", now)
        db.commit()
        return {
            "checked": len(rows),
            "present": len(rows) - len(missing_updates),
            "missing": sum(1 for update in missing_updates if update[0] == "missing"),
            "newly_deleted": newly_deleted,
            "already_deleted": already_deleted,
        }
    finally:
        db.close()


def save_pdf(
    record_id: int,
    path: Path,
    sha256: str,
    *,
    registration_number: str | None = None,
    issuer_name: str | None = None,
) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE etc_freee_records
            SET pdf_path=%s, pdf_sha256=%s,
                invoice_registration_number=%s, invoice_issuer_name=%s,
                fetched_at=%s, status=IF(status='registered', status, 'pending'),
                freee_error=NULL, updated_at=%s
            WHERE id=%s
            """,
            (
                str(path), sha256, registration_number,
                canonical_issuer_name(registration_number, issuer_name or "") or None,
                datetime.now(), datetime.now(), record_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def get_record(record_id: int) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM etc_freee_records WHERE id=%s", (record_id,))
        return cur.fetchone()
    finally:
        db.close()


def get_records_by_ids(record_ids: list[int]) -> list[dict]:
    ensure_schema()
    values = list(dict.fromkeys(int(value) for value in record_ids if int(value) > 0))
    if not values:
        return []
    if len(values) > 50:
        raise ValueError("一括登録は50件までです。")
    placeholders = ",".join(["%s"] * len(values))
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            f"SELECT * FROM etc_freee_records WHERE id IN ({placeholders})",
            tuple(values),
        )
        rows = {int(row["id"]): row for row in cur.fetchall()}
        return [rows[value] for value in values if value in rows]
    finally:
        db.close()


def update_pdf_metadata(record_id: int, registration_number: str, issuer_name: str = "") -> None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE etc_freee_records
            SET invoice_registration_number=%s, invoice_issuer_name=%s, updated_at=%s
            WHERE id=%s
            """,
            (
                registration_number,
                canonical_issuer_name(registration_number, issuer_name) or None,
                datetime.now(),
                record_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def list_registration_mappings(company_id: int | None) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT invoice_registration_number AS registration_number,
                   MAX(invoice_issuer_name) AS issuer_name,
                   COUNT(*) AS record_count,
                   SUM(CASE WHEN freee_deal_id IS NULL THEN 1 ELSE 0 END) AS unregistered_count
            FROM etc_freee_records
            WHERE invoice_registration_number IS NOT NULL
              AND invoice_registration_number <> ''
            GROUP BY invoice_registration_number
            ORDER BY invoice_registration_number
            """
        )
        observed_rows = {
            row["registration_number"]: row
            for row in cur.fetchall()
        }
        mappings = {}
        if company_id:
            cur.execute("SELECT * FROM etc_registration_mappings WHERE company_id=%s", (int(company_id),))
            mappings = {row["registration_number"]: row for row in cur.fetchall()}
        rows = []
        ordered_numbers = [number for number, _name in INVOICE_ISSUERS]
        unknown_numbers = sorted((set(observed_rows) | set(mappings)) - set(ordered_numbers))
        for registration_number in ordered_numbers + unknown_numbers:
            observed = observed_rows.get(registration_number) or {}
            row = {
                "registration_number": registration_number,
                "issuer_name": canonical_issuer_name(
                    registration_number,
                    str(observed.get("issuer_name") or ""),
                ),
                "record_count": int(observed.get("record_count") or 0),
                "unregistered_count": int(observed.get("unregistered_count") or 0),
            }
            row.update(mappings.get(registration_number, {}))
            row["configured"] = bool(row.get("partner_id") and row.get("item_id"))
            rows.append(row)
        return rows
    finally:
        db.close()


def get_registration_mapping(company_id: int, registration_number: str) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT * FROM etc_registration_mappings
            WHERE company_id=%s AND registration_number=%s
            """,
            (int(company_id), registration_number),
        )
        row = cur.fetchone()
        if not row or not row.get("partner_id") or not row.get("item_id"):
            return None
        return row
    finally:
        db.close()


def save_registration_mapping(
    *,
    company_id: int,
    registration_number: str,
    partner_id: int,
    partner_name: str,
    item_id: int,
    item_name: str,
) -> None:
    ensure_schema()
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO etc_registration_mappings (
                company_id, registration_number, partner_id, partner_name,
                item_id, item_name, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                partner_id=VALUES(partner_id), partner_name=VALUES(partner_name),
                item_id=VALUES(item_id), item_name=VALUES(item_name), updated_at=VALUES(updated_at)
            """,
            (
                int(company_id), registration_number, int(partner_id), partner_name,
                int(item_id), item_name, now, now,
            ),
        )
        db.commit()
    finally:
        db.close()


def list_records(
    status: str = "",
    limit: int | None = 500,
    *,
    date_from=None,
    date_to=None,
    operator_name: str = "",
) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        conditions = []
        params = []
        if status == "deleted":
            conditions.append("source_state='deleted'")
        elif status:
            conditions.append("source_state<>'deleted'")
        if status and status != "deleted":
            conditions.append("status=%s")
            params.append(status)
        if date_from:
            conditions.append("used_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("used_at < DATE_ADD(%s, INTERVAL 1 DAY)")
            params.append(date_to)
        if operator_name == "__unmatched__":
            conditions.append(
                "(COALESCE(tollgate_operator_name, '')='' "
                "OR COALESCE(tollgate_match_status, '')<>'matched')"
            )
        elif operator_name:
            conditions.append("tollgate_operator_name=%s")
            params.append(operator_name)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM etc_freee_records{where} ORDER BY used_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        for row in rows:
            row["invoice_issuer_name"] = canonical_issuer_name(
                row.get("invoice_registration_number"),
                str(row.get("invoice_issuer_name") or ""),
            ) or None
        return rows
    finally:
        db.close()


def list_tollgate_operators() -> list[str]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT operator_name
            FROM (
                SELECT operator_name
                FROM etc_tollgate_reference
                WHERE COALESCE(operator_name, '') <> ''
                UNION
                SELECT tollgate_operator_name AS operator_name
                FROM etc_freee_records
                WHERE COALESCE(tollgate_operator_name, '') <> ''
            ) AS operators
            ORDER BY operator_name
            """
        )
        return [str(row[0]) for row in cur.fetchall() if row and row[0]]
    finally:
        db.close()


def list_runs(limit: int = 10) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM etc_fetch_runs ORDER BY id DESC LIMIT %s", (int(limit),))
        return cur.fetchall()
    finally:
        db.close()


def record_scheduled_fetch_completed(status: str) -> None:
    ensure_schema()
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO etc_fetch_automation_state (id, last_completed_at, last_status, updated_at)
            VALUES (1, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_completed_at=VALUES(last_completed_at),
                last_status=VALUES(last_status),
                updated_at=VALUES(updated_at)
            """,
            (now, (status or "unknown")[:32], now),
        )
        db.commit()
    finally:
        db.close()


def get_scheduled_fetch_state() -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM etc_fetch_automation_state WHERE id=1")
        return cur.fetchone()
    finally:
        db.close()


def claim_pending_record_notifications(limit: int = 100) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        db.start_transaction()
        cur.execute(
            """
            SELECT notification.id
            FROM etc_record_notifications AS notification
            JOIN etc_freee_records AS record ON record.id=notification.record_id
            WHERE notification.notification_kind IN ('new_record', 'finalized', 'source_deleted')
              AND (
                    notification.status IN ('pending', 'error')
                    OR (notification.status='sending' AND notification.updated_at < DATE_SUB(NOW(), INTERVAL 30 MINUTE))
              )
              AND (
                    notification.notification_kind <> 'finalized'
                    OR (
                        record.pdf_path IS NOT NULL
                        AND record.invoice_registration_number IS NOT NULL
                        AND record.fetched_at >= notification.updated_at
                    )
              )
            ORDER BY notification.id
            LIMIT %s
            FOR UPDATE
            """,
            (min(max(int(limit), 1), 100),),
        )
        ids = [int(row["id"]) for row in cur.fetchall()]
        if not ids:
            db.commit()
            return []
        placeholders = ",".join(["%s"] * len(ids))
        now = datetime.now()
        cur.execute(
            f"""
            UPDATE etc_record_notifications
            SET status='sending', attempt_count=attempt_count+1, last_error=NULL, updated_at=%s
            WHERE id IN ({placeholders})
            """,
            (now, *ids),
        )
        cur.execute(
            f"""
            SELECT notification.id AS notification_id, notification.record_id,
                   notification.notification_kind,
                   record.used_at, record.entry_at, record.exit_at,
                   record.entry_ic, record.exit_ic, record.amount,
                   record.remarks, record.statement_month,
                   record.source_state, record.source_deleted_at
            FROM etc_record_notifications AS notification
            JOIN etc_freee_records AS record ON record.id=notification.record_id
            WHERE notification.id IN ({placeholders})
            ORDER BY record.used_at, record.id
            """,
            tuple(ids),
        )
        rows = cur.fetchall()
        db.commit()
        return rows
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def finish_record_notifications(notification_ids: list[int], *, error: str | None = None) -> None:
    ensure_schema()
    ids = list(dict.fromkeys(int(value) for value in notification_ids if int(value) > 0))
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    now = datetime.now()
    status = "error" if error else "sent"
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            f"""
            UPDATE etc_record_notifications
            SET status=%s, last_error=%s, sent_at=%s, updated_at=%s
            WHERE id IN ({placeholders})
            """,
            (
                status,
                (error or "")[:2000] or None,
                None if error else now,
                now,
                *ids,
            ),
        )
        db.commit()
    finally:
        db.close()


def create_batch_job(*, requested_by: str, record_ids: list[int], total_amount: int) -> str:
    ensure_schema()
    values = list(dict.fromkeys(int(value) for value in record_ids if int(value) > 0))
    if not values:
        raise ValueError("登録対象がありません。")
    if len(values) > 50:
        raise ValueError("一括登録は50件までです。")
    job_id = uuid.uuid4().hex
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO etc_freee_batch_jobs (
                id, requested_by, status, total_count, total_amount, created_at, updated_at
            ) VALUES (%s,%s,'queued',%s,%s,%s,%s)
            """,
            (job_id, requested_by[:191], len(values), int(total_amount), now, now),
        )
        cur.executemany(
            """
            INSERT INTO etc_freee_batch_job_items (
                job_id, record_id, sequence_no, status, created_at, updated_at
            ) VALUES (%s,%s,%s,'queued',%s,%s)
            """,
            [(job_id, record_id, index, now, now) for index, record_id in enumerate(values, 1)],
        )
        db.commit()
        return job_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def claim_batch_job(job_id: str) -> bool:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor()
        now = datetime.now()
        cur.execute(
            """
            UPDATE etc_freee_batch_jobs
            SET status='running', started_at=%s, updated_at=%s, error_text=NULL
            WHERE id=%s AND status='queued'
            """,
            (now, now, job_id),
        )
        claimed = cur.rowcount == 1
        db.commit()
        return claimed
    finally:
        db.close()


def get_batch_job(job_id: str) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM etc_freee_batch_jobs WHERE id=%s", (job_id,))
        return cur.fetchone()
    finally:
        db.close()


def list_batch_jobs(limit: int = 10) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM etc_freee_batch_jobs ORDER BY created_at DESC LIMIT %s",
            (int(limit),),
        )
        return cur.fetchall()
    finally:
        db.close()


def get_batch_items(job_id: str) -> list[dict]:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT item.*, record.used_at, record.entry_ic, record.exit_ic, record.amount,
                   record.invoice_registration_number
            FROM etc_freee_batch_job_items AS item
            JOIN etc_freee_records AS record ON record.id=item.record_id
            WHERE item.job_id=%s
            ORDER BY item.sequence_no
            """,
            (job_id,),
        )
        return cur.fetchall()
    finally:
        db.close()


def update_batch_item(
    item_id: int,
    *,
    status: str,
    deal_id: int | None = None,
    error: str | None = None,
) -> None:
    ensure_schema()
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT job_id FROM etc_freee_batch_job_items WHERE id=%s", (item_id,))
        item = cur.fetchone()
        if not item:
            return
        job_id = item["job_id"]
        started_at = now if status == "running" else None
        finished_at = now if status in {"success", "failed", "skipped"} else None
        cur.execute(
            """
            UPDATE etc_freee_batch_job_items
            SET status=%s, freee_deal_id=%s, error_text=%s,
                started_at=COALESCE(started_at, %s), finished_at=%s, updated_at=%s
            WHERE id=%s
            """,
            (status, deal_id, (error or "")[:2000] or None, started_at, finished_at, now, item_id),
        )
        cur.execute(
            """
            SELECT
                SUM(status='success') AS success_count,
                SUM(status='failed') AS failure_count,
                SUM(status='skipped') AS skipped_count
            FROM etc_freee_batch_job_items WHERE job_id=%s
            """,
            (job_id,),
        )
        counts = cur.fetchone() or {}
        cur.execute(
            """
            UPDATE etc_freee_batch_jobs
            SET success_count=%s, failure_count=%s, skipped_count=%s, updated_at=%s
            WHERE id=%s
            """,
            (
                int(counts.get("success_count") or 0),
                int(counts.get("failure_count") or 0),
                int(counts.get("skipped_count") or 0),
                now,
                job_id,
            ),
        )
        db.commit()
    finally:
        db.close()


def finish_batch_job(job_id: str, *, error: str | None = None) -> dict | None:
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                SUM(status='success') AS success_count,
                SUM(status='failed') AS failure_count,
                SUM(status='skipped') AS skipped_count,
                SUM(status IN ('queued','running')) AS pending_count
            FROM etc_freee_batch_job_items WHERE job_id=%s
            """,
            (job_id,),
        )
        counts = cur.fetchone() or {}
        success = int(counts.get("success_count") or 0)
        failure = int(counts.get("failure_count") or 0)
        skipped = int(counts.get("skipped_count") or 0)
        pending = int(counts.get("pending_count") or 0)
        if error:
            status = "failed"
        elif pending:
            status = "running"
        elif failure or skipped:
            status = "partial" if success else "failed"
        else:
            status = "completed"
        finished_at = datetime.now() if status in {"completed", "partial", "failed"} else None
        cur.execute(
            """
            UPDATE etc_freee_batch_jobs
            SET status=%s, success_count=%s, failure_count=%s, skipped_count=%s,
                error_text=%s, finished_at=%s, updated_at=%s
            WHERE id=%s
            """,
            (
                status, success, failure, skipped, (error or "")[:2000] or None,
                finished_at, datetime.now(), job_id,
            ),
        )
        db.commit()
        cur.execute("SELECT * FROM etc_freee_batch_jobs WHERE id=%s", (job_id,))
        return cur.fetchone()
    finally:
        db.close()


def update_registration(record_id: int, **fields) -> None:
    allowed = {"status", "freee_receipt_id", "freee_deal_id", "freee_error", "registered_at"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    values["updated_at"] = datetime.now()
    columns = ", ".join(f"{key}=%s" for key in values)
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(f"UPDATE etc_freee_records SET {columns} WHERE id=%s", (*values.values(), record_id))
        db.commit()
    finally:
        db.close()


def claim_registration(record_id: int) -> bool:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE etc_freee_records
            SET status='registering', freee_error=NULL, updated_at=%s
            WHERE id=%s AND status IN ('pending', 'error') AND freee_deal_id IS NULL
              AND COALESCE(remarks, '') NOT LIKE %s
            """,
            (datetime.now(), record_id, "%確認中%"),
        )
        claimed = cur.rowcount == 1
        db.commit()
        return claimed
    finally:
        db.close()


def claim_registered_update(record_id: int) -> bool:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE etc_freee_records
            SET status='updating', freee_error=NULL, updated_at=%s
            WHERE id=%s AND status='registered' AND freee_deal_id IS NOT NULL
            """,
            (datetime.now(), record_id),
        )
        claimed = cur.rowcount == 1
        db.commit()
        return claimed
    finally:
        db.close()
