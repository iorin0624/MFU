from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .uber_browser import UberAuthenticationRequired, UberPage, read_detail, uber_browser_lock
from .uber_parser import normalize_list_row, parse_detail_text, uber_work_date
from .uber_repository import remove_mirrored_quest_duplicates, sync_activity_day, update_import_job, upsert_activity


def _week_chunks(date_from: date, date_to: date):
    week_start = date_from - timedelta(days=date_from.weekday())
    while week_start <= date_to:
        week_last_day = week_start + timedelta(days=6)
        # Uber selects the business week from Monday 04:00 through the next
        # Monday 03:59. The UI represents that interval by its Monday date.
        yield week_start, week_start + timedelta(days=7), max(date_from, week_start), min(date_to, week_last_day)
        week_start += timedelta(days=7)


def _event_type(detail_url: str) -> str:
    values = parse_qs(urlparse(detail_url).query).get("eventType") or []
    return str(values[0] if values else "").upper()


def _without_mirrored_quest_rows(rows: list[dict]) -> list[dict]:
    """Drop Uber's MISC mirror when the same quest is also exposed as QUEST."""
    quest_rows = [row for row in rows if _event_type(row["detail_url"]) == "QUEST"]
    used_quest_indexes: set[int] = set()
    result: list[dict] = []
    for row in rows:
        if _event_type(row["detail_url"]) != "MISC":
            result.append(row)
            continue
        candidates = [
            (abs((quest["occurred_at"] - row["occurred_at"]).total_seconds()), index)
            for index, quest in enumerate(quest_rows)
            if index not in used_quest_indexes
            and quest["list_amount_yen"] == row["list_amount_yen"]
            and quest["occurred_at"].date() == row["occurred_at"].date()
            and abs((quest["occurred_at"] - row["occurred_at"]).total_seconds()) <= 120
        ]
        if candidates:
            _, index = min(candidates)
            used_quest_indexes.add(index)
            continue
        result.append(row)
    return result


def fetch_uber_activities(job_id: str, date_from: date, date_to: date) -> dict:
    counters = {"found_count": 0, "inserted_count": 0, "updated_count": 0, "unchanged_count": 0, "error_count": 0}
    touched_days: set[date] = set()
    conflict_days: list[str] = []
    update_import_job(job_id, status="running", started_at=datetime.now(), error=None)
    try:
        with uber_browser_lock(blocking=False), UberPage() as page:
            page.ensure_logged_in()
            processed = 0
            for query_from, query_to, wanted_from, wanted_to in _week_chunks(date_from, date_to):
                update_import_job(job_id, current_work_date=wanted_from)
                page.select_range(query_from, query_to)
                page.load_all()
                normalized_rows = []
                for raw_row in page.list_rows():
                    try:
                        row = normalize_list_row(raw_row)
                    except Exception:
                        counters["error_count"] += 1
                        continue
                    if wanted_from <= uber_work_date(row["occurred_at"]) <= wanted_to:
                        normalized_rows.append(row)
                normalized_rows = _without_mirrored_quest_rows(normalized_rows)
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
                processed += (wanted_to - wanted_from).days + 1
                update_import_job(job_id, processed_days=processed, current_work_date=wanted_to, **counters)

        remove_mirrored_quest_duplicates(date_from, date_to)
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
