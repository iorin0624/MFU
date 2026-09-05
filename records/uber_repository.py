from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from statistics import median

from app.utils.db import get_db


TERMINAL_JOB_STATUSES = {"success", "partial", "error", "auth_required", "blocked"}


def _quest_goal_count(raw_text: str | None) -> int | None:
    text = str(raw_text or "")
    patterns = (
        r"completed\s*\d+\s*/\s*(\d+)\s*trips?",
        r"completing\s*(\d+)\s*trips?",
        r"(?:^|[):：])\s*(\d+)\s*回の乗車",
        r"(\d+)\s*回(?:の)?(?:乗車|配達)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            return int(match.group(1))
    return None


def _is_mirrored_quest(misc: dict, quest: dict) -> bool:
    seconds = abs((quest["occurred_at"] - misc["occurred_at"]).total_seconds())
    if int(quest.get("earnings_yen") or 0) != int(misc.get("earnings_yen") or 0):
        return False
    if quest["occurred_at"].date() != misc["occurred_at"].date():
        return False
    if seconds <= 120:
        return True
    misc_text = str(misc.get("raw_text") or "")
    quest_text = str(quest.get("raw_text") or "")
    explicit_completion_pair = bool(
        re.search(r"クエスト.*達成", misc_text, re.I | re.S)
        and re.search(r"(?:支払い明細|お支払い明細).*追加", misc_text, re.I | re.S)
        and re.search(r"QUEST\s+COMPLETE", quest_text, re.I)
    )
    if explicit_completion_pair and seconds <= 30 * 60:
        return True
    misc_goal = _quest_goal_count(misc.get("raw_text"))
    quest_goal = _quest_goal_count(quest.get("raw_text"))
    return bool(misc_goal is not None and misc_goal == quest_goal and seconds <= 6 * 3600)


def create_import_job(date_from: date, date_to: date, *, mode: str = "manual") -> str:
    job_id = uuid.uuid4().hex
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT GET_LOCK('mfu_uber_import_job_create', 5)")
        if int((cur.fetchone() or [0])[0] or 0) != 1:
            raise RuntimeError("Uber取得処理の開始ロックを取得できませんでした。")
        cur.execute("SELECT COUNT(*) FROM uber_import_jobs WHERE status IN ('pending', 'running')")
        if int((cur.fetchone() or [0])[0] or 0):
            raise RuntimeError("別のUber取得処理が実行中です。")
        cur.execute(
            """
            INSERT INTO uber_import_jobs (
                id, mode, date_from, date_to, status, total_days, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)
            """,
            (job_id, mode, date_from, date_to, (date_to - date_from).days + 1, now, now),
        )
        db.commit()
        return job_id
    finally:
        try:
            db.cursor().execute("SELECT RELEASE_LOCK('mfu_uber_import_job_create')")
        except Exception:
            pass
        db.close()


def update_import_job(job_id: str, **fields) -> None:
    allowed = {
        "status", "processed_days", "found_count", "inserted_count", "updated_count",
        "unchanged_count", "error_count", "current_work_date", "conflict_days_json", "error",
        "started_at", "finished_at",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    values["updated_at"] = datetime.now()
    assignments = ", ".join(f"{key} = %s" for key in values)
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            f"UPDATE uber_import_jobs SET {assignments} WHERE id = %s",
            (*values.values(), job_id),
        )
        db.commit()
    finally:
        db.close()


def get_import_job(job_id: str) -> dict | None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM uber_import_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if row and row.get("conflict_days_json"):
            try:
                row["conflict_days"] = json.loads(row["conflict_days_json"])
            except (TypeError, json.JSONDecodeError):
                row["conflict_days"] = []
        return row
    finally:
        db.close()


def get_active_import_job() -> dict | None:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            UPDATE uber_import_jobs
            SET status='error', error='取得処理が中断されました。もう一度実行してください。',
                finished_at=NOW(), updated_at=NOW()
            WHERE status IN ('pending', 'running')
              AND updated_at < DATE_SUB(NOW(), INTERVAL 6 HOUR)
            """
        )
        cur.execute(
            """
            SELECT * FROM uber_import_jobs
            WHERE status IN ('pending', 'running')
            ORDER BY created_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        db.commit()
        return row
    finally:
        db.close()


def list_import_jobs(limit: int = 10) -> list[dict]:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT * FROM uber_import_jobs ORDER BY created_at DESC LIMIT %s",
            (max(1, min(int(limit), 50)),),
        )
        return cur.fetchall()
    finally:
        db.close()


