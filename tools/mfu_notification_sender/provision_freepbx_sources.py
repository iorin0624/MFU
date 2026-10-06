#!/usr/bin/env python3
"""Provision MFU common-notification sources for the FreePBX host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from app import app
from app.utils.db import get_db
from app.utils.notification_service import SOURCE_TOKEN_PREFIX, ensure_notification_service_schema


FEATURES = ("freepbx_calls", "voicemail_transcription")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    tokens: dict[str, str] = {}
    with app.app_context():
        ensure_notification_service_schema()
        db = get_db()
        cur = db.cursor()
        try:
            for feature in FEATURES:
                source_name = f"FreePBX - {feature}"
                cur.execute(
                    "UPDATE mfu_notification_sources SET enabled=0,revoked_at=UTC_TIMESTAMP() "
                    "WHERE source_name=%s AND device_name='FreePBX' AND revoked_at IS NULL",
                    (source_name,),
                )
                token = SOURCE_TOKEN_PREFIX + secrets.token_urlsafe(32)
                cur.execute(
                    "INSERT INTO mfu_notification_sources "
                    "(source_name,device_name,feature_key,recipient_username,token_hash) "
                    "VALUES(%s,'FreePBX',%s,'admin',%s)",
                    (source_name, feature, hashlib.sha256(token.encode()).hexdigest()),
                )
                tokens[feature] = token
            db.commit()
        finally:
            cur.close()
            db.close()
    args.output.write_text(json.dumps({
        "base_url": "https://mfu.iori0624.jp/api/notification-ingress/discord",
        "tokens": tokens,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(f"provisioned={len(tokens)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
