"""Expose systemd-timesyncd status through the node metrics agent."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone


_OFFSET_RE = re.compile(r"^([+-]?[0-9]+(?:\.[0-9]+)?)\s*(ns|us|µs|ms|s)$", re.I)
_UNIT_SECONDS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0}


def parse_timesync_status(text):
    fields = {}
    for raw_line in (text or "").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        fields[key.strip()] = value.strip()

    offset_text = fields.get("Offset", "")
    match = _OFFSET_RE.match(offset_text)
    offset_seconds = None
    if match:
        offset_seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]

    try:
        stratum = int(fields.get("Stratum", ""))
    except ValueError:
        stratum = None

    return {
        "available": bool(fields),
        "server": fields.get("Server"),
        "stratum": stratum,
        "leap": fields.get("Leap"),
        "offset": offset_text or None,
        "offset_seconds": offset_seconds,
        "delay": fields.get("Delay"),
        "jitter": fields.get("Jitter"),
        "poll_interval": fields.get("Poll interval"),
    }


def _run(command):
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=3,
        check=False,
        env=env,
    )


def timesync_info():
    result = {
        "available": False,
        "synchronized": False,
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    try:
        sync = _run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
        result["synchronized"] = sync.returncode == 0 and sync.stdout.strip().lower() == "yes"
        status = _run(["timedatectl", "timesync-status"])
        if status.returncode != 0:
            result["error"] = (status.stderr or "timesync status unavailable").strip()[:200]
            return result
        result.update(parse_timesync_status(status.stdout))
        return result
    except Exception as exc:
        result["error"] = str(exc)[:200]
        return result
