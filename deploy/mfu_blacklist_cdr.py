#!/usr/bin/env python3
"""Insert one synthetic FreePBX CDR row for a blacklist action."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import syslog
import time
import unicodedata
from pathlib import Path


MYSQL = "/usr/bin/mysql"
MYSQL_CONFIG = "/etc/asterisk/mfu-blacklist-cdr.cnf"
DONE_DIR = Path("/var/lib/asterisk/mfu_blacklist_cdr_done")
MAX_NAME_LENGTH = 32
MARKER_RETENTION_SECONDS = 90 * 24 * 60 * 60
ACTIONS = {
    "hangup": ("Hangup", "21", "FAILED", "BLACKLIST"),
    "ring_until_caller_hangup": ("Ringing", "caller_hangup", "NO ANSWER", "BLACKLIST_RING"),
    "ring_15": ("Ringing", "15 seconds", "NO ANSWER", "BLACKLIST_RING_15"),
    "busy": ("Hangup", "17", "BUSY", "BLACKLIST_BUSY"),
}


def sanitize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    if not re.fullmatch(r"0\d{9,10}", digits):
        raise ValueError("invalid caller number")
    return digits


def sanitize_did(value: str) -> str:
    return re.sub(r"\D", "", value or "")[:50]


def decode_name(value: str, fallback: str) -> str:
    if not value:
        return fallback
    try:
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("non-canonical base64")
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return fallback
    safe = "".join(char for char in text.strip() if not unicodedata.category(char).startswith("C"))
    return safe[:MAX_NAME_LENGTH] or fallback


def sql_text(value: str) -> str:
    if not value:
        return "''"
    return f"(CONVERT(0x{value.encode('utf-8').hex()} USING utf8mb4) COLLATE utf8mb4_unicode_ci)"


def build_sql(
    caller: str,
    display_name: str,
    did: str,
    unique_id: str,
    action: str,
    duration: int,
) -> str:
    lastapp, lastdata, disposition, userfield = ACTIONS[action]
    clid = f'"{display_name}" <{caller}>'
    destination = did or "s"
    values = {
        "clid": sql_text(clid),
        "caller": sql_text(caller),
        "destination": sql_text(destination),
        "context": sql_text("custom-callerid-whitelist"),
        "lastapp": sql_text(lastapp),
        "lastdata": sql_text(lastdata),
        "disposition": sql_text(disposition),
        "uniqueid": sql_text(unique_id),
        "userfield": sql_text(userfield),
        "did": sql_text(did),
        "display_name": sql_text(display_name),
    }
    return f"""
INSERT INTO cdr
    (calldate, clid, src, dst, dcontext, channel, dstchannel, lastapp, lastdata,
     duration, billsec, disposition, amaflags, accountcode, uniqueid, userfield,
     did, recordingfile, cnum, cnam, outbound_cnum, outbound_cnam, dst_cnam,
     linkedid, peeraccount, sequence)
SELECT
    DATE_SUB(NOW(), INTERVAL {duration} SECOND), {values['clid']}, {values['caller']},
    {values['destination']}, {values['context']}, '', '', {values['lastapp']},
    {values['lastdata']}, {duration}, 0, {values['disposition']}, 0, '',
    {values['uniqueid']}, {values['userfield']}, {values['did']}, '', {values['caller']},
    {values['display_name']}, '', '', '', {values['uniqueid']}, '', 0
FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM cdr WHERE uniqueid={values['uniqueid']}
);
""".strip() + "\n"


def claim(unique_id: str) -> Path | None:
    DONE_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    digest = hashlib.sha256(unique_id.encode("ascii", errors="ignore")).hexdigest()
    marker = DONE_DIR / f"{digest}.done"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("caller")
    parser.add_argument("name_b64")
    parser.add_argument("did")
    parser.add_argument("unique_id")
    parser.add_argument("action", nargs="?", default="hangup", choices=sorted(ACTIONS))
    parser.add_argument("duration", nargs="?", default="0")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        caller = sanitize_phone(args.caller)
        duration = max(0, min(int(args.duration), 7 * 24 * 60 * 60))
    except (ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    did = sanitize_did(args.did)
    unique_id = re.sub(r"[^A-Za-z0-9_.:-]", "", args.unique_id)[:32]
    if not unique_id:
        print("invalid unique ID", file=sys.stderr)
        return 2
    name = decode_name(args.name_b64, caller)
    display_name = f"BL　{name}"
    sql = build_sql(caller, display_name, did, unique_id, args.action, duration)

    if args.dry_run:
        print(json.dumps(
            {
                "clid": f'"{display_name}" <{caller}>',
                "dst": did or "s",
                "action": args.action,
                "duration": duration,
                "disposition": ACTIONS[args.action][2],
            },
            ensure_ascii=False,
        ))
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
        syslog.syslog(
            syslog.LOG_INFO,
            f"mfu-blacklist-cdr inserted caller={caller} uniqueid={unique_id} action={args.action}",
        )
    except Exception as exc:
        try:
            marker.unlink()
        except OSError:
            pass
        syslog.syslog(syslog.LOG_ERR, f"mfu-blacklist-cdr failed: {type(exc).__name__}")
        print(f"CDR insert failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
