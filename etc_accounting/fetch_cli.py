from __future__ import annotations

import argparse
import json
import os
import time

from .fetcher import fetch_month, scheduled_months
from .manual_jobs import update_manual_fetch_job
from .notifications import dispatch_pending_new_record_notifications
from .repository import record_scheduled_fetch_completed
from app.utils.browser_automation_lock import BrowserAutomationBusy, browser_automation_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="ETC利用証明書PDF取得")
    parser.add_argument("--month", action="append", help="対象月 YYYYMM。複数指定可")
    parser.add_argument(
        "--force-id",
        action="append",
        type=int,
        default=[],
        help="既存PDFがあっても再取得するレコードID。複数指定可",
    )
    parser.add_argument("--manual-job-id", help="画面から開始した手動取得のジョブID")
    args = parser.parse_args()
    scheduled_run = not args.month and not args.force_id
    months = args.month or scheduled_months(months_back=int(os.environ.get("ETC_FETCH_MONTHS_BACK", "2")))
    results = []
    exit_code = 0
    if args.manual_job_id:
        update_manual_fetch_job(args.manual_job_id, status="running")
    try:
        with browser_automation_lock("etc", wait_seconds=600):
            for month in months:
                try:
                    results.append(fetch_month(month, force_record_ids=set(args.force_id)))
                except Exception as exc:
                    results.append({"status": "error", "statement_month": month, "error": str(exc)})
                    exit_code = 1
    except BrowserAutomationBusy as exc:
        results.append({"status": "error", "error": str(exc)})
        exit_code = 1
    notification_error = ""
    try:
        notification = dispatch_pending_new_record_notifications()
    except Exception as exc:
        notification = {"status": "error", "error": str(exc)}
        notification_error = str(exc)
        exit_code = 1
    results.append({"notification": notification})
    if scheduled_run:
        maintenance = any(
            isinstance(result, dict) and result.get("status") == "maintenance"
            for result in results
        )
        final_status = "error" if exit_code else ("maintenance" if maintenance else "success")
        record_scheduled_fetch_completed(final_status)
    if args.manual_job_id:
        fetch_result = next(
            (result for result in results if isinstance(result, dict) and "statement_month" in result),
            {"status": "error", "error": "取得結果を確認できませんでした。"},
        )
        fetch_status = str(fetch_result.get("status") or "error")
        job_status = fetch_status if fetch_status in {"success", "maintenance", "auth_required"} else "error"
        update_manual_fetch_job(
            args.manual_job_id,
            status=job_status,
            result=fetch_result,
            notification=notification,
            notificationError=notification_error,
            finishedAt=time.time(),
        )
    print(json.dumps(results, ensure_ascii=False, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
