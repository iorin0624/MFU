"""systemd/cron entry point for upload expiry processing."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from app import app
from app.utils.access_log_retention import run_access_log_retention
from app.utils.upload_expiry import JST, configured_storage_root, ensure_upload_expiry_schema, run_upload_expiry


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notify and remove expired normal uploads")
    parser.add_argument("--dry-run", action="store_true", help="report actions without sending or deleting")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--access-log-batch-size", type=int, default=5000)
    parser.add_argument("--now", help="test-only ISO datetime; a timezone-less value is interpreted as JST")
    parser.add_argument("--ensure-schema", action="store_true", help="create the action history table if missing")
    return parser.parse_args()


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=JST) if parsed.tzinfo is None else parsed.astimezone(JST)


def main() -> int:
    args = _parse_args()
    enabled = os.environ.get("UPLOAD_EXPIRY_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not args.dry_run and not enabled:
        print(json.dumps({"skipped": True, "reason": "UPLOAD_EXPIRY_ENABLED is disabled"}, ensure_ascii=False))
        return 0

    with app.app_context():
        if args.ensure_schema:
            ensure_upload_expiry_schema()
        upload_result = run_upload_expiry(
            dry_run=args.dry_run,
            now=_parse_now(args.now),
            limit=args.limit,
            storage_root=configured_storage_root(app),
            public_base_url=os.environ.get("MFU_PUBLIC_BASE_URL", "https://mfu.iori0624.jp"),
            logger=app.logger,
        )
        retention_enabled = os.environ.get("ACCESS_LOG_RETENTION_ENABLED", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if args.dry_run or retention_enabled:
            access_log_result = run_access_log_retention(
                dry_run=args.dry_run,
                now=_parse_now(args.now),
                batch_size=args.access_log_batch_size,
                logger=app.logger,
            )
        else:
            access_log_result = {"skipped": True, "reason": "ACCESS_LOG_RETENTION_ENABLED is disabled"}
        result = {
            "upload_expiry": upload_result,
            "access_log_retention": access_log_result,
        }
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    upload_failed = upload_result.get("notice_failed") or upload_result.get("delete_failed")
    access_failed = access_log_result.get("error")
    return 2 if upload_failed or access_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
