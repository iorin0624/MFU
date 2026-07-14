from __future__ import annotations

import json
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
BASE_DIR = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(BASE_DIR))

from utils.db import get_db  # noqa: E402


SETTINGS = {
    "whitelist_disabled": "whitelist_disabled_until",
    "anonymous_allowed": "anonymous_allowed_until",
}


def expire_settings() -> list[str]:
    db = get_db()
    expired: list[str] = []
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT whitelist_disabled_until, anonymous_allowed_until, NOW() AS now_value "
            "FROM phone_whitelist_sync_state WHERE id=1 FOR UPDATE"
        )
        row = cur.fetchone() or {}
        now = row.get("now_value")
        for setting, column in SETTINGS.items():
            until = row.get(column)
            if until and now and until <= now:
                cur.execute(f"UPDATE phone_whitelist_sync_state SET {column}=NULL WHERE id=1")
                payload = json.dumps(
                    {"action": "auto_expire", "result": "ok", "details": {"setting": setting, "expired_at": str(until)}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cur.execute(
                    """
                    INSERT INTO logs
                        (log_date, ip, method, path, status, endpoint, username, latency_ms, log_text)
                    VALUES
                        (NOW(), '127.0.0.1', 'SYSTEM', %s, 200, 'phone_whitelist.audit', 'system', 0, %s)
                    """,
                    (f"/admin/phone-whitelist/audit/auto_expire/{setting}", "PHONE_WHITELIST " + payload),
                )
                expired.append(setting)
        db.commit()
        return expired
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps({"expired": expire_settings()}, ensure_ascii=False))

