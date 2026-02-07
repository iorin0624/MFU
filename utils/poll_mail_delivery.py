#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from app import create_app
from app.utils.mail_delivery import poll_mail_delivery_statuses


def main() -> int:
    max_rows = int(os.environ.get("MFU_MAIL_STATUS_POLL_MAX_ROWS", "200"))
    timeout_sec_env = os.environ.get("MFU_MAIL_STATUS_HTTP_TIMEOUT_SEC")
    timeout_sec = int(timeout_sec_env) if timeout_sec_env else None

    app = create_app()
    with app.app_context():
        summary = poll_mail_delivery_statuses(max_rows=max_rows, timeout_sec=timeout_sec)

    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[poll_mail_delivery] ERROR: {e.__class__.__name__}: {e}", file=sys.stderr)
        raise