def _comparable_activity(row: dict) -> tuple:
    fields = (
        "activity_type", "occurred_at", "work_date", "duration_seconds", "distance_km",
        "points", "deliveries", "earnings_yen", "sales_yen", "promo_yen", "other_yen",
        "tip_yen", "cash_collected_yen", "uber_payment_yen", "merchant_name",
        "pickup_address", "delivery_address", "detail_url", "raw_text",
    )
    return tuple(str(row.get(field) if row.get(field) is not None else "") for field in fields)


def upsert_activity(activity: dict) -> str:
    now = datetime.now()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM uber_activities WHERE activity_key = %s", (activity["activity_key"],))
        previous = cur.fetchone()
        if previous and _comparable_activity(previous) == _comparable_activity(activity):
            cur.execute(
                "UPDATE uber_activities SET last_imported_at = %s WHERE id = %s",
                (now, previous["id"]),
            )
            db.commit()
            return "unchanged"

        fields = (
            "activity_key", "activity_type", "occurred_at", "work_date", "duration_seconds",
            "distance_km", "points", "deliveries", "earnings_yen", "sales_yen", "promo_yen",
            "other_yen", "tip_yen", "cash_collected_yen", "uber_payment_yen", "merchant_name",
            "pickup_address", "delivery_address", "detail_url", "raw_text",
        )
        values = [activity.get(field) for field in fields]
        cur.execute(
            f"""
            INSERT INTO uber_activities ({', '.join(fields)}, first_imported_at, last_imported_at, created_at, updated_at)
            VALUES ({', '.join(['%s'] * len(fields))}, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                activity_type=VALUES(activity_type), occurred_at=VALUES(occurred_at), work_date=VALUES(work_date),
                duration_seconds=VALUES(duration_seconds), distance_km=VALUES(distance_km), points=VALUES(points),
                deliveries=VALUES(deliveries), earnings_yen=VALUES(earnings_yen), sales_yen=VALUES(sales_yen),
                promo_yen=VALUES(promo_yen), other_yen=VALUES(other_yen), tip_yen=VALUES(tip_yen),
                cash_collected_yen=VALUES(cash_collected_yen), uber_payment_yen=VALUES(uber_payment_yen),
                merchant_name=VALUES(merchant_name), pickup_address=VALUES(pickup_address),
                delivery_address=VALUES(delivery_address), detail_url=VALUES(detail_url), raw_text=VALUES(raw_text),
                last_imported_at=VALUES(last_imported_at), updated_at=VALUES(updated_at)
            """,
            (*values, now, now, now, now),
        )
        db.commit()
        return "updated" if previous else "inserted"
    finally:
        db.close()


def get_continuous_fetch_state() -> dict:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM uber_continuous_fetch_state WHERE id = 1")
        return cur.fetchone() or {"id": 1, "enabled": 0, "status": "stopped"}
    finally:
        db.close()


def update_continuous_fetch_state(**fields) -> dict:
    allowed = {
        "enabled", "active_work_date", "status", "started_at", "stopped_at",
        "last_run_started_at", "last_run_finished_at", "next_run_at", "last_job_id",
        "last_found_count", "last_inserted_count", "last_updated_count",
        "last_unchanged_count", "last_error_count", "consecutive_errors", "last_error",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if values:
        values["updated_at"] = datetime.now()
        assignments = ", ".join(f"{key} = %s" for key in values)
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                f"UPDATE uber_continuous_fetch_state SET {assignments} WHERE id = 1",
                tuple(values.values()),
            )
            db.commit()
        finally:
            db.close()
    return get_continuous_fetch_state()


