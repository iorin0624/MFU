#!/usr/bin/env python3
"""Archive Chiba Prefectural Police incident pages and notify Discord."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pymysql
from bs4 import BeautifulSoup


INDEX_URL = "https://www.police.pref.chiba.jp/kohoka/safe-life_trouble.html"
USER_AGENT = "ChibaPoliceIncidentWatcher/1.0 (+private archival notifier)"
PAGE_RE = re.compile(r"orders_prefecture_(\d{5})\.html$")
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
DEPARTMENT_RE = re.compile(r"（([^（）]+)）\s*$")
MAX_BACKFILL_PAGES = 400
BACKFILL_MISS_LIMIT = 30
DISCORD_MAX_EMBEDS = 10
DISCORD_MAX_TOTAL_CHARS = 5800
JST = ZoneInfo("Asia/Tokyo")
DAILY_POLL_START = wall_time(6, 0)
STATE_COMPLETED_CYCLE = "completed_cycle_date"
STATE_IN_PROGRESS = "in_progress_cycle"


@dataclass(frozen=True)
class PageLink:
    url: str
    publication_date: date


@dataclass(frozen=True)
class Incident:
    ordinal: int
    title: str
    body: str
    department: str
    fingerprint: str


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("CHIBA_POLICE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def normalize_text(value: str) -> str:
    value = html.unescape(value).replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", value).strip()


def parse_date(value: str) -> date:
    match = DATE_RE.search(value)
    if not match:
        raise ValueError(f"publication date was not found: {value!r}")
    return date(*(int(part) for part in match.groups()))


def fetch(url: str, *, allow_not_found: bool = False) -> tuple[bytes | None, dict[str, str]]:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(), dict(response.headers.items())
    except HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return None, {}
        raise


def parse_index(raw: bytes) -> list[PageLink]:
    soup = BeautifulSoup(raw, "html.parser")
    found: dict[str, PageLink] = {}
    for anchor in soup.select('a[href*="orders_prefecture_"]'):
        href = anchor.get("href", "")
        url = urljoin(INDEX_URL, href)
        if not PAGE_RE.search(urlparse(url).path):
            continue
        try:
            publication_date = parse_date(anchor.get_text(" ", strip=True))
        except ValueError:
            continue
        found[url] = PageLink(url=url, publication_date=publication_date)
    if not found:
        raise RuntimeError("no incident page links were found on the index")
    return sorted(found.values(), key=lambda item: item.publication_date, reverse=True)


def parse_page(raw: bytes, expected_date: date | None = None) -> tuple[date, list[Incident]]:
    soup = BeautifulSoup(raw, "html.parser")
    heading = soup.select_one("h1.titPage") or soup.find("h1")
    if not heading:
        raise RuntimeError("page heading was not found")
    publication_date = parse_date(heading.get_text(" ", strip=True))
    if expected_date and publication_date != expected_date:
        raise RuntimeError(
            f"publication date mismatch: expected={expected_date} actual={publication_date}"
        )

    section = soup.select_one("#ctmsg_1")
    if not section:
        raise RuntimeError("incident content section was not found")
    lines = [normalize_text(line) for line in section.get_text("\n").splitlines()]
    lines = [line for line in lines if line]

    grouped: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []
    for line in lines:
        if line.startswith("・"):
            if current_title:
                grouped.append((current_title, current_body))
            current_title = normalize_text(line[1:])
            current_body = []
        elif current_title:
            current_body.append(line)
    if current_title:
        grouped.append((current_title, current_body))
    if not grouped:
        raise RuntimeError("no incidents were parsed from the page")

    incidents: list[Incident] = []
    for ordinal, (title, body_lines) in enumerate(grouped, start=1):
        body = normalize_text("\n".join(body_lines))
        department_match = DEPARTMENT_RE.search(title)
        department = department_match.group(1) if department_match else ""
        fingerprint_source = f"{title}\n{body}".encode("utf-8")
        incidents.append(
            Incident(
                ordinal=ordinal,
                title=title,
                body=body,
                department=department,
                fingerprint=hashlib.sha256(fingerprint_source).hexdigest(),
            )
        )
    return publication_date, incidents


def db_connect():
    required = {
        "host": os.getenv("CHIBA_POLICE_DB_HOST", ""),
        "user": os.getenv("CHIBA_POLICE_DB_USER", ""),
        "password": os.getenv("CHIBA_POLICE_DB_PASSWORD", ""),
        "database": os.getenv("CHIBA_POLICE_DB_NAME", ""),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"missing database settings: {', '.join(missing)}")
    return pymysql.connect(
        **required,
        port=int(os.getenv("CHIBA_POLICE_DB_PORT", "3306")),
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def validate_state_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT state_key FROM watcher_state LIMIT 1")


def get_state(connection, key: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT state_value FROM watcher_state WHERE state_key=%s",
            (key,),
        )
        row = cursor.fetchone()
    return row["state_value"] if row else ""


def set_state(connection, key: str, value: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO watcher_state (state_key, state_value, updated_at)
            VALUES (%s, %s, NOW(6))
            ON DUPLICATE KEY UPDATE
                state_value=VALUES(state_value), updated_at=NOW(6)
            """,
            (key, value),
        )
    connection.commit()


