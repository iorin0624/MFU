#!/usr/bin/env python3
"""Chrony status endpoint for the MFU node metrics agent.

Only the commands listed in ``CHRONY_COMMANDS`` can be executed.  Request
parameters are deliberately ignored so this endpoint cannot become a remote
shell.
"""

from datetime import datetime, timezone
import os
import re
import shutil
import subprocess
import time

from flask import jsonify


CHRONY_COMMANDS = {
    "tracking": ["chronyc", "tracking"],
    "sources": ["chronyc", "sources", "-v"],
    "selectdata": ["chronyc", "selectdata", "-v"],
    "clients": ["chronyc", "clients"],
    "clients_numeric": ["chronyc", "-n", "clients"],
    "serverstats": ["chronyc", "serverstats"],
}


def _run_fixed(command, timeout=5):
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": "chronyc command timed out",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }


def _parse_tracking_value(text, key):
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        current_key, value = (part.strip() for part in line.split(":", 1))
        if current_key == key:
            return value
    return ""


def _parse_system_offset_seconds(tracking_text):
    value = _parse_tracking_value(tracking_text, "System time")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    if not match:
        return None
    offset = float(match.group(0))
    # chronyc describes the local system clock relative to NTP time.
    # Positive means the Pi clock is fast; negative means it is slow.
    if "slow" in value.lower():
        return -abs(offset)
    if "fast" in value.lower():
        return abs(offset)
    return offset


def register_chrony_endpoint(app):
    @app.get("/chrony")
    def chrony_status():
        if shutil.which("chronyc") is None:
            return jsonify({
                "ok": False,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "error": "chronyc is not installed",
                "commands": {},
            }), 503

        commands = {
            name: _run_fixed(command)
            for name, command in CHRONY_COMMANDS.items()
        }
        required = ("tracking", "sources", "selectdata", "clients", "serverstats")
        ok = all(commands[name]["ok"] for name in required)
        return jsonify({
            "ok": ok,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename,
            "commands": commands,
        }), 200 if ok else 503

    @app.get("/chrony/time")
    def chrony_time_sample():
        """Return one lightweight, high-resolution Pi/NTP time sample."""
        if shutil.which("chronyc") is None:
            return jsonify({"ok": False, "error": "chronyc is not installed"}), 503

        started_ns = time.time_ns()
        tracking = _run_fixed(CHRONY_COMMANDS["tracking"], timeout=3)
        finished_ns = time.time_ns()
        sample_ns = (started_ns + finished_ns) // 2
        offset_seconds = _parse_system_offset_seconds(tracking.get("stdout"))
        reference_id = _parse_tracking_value(tracking.get("stdout"), "Reference ID")
        leap_status = _parse_tracking_value(tracking.get("stdout"), "Leap status")
        stratum_value = _parse_tracking_value(tracking.get("stdout"), "Stratum")
        try:
            stratum = int(stratum_value)
        except (TypeError, ValueError):
            stratum = None

        ok = bool(
            tracking.get("ok")
            and offset_seconds is not None
            and reference_id
            and leap_status == "Normal"
        )
        payload = {
            "ok": ok,
            "sample_time_unix_ns": sample_ns,
            "sample_time_unix_ms": sample_ns / 1_000_000,
            "response_time_unix_ns": time.time_ns(),
            "ntp_time_unix_ns": (
                sample_ns - int(offset_seconds * 1_000_000_000)
                if offset_seconds is not None else None
            ),
            "ntp_time_unix_ms": (
                (sample_ns / 1_000_000) - (offset_seconds * 1_000)
                if offset_seconds is not None else None
            ),
            "system_offset_seconds": offset_seconds,
            "reference_id": reference_id,
            "stratum": stratum,
            "leap_status": leap_status,
            "command_duration_ms": round((finished_ns - started_ns) / 1_000_000, 3),
            "sampled_at": datetime.fromtimestamp(
                sample_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat(),
        }
        if not tracking.get("ok"):
            payload["error"] = tracking.get("stderr") or "chronyc tracking failed"
        return jsonify(payload), 200 if ok else 503
