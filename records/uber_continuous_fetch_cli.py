from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.browser_automation_lock import BrowserAutomationBusy, browser_automation_lock

from .models import ensure_records_schema
from .uber_fetcher import fetch_uber_activities
from .uber_parser import uber_work_date
from .uber_repository import (
    create_import_job,
    get_active_import_job,
    get_continuous_fetch_state,
    update_import_job,
    update_continuous_fetch_state,
)


JST = ZoneInfo("Asia/Tokyo")


def _next_run(now: datetime) -> datetime:
    candidate = now.replace(minute=40, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate.replace(tzinfo=None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Uber売上明細の継続増分取得")
    parser.add_argument("--force", action="store_true", help="前回取得時刻にかかわらず実行")
    args = parser.parse_args()
    ensure_records_schema()
    state = get_continuous_fetch_state()
    if not state.get("enabled"):
        print(json.dumps({"status": "disabled"}, ensure_ascii=False))
        return 0

    now_aware = datetime.now(JST)
    now = now_aware.replace(tzinfo=None)
    work_date = uber_work_date(now_aware)
    if state.get("active_work_date") != work_date:
        update_continuous_fetch_state(
            enabled=0,
            status="stopped",
            stopped_at=now,
            next_run_at=None,
            last_error="営業日の切替（4:00）で自動停止しました。",
        )
        print(json.dumps({"status": "auto_stopped", "work_date": str(work_date)}, ensure_ascii=False))
        return 0

    last_finished = state.get("last_run_finished_at")
    if not args.force and last_finished and now - last_finished < timedelta(minutes=45):
        update_continuous_fetch_state(status="monitoring", next_run_at=_next_run(now_aware))
        print(json.dumps({"status": "skipped", "reason": "recently_completed"}, ensure_ascii=False))
        return 0

    active = get_active_import_job()
    if active:
        update_continuous_fetch_state(status="monitoring", next_run_at=_next_run(now_aware))
        print(json.dumps({"status": "skipped", "reason": "import_running"}, ensure_ascii=False))
        return 0

    try:
        job_id = create_import_job(work_date, work_date, mode="continuous")
    except RuntimeError as exc:
        update_continuous_fetch_state(status="monitoring", next_run_at=_next_run(now_aware))
        print(json.dumps({"status": "skipped", "reason": str(exc)}, ensure_ascii=False))
        return 0

    update_continuous_fetch_state(
        status="running",
        last_job_id=job_id,
        last_run_started_at=now,
        last_error=None,
    )
    try:
        with browser_automation_lock("uber-continuous", wait_seconds=600):
            result = fetch_uber_activities(job_id, work_date, work_date, incremental=True)
    except BrowserAutomationBusy as exc:
        update_import_job(job_id, status="error", error=str(exc), finished_at=datetime.now())
        result = {"status": "busy", "error": str(exc)}

    finished = datetime.now(JST).replace(tzinfo=None)
    result_status = str(result.get("status") or "error")
    current_errors = int(state.get("consecutive_errors") or 0)
    counts = {
        "last_found_count": int(result.get("found_count") or 0),
        "last_inserted_count": int(result.get("inserted_count") or 0),
        "last_updated_count": int(result.get("updated_count") or 0),
        "last_unchanged_count": int(result.get("unchanged_count") or 0),
        "last_error_count": int(result.get("error_count") or 0),
    }
    # A Stop click during a detail fetch takes effect after that detail/run
    # finishes; never re-enable monitoring from this process afterwards.
    if not get_continuous_fetch_state().get("enabled"):
        update_continuous_fetch_state(
            status="stopped",
            last_run_finished_at=finished,
            next_run_at=None,
            **counts,
        )
    elif result_status in {"auth_required", "blocked"}:
        update_continuous_fetch_state(
            enabled=0,
            status=result_status,
            stopped_at=finished,
            last_run_finished_at=finished,
            next_run_at=None,
            consecutive_errors=current_errors + 1,
            last_error=result.get("error"),
            **counts,
        )
    elif result_status in {"error", "partial"}:
        consecutive = current_errors + 1
        disabled = consecutive >= 3
        update_continuous_fetch_state(
            enabled=0 if disabled else 1,
            status="error_paused" if disabled else "monitoring",
            stopped_at=finished if disabled else None,
            last_run_finished_at=finished,
            next_run_at=None if disabled else _next_run(datetime.now(JST)),
            consecutive_errors=consecutive,
            last_error=result.get("error") or f"明細取得で{counts['last_error_count']}件失敗しました。",
            **counts,
        )
    else:
        update_continuous_fetch_state(
            status="monitoring",
            last_run_finished_at=finished,
            next_run_at=_next_run(datetime.now(JST)),
            consecutive_errors=0,
            last_error=None,
            **counts,
        )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result_status in {"success", "partial", "auth_required", "blocked", "busy"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
