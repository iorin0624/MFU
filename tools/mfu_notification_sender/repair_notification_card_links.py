#!/usr/bin/env python3
"""Restore source-page links in saved common notifications.

On the police watcher host, export the source-page date map to stdout:
  repair_notification_card_links.py export-chiba

On the MFU host, pipe that JSON to this script. Repair is dry-run by default;
pass --apply only after reviewing its proposed counts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


SELF_LINKS = {"", "/mfu-notifications", "/mfu-notifications/"}
POLICE_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日事件・事故ファイル")


def source_link(value: object, host: str, path_prefix: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith(path_prefix)
        or "\\" in raw
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        return ""
    return raw


def police_date(title: object) -> str:
    match = POLICE_DATE.search(str(title or ""))
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def train_urls(catalog_path: Path, state_path: Path) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for path, key in ((catalog_path, "lines"), (state_path, "routes")):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get(key) or []
        if isinstance(rows, dict):
            rows = rows.values()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("line_name") or "").strip()
            url = source_link(row.get("url"), "transit.yahoo.co.jp", "/diainfo/")
            if name and url:
                candidates.setdefault(name, set()).add(url)
    return {name: next(iter(urls)) for name, urls in candidates.items() if len(urls) == 1}


def repaired_cards(feature_key: str, content: dict, chiba: dict[str, str], trains: dict[str, str]) -> tuple[dict, int, int]:
    cards = content.get("cards")
    if not isinstance(cards, list):
        return content, 0, 0
    changed = 0
    unresolved = 0
    for card in cards:
        if not isinstance(card, dict) or str(card.get("url") or "") not in SELF_LINKS:
            continue
        url = ""
        if feature_key == "chiba_police_incidents" and "千葉県警察の掲載ページを開く" in str(card.get("description") or ""):
            url = source_link(chiba.get(police_date(card.get("title"))), "www.police.pref.chiba.jp", "/kohoka/orders_prefecture_")
        elif feature_key == "train_status":
            fields = card.get("fields") if isinstance(card.get("fields"), list) else []
            line = next((str(field.get("value") or "").strip() for field in fields if isinstance(field, dict) and field.get("name") == "路線"), "")
            if line:
                url = trains.get(line, "")
        else:
            continue
        if url:
            card["url"] = url
            changed += 1
        else:
            unresolved += 1
    return content, changed, unresolved


def export_chiba() -> None:
    if not (Path(__file__).parent / "chiba_police_watcher.py").exists():
        sys.path.insert(0, "/usr/local/bin")
    from chiba_police_watcher import db_connect

    connection = db_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT publication_date, source_url FROM source_pages "
                "WHERE publication_date >= CURDATE() - INTERVAL 90 DAY"
            )
            rows = cursor.fetchall()
        print(json.dumps({str(row["publication_date"]): row["source_url"] for row in rows}, ensure_ascii=False))
    finally:
        connection.close()


def repair(*, apply: bool) -> None:
    import mysql.connector

    sys.path.insert(0, "/mnt/mfu")
    from config import MYSQL_CONFIG

    chiba = json.load(sys.stdin)
    if not isinstance(chiba, dict):
        raise ValueError("Chiba source map must be a JSON object")
    trains = train_urls(
        Path("/mnt/mfu/signage_archive/train-status/catalog.json"),
        Path("/mnt/mfu/signage_archive/train-status/alert-state.json"),
    )
    connection = mysql.connector.connect(**MYSQL_CONFIG)
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, feature_key, content_json FROM mfu_notifications "
            "WHERE feature_key IN ('chiba_police_incidents', 'train_status') ORDER BY id"
        )
        rows = cursor.fetchall()
        updates = []
        unresolved_ids = []
        for row in rows:
            try:
                content = json.loads(row["content_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(content, dict):
                continue
            content, changed, unresolved = repaired_cards(row["feature_key"], content, chiba, trains)
            if changed:
                updates.append((json.dumps(content, ensure_ascii=False), row["id"], changed))
            if unresolved:
                unresolved_ids.append(row["id"])
        print(json.dumps({
            "mode": "apply" if apply else "dry-run",
            "notifications": len(rows),
            "updated_notifications": len(updates),
            "updated_cards": sum(entry[2] for entry in updates),
            "unresolved_notification_ids": unresolved_ids,
        }, ensure_ascii=False))
        if apply:
            cursor.executemany(
                "UPDATE mfu_notifications SET content_json=%s WHERE id=%s",
                [(content, notification_id) for content, notification_id, _ in updates],
            )
            connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("export-chiba", "repair"))
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    if arguments.command == "export-chiba":
        export_chiba()
    else:
        repair(apply=arguments.apply)
