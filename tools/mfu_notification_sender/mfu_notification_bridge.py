#!/usr/bin/env python3
"""Local Discord-compatible bridge for MFU common notifications.

Existing programs post their Discord payload to localhost.  The bridge forwards
it to the feature-specific MFU ingress URL and durably queues delivery failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://mfu.iori0624.jp/api/notification-ingress/discord"
LOG = logging.getLogger("mfu-notification-bridge")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS pending_discord_payloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_key TEXT NOT NULL, payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(feature_key, payload))"""
            )
        os.chmod(path, 0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def enqueue(self, feature: str, payload: dict[str, Any], error: Exception) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO pending_discord_payloads(feature_key,payload,last_error,created_at) VALUES(?,?,?,?)",
                (feature, raw, str(error)[:1000], int(time.time())),
            )

    def due(self, limit: int = 100) -> list[tuple[int, str, str, int]]:
        with self.lock, self.connect() as db:
            return list(db.execute(
                "SELECT id,feature_key,payload,attempts FROM pending_discord_payloads "
                "WHERE next_attempt_at<=? ORDER BY id LIMIT ?", (int(time.time()), limit)
            ))

    def sent(self, row_id: int) -> None:
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM pending_discord_payloads WHERE id=?", (row_id,))

    def failed(self, row_id: int, attempts: int, error: Exception) -> None:
        attempts += 1
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        with self.lock, self.connect() as db:
            db.execute(
                "UPDATE pending_discord_payloads SET attempts=?,last_error=?,next_attempt_at=? WHERE id=?",
                (attempts, str(error)[:1000], int(time.time()) + delay, row_id),
            )


class Bridge:
    def __init__(self, config_path: Path, store: Store):
        self.config_path = config_path
        self.store = store
        self._mtime = 0.0
        self._config: dict[str, Any] = {}

    def config(self) -> dict[str, Any]:
        mtime = self.config_path.stat().st_mtime
        if mtime != self._mtime:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data.get("tokens"), dict):
                raise ValueError("tokens is required")
            self._config, self._mtime = data, mtime
        return self._config

    def deliver(self, feature: str, payload: dict[str, Any]) -> None:
        config = self.config()
        token = str(config["tokens"].get(feature) or "")
        if not token:
            raise KeyError(f"unknown feature: {feature}")
        base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/{token}", data=raw, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "mfu-notification-bridge/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            result = json.loads(body)
            if response.status >= 300 or not result.get("ok"):
                raise RuntimeError(result.get("error") or f"HTTP {response.status}")

    def accept(self, feature: str, payload: dict[str, Any]) -> bool:
        payload.setdefault("dedup_key", "bridge:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:40])
        try:
            self.deliver(feature, payload)
            return True
        except Exception as exc:
            self.store.enqueue(feature, payload, exc)
            LOG.warning("queued feature=%s error=%s", feature, exc)
            return False

    def drain(self) -> None:
        for row_id, feature, raw, attempts in self.store.due():
            try:
                self.deliver(feature, json.loads(raw))
                self.store.sent(row_id)
            except Exception as exc:
                self.store.failed(row_id, attempts, exc)


def handler_for(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            feature = self.path.removeprefix("/discord/").strip("/")
            if not feature or self.path == feature:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 2 * 1024 * 1024:
                    raise ValueError("invalid payload size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
                bridge.config()["tokens"][feature]
                delivered = bridge.accept(feature, payload)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self.send_error(400, str(exc))
                return
            self.send_response(204)
            self.send_header("X-MFU-Delivery", "sent" if delivered else "queued")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="MFU common notification bridge")
    parser.add_argument("--config", default=os.getenv("MFU_NOTIFICATION_BRIDGE_CONFIG", "/etc/mfu-notification-bridge.json"))
    parser.add_argument("--queue", default=os.getenv("MFU_NOTIFICATION_BRIDGE_QUEUE", "/var/lib/mfu-notification-bridge/queue.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = Bridge(Path(args.config), Store(Path(args.queue)))
    bridge.config()

    def retry_loop() -> None:
        while True:
            time.sleep(30)
            bridge.drain()

    threading.Thread(target=retry_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(bridge))
    LOG.info("listening on %s:%s", args.host, args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