def get_cached_activities(activity_keys: list[str]) -> dict[str, dict]:
    keys = list(dict.fromkeys(str(key) for key in activity_keys if key))
    if not keys:
        return {}
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        placeholders = ", ".join(["%s"] * len(keys))
        cur.execute(
            f"""
            SELECT activity_key, activity_type, occurred_at, duration_seconds,
                   distance_km, points, deliveries, earnings_yen, merchant_name,
                   delivery_address, raw_text, last_imported_at
            FROM uber_activities
            WHERE activity_key IN ({placeholders})
            """,
            keys,
        )
        return {str(row["activity_key"]): row for row in cur.fetchall()}
    finally:
        db.close()


def remove_mirrored_quest_duplicates(date_from: date, date_to: date) -> int:
    """Prefer posted MISC rewards over duplicate placeholder QUEST cards."""
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT activity_key, occurred_at, earnings_yen, raw_text
            FROM uber_activities
            WHERE work_date BETWEEN %s AND %s
              AND activity_type = 'quest'
            ORDER BY occurred_at, activity_key
            """,
            (date_from, date_to),
        )
        rows = cur.fetchall()
        misc_rows = [row for row in rows if str(row["activity_key"]).startswith("ACTIVITY:MISC:")]
        used_misc_keys: set[str] = set()
        delete_keys: list[str] = []
        for row in rows:
            if not str(row["activity_key"]).startswith("ACTIVITY:QUEST:"):
                continue
            candidates = [
                (abs((row["occurred_at"] - misc["occurred_at"]).total_seconds()), misc)
                for misc in misc_rows
                if misc["activity_key"] not in used_misc_keys
                and _is_mirrored_quest(misc, row)
            ]
            if candidates:
                _, misc = min(candidates, key=lambda item: item[0])
                used_misc_keys.add(misc["activity_key"])
                delete_keys.append(row["activity_key"])
        if delete_keys:
            placeholders = ", ".join(["%s"] * len(delete_keys))
            cur.execute(f"DELETE FROM uber_activities WHERE activity_key IN ({placeholders})", delete_keys)
        db.commit()
        return len(delete_keys)
    finally:
        db.close()


def daily_activity_summary(work_date: date) -> dict:
    return activity_range_summary(work_date, work_date)


def _median_rate(rows: list[dict], numerator_keys: tuple[str, ...], denominator_key: str, scale=1) -> int | None:
    values = []
    for daily in rows:
        denominator = Decimal(str(daily.get(denominator_key) or 0))
        if denominator <= 0:
            continue
        numerator = sum(Decimal(str(daily.get(key) or 0)) for key in numerator_keys)
        values.append(numerator * Decimal(str(scale)) / denominator)
    return round(median(values)) if values else None


def activity_range_summary(date_from: date, date_to: date) -> dict:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                COUNT(*) AS activity_count,
                COALESCE(SUM(deliveries), 0) AS deliveries,
                COALESCE(SUM(CASE WHEN activity_type = 'delivery' THEN sales_yen ELSE 0 END), 0) AS net_yen,
                COALESCE(SUM(promo_yen), 0) AS promo_yen,
                COALESCE(SUM(other_yen), 0) AS other_yen,
                COALESCE(SUM(tip_yen), 0) AS tip_yen,
                COALESCE(SUM(sales_yen), 0) AS sales_yen,
                COALESCE(SUM(cash_collected_yen), 0) AS cash_collected_yen,
                COALESCE(SUM(uber_payment_yen), 0) AS uber_payment_yen,
                COALESCE(SUM(duration_seconds), 0) AS duration_seconds,
                COALESCE(SUM(distance_km), 0) AS distance_km,
                MAX(last_imported_at) AS last_imported_at
            FROM uber_activities WHERE work_date BETWEEN %s AND %s
            """,
            (date_from, date_to),
        )
        row = cur.fetchone() or {}
        cur.execute(
            """
            SELECT sales_yen, tip_yen, deliveries, duration_seconds, distance_km
            FROM uber_activities
            WHERE activity_type = 'delivery'
              AND work_date BETWEEN %s AND %s
            """,
            (date_from, date_to),
        )
        delivery_rows = cur.fetchall()
        total_keys = ("sales_yen", "tip_yen")
        net_keys = ("sales_yen",)
        row.update(
            {
                "total_per_delivery_median": _median_rate(delivery_rows, total_keys, "deliveries"),
                "total_per_hour_median": _median_rate(delivery_rows, total_keys, "duration_seconds", 3600),
                "total_per_km_median": _median_rate(delivery_rows, total_keys, "distance_km"),
                "net_per_delivery_median": _median_rate(delivery_rows, net_keys, "deliveries"),
                "net_per_hour_median": _median_rate(delivery_rows, net_keys, "duration_seconds", 3600),
                "net_per_km_median": _median_rate(delivery_rows, net_keys, "distance_km"),
            }
        )
        row["date_from"] = date_from
        row["date_to"] = date_to
        return row
    finally:
        db.close()


