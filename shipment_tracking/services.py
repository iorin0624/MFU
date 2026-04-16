from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests
from flask import current_app
from mysql.connector import IntegrityError

from app.utils.db import get_db

from .models import CARRIER_MASTER, TRIGGERED_BY_VALUES
from .parsers import parse_japanpost, parse_sagawa, parse_yamato

REQUEST_TIMEOUT = (5, 20)
USER_AGENT = "MFU-ShipmentTracking/1.0 (+https://mfu.local)"


class ShipmentTrackingError(Exception):
    pass


def normalize_tracking_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def ensure_shipment_tracking_schema() -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shipment_tracking_target (
            id INT AUTO_INCREMENT PRIMARY KEY,
            carrier_code VARCHAR(32) NOT NULL,
            tracking_number VARCHAR(64) NOT NULL,
            label VARCHAR(255) NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            last_checked_at DATETIME NULL,
            last_check_success_at DATETIME NULL,
            last_error_text TEXT NULL,
            last_payload_json LONGTEXT NULL,
            last_current_status VARCHAR(255) NULL,
            last_current_status_detail TEXT NULL,
            last_latest_event_at DATETIME NULL,
            last_completed TINYINT(1) NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_shipment_tracking_target_carrier_number (carrier_code, tracking_number)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS shipment_tracking_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            target_id INT NOT NULL,
            triggered_by VARCHAR(16) NOT NULL,
            checked_at DATETIME NOT NULL,
            success TINYINT(1) NOT NULL,
            changed TINYINT(1) NOT NULL,
            error_text TEXT NULL,
            payload_json LONGTEXT NULL,
            current_status VARCHAR(255) NULL,
            current_status_detail TEXT NULL,
            latest_event_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_shipment_tracking_log_target_id (target_id),
            CONSTRAINT fk_shipment_tracking_log_target_id FOREIGN KEY (target_id)
                REFERENCES shipment_tracking_target(id)
                ON DELETE RESTRICT
                ON UPDATE CASCADE
        )
        """
    )
    db.commit()
    db.close()


def ensure_nav_item() -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT 1
          FROM mfu_nav_items
         WHERE url=%s
         LIMIT 1
        """,
        ("/admin/shipment-tracking",),
    )
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(
            """
            INSERT INTO mfu_nav_items
                (label, url, parent_id, order_no, is_enabled, feature_key, open_in_new_tab, is_external)
            VALUES
                (%s, %s, NULL, 0, 1, NULL, 0, 0)
            """,
            ("配送追跡", "/admin/shipment-tracking"),
        )
    db.commit()
    db.close()


def _now() -> datetime:
    return datetime.now()


