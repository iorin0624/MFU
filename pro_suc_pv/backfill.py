"""One-off, idempotent import of retained historical public page views.

Run on the MFU server with ``python -m app.pro_suc_pv.backfill``. The Pi's
existing Apache logs are read over SSH; no Apache configuration is modified.
"""

from collections import defaultdict
from datetime import datetime
import subprocess

from app.pro_suc_pv.analytics import (
    JST, _INSERT_SQL, ensure_schema, legacy_source, make_event,
    parse_apache_page_view,
)
from app.utils.db import get_db


def _batch_insert(events):
    if not events:
        return 0
    db = get_db()
    try:
        cur = db.cursor()
        cur.executemany(_INSERT_SQL, events)
        db.commit()
        return len(events)
    finally:
        db.close()


def _read_pi_log(command):
    process = subprocess.Popen(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         "192.168.103.15", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        yield line.rstrip("\n")
    error = process.stderr.read() if process.stderr else ""
    if process.wait() != 0:
        raise RuntimeError("PiのApacheログを読み取れません: " + error[:200])


def run():
    ensure_schema()
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT site, MIN(viewed_at) AS first_live FROM pro_suc_page_views WHERE origin='live' GROUP BY site")
        first_live = {row["site"]: row["first_live"] for row in cur.fetchall()}
    finally:
        db.close()
    cutoff = {site: first_live.get(site) or datetime.now(JST).replace(tzinfo=None) for site in ("pro", "suc")}
    oldest_apache = {}
    counts = defaultdict(int)
    logs = (
        ("pro", "zcat -f /var/log/apache2/pro_access.log*"),
        ("suc", "zcat -f /var/log/apache2/other_vhosts_access.log*"),
    )
    for site, command in logs:
        batch = []
        for line in _read_pi_log(command):
            if site == "suc" and not line.startswith("suc.iori0624.jp:"):
                continue
            event = parse_apache_page_view(line, site)
            if event is None or event["viewed_at"] >= cutoff[site]:
                continue
            oldest_apache[site] = min(oldest_apache.get(site, event["viewed_at"]), event["viewed_at"])
            batch.append(event)
            if len(batch) >= 250:
                counts[f"{site}_apache"] += _batch_insert(batch)
                batch.clear()
        counts[f"{site}_apache"] += _batch_insert(batch)

    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        for site, path in (("pro", "/profile"), ("suc", "/suc/")):
            end = oldest_apache.get(site, cutoff[site])
            cur.execute("""
                SELECT id,log_date,ip,ua,referer,log_text FROM logs
                WHERE method='GET' AND path=%s AND status=200 AND log_date < %s
                ORDER BY id
            """, (path, end))
            batch = []
            for row in cur:
                if "[INAPP_WARNING]" in (row["log_text"] or ""):
                    continue
                event = make_event(
                    site=site, viewed_at=row["log_date"], ip=row["ip"] or "-",
                    ua=row["ua"] or "", referer=row["referer"] or "",
                    src=legacy_source(row["log_text"]), origin="legacy_db",
                    legacy_key="db:" + str(row["id"]),
                )
                batch.append(event)
                if len(batch) >= 250:
                    counts[f"{site}_db"] += _batch_insert(batch)
                    batch.clear()
            counts[f"{site}_db"] += _batch_insert(batch)
    finally:
        db.close()
    print("Backfill examined (idempotent): " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print("Apache coverage begins: " + ", ".join(f"{site}={oldest_apache.get(site, 'none')}" for site in ("pro", "suc")))


if __name__ == "__main__":
    run()
