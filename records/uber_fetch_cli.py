from __future__ import annotations

import argparse
import json
from datetime import datetime

from .models import ensure_records_schema
from .uber_fetcher import fetch_uber_activities
from .uber_repository import update_import_job
from app.utils.browser_automation_lock import BrowserAutomationBusy, browser_automation_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="Uber売上明細取得")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    args = parser.parse_args()
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    ensure_records_schema()
    try:
        with browser_automation_lock("uber-manual", wait_seconds=600):
            result = fetch_uber_activities(args.job_id, date_from, date_to)
    except BrowserAutomationBusy as exc:
        update_import_job(
            args.job_id,
            status="error",
            error=str(exc),
            finished_at=datetime.now(),
        )
        result = {"status": "error", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in {"success", "partial", "auth_required", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
