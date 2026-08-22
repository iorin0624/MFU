from __future__ import annotations

import json
from typing import Any

from app.utils.db import get_db


def ensure_mail_filter_schema() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_mail_filter_state (
                mailbox VARCHAR(255) NOT NULL PRIMARY KEY,
                remote_hash CHAR(64) NOT NULL DEFAULT '',
                latest_version_id BIGINT NULL,
                last_synced_at DATETIME NULL,
                last_deployed_at DATETIME NULL,
                last_result VARCHAR(32) NOT NULL DEFAULT '',
                message VARCHAR(500) NOT NULL DEFAULT '',
                updated_by VARCHAR(128) NOT NULL DEFAULT ''
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_mail_filter_versions (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                mailbox VARCHAR(255) NOT NULL,
                rules_json LONGTEXT NOT NULL,
                script_text MEDIUMTEXT NOT NULL,
                script_hash CHAR(64) NOT NULL,
                remote_hash_before CHAR(64) NOT NULL DEFAULT '',
                source VARCHAR(32) NOT NULL DEFAULT 'mfu',
                result VARCHAR(32) NOT NULL DEFAULT 'ok',
                message VARCHAR(500) NOT NULL DEFAULT '',
                deployed_by VARCHAR(128) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_mail_filter_versions_mailbox_id (mailbox, id),
                INDEX idx_mail_filter_versions_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mfu_mail_filter_manual_runs (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                mailbox VARCHAR(255) NOT NULL,
                rule_name VARCHAR(200) NOT NULL,
                source_folder VARCHAR(255) NOT NULL,
                date_from DATE NOT NULL,
                date_to DATE NOT NULL,
                unread_only TINYINT(1) NOT NULL DEFAULT 0,
                run_type VARCHAR(16) NOT NULL,
                scanned_count INT NOT NULL DEFAULT 0,
                matched_count INT NOT NULL DEFAULT 0,
                executed_count INT NOT NULL DEFAULT 0,
                result VARCHAR(32) NOT NULL DEFAULT '',
                message VARCHAR(500) NOT NULL DEFAULT '',
                executed_by VARCHAR(128) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_mail_filter_manual_mailbox_created (mailbox, created_at),
                INDEX idx_mail_filter_manual_result_created (result, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    finally:
        db.close()


def record_manual_run(
    *,
    mailbox: str,
    rule_name: str,
    scope: dict[str, Any],
    run_type: str,
    scanned_count: int = 0,
    matched_count: int = 0,
    executed_count: int = 0,
    result: str,
    message: str = "",
    executed_by: str = "",
) -> int:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO mfu_mail_filter_manual_runs
                (mailbox, rule_name, source_folder, date_from, date_to, unread_only,
                 run_type, scanned_count, matched_count, executed_count, result,
                 message, executed_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                mailbox[:255], rule_name[:200], str(scope.get("source_folder") or "INBOX")[:255],
                scope.get("date_from"), scope.get("date_to"), 1 if scope.get("unread_only") else 0,
                run_type[:16], int(scanned_count), int(matched_count), int(executed_count),
                result[:32], message[:500], executed_by[:128],
            ),
        )
        run_id = int(cur.lastrowid)
        db.commit()
        return run_id
    finally:
        db.close()


def ensure_mail_filter_nav_item() -> None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1",
            ("/admin/mail-filters",),
        )
        if cur.fetchone():
            return
        cur.execute(
            """
            SELECT id
              FROM mfu_nav_items
             WHERE parent_id IS NULL
               AND (label LIKE %s OR label LIKE %s)
             ORDER BY id
             LIMIT 1
            """,
            ("%システム系%", "%メール%"),
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
            (parent_id, "メールフィルター", "/admin/mail-filters", order_no),
        )
        db.commit()
    finally:
        db.close()


def record_version(
    *,
    mailbox: str,
    document: dict[str, Any],
    script_text: str,
    script_hash: str,
    remote_hash_before: str,
    source: str,
    result: str,
    message: str,
    deployed_by: str,
) -> int:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO mfu_mail_filter_versions
                (mailbox, rules_json, script_text, script_hash, remote_hash_before,
                 source, result, message, deployed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                mailbox,
                json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                script_text,
                script_hash,
                remote_hash_before,
                source,
                result,
                message[:500],
                deployed_by[:128],
            ),
        )
        version_id = int(cur.lastrowid)
        cur.execute(
            """
            INSERT INTO mfu_mail_filter_state
                (mailbox, remote_hash, latest_version_id, last_synced_at, last_deployed_at,
                 last_result, message, updated_by)
            VALUES (%s, %s, %s, NOW(), NOW(), %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                remote_hash=VALUES(remote_hash),
                latest_version_id=VALUES(latest_version_id),
                last_synced_at=NOW(),
                last_deployed_at=NOW(),
                last_result=VALUES(last_result),
                message=VALUES(message),
                updated_by=VALUES(updated_by)
            """,
            (mailbox, script_hash, version_id, result, message[:500], deployed_by[:128]),
        )
        db.commit()
        return version_id
    finally:
        db.close()


def update_sync_state(
    *,
    mailbox: str,
    remote_hash: str,
    result: str = "synced",
    message: str = "",
    updated_by: str = "",
) -> None:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO mfu_mail_filter_state
                (mailbox, remote_hash, last_synced_at, last_result, message, updated_by)
            VALUES (%s, %s, NOW(), %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                remote_hash=VALUES(remote_hash),
                last_synced_at=NOW(),
                last_result=VALUES(last_result),
                message=VALUES(message),
                updated_by=VALUES(updated_by)
            """,
            (mailbox, remote_hash, result, message[:500], updated_by[:128]),
        )
        db.commit()
    finally:
        db.close()


def latest_version(mailbox: str) -> dict[str, Any] | None:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT *
              FROM mfu_mail_filter_versions
             WHERE mailbox=%s AND result='ok'
             ORDER BY id DESC
             LIMIT 1
            """,
            (mailbox,),
        )
        row = cur.fetchone()
    finally:
        db.close()
    if row:
        try:
            row["document"] = json.loads(row.get("rules_json") or "{}")
        except Exception:
            row["document"] = None
    return row


def get_version(version_id: int) -> dict[str, Any] | None:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM mfu_mail_filter_versions WHERE id=%s LIMIT 1",
            (version_id,),
        )
        row = cur.fetchone()
    finally:
        db.close()
    if row:
        try:
            row["document"] = json.loads(row.get("rules_json") or "{}")
        except Exception:
            row["document"] = None
    return row


def list_versions(mailbox: str, *, limit: int = 30) -> list[dict[str, Any]]:
    ensure_mail_filter_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, mailbox, script_hash, remote_hash_before, source, result,
                   message, deployed_by, created_at
              FROM mfu_mail_filter_versions
             WHERE mailbox=%s
             ORDER BY id DESC
             LIMIT %s
            """,
            (mailbox, max(1, min(int(limit), 100))),
        )
        return cur.fetchall() or []
    finally:
        db.close()