def delete_state(connection, key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM watcher_state WHERE state_key=%s", (key,))
    connection.commit()


def known_page_urls(connection, urls: Iterable[str]) -> set[str]:
    urls = list(urls)
    if not urls:
        return set()
    placeholders = ",".join(["%s"] * len(urls))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT source_url FROM source_pages WHERE source_url IN ({placeholders})",
            urls,
        )
        return {row["source_url"] for row in cursor.fetchall()}


def has_page_for_date(connection, publication_date: date) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM source_pages WHERE publication_date=%s LIMIT 1",
            (publication_date,),
        )
        return cursor.fetchone() is not None


def store_page(
    connection,
    link: PageLink,
    raw: bytes,
    incidents: list[Incident],
    *,
    notification_status: str,
) -> int:
    content_hash = hashlib.sha256(raw).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO source_pages
                (source_url, publication_date, content_hash, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, NOW(6), NOW(6))
            ON DUPLICATE KEY UPDATE
                id=LAST_INSERT_ID(id), publication_date=VALUES(publication_date),
                content_hash=VALUES(content_hash), last_seen_at=NOW(6)
            """,
            (link.url, link.publication_date, content_hash),
        )
        page_id = cursor.lastrowid
        inserted = 0
        for incident in incidents:
            cursor.execute(
                """
                INSERT IGNORE INTO incidents
                    (page_id, ordinal_no, title, body, department, fingerprint,
                     notification_status, first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(6), NOW(6))
                """,
                (
                    page_id,
                    incident.ordinal,
                    incident.title,
                    incident.body,
                    incident.department,
                    incident.fingerprint,
                    notification_status,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                cursor.execute(
                    """
                    UPDATE incidents SET last_seen_at=NOW(6), ordinal_no=%s
                    WHERE page_id=%s AND fingerprint=%s
                    """,
                    (incident.ordinal, page_id, incident.fingerprint),
                )
    connection.commit()
    return inserted


def pending_by_page(connection) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.id AS page_id, p.source_url, p.publication_date,
                   i.id, i.ordinal_no, i.title, i.body, i.department
            FROM incidents i
            JOIN source_pages p ON p.id=i.page_id
            WHERE i.notification_status='pending'
            ORDER BY p.publication_date, p.id, i.ordinal_no, i.id
            """
        )
        rows = cursor.fetchall()
    grouped: dict[int, dict] = {}
    for row in rows:
        page = grouped.setdefault(
            row["page_id"],
            {
                "page_id": row["page_id"],
                "source_url": row["source_url"],
                "publication_date": row["publication_date"],
                "incidents": [],
            },
        )
        page["incidents"].append(row)
    return list(grouped.values())


def pending_count(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS pending FROM incidents WHERE notification_status='pending'"
        )
        row = cursor.fetchone()
    return int(row["pending"])


def header_embed(page: dict) -> dict:
    published = page["publication_date"]
    return {
        "title": f"{published.year}年{published.month}月{published.day}日事件・事故ファイル",
        "url": page["source_url"],
        "description": "千葉県警察の掲載ページを開く",
        "color": 0x1F4E79,
        "footer": {"text": "千葉県警察"},
    }


def incident_embed(row: dict) -> dict:
    return {
        "title": row["title"][:256],
        "description": (row["body"] or "（本文なし）")[:4096],
        "color": 0xC0392B,
    }