def _parse_payload_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    patterns = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    for pattern in patterns:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _http_get(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _fetch_sagawa(tracking_number: str) -> tuple[str, str]:
    url = f"https://k2k.sagawa-exp.co.jp/p/web/okurijosearch.do?okurijoNo={tracking_number}"
    return url, _http_get(url)


def _fetch_yamato(tracking_number: str) -> tuple[str, str]:
    search_url = "https://toi.kuronekoyamato.co.jp/cgi-bin/tneko"
    headers = {"User-Agent": USER_AGENT}
    search_params = {"number00": "1", "number01": tracking_number, "type": "1"}
    response = requests.get(
        search_url,
        params=search_params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.url, response.text


def _fetch_japanpost(tracking_number: str) -> tuple[str, str]:
    url = (
        "https://trackings.post.japanpost.jp/services/srv/search/direct"
        f"?searchKind=S002&reqCodeNo1={tracking_number}&locale=jp"
    )
    return url, _http_get(url)


def fetch_and_parse(carrier_code: str, tracking_number: str) -> dict[str, Any]:
    if carrier_code == "sagawa":
        tracking_url, html = _fetch_sagawa(tracking_number)
        return parse_sagawa(html, tracking_number, tracking_url)
    if carrier_code == "yamato":
        tracking_url, html = _fetch_yamato(tracking_number)
        return parse_yamato(html, tracking_number, tracking_url)
    if carrier_code == "japanpost":
        tracking_url, html = _fetch_japanpost(tracking_number)
        return parse_japanpost(html, tracking_number, tracking_url)
    raise ShipmentTrackingError("未対応の配送業者です。")


def list_targets() -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT *
          FROM shipment_tracking_target
         ORDER BY id DESC
        """
    )
    rows = cur.fetchall()
    db.close()
    return rows


def get_target(target_id: int) -> dict[str, Any] | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM shipment_tracking_target WHERE id=%s", (target_id,))
    row = cur.fetchone()
    db.close()
    return row


def get_logs(target_id: int, limit: int = 20) -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT *
          FROM shipment_tracking_log
         WHERE target_id=%s
         ORDER BY id DESC
         LIMIT %s
        """,
        (target_id, limit),
    )
    rows = cur.fetchall()
    db.close()
    return rows


def create_target(carrier_code: str, tracking_number: str, label: str | None, is_active: bool) -> int:
    if carrier_code not in CARRIER_MASTER:
        raise ShipmentTrackingError("配送業者が不正です。")
    normalized = normalize_tracking_number(tracking_number)
    if not normalized:
        raise ShipmentTrackingError("配達番号を入力してください。")

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO shipment_tracking_target (carrier_code, tracking_number, label, is_active)
            VALUES (%s, %s, %s, %s)
            """,
            (carrier_code, normalized, (label or "").strip() or None, 1 if is_active else 0),
        )
        target_id = cur.lastrowid
        db.commit()
        return int(target_id)
    except IntegrityError as exc:
        db.rollback()
        raise ShipmentTrackingError("同じ業者・配達番号はすでに登録されています。") from exc
    finally:
        db.close()


def update_target(target_id: int, carrier_code: str, tracking_number: str, label: str | None, is_active: bool) -> None:
    if carrier_code not in CARRIER_MASTER:
        raise ShipmentTrackingError("配送業者が不正です。")
    normalized = normalize_tracking_number(tracking_number)
    if not normalized:
        raise ShipmentTrackingError("配達番号を入力してください。")

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE shipment_tracking_target
               SET carrier_code=%s,
                   tracking_number=%s,
                   label=%s,
                   is_active=%s
             WHERE id=%s
            """,
            (carrier_code, normalized, (label or "").strip() or None, 1 if is_active else 0, target_id),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ShipmentTrackingError("同じ業者・配達番号はすでに登録されています。") from exc
    finally:
        db.close()


def toggle_target_active(target_id: int) -> bool:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT is_active FROM shipment_tracking_target WHERE id=%s", (target_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        raise ShipmentTrackingError("対象が見つかりません。")
    new_value = 0 if int(row["is_active"]) else 1
    cur = db.cursor()
    cur.execute("UPDATE shipment_tracking_target SET is_active=%s WHERE id=%s", (new_value, target_id))
    db.commit()
    db.close()
    return bool(new_value)


def run_check(target_id: int, triggered_by: str) -> bool:
    if triggered_by not in TRIGGERED_BY_VALUES:
        raise ShipmentTrackingError("triggered_by が不正です。")

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM shipment_tracking_target WHERE id=%s", (target_id,))
    target = cur.fetchone()
    if not target:
        db.close()
        raise ShipmentTrackingError("対象が見つかりません。")

    checked_at = _now()
    prev_payload_json = target.get("last_payload_json")

    try:
        payload = fetch_and_parse(target["carrier_code"], target["tracking_number"])
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        changed = True if not prev_payload_json else prev_payload_json != payload_json
        latest_event_at = _parse_payload_datetime(payload.get("latest_event_at"))
        current_status = payload.get("current_status")
        current_status_detail = payload.get("current_status_detail")
        completed = 1 if payload.get("completed") else 0

        cur_plain = db.cursor()
        cur_plain.execute(
            """
            UPDATE shipment_tracking_target
               SET last_checked_at=%s,
                   last_check_success_at=%s,
                   last_error_text=NULL,
                   last_payload_json=%s,
                   last_current_status=%s,
                   last_current_status_detail=%s,
                   last_latest_event_at=%s,
                   last_completed=%s
             WHERE id=%s
            """,
            (
                checked_at,
                checked_at,
                payload_json,
                current_status,
                current_status_detail,
                latest_event_at,
                completed,
                target_id,
            ),
        )
        cur_plain.execute(
            """
            INSERT INTO shipment_tracking_log
                (target_id, triggered_by, checked_at, success, changed, error_text,
                 payload_json, current_status, current_status_detail, latest_event_at)
            VALUES
                (%s, %s, %s, 1, %s, NULL, %s, %s, %s, %s)
            """,
            (
                target_id,
                triggered_by,
                checked_at,
                1 if changed else 0,
                payload_json,
                current_status,
                current_status_detail,
                latest_event_at,
            ),
        )
        db.commit()
        return True
    except Exception as exc:
        error_text = str(exc)
        current_app.logger.exception(
            "[shipment_tracking] check failed target_id=%s carrier=%s tracking_number=%s",
            target_id,
            target.get("carrier_code"),
            target.get("tracking_number"),
        )
        cur_plain = db.cursor()
        cur_plain.execute(
            """
            UPDATE shipment_tracking_target
               SET last_checked_at=%s,
                   last_error_text=%s
             WHERE id=%s
            """,
            (checked_at, error_text[:65535], target_id),
        )
        cur_plain.execute(
            """
            INSERT INTO shipment_tracking_log
                (target_id, triggered_by, checked_at, success, changed, error_text,
                 payload_json, current_status, current_status_detail, latest_event_at)
            VALUES
                (%s, %s, %s, 0, 0, %s, NULL, NULL, NULL, NULL)
            """,
            (
                target_id,
                triggered_by,
                checked_at,
                error_text[:65535],
            ),
        )
        db.commit()
        return False
    finally:
        db.close()


def run_scheduled_checks() -> list[dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, carrier_code, tracking_number
          FROM shipment_tracking_target
         WHERE is_active=1
         ORDER BY id ASC
        """
    )
    targets = cur.fetchall()
    db.close()

    results = []
    for target in targets:
        ok = run_check(int(target["id"]), "scheduled")
        results.append(
            {
                "id": int(target["id"]),
                "carrier_code": target["carrier_code"],
                "tracking_number": target["tracking_number"],
                "success": ok,
            }
        )
    return results
