#!/usr/bin/env python3
"""Create separate Raspberry Pi ingress sources for au PAY mail notifications."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from app import app
from app.discord_notifications.repository import ensure_discord_notification_schema
from app.utils.db import get_db
from app.utils.notification_service import SOURCE_TOKEN_PREFIX, ensure_notification_service_schema


FEATURES = ("au_pay_usage", "au_pay_otp")


def provision(output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    tokens = {feature: SOURCE_TOKEN_PREFIX + secrets.token_urlsafe(32) for feature in FEATURES}
    with app.app_context():
        ensure_notification_service_schema()
        ensure_discord_notification_schema()
        db = get_db()
        cur = db.cursor()
        try:
            for feature, token in tokens.items():
                cur.execute(
                    "SELECT COUNT(*) FROM mfu_notification_sources "
                    "WHERE device_name='raspberrypi' AND feature_key=%s AND enabled=1 AND revoked_at IS NULL",
                    (feature,),
                )
                if int(cur.fetchone()[0]) > 0:
                    raise RuntimeError(f"active source already exists: {feature}")
                cur.execute(
                    "INSERT INTO mfu_notification_sources "
                    "(source_name,device_name,feature_key,recipient_username,token_hash) "
                    "VALUES(%s,'raspberrypi',%s,'admin',%s)",
                    (f"Raspberry Pi - {feature}", feature, hashlib.sha256(token.encode()).hexdigest()),
                )
                cur.execute(
                    "UPDATE mfu_discord_notification_settings AS target "
                    "JOIN mfu_discord_notification_settings AS original ON original.feature_key='mail_summary' "
                    "SET target.webhook_url=original.webhook_url, target.enabled=original.enabled "
                    "WHERE target.feature_key=%s",
                    (feature,),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()
            db.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(output, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle, ensure_ascii=False)
        handle.write("\n")
    print(f"provisioned={len(tokens)} output={output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    provision(args.output)
