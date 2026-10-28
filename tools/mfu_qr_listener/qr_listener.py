#!/usr/bin/env python3
"""Read the MFU USB keyboard-wedge scanner and report scans to the timer API."""

from __future__ import annotations

import time

import requests
from evdev import InputDevice, categorize, ecodes

from scan_decoder import ScanDecoder, classify_code, normalize_scanned_url


MFU_BASE_URL = "http://127.0.0.1:8080"
INPUT_DEVICE_PATH = "/dev/input/by-id/usb-Honeywell_Imaging___Mobility_1900_16319B3C95-event-kbd"
ENABLE_SCAN_REPORT = True
SCAN_REPORT_PATH = "/api/timer/scan-report"


def post_json(path: str, payload: dict | None = None) -> bool:
    try:
        response = requests.post(MFU_BASE_URL + path, json=payload or {}, timeout=3)
        response.raise_for_status()
        print(f"[OK] POST {path} -> {response.status_code} {response.text}", flush=True)
        return True
    except Exception as exc:
        print(f"[ERR] POST {path} failed: {exc}", flush=True)
        return False


def report_scan(raw: str, kind: str, note: str = "") -> None:
    if ENABLE_SCAN_REPORT:
        post_json(SCAN_REPORT_PATH, {"raw": raw, "kind": kind, "note": note})


def handle_code(code: str) -> None:
    value = normalize_scanned_url(code.strip())
    if not value:
        return
    kind = classify_code(value)
    print(f"[INFO] scanned kind={kind} length={len(value)}", flush=True)
    report_scan(value, kind)

    if kind == "START":
        post_json("/api/timer/pending-start")
    elif kind == "JAN":
        post_json("/api/timer/pending-set-by-jan", {"jan": value})
    elif kind == "MIN":
        minutes = int(value)
        post_json("/api/timer/pending-set", {"seconds": minutes * 60, "label": f"{minutes}分"})
    elif kind == "SEC":
        seconds = int(value[:-1])
        post_json("/api/timer/pending-set", {"seconds": seconds, "label": f"{seconds}秒"})


def main() -> None:
    while True:
        try:
            device = InputDevice(INPUT_DEVICE_PATH)
            print(f"[INFO] open device: {device} path={INPUT_DEVICE_PATH}", flush=True)
            decoder = ScanDecoder()
            for event in device.read_loop():
                if event.type != ecodes.EV_KEY:
                    continue
                keycode = categorize(event).keycode
                if not isinstance(keycode, str):
                    continue
                scanned = decoder.feed(keycode, event.value)
                if scanned is not None:
                    handle_code(scanned)
        except Exception as exc:
            print(f"[ERR] device loop error: {exc}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
