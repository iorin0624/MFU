from __future__ import annotations

import argparse
import random
import time
from datetime import date, datetime, timedelta

from app.records.models import ensure_records_schema
from app.records.uber_fetcher import fetch_uber_activities
from app.records.uber_repository import create_import_job
from app.utils.db import get_db


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _weekly_segments(date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        sunday = current + timedelta(days=6 - current.weekday())
        segment_to = min(sunday, date_to)
        yield current, segment_to
        current = segment_to + timedelta(days=1)


def _already_completed(date_from: date, date_to: date) -> bool:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT 1
            FROM uber_import_jobs
            WHERE date_from = %s AND date_to = %s AND status = 'success'
            LIMIT 1
            """,
            (date_from, date_to),
        )
        return cur.fetchone() is not None
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Uber履歴を週単位で安全に一括取得")
    parser.add_argument("--date-from", type=_parse_date, required=True)
    parser.add_argument("--date-to", type=_parse_date, required=True)
    parser.add_argument("--week-delay-min", type=float, default=90.0)
    parser.add_argument("--week-delay-max", type=float, default=180.0)
    parser.add_argument("--blocked-delay", type=float, default=1800.0)
    parser.add_argument("--max-retries", type=int, default=8)
    args = parser.parse_args()
    if args.date_from > args.date_to:
        parser.error("date-from must not be after date-to")

    ensure_records_schema()
    segments = list(_weekly_segments(args.date_from, args.date_to))
    for index, (segment_from, segment_to) in enumerate(segments, start=1):
        if _already_completed(segment_from, segment_to):
            print(f"[{index}/{len(segments)}] skip completed {segment_from}..{segment_to}", flush=True)
            continue

        retries = 0
        while True:
            job_id = create_import_job(segment_from, segment_to)
            print(f"[{index}/{len(segments)}] start {segment_from}..{segment_to} job={job_id}", flush=True)
            result = fetch_uber_activities(job_id, segment_from, segment_to)
            status = result.get("status")
            print(f"[{index}/{len(segments)}] result {result}", flush=True)
            if status == "success":
                break
            if status == "auth_required":
                print("Uber authentication is required; stopping the batch.", flush=True)
                return 2

            retries += 1
            if retries > max(0, args.max_retries):
                print(f"Retry limit reached for {segment_from}..{segment_to}.", flush=True)
                return 3
            delay = args.blocked_delay if status == "blocked" else min(args.blocked_delay, 600.0)
            print(f"Retrying the same week in {delay:.0f} seconds.", flush=True)
            time.sleep(max(0.0, delay))

        if index < len(segments):
            low, high = sorted((max(0.0, args.week_delay_min), max(0.0, args.week_delay_max)))
            delay = random.uniform(low, high)
            print(f"Waiting {delay:.0f} seconds before the next week.", flush=True)
            time.sleep(delay)

    print("Historical Uber import completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