def embed_chars(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    return total


def discord_batches(page: dict) -> list[tuple[list[dict], list[int]]]:
    header = header_embed(page)
    batches: list[tuple[list[dict], list[int]]] = []
    embeds = [header]
    ids: list[int] = []
    chars = embed_chars(header)
    for row in page["incidents"]:
        embed = incident_embed(row)
        next_chars = chars + embed_chars(embed)
        if len(embeds) >= DISCORD_MAX_EMBEDS or next_chars > DISCORD_MAX_TOTAL_CHARS:
            batches.append((embeds, ids))
            embeds = [header]
            ids = []
            chars = embed_chars(header)
        embeds.append(embed)
        ids.append(row["id"])
        chars += embed_chars(embed)
    if ids:
        batches.append((embeds, ids))
    return batches


def validate_webhook(webhook: str) -> None:
    parsed = urlparse(webhook)
    if not ((parsed.scheme == "https" and parsed.hostname in {"discord.com", "discordapp.com"}) or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port == 8765)):
        raise RuntimeError("CHIBA_POLICE_DISCORD_WEBHOOK_URL is invalid")
    if parsed.hostname in {"discord.com", "discordapp.com"} and "/api/webhooks/" not in parsed.path:
        raise RuntimeError("CHIBA_POLICE_DISCORD_WEBHOOK_URL is not a webhook URL")


