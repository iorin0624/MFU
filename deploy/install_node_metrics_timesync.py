#!/usr/bin/env python3
"""Idempotently add time-sync data to the existing node metrics endpoint."""

from datetime import datetime
from pathlib import Path
import os
import py_compile
import shutil


AGENT = Path("/opt/node-metrics/node_metrics_agent.py")
MODULE = Path("/opt/node-metrics/node_metrics_timesync.py")
BACKUP_DIR = Path("/root/node-metrics-backups")
IMPORT_LINE = "from node_metrics_timesync import timesync_info"
PAYLOAD_LINE = '        "time_sync": timesync_info(),'


def main():
    source = AGENT.read_text(encoding="utf-8")
    changed = False

    if IMPORT_LINE not in source:
        marker = "import psutil, subprocess, socket, time, os, re, json, shutil"
        if marker not in source:
            raise SystemExit("node metrics import marker was not found")
        source = source.replace(marker, marker + "\n" + IMPORT_LINE, 1)
        changed = True

    if PAYLOAD_LINE not in source:
        marker = '        "runtime": runtime_info(), # 追加: 起動日時/稼働時間（UTC互換 + JST追加）'
        if marker not in source:
            raise SystemExit("node metrics payload marker was not found")
        source = source.replace(marker, marker + "\n" + PAYLOAD_LINE, 1)
        changed = True

    if changed:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)
        backup = BACKUP_DIR / f"node_metrics_agent.py.{datetime.now():%Y%m%d%H%M%S}"
        shutil.copy2(AGENT, backup)
        os.chmod(backup, 0o600)
        AGENT.write_text(source, encoding="utf-8")

    py_compile.compile(str(MODULE), doraise=True)
    py_compile.compile(str(AGENT), doraise=True)
    print("node metrics time-sync support is installed")


if __name__ == "__main__":
    main()
