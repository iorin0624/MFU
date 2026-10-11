#!/usr/bin/env python3
"""Insert one synthetic CDR row for a rejected international inbound call."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import syslog
import time
from pathlib import Path


MYSQL = "/usr/bin/mysql"
MYSQL_CONFIG = "/etc/asterisk/mfu-blacklist-cdr.cnf"
DONE_DIR = Path("/var/lib/asterisk/mfu_international_cdr_done")
MARKER_RETENTION_SECONDS = 90 * 24 * 60 * 60
DISPLAY_NAME = "海外番号拒否"
USERFIELD = "INTERNATIONAL_REJECT"


def sanitize_caller(value: str) -> str:
    caller = (value or "").strip()
    if not re.fullmatch(r"\+[0-9]{6,15}", caller):
        raise ValueError("invalid international caller")
    return caller


def sanitize_did(value: str) -> str:
    return re.sub(r"\D", "", value or "")[:50]


def sql_text(value: str) -> str:
    if not value:
        return "''"
    return f"(CONVERT(0x{value.encode('utf-8').hex()} USING utf8mb4) COLLATE utf8mb4_unicode_ci)"


def build_sql(caller: str, did: str, unique_id: str) -> str:
    clid = f'"{DISPLAY_NAME}" <{caller}>'
    destination = did or "s"
    values = {key: sql_text(value) for key, value in {
        "clid": clid,
        "caller": caller,
        "destination": destination,
        "context": "custom-callerid-whitelist",
        "lastapp": "Hangup",
        "lastdata": "21",
        "disposition": "FAILED",
        "uniqueid": unique_id,
        "userfield": USERFIELD,
        "did": did,
        "display_name": DISPLAY_NAME,
    }.items()}
    return f"""
INSERT INTO cdr
    (calldate, clid, src, dst, dcontext, channel, dstchannel, lastapp, lastdata,
     duration, billsec, disposition, amaflags, accountcode, uniqueid, userfield,
     did, recordingfile, cnum, cnam, outbound_cnum, outbound_cnam, dst_cnam,
     linkedid, peeraccount, sequence)
SELECT
    NOW(), {values['clid']}, {values['caller']}, {values['destination']}, {values['context']}, '', '',
    {values['lastapp']}, {values['lastdata']}, 0, 0, {values['disposition']}, 0, '',
    {values['uniqueid']}, {values['userfield']}, {values['did']}, '', {values['caller']},
    {values['display_name']}, '', '', '', {values['uniqueid']}, '', 0
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM cdr WHERE uniqueid={values['uniqueid']});
""".strip() + "\n"


def claim(unique_id: str) -> Path | None:
    DONE_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    marker = DONE_DIR / f"{hashlib.sha256(unique_id.encode('ascii')).hexdigest()}.done"
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write("pending\n")
    return marker


def prune_markers() -> None:
    cutoff = time.time() - MARKER_RETENTION_SECONDS
    for marker in DONE_DIR.glob("*.done"):
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink()
        except OSError:
            continue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("caller")
    parser.add_argument("did")
    parser.add_argument("unique_id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        caller = sanitize_caller(args.caller)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    did = sanitize_did(args.did)
    unique_id = re.sub(r"[^A-Za-z0-9_.:-]", "", args.unique_id)[:32]
    if not unique_id:
        print("invalid unique ID", file=sys.stderr)
        return 2
    sql = build_sql(caller, did, unique_id)
    if args.dry_run:
        print(f'"{DISPLAY_NAME}" <{caller}> dst={did or "s"}')
        return 0
    marker = claim(unique_id)
    if marker is None:
        return 0
    try:
        result = subprocess.run(
            [MYSQL, f"--defaults-extra-file={MYSQL_CONFIG}", "--batch", "--skip-column-names"],
            input=sql,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "mysql failed").strip()[:300])
        marker.write_text("inserted\n", encoding="ascii")
        prune_markers()
        syslog.syslog(syslog.LOG_INFO, f"mfu-international-cdr inserted caller={caller} uniqueid={unique_id}")
    except Exception as exc:
        try:
            marker.unlink()
        except OSError:
            pass
        syslog.syslog(syslog.LOG_ERR, f"mfu-international-cdr failed: {type(exc).__name__}")
        print(f"CDR insert failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
