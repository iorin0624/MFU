#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from app import create_app
from app.utils.mail_delivery import backfill_mail_delivery_recipients_from_logs


def main() -> int:
    max_rows_env = os.environ.get("MFU_MAIL_BACKFILL_MAX_ROWS")
    max_rows = int(max_rows_env) if max_rows_env else None

    app = create_app()
    with app.app_context():
        summary = backfill_mail_delivery_recipients_from_logs(max_rows=max_rows)

    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"[backfill_mail_delivery_recipients] ERROR: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        raise
