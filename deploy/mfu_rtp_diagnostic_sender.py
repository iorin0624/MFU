#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.request


SECRET_FILE = "/etc/mfu-phone-diagnostics.secret"
CONFIG_FILE = "/etc/mfu-phone-diagnostics.conf"
DEFAULT_URL = "http://192.168.103.16:8080/internal/phone-diagnostics/calls"
FIELDS = (
    "version", "event_id", "uniqueid", "linkedid", "started_at", "ended_at",
    "duration", "billsec", "direction", "endpoint", "remote_number", "channel_name",
    "read_codec", "write_codec", "rtp_source", "rtp_dest", "sip_remote_addr", "sip_call_id",
    "qos_all", "qos_jitter", "qos_loss", "qos_rtt", "qos_mes",
)


def _logger() -> logging.Logger:
    logger = logging.getLogger("mfu-rtp-diagnostics")
    logger.setLevel(logging.INFO)
    try:
        logger.addHandler(logging.handlers.SysLogHandler(address="/dev/log"))
    except OSError:
        logger.addHandler(logging.StreamHandler())
    return logger


def _read_secret() -> str:
    with open(SECRET_FILE, "r", encoding="utf-8") as handle:
        secret = handle.read().strip()
    if len(secret) < 32:
        raise RuntimeError("diagnostic secret is missing or too short")
    return secret


def _config() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return result


def _decode(argument: str) -> dict[str, str]:
    if len(argument) > 100_000:
        raise ValueError("encoded payload is too large")
    raw = base64.b64decode(argument.encode("ascii"), validate=True).decode("utf-8")
    values = raw.split("|")
    if len(values) != len(FIELDS):
        raise ValueError(f"unexpected payload field count: {len(values)}")
    payload = dict(zip(FIELDS, values))
    if payload["version"] != "1" or payload["endpoint"] != "10610":
        raise ValueError("unsupported payload")
    return payload


def signed_post(url: str, payload: dict, secret: str, *, timeout: int = 8) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-MFU-Timestamp": timestamp,
            "X-MFU-Signature": signature,
            "User-Agent": "mfu-rtp-diagnostics/1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"diagnostic API returned HTTP {response.status}")


def main() -> int:
    log = _logger()
    if len(sys.argv) != 2:
        log.error("call diagnostic sender requires one payload")
        return 2
    try:
        payload = _decode(sys.argv[1])
        secret = _read_secret()
        url = _config().get("CALL_API_URL", DEFAULT_URL)
        last_error: Exception | None = None
        for delay in (0, 2, 8):
            if delay:
                time.sleep(delay)
            try:
                signed_post(url, payload, secret)
                return 0
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                last_error = exc
        raise RuntimeError(f"delivery failed: {last_error}")
    except Exception as exc:
        log.error("call diagnostic sender failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
