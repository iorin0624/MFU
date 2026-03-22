#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app import create_app
from app.external_login_user.notifications import send_external_unread_reminder_emails


def main() -> int:
    app = create_app()
    with app.app_context():
        summary = send_external_unread_reminder_emails(now_utc=datetime.now(timezone.utc))

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"[send_external_unread_reminders] ERROR: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        raise
