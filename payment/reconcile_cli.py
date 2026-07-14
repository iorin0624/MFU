"""Systemd/cron entry point for managed Square reconciliation."""

from __future__ import annotations

import json
import os

from app import app
from app.payment import reconcile_square_managed_records


def main() -> int:
    if os.environ.get("SQUARE_RECONCILE_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        print(json.dumps({"skipped": True, "reason": "SQUARE_RECONCILE_ENABLED is disabled"}, ensure_ascii=False))
        return 0
    with app.app_context():
        result = reconcile_square_managed_records(limit=50)
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0 if not result.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
