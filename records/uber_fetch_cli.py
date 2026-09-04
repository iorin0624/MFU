from __future__ import annotations

import argparse
import json
from datetime import datetime

from .models import ensure_records_schema
from .uber_fetcher import fetch_uber_activities


def main() -> int:
    parser = argparse.ArgumentParser(description="Uber売上明細取得")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    args = parser.parse_args()
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    ensure_records_schema()
    result = fetch_uber_activities(args.job_id, date_from, date_to)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in {"success", "partial", "auth_required", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