def sync_activity_day(work_date: date) -> dict:
    summary = daily_activity_summary(work_date)
    if not int(summary.get("activity_count") or 0):
        return {"status": "empty", "summary": summary}
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, source FROM uber_daily WHERE work_date = %s", (work_date,))
        current = cur.fetchone()
        replaced_existing = bool(current and str(current.get("source") or "manual") != "uber_browser")
        now = datetime.now()
        params = (
            int(summary.get("deliveries") or 0), int(summary.get("net_yen") or 0),
            int(summary.get("promo_yen") or 0), int(summary.get("other_yen") or 0),
            int(summary.get("tip_yen") or 0), now, work_date,
        )
        if current:
            # Only replace the sales fields. The existing row ID and freee
            # linkage/status columns intentionally remain untouched.
            cur.execute(
                """
                UPDATE uber_daily SET deliveries=%s, net_yen=%s, promo_yen=%s, other_yen=%s,
                    tip_yen=%s, source='uber_browser', updated_at=%s WHERE work_date=%s
                """,
                params,
            )
        else:
            cur.execute(
                """
                INSERT INTO uber_daily (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen,
                    source, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'uber_browser', %s, %s)
                """,
                (work_date, *params[:5], now, now),
            )
        db.commit()
        return {"status": "replaced" if replaced_existing else "synced", "summary": summary}
    finally:
        db.close()


def list_activity_daily_summaries(date_from: date, date_to: date) -> list[dict]:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT work_date, COUNT(*) activity_count, COALESCE(SUM(deliveries), 0) deliveries,
                COALESCE(SUM(CASE WHEN activity_type='delivery' THEN sales_yen ELSE 0 END), 0) net_yen,
                COALESCE(SUM(promo_yen), 0) promo_yen, COALESCE(SUM(other_yen), 0) other_yen,
                COALESCE(SUM(tip_yen), 0) tip_yen, COALESCE(SUM(sales_yen), 0) sales_yen,
                COALESCE(SUM(cash_collected_yen), 0) cash_collected_yen,
                COALESCE(SUM(uber_payment_yen), 0) uber_payment_yen,
                COALESCE(SUM(duration_seconds), 0) duration_seconds,
                COALESCE(SUM(distance_km), 0) distance_km, MAX(last_imported_at) last_imported_at
            FROM uber_activities WHERE work_date BETWEEN %s AND %s
            GROUP BY work_date ORDER BY work_date DESC
            """,
            (date_from, date_to),
        )
        return cur.fetchall()
    finally:
        db.close()


def list_activities(date_from: date, date_to: date, limit: int = 1000) -> list[dict]:
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT activity_key, activity_type, occurred_at, duration_seconds, distance_km,
                points, deliveries, earnings_yen, sales_yen, promo_yen, other_yen, tip_yen,
                cash_collected_yen, uber_payment_yen, merchant_name, pickup_address,
                delivery_address, detail_url, last_imported_at
            FROM uber_activities
            WHERE work_date BETWEEN %s AND %s
            ORDER BY occurred_at DESC, id DESC
            LIMIT %s
            """,
            (date_from, date_to, max(1, min(int(limit), 5000))),
        )
        return cur.fetchall()
    finally:
        db.close()


def list_activities_for_export(date_from: date, date_to: date) -> list[dict]:
    """Return every activity in a range in chronological CSV order."""
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT activity_type, occurred_at, duration_seconds, distance_km,
                deliveries, earnings_yen, cash_collected_yen, merchant_name,
                delivery_address
            FROM uber_activities
            WHERE work_date BETWEEN %s AND %s
            ORDER BY occurred_at, id
            """,
            (date_from, date_to),
        )
        return cur.fetchall()
    finally:
        db.close()
