"""Six-month retention for HTTP access rows in the shared ``logs`` table."""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable

from app.utils.db import get_db


JST = timezone(timedelta(hours=9))
DEFAULT_RETENTION_MONTHS = 6
DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_SIZE = 20000
LOCK_NAME = "mfu_access_log_retention"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# Access rows written by the current logger have structured method/path fields.
# Older rows are recognized only when log_text begins with a known HTTP method.
# LOGIN, LINE_LOGIN, SMTP, and unclassified audit rows match neither branch.
_LEGACY_METHOD_SQL = " OR ".join(f"log_text LIKE '{method} %'" for method in HTTP_METHODS)
ACCESS_LOG_WHERE_SQL = f"""
    log_date < %s
    AND (
        (COALESCE(method, '') <> '' AND COALESCE(path, '') <> '')
        OR (
            (COALESCE(method, '') = '' OR COALESCE(path, '') = '')
            AND ({_LEGACY_METHOD_SQL})
        )
    )
"""


def subtract_calendar_months(value: datetime, months: int = DEFAULT_RETENTION_MONTHS) -> datetime:
    """Subtract calendar months while clamping to the target month's last day."""

    if months < 1:
        raise ValueError("retention months must be positive")
    source_month = value.month - 1 - months
    target_year = value.year + source_month // 12
    target_month = source_month % 12 + 1
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return value.replace(year=target_year, month=target_month, day=target_day)


def _mysql_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(JST).replace(tzinfo=None)


def _as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def run_access_log_retention(
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    retention_months: int = DEFAULT_RETENTION_MONTHS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    db_factory: Callable = get_db,
    logger=None,
) -> dict:
    """Report or delete one oldest-first batch of expired HTTP access rows."""

    logger = logger or logging.getLogger("mfu.access_log_retention")
    current = _as_jst(now or datetime.now(JST))
    cutoff = subtract_calendar_months(current, retention_months)
    batch_limit = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    result = {
        "dry_run": dry_run,
        "now": current.isoformat(),
        "cutoff": cutoff.isoformat(),
        "retention_months": retention_months,
        "batch_size": batch_limit,
        "matched": None,
        "oldest_matched": None,
        "newest_matched": None,
        "deleted": 0,
        "locked": False,
        "error": None,
    }

    db = db_factory()
    cursor = db.cursor(dictionary=True)
    lock_acquired = False
    try:
        if dry_run:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS matched,
                       MIN(log_date) AS oldest_matched,
                       MAX(log_date) AS newest_matched
                  FROM logs
                 WHERE {ACCESS_LOG_WHERE_SQL}
                """,
                (_mysql_datetime(cutoff),),
            )
            row = cursor.fetchone() or {}
            result["matched"] = int(row.get("matched") or 0)
            result["oldest_matched"] = row.get("oldest_matched")
            result["newest_matched"] = row.get("newest_matched")
            return result

        cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LOCK_NAME,))
        lock_acquired = bool((cursor.fetchone() or {}).get("acquired"))
        if not lock_acquired:
            result["locked"] = True
            return result

        cursor.execute(
            f"""
            DELETE FROM logs
             WHERE {ACCESS_LOG_WHERE_SQL}
             ORDER BY log_date ASC, id ASC
             LIMIT %s
            """,
            (_mysql_datetime(cutoff), batch_limit),
        )
        result["deleted"] = max(0, int(cursor.rowcount or 0))
        db.commit()
        logger.info(
            "access log retention completed: cutoff=%s batch_size=%s deleted=%s",
            cutoff.isoformat(),
            batch_limit,
            result["deleted"],
        )
    except Exception as exc:
        db.rollback()
        result["error"] = repr(exc)
        logger.exception("access log retention failed: cutoff=%s", cutoff.isoformat())
    finally:
        if lock_acquired:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            except Exception:
                logger.warning("failed to release access log retention advisory lock", exc_info=True)
        db.close()

    return result
