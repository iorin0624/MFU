"""Read the SUC source calendars through Baikal CalDAV, never its database.

The CLI is intended to run once per minute from a systemd timer.  A failed
CalDAV read must not change the last successfully published MFU snapshot.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
DAV_BASE = "https://mail.iori0624.jp/baikal/dav.php/calendars/iori0624"
CALENDARS = {"main": "default", "holidays": "japan-holidays"}
DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _password() -> str:
    path = Path(os.environ.get("SUC_BAIKAL_PASSWORD_FILE", "/mnt/mfu/secrets/suc-baikal-password"))
    password = path.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("Baikal credential file is empty")
    return password


def _request(method: str, url: str, body: str, password: str, depth: str) -> ET.Element:
    response = requests.request(
        method,
        url,
        data=body.encode("utf-8"),
        auth=("iori0624", password),
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
        timeout=(5, 25),
        allow_redirects=False,
    )
    if response.status_code != 207:
        # Never include server response bodies: they can contain calendar data.
        raise RuntimeError(f"CalDAV {method} failed: HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("CalDAV response exceeds size limit")
    return ET.fromstring(response.content)


def collection_token(kind: str, password: str) -> str:
    url = f"{DAV_BASE}/{CALENDARS[kind]}/"
    root = _request(
        "PROPFIND", url,
        '<d:propfind xmlns:d="DAV:"><d:prop><d:sync-token/></d:prop></d:propfind>',
        password, "0",
    )
    tokens = [el.text for el in root.iter(DAV + "sync-token") if el.text]
    if len(tokens) != 1:
        raise RuntimeError("CalDAV sync token missing or ambiguous")
    return tokens[0]


def collection_objects(kind: str, password: str) -> list[str]:
    url = f"{DAV_BASE}/{CALENDARS[kind]}/"
    body = (
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT"/>'
        '</c:comp-filter></c:filter></c:calendar-query>'
    )
    root = _request("REPORT", url, body, password, "1")
    objects = []
    for item in root.findall(DAV + "response"):
        propstats = item.findall(DAV + "propstat")
        if not propstats:
            raise RuntimeError("CalDAV object has no propstat")
        matched = False
        for propstat in propstats:
            status = propstat.findtext(DAV + "status", "")
            if " 200 " not in status:
                continue
            prop = propstat.find(DAV + "prop")
            calendar_data = prop.find(CALDAV + "calendar-data") if prop is not None else None
            if calendar_data is not None and calendar_data.text:
                objects.append(calendar_data.text)
                matched = True
        if not matched:
            raise RuntimeError("CalDAV object is missing calendar-data")
    return objects


def _as_datetime(value, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=JST) if value.tzinfo is None else value.astimezone(JST)
    if isinstance(value, date):
        return datetime.combine(value, time.min, JST)
    raise ValueError("Unexpected calendar date value")


def expand_days(objects: list[str], start: date, end: date, *, holidays: bool = False):
    """Return busy dates or holiday-name mapping for an inclusive JST range."""
    from icalendar import Calendar
    import recurring_ical_events

    left = datetime.combine(start, time.min, JST)
    right = datetime.combine(end + timedelta(days=1), time.min, JST)
    result = {} if holidays else set()
    for raw in objects:
        calendar = Calendar.from_ical(raw)
        for event in recurring_ical_events.of(calendar).between(left, right):
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            begin_prop = event.get("DTSTART")
            if begin_prop is None:
                raise ValueError("VEVENT without DTSTART")
            begin = _as_datetime(begin_prop.dt)
            end_prop = event.get("DTEND")
            if end_prop is not None:
                finish = _as_datetime(end_prop.dt)
            elif isinstance(begin_prop.dt, date) and not isinstance(begin_prop.dt, datetime):
                finish = begin + timedelta(days=1)
            else:
                finish = begin + timedelta(seconds=1)
            if finish <= begin:
                finish = begin + timedelta(seconds=1)
            day = max(start, begin.date())
            last = min(end, (finish - timedelta(microseconds=1)).date())
            while day <= last:
                if holidays:
                    title = str(event.get("SUMMARY", "")).strip()
                    if title:
                        result[day.isoformat()] = title
                else:
                    # All-day events count as busy even if TRANSP:TRANSPARENT.
                    result.add(day.isoformat())
                day += timedelta(days=1)
    return result


def read_range(start: date, end: date, *, kind: str, password: str | None = None):
    secret = password or _password()
    return expand_days(collection_objects(kind, secret), start, end, holidays=(kind == "holidays"))


def _meta(cursor, key: str) -> str | None:
    cursor.execute("SELECT v FROM su_calendar_meta WHERE k=%s", (key,))
    row = cursor.fetchone()
    return row[0] if row else None


def _put_meta(cursor, key: str, value: str) -> None:
    cursor.execute("REPLACE INTO su_calendar_meta (k, v) VALUES (%s, %s)", (key, value))


def _month_horizon(today: date) -> tuple[date, date]:
    first = today.replace(day=1)
    offset = today.year * 12 + today.month - 1 + 5
    year, month_zero = divmod(offset, 12)
    return first, date(year, month_zero + 1, monthrange(year, month_zero + 1)[1])


def synchronize(db, *, today: date | None = None, apply: bool = False) -> dict:
    """Compare source calendars and atomically publish their MFU snapshots.

    No SQL mutations occur before *both* calendars have been fetched and parsed.
    Existing manual busy rows (synced_busy=0) are never overwritten or cleared.
    """
    today = today or datetime.now(JST).date()
    first, last = _month_horizon(today)
    holiday_first = date(today.year, 1, 1)
    holiday_last = date(today.year + 1, 12, 31)
    cursor = db.cursor()
    try:
        old_tokens = {kind: _meta(cursor, f"suc_baikal_{kind}_token") for kind in CALENDARS}
        last_full = _meta(cursor, "suc_baikal_last_full_day")
        cursor.execute(
            "SELECT day_date, status, synced_busy FROM su_calendar_days "
            "WHERE day_date BETWEEN %s AND %s", (first, last)
        )
        current = {row[0].isoformat(): (row[1], bool(row[2])) for row in cursor.fetchall()}
        try:
            cursor.execute(
                "SELECT day_date, name FROM su_calendar_holidays "
                "WHERE day_date BETWEEN %s AND %s", (holiday_first, holiday_last)
            )
            old_holidays = {row[0].isoformat(): row[1] for row in cursor.fetchall()}
        except Exception as exc:
            # The table is first created on the initial successful --apply.
            if getattr(exc, "errno", None) != 1146:
                raise
            old_holidays = {}

        secret = _password()
        tokens = {kind: collection_token(kind, secret) for kind in CALENDARS}
        full = last_full != today.isoformat()
        changed = {kind: full or tokens[kind] != old_tokens[kind] for kind in CALENDARS}
        # After a calendar rolls into a new month, daily full refresh is mandatory.
        if not any(changed.values()):
            return {"changed": False, "main": False, "holidays": False}

        busy = read_range(first, last, kind="main", password=secret) if changed["main"] else None
        holidays = (read_range(holiday_first, holiday_last, kind="holidays", password=secret)
                    if changed["holidays"] else None)
        # A concurrent calendar edit invalidates the snapshot. Retry next minute.
        for kind in CALENDARS:
            if changed[kind] and collection_token(kind, secret) != tokens[kind]:
                raise RuntimeError(f"CalDAV {kind} changed during fetch")

        added, cleared, manual_overlap = [], [], []
        if busy is not None:
            for day in sorted(busy):
                status, synced = current.get(day, (None, False))
                if status == "busy" and not synced:
                    manual_overlap.append(day)
                elif status != "busy":
                    added.append(day)
            cleared = sorted(day for day, (status, synced) in current.items()
                             if synced and day not in busy and status == "busy")
        holiday_changes = 0
        if holidays is not None:
            holiday_changes = sum(old_holidays.get(day) != name for day, name in holidays.items())
            holiday_changes += sum(day not in holidays for day in old_holidays)
        summary = {
            "changed": True, "main": changed["main"], "holidays": changed["holidays"],
            "range": f"{first}..{last}", "added": added, "cleared": cleared,
            "manual_overlap": manual_overlap, "holiday_changes": holiday_changes,
        }
        if not apply:
            return summary

        cursor.execute(
            "CREATE TABLE IF NOT EXISTS su_calendar_holidays ("
            "day_date DATE PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        if busy is not None:
            for day in sorted(busy):
                status, synced = current.get(day, (None, False))
                if status == "busy" and not synced:
                    continue
                cursor.execute(
                    "INSERT INTO su_calendar_days "
                    "(day_date, status, label, is_public, synced_busy) "
                    "VALUES (%s, 'busy', NULL, 1, 1) "
                    "ON DUPLICATE KEY UPDATE status='busy', is_public=1, synced_busy=1",
                    (day,),
                )
            for day in cleared:
                cursor.execute(
                    "UPDATE su_calendar_days SET status='free', synced_busy=0 "
                    "WHERE day_date=%s AND synced_busy=1", (day,)
                )
        if holidays is not None:
            cursor.execute(
                "DELETE FROM su_calendar_holidays WHERE day_date BETWEEN %s AND %s",
                (holiday_first, holiday_last),
            )
            for day, name in sorted(holidays.items()):
                cursor.execute(
                    "INSERT INTO su_calendar_holidays (day_date, name) VALUES (%s, %s)",
                    (day, name[:255]),
                )
        for kind in CALENDARS:
            if changed[kind]:
                _put_meta(cursor, f"suc_baikal_{kind}_token", tokens[kind])
        if full:
            _put_meta(cursor, "suc_baikal_last_full_day", today.isoformat())
        _put_meta(cursor, "last_sync_at", datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S%z"))
        _put_meta(cursor, "last_sync_range", f"{first}..{last}")
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, "/mnt/mfu/app")
    sys.path.insert(0, "/mnt/mfu")
    from utils.db import get_db
    db = get_db()
    try:
        result = synchronize(db, apply=args.apply)
    finally:
        db.close()
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
