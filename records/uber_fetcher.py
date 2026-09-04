from __future__ import annotations

import json
import os
import random
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from .uber_browser import UberAccessRestricted, UberAuthenticationRequired, UberPage, read_detail, uber_browser_lock
from .uber_parser import activity_key, normalize_list_row, parse_detail_text
from .uber_repository import get_cached_activities, remove_mirrored_quest_duplicates, sync_activity_day, update_import_job, upsert_activity


DETAIL_DELAY_MIN_SECONDS = float(os.getenv("UBER_DETAIL_DELAY_MIN_SECONDS", "3"))
DETAIL_DELAY_MAX_SECONDS = float(os.getenv("UBER_DETAIL_DELAY_MAX_SECONDS", "7"))
DETAIL_BATCH_SIZE = max(1, int(os.getenv("UBER_DETAIL_BATCH_SIZE", "20")))
DETAIL_BATCH_PAUSE_MIN_SECONDS = float(os.getenv("UBER_DETAIL_BATCH_PAUSE_MIN_SECONDS", "30"))
DETAIL_BATCH_PAUSE_MAX_SECONDS = float(os.getenv("UBER_DETAIL_BATCH_PAUSE_MAX_SECONDS", "90"))
RECENT_REFRESH_HOURS = float(os.getenv("UBER_RECENT_REFRESH_HOURS", "48"))


def _random_pause(minimum: float, maximum: float) -> None:
    low, high = sorted((max(0.0, minimum), max(0.0, maximum)))
    time.sleep(random.uniform(low, high))


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
            network_detail_count = 0
            recent_cutoff = datetime.now() - timedelta(hours=RECENT_REFRESH_HOURS)
            for query_from, query_to, wanted_from, wanted_to in _week_chunks(date_from, date_to):
                update_import_job(job_id, current_work_date=wanted_from)
                page.select_range(query_from, query_to)
                page.load_all()
                normalized_rows = []
                for raw_row in page.list_rows():
                    try:
                        row = normalize_list_row(raw_row)
                        row["activity_key"] = activity_key(row["detail_url"])[0]
                    except Exception:
                        counters["error_count"] += 1
                        continue
                    # The detail header is authoritative. Keep the following
                    # calendar day as a candidate because 00:00-03:59 belongs
                    # to the preceding Uber business date.
                    if wanted_from <= row["occurred_at"].date() <= wanted_to + timedelta(days=1):
                        normalized_rows.append(row)
                normalized_rows = _without_mirrored_quest_rows(normalized_rows)
                row_keys = [row["activity_key"] for row in normalized_rows]
                cached_activities = get_cached_activities(row_keys)
                for row in normalized_rows:
                    try:
                        key = row["activity_key"]
                        cached = cached_activities.get(key)
                        use_cache = bool(
                            cached
                            and cached.get("raw_text")
                            and row["occurred_at"] < recent_cutoff
                        )
                        if use_cache:
                            text = str(cached["raw_text"])
                            occurred_at = cached.get("occurred_at") or row["occurred_at"]
                            list_amount_yen = int(cached.get("earnings_yen") or row["list_amount_yen"] or 0)
                        else:
                            if network_detail_count:
                                if network_detail_count % DETAIL_BATCH_SIZE == 0:
                                    _random_pause(DETAIL_BATCH_PAUSE_MIN_SECONDS, DETAIL_BATCH_PAUSE_MAX_SECONDS)
                                _random_pause(DETAIL_DELAY_MIN_SECONDS, DETAIL_DELAY_MAX_SECONDS)
                            text = read_detail(row["detail_url"])
                            network_detail_count += 1
                            occurred_at = row["occurred_at"]
                            list_amount_yen = row["list_amount_yen"]
                        activity = parse_detail_text(
                            detail_url=row["detail_url"],
                            detail_text=text,
                            occurred_at=occurred_at,
                            list_amount_yen=list_amount_yen,
                        )
                        if not (wanted_from <= activity["work_date"] <= wanted_to):
                            continue
                        counters["found_count"] += 1
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
    except UberAccessRestricted as exc:
        update_import_job(job_id, status="blocked", error=str(exc), finished_at=datetime.now(), **counters)
        return {"status": "blocked", "error": str(exc), **counters}
    except Exception as exc:
        update_import_job(job_id, status="error", error=str(exc), finished_at=datetime.now(), **counters)
        return {"status": "error", "error": str(exc), **counters}
