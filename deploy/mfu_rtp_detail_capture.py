#!/usr/bin/env python3
from __future__ import annotations

import collections
import ipaddress
import json
import os
import re
import select
import signal
import subprocess
import sys
import time

from mfu_rtp_diagnostic_sender import _config, _logger, _read_secret, signed_post


DEFAULT_SESSION_URL = "http://192.168.103.16:8080/internal/phone-diagnostics/sessions"
LINE_RE = re.compile(
    r"^(?P<ts>\d+(?:\.\d+)?)\s+(?:(?:\S+)\s+(?:In|Out)\s+)?IP6?\s+"
    r"(?P<src>\S+)\s+>\s+(?P<dst>\S+):\s+UDP,\s+length\s+(?P<length>\d+)"
)
stop_requested = False


def _signal_handler(_signum, _frame):
    global stop_requested
    stop_requested = True


def _host(endpoint: str) -> str:
    value = endpoint.rsplit(".", 1)[0]
    return value.strip("[]")


def _summarize(stream_times: dict[tuple[str, str], list[float]], contact_ips: set[str]) -> dict:
    directions: dict[str, list[tuple[tuple[str, str], list[float]]]] = {"to_phone": [], "from_phone": []}
    for stream, timestamps in stream_times.items():
        src, dst = stream
        if _host(dst) in contact_ips:
            directions["to_phone"].append((stream, timestamps))
        elif _host(src) in contact_ips:
            directions["from_phone"].append((stream, timestamps))

    result: dict[str, object] = {}
    for direction, streams in directions.items():
        dominant = max(streams, key=lambda item: len(item[1]), default=(('', ''), []))
        timestamps = dominant[1]
        gaps = [(right - left) * 1000.0 for left, right in zip(timestamps, timestamps[1:])]
        suffix = "to_phone" if direction == "to_phone" else "from_phone"
        result[f"packet_count_{suffix}"] = len(timestamps)
        result[f"max_gap_{suffix}_ms"] = round(max(gaps), 3) if gaps else None
        result[f"gaps_{suffix}_over_60ms"] = sum(gap > 60.0 for gap in gaps)
        result[f"gaps_{suffix}_over_100ms"] = sum(gap > 100.0 for gap in gaps)
        result[f"gaps_{suffix}_over_200ms"] = sum(gap > 200.0 for gap in gaps)
        result[f"dominant_stream_{suffix}"] = {"src": dominant[0][0], "dst": dominant[0][1]}
    result["stream_count"] = len(stream_times)
    return result


def main() -> int:
    log = _logger()
    if len(sys.argv) != 4:
        log.error("detail capture requires session_id, contact_ip and duration")
        return 2
    session_id, contact_text, duration_text = sys.argv[1:]
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        return 2
    try:
        contact_ips = {str(ipaddress.ip_address(item.strip())) for item in contact_text.split(",") if item.strip()}
        if not contact_ips or len(contact_ips) > 4:
            raise ValueError("invalid contact count")
        duration = min(1800, max(60, int(duration_text)))
    except ValueError:
        return 2

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _signal_handler)

    status = "completed"
    error = ""
    streams: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    packet_sizes: dict[tuple[str, str], int] = collections.defaultdict(int)
    process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        subprocess.run(["/usr/sbin/asterisk", "-rx", "rtcp set stats on"], check=False, timeout=5, capture_output=True, text=True)
        host_filter = " or ".join(f"host {item}" for item in sorted(contact_ips))
        capture_filter = f"({host_filter}) and udp portrange 10000-20000"
        process = subprocess.Popen(
            ["/usr/bin/tcpdump", "-i", "any", "-n", "-tt", "-l", capture_filter],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        assert process.stdout is not None
        while time.monotonic() - started < duration and not stop_requested:
            readable, _, _ = select.select([process.stdout], [], [], 0.5)
            if not readable:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            match = LINE_RE.match(line.strip())
            if not match:
                continue
            stream = (match.group("src"), match.group("dst"))
            streams[stream].append(float(match.group("ts")))
            packet_sizes[stream] += int(match.group("length"))
        if stop_requested:
            status = "stopped"
    except Exception as exc:
        status = "failed"
        error = str(exc)[:500]
        log.error("detail capture failed: %s", exc)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        subprocess.run(["/usr/sbin/asterisk", "-rx", "rtcp set stats off"], check=False, timeout=5, capture_output=True, text=True)

    summary = _summarize(streams, contact_ips)
    summary["capture_seconds"] = round(time.monotonic() - started, 3)
    summary["total_packets"] = sum(len(items) for items in streams.values())
    summary["total_udp_bytes"] = sum(packet_sizes.values())
    payload = {
        "session_id": session_id,
        "status": status,
        "contact_ip": ",".join(sorted(contact_ips)),
        "summary": summary,
        "error": error,
    }
    try:
        signed_post(_config().get("SESSION_API_URL", DEFAULT_SESSION_URL), payload, _read_secret())
    except Exception as exc:
        log.error("detail session delivery failed: %s", exc)
        return 1
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
