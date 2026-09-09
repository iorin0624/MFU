from __future__ import annotations

import argparse
import json
import os
import time

from .fetcher import fetch_month, scheduled_months
from .browser_session import ETCTargetPage
from .credentials import etc_browser_lock
from .manual_jobs import update_manual_fetch_job
from .notifications import dispatch_pending_new_record_notifications, send_fetch_failure_notification
from .parser import ETCAuthenticationRequired, ETCNavigationStateError
from .repository import record_scheduled_fetch_completed
from app.utils.browser_automation_lock import BrowserAutomationBusy, browser_automation_lock


def _failure_status(exc: Exception) -> str:
    if isinstance(exc, ETCAuthenticationRequired):
        return "auth_required"
    message = str(exc)
    if any(marker in message for marker in ("自動ログインに失敗", "ログイン有効期限", "認証情報", "再認証")):
        return "auth_required"
    return "error"


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
        with browser_automation_lock("etc", wait_seconds=600), etc_browser_lock(), ETCTargetPage() as browser:
            for month in months:
                for attempt in range(2):
                    try:
                        results.append(fetch_month(month, force_record_ids=set(args.force_id), browser=browser))
                        break
                    except ETCNavigationStateError as exc:
                        if attempt == 0 and browser.recover_statement_page():
                            continue
                        results.append({"status": "error", "statement_month": month, "error": str(exc)})
                        exit_code = 1
                        break
                    except ETCAuthenticationRequired as exc:
                        if attempt == 0:
                            try:
                                browser.ensure_logged_in()
                            except Exception as login_exc:
                                results.append({
                                    "status": _failure_status(login_exc),
                                    "statement_month": month,
                                    "error": str(login_exc),
                                })
                                exit_code = 1
                                break
                            continue
                        results.append({"status": "auth_required", "statement_month": month, "error": str(exc)})
                        exit_code = 1
                        break
                    except Exception as exc:
                        results.append({"status": _failure_status(exc), "statement_month": month, "error": str(exc)})
                        exit_code = 1
                        break
    except BrowserAutomationBusy as exc:
        results.append({"status": "error", "error": str(exc)})
        exit_code = 1
    except ETCAuthenticationRequired as exc:
        results.append({"status": "auth_required", "error": str(exc)})
        exit_code = 1
    except Exception as exc:
        results.append({"status": _failure_status(exc), "error": str(exc)})
        exit_code = 1
    fetch_failures = [
        result for result in results
        if isinstance(result, dict) and result.get("status") in {"error", "auth_required"}
    ]
    failure_notification_error = ""
    try:
        failure_notification = send_fetch_failure_notification(fetch_failures, scheduled=scheduled_run)
    except Exception as exc:
        failure_notification = {"status": "error", "error": str(exc)}
        failure_notification_error = str(exc)
    results.append({"fetch_failure_notification": failure_notification})
    notification_error = ""
    try:
        notification = dispatch_pending_new_record_notifications()
    except Exception as exc:
        notification = {"status": "error", "error": str(exc)}
        notification_error = str(exc)
        exit_code = 1
    if failure_notification_error:
        notification_error = "; ".join(filter(None, (failure_notification_error, notification_error)))
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