def post_discord(webhook: str, embeds: list[dict]) -> None:
    payload = json.dumps(
        {"username": "千葉県警 事件・事故情報", "embeds": embeds},
        ensure_ascii=False,
    ).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"Discord returned HTTP {response.status}")
                return
        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(exc.read())["retry_after"])
                except Exception:
                    retry_after = 2.0
                time.sleep(min(max(retry_after, 1.0), 30.0))
                continue
            if 500 <= exc.code < 600:
                time.sleep(attempt * 2)
                continue
            raise
        except (URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(attempt * 2)
    raise RuntimeError(f"Discord delivery failed: {last_error!r}")


def mark_sent(connection, incident_ids: list[int]) -> None:
    placeholders = ",".join(["%s"] * len(incident_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE incidents
            SET notification_status='sent', notified_at=NOW(6)
            WHERE id IN ({placeholders}) AND notification_status='pending'
            """,
            incident_ids,
        )
    connection.commit()


def deliver_pending(connection, webhook: str) -> int:
    if not webhook:
        pending = sum(len(page["incidents"]) for page in pending_by_page(connection))
        if pending:
            logging.warning("webhook is empty; pending incidents=%d", pending)
        return 0
    validate_webhook(webhook)
    delivered = 0
    for page in pending_by_page(connection):
        for embeds, incident_ids in discord_batches(page):
            post_discord(webhook, embeds)
            mark_sent(connection, incident_ids)
            delivered += len(incident_ids)
            logging.info(
                "Discord delivered date=%s incidents=%d",
                page["publication_date"],
                len(incident_ids),
            )
    return delivered


def backfill(connection) -> tuple[int, int]:
    index_raw, _ = fetch(INDEX_URL)
    links = parse_index(index_raw or b"")
    latest_match = PAGE_RE.search(urlparse(links[0].url).path)
    if not latest_match:
        raise RuntimeError("latest page number was not found")
    latest_number = int(latest_match.group(1))
    pages_stored = 0
    incidents_stored = 0
    consecutive_missing = 0
    seen_existing = False
    base = links[0].url.rsplit("_", 1)[0]
    for page_number in range(latest_number, max(latest_number - MAX_BACKFILL_PAGES, 0), -1):
        url = f"{base}_{page_number:05d}.html"
        raw, _ = fetch(url, allow_not_found=True)
        if raw is None:
            consecutive_missing += 1
            if seen_existing and consecutive_missing >= BACKFILL_MISS_LIMIT:
                break
            time.sleep(0.15)
            continue
        try:
            publication_date, incidents = parse_page(raw)
        except (RuntimeError, ValueError) as exc:
            logging.warning("backfill page skipped url=%s error=%s", url, exc)
            consecutive_missing += 1
            time.sleep(0.15)
            continue
        seen_existing = True
        consecutive_missing = 0
        inserted = store_page(
            connection,
            PageLink(url, publication_date),
            raw,
            incidents,
            notification_status="suppressed",
        )
        pages_stored += 1
        incidents_stored += inserted
        logging.info(
            "backfill stored date=%s incidents=%d new=%d", publication_date, len(incidents), inserted
        )
        time.sleep(0.15)
    return pages_stored, incidents_stored


def poll(connection, *, force_cycle: bool = False) -> tuple[int, int, int]:
    now = datetime.now(JST)
    cycle_date = now.date().isoformat()
    yesterday = now.date() - timedelta(days=1)
    if not force_cycle and now.time().replace(tzinfo=None) < DAILY_POLL_START:
        logging.info("daily access paused until 06:00 JST cycle=%s", cycle_date)
        return 0, 0, 0

    completed_cycle = get_state(connection, STATE_COMPLETED_CYCLE)
    if not force_cycle and completed_cycle == cycle_date and has_page_for_date(connection, yesterday):
        logging.info("daily access paused; cycle already completed date=%s", cycle_date)
        return 0, 0, 0
    if completed_cycle == cycle_date:
        logging.warning("yesterday's page is not stored; retrying date=%s", yesterday)

    index_raw, _ = fetch(INDEX_URL)
    links = parse_index(index_raw or b"")
    known = known_page_urls(connection, [link.url for link in links])
    latest = links[0]
    yesterday_link = next((link for link in links if link.publication_date == yesterday), None)

    in_progress_raw = get_state(connection, STATE_IN_PROGRESS)
    try:
        in_progress = json.loads(in_progress_raw) if in_progress_raw else {}
    except json.JSONDecodeError:
        logging.warning("invalid in-progress state was ignored")
        in_progress = {}
    resuming = (
        in_progress.get("cycle_date") == cycle_date
        and in_progress.get("latest_url") == latest.url
    )

    if (
        latest.url in known
        and (yesterday_link is None or yesterday_link.url in known)
        and not resuming
        and not force_cycle
    ):
        logging.info(
            "index unchanged; latest date=%s detail pages skipped",
            latest.publication_date,
        )
        delivered = deliver_pending(
            connection, os.getenv("CHIBA_POLICE_DISCORD_WEBHOOK_URL", "").strip()
        )
        remaining = pending_count(connection)
        if remaining:
            raise RuntimeError(
                f"daily cycle is not complete because pending incidents remain: {remaining}"
            )
        if yesterday_link:
            set_state(connection, STATE_COMPLETED_CYCLE, cycle_date)
            logging.info("daily cycle completed date=%s yesterday=%s", cycle_date, yesterday)
        else:
            logging.info("yesterday's page is not available yet; recheck next hour date=%s", yesterday)
        return 0, 0, delivered

    set_state(
        connection,
        STATE_IN_PROGRESS,
        json.dumps(
            {"cycle_date": cycle_date, "latest_url": latest.url},
            ensure_ascii=True,
        ),
    )
    targets = links[:2]
    if yesterday_link and yesterday_link not in targets:
        targets.append(yesterday_link)
    pages_checked = 0
    incidents_new = 0
    for link in targets:
        raw, _ = fetch(link.url)
        publication_date, incidents = parse_page(raw or b"", link.publication_date)
        inserted = store_page(
            connection,
            PageLink(link.url, publication_date),
            raw or b"",
            incidents,
            notification_status="pending",
        )
        pages_checked += 1
        incidents_new += inserted
        logging.info(
            "page checked date=%s incidents=%d new=%d", publication_date, len(incidents), inserted
        )
    delivered = deliver_pending(
        connection, os.getenv("CHIBA_POLICE_DISCORD_WEBHOOK_URL", "").strip()
    )
    remaining = pending_count(connection)
    if remaining:
        raise RuntimeError(
            f"daily cycle is not complete because pending incidents remain: {remaining}"
        )
    delete_state(connection, STATE_IN_PROGRESS)
    if yesterday_link and (yesterday_link.url in known or yesterday_link in targets):
        set_state(connection, STATE_COMPLETED_CYCLE, cycle_date)
        logging.info(
            "daily cycle completed date=%s yesterday=%s; all source access paused until next 06:00 JST",
            cycle_date,
            yesterday,
        )
    else:
        logging.info("yesterday's page is not available yet; recheck next hour date=%s", yesterday)
    return pages_checked, incidents_new, delivered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="archive live historical pages")
    parser.add_argument("--check-latest", action="store_true", help="parse latest page without DB writes")
    parser.add_argument(
        "--force-cycle",
        action="store_true",
        help="fetch the latest and previous pages and complete today's cycle",
    )
    args = parser.parse_args()
    configure_logging()
    if args.check_latest:
        index_raw, _ = fetch(INDEX_URL)
        latest = parse_index(index_raw or b"")[0]
        raw, _ = fetch(latest.url)
        publication_date, incidents = parse_page(raw or b"", latest.publication_date)
        sample = {
            "header": header_embed({"publication_date": publication_date, "source_url": latest.url}),
            "incident_count": len(incidents),
            "first_incident": incident_embed(incidents[0].__dict__),
        }
        print(json.dumps(sample, ensure_ascii=False, indent=2, default=str))
        return 0

    connection = db_connect()
    try:
        validate_state_table(connection)
        if args.backfill:
            pages, incidents = backfill(connection)
            logging.info("backfill complete pages=%d new_incidents=%d", pages, incidents)
        else:
            pages, incidents, delivered = poll(connection, force_cycle=args.force_cycle)
            logging.info(
                "poll complete pages=%d new_incidents=%d delivered=%d",
                pages,
                incidents,
                delivered,
            )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("watcher failed")
        raise SystemExit(1)
