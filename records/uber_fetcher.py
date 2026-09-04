from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .uber_browser import UberAuthenticationRequired, UberPage, read_detail, uber_browser_lock
from .uber_parser import normalize_list_row, parse_detail_text
from .uber_repository import sync_activity_day, update_import_job, upsert_activity


def _week_chunks(date_from: date, date_to: date):
    cursor = date_from
    while cursor <= date_to:
        end = min(cursor + timedelta(days=6), date_to)
        yield cursor, end
        cursor = end + timedelta(days=1)


def fetch_uber_activities(job_id: str, date_from: date, date_to: date) -> dict:
    counters = {"found_count": 0, "inserted_count": 0, "updated_count": 0, "unchanged_count": 0, "error_count": 0}
    touched_days: set[date] = set()
    conflict_days: list[str] = []
    update_import_job(job_id, status="running", started_at=datetime.now(), error=None)
    try:
        with uber_browser_lock(blocking=False), UberPage() as page:
            page.ensure_logged_in()
            for chunk_from, chunk_to in _week_chunks(date_from, date_to):
                update_import_job(job_id, current_work_date=chunk_from)
                page.select_range(chunk_from, chunk_to)
                page.load_all()
                normalized_rows = []
                for raw_row in page.list_rows():
                    try:
                        row = normalize_list_row(raw_row)
                    except Exception:
                        counters["error_count"] += 1
                        continue
                    if date_from <= row["occurred_at"].date() <= date_to:
                        normalized_rows.append(row)
                counters["found_count"] += len(normalized_rows)
                for row in normalized_rows:
                    try:
                        text = read_detail(row["detail_url"])
                        activity = parse_detail_text(
                            detail_url=row["detail_url"],
                            detail_text=text,
                            occurred_at=row["occurred_at"],
                            list_amount_yen=row["list_amount_yen"],
                        )
                        outcome = upsert_activity(activity)
                        counters[f"{outcome}_count"] += 1
                        touched_days.add(activity["work_date"])
                    except UberAuthenticationRequired:
                        raise
                    except Exception:
                        counters["error_count"] += 1
                    update_import_job(job_id, **counters)
                processed = min((chunk_to - date_from).days + 1, (date_to - date_from).days + 1)
                update_import_job(job_id, processed_days=processed, current_work_date=chunk_to, **counters)

        for work_date in sorted(touched_days):
            result = sync_activity_day(work_date)
            if result["status"] == "conflict":
                conflict_days.append(work_date.isoformat())
        status = "partial" if counters["error_count"] else "success"
        result = {"status": status, **counters, "conflict_days": conflict_days}
        update_import_job(
            job_id,
            status=status,
            processed_days=(date_to - date_from).days + 1,
            conflict_days_json=json.dumps(conflict_days, ensure_ascii=False),
            finished_at=datetime.now(),
            current_work_date=None,
            **counters,
        )
        return result
    except UberAuthenticationRequired as exc:
        update_import_job(job_id, status="auth_required", error=str(exc), finished_at=datetime.now(), **counters)
        return {"status": "auth_required", "error": str(exc), **counters}
    except Exception as exc:
        update_import_job(job_id, status="error", error=str(exc), finished_at=datetime.now(), **counters)
        return {"status": "error", "error": str(exc), **counters}
