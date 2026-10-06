#!/usr/bin/env python3
"""Small, dependency-free MFU notification sender for Raspberry Pi/Linux hosts.

Failed requests are retained in a local SQLite queue.  A timer/cron job can run
``mfu_notify.py --drain`` to retry them after the network or MFU recovers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "https://mfu.iori0624.jp/api/notification-ingress/v1/events"


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS pending_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL, next_attempt_at INTEGER NOT NULL DEFAULT 0)"""
    )
    db.commit()
    return db


def _post(endpoint: str, token: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=raw,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "mfu-notify/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
        data = json.loads(body)
        if response.status >= 300 or not data.get("ok"):
            raise RuntimeError(data.get("error") or f"HTTP {response.status}")
        return data


def _enqueue(db: sqlite3.Connection, payload: dict[str, Any], error: str = "") -> int:
    cursor = db.execute(
        "INSERT INTO pending_notifications(payload,last_error,created_at) VALUES(?,?,?)",
        (json.dumps(payload, ensure_ascii=False), str(error)[:1000], int(time.time())),
    )
    db.commit()
    return int(cursor.lastrowid)


def _drain(db: sqlite3.Connection, endpoint: str, token: str, limit: int = 100) -> tuple[int, int]:
    sent = failed = 0
    now = int(time.time())
    rows = db.execute(
        "SELECT id,payload,attempts FROM pending_notifications WHERE next_attempt_at<=? ORDER BY id LIMIT ?",
        (now, max(1, min(limit, 1000))),
    ).fetchall()
    for row_id, raw, attempts in rows:
        try:
            _post(endpoint, token, json.loads(raw))
            db.execute("DELETE FROM pending_notifications WHERE id=?", (row_id,))
            db.commit()
            sent += 1
        except Exception as exc:
            attempts = int(attempts) + 1
            delay = min(3600, 30 * (2 ** min(attempts, 7)))
            db.execute(
                "UPDATE pending_notifications SET attempts=?,last_error=?,next_attempt_at=? WHERE id=?",
                (attempts, str(exc)[:1000], int(time.time()) + delay, row_id),
            )
            db.commit()
            failed += 1
    return sent, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="MFU共通通知送信")
    parser.add_argument("--endpoint", default=os.getenv("MFU_NOTIFICATION_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--token", default=os.getenv("MFU_NOTIFICATION_TOKEN", ""))
    parser.add_argument("--queue", default=os.getenv("MFU_NOTIFICATION_QUEUE", "~/.local/state/mfu-notify/queue.sqlite3"))
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--feature", default=os.getenv("MFU_NOTIFICATION_FEATURE", "general"))
    parser.add_argument("--kind", default="")
    parser.add_argument("--severity", choices=("info", "success", "warning", "error", "critical"), default="info")
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--target-url", default="/mfu-notifications")
    parser.add_argument("--topic", default="")
    parser.add_argument("--dedup-key", default="")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token または MFU_NOTIFICATION_TOKEN が必要です")

    db = _database(Path(args.queue).expanduser())
    if args.drain:
        sent, failed = _drain(db, args.endpoint, args.token)
        print(json.dumps({"ok": failed == 0, "sent": sent, "failed": failed}, ensure_ascii=False))
        return 0 if failed == 0 else 1
    if not args.title:
        parser.error("--title が必要です")

    payload = {
        "feature_key": args.feature,
        "kind": args.kind or args.feature,
        "severity": args.severity,
        "title": args.title,
        "description": args.body,
        "target_url": args.target_url,
        "topic_key": args.topic,
    }
    payload["dedup_key"] = args.dedup_key or "sender:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:40]
    try:
        result = _post(args.endpoint, args.token, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
        queued_id = _enqueue(db, payload, str(exc))
        print(json.dumps({"ok": False, "queued": True, "queue_id": queued_id, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
