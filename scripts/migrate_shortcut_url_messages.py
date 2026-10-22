"""Replace old iCloud Shortcut links in existing upload notices.

Run without arguments for a read-only preview. Applying requires the counts
observed in that preview, so a changed production data set cannot be updated
accidentally. Only a complete Shortcut URL on its own line is eligible.
"""

from __future__ import annotations

import argparse
import re


SHORTCUT_URL_PREFIX = "https://www.icloud.com/shortcuts/"
SHORTCUT_URL_LINE = re.compile(
    r"(?m)^([ \t]*)(https://www\.icloud\.com/shortcuts/[A-Za-z0-9]+)([ \t]*)(?=\r?$)"
)
PLACEHOLDER = "{{ shortcut_download_url }}"


def replace_shortcut_url_lines(value: str) -> tuple[str, list[str]]:
    """Replace only standalone distribution URLs, preserving other text."""
    urls: list[str] = []

    def replace(match: re.Match[str]) -> str:
        urls.append(match.group(2))
        return f"{match.group(1)}{PLACEHOLDER}{match.group(3)}"

    result = SHORTCUT_URL_LINE.sub(replace, value)
    if SHORTCUT_URL_PREFIX in result:
        raise ValueError("A Shortcut URL was found outside a standalone URL line")
    return result, urls


SOURCES = (
    (
        "site_notices",
        "SELECT m.uuid, m.message AS body FROM messages AS m "
        "JOIN uploads AS u ON u.uuid = m.uuid "
        "WHERE u.upload_deleted_at IS NULL AND m.message LIKE %s FOR UPDATE",
        "UPDATE messages SET message = %s WHERE uuid = %s",
        ("uuid",),
    ),
    (
        "custom_site_templates",
        "SELECT t.uuid, t.template AS body FROM upload_site_message_templates AS t "
        "JOIN uploads AS u ON u.uuid = t.uuid "
        "WHERE u.upload_deleted_at IS NULL AND t.template LIKE %s FOR UPDATE",
        "UPDATE upload_site_message_templates SET template = %s WHERE uuid = %s",
        ("uuid",),
    ),
    (
        "mode_site_templates",
        "SELECT username, mode, template AS body FROM message_templates "
        "WHERE template LIKE %s FOR UPDATE",
        "UPDATE message_templates SET template = %s WHERE username = %s AND mode = %s",
        ("username", "mode"),
    ),
)


def migrate(*, apply: bool, expected: tuple[int, int, int] | None = None) -> None:
    from app.utils.db import get_db

    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        db.start_transaction()
        changes = []
        counts = []
        urls = set()
        for name, select_sql, update_sql, key_columns in SOURCES:
            cursor.execute(select_sql, (f"%{SHORTCUT_URL_PREFIX}%",))
            source_changes = []
            for row in cursor.fetchall():
                original = str(row["body"] or "")
                converted, found = replace_shortcut_url_lines(original)
                if not found:
                    raise ValueError(f"{name} contains an unsupported Shortcut URL")
                source_changes.append(
                    (update_sql, (converted, *(row[column] for column in key_columns)))
                )
                urls.update(found)
            counts.append(len(source_changes))
            changes.extend(source_changes)

        summary = dict(zip((source[0] for source in SOURCES), counts))
        print(f"matched rows: {summary}; distinct Shortcut URLs: {len(urls)}")
        if not apply:
            db.rollback()
            return

        if expected is None or tuple(counts) != expected:
            raise ValueError(f"row counts differ from approval: expected={expected}, actual={tuple(counts)}")
        if not urls:
            raise ValueError("no Shortcut URL was found")
        for update_sql, params in changes:
            cursor.execute(update_sql, params)
            if cursor.rowcount != 1:
                raise ValueError("a notice changed while migration was running")
        db.commit()
        print("committed")
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the replacement")
    parser.add_argument(
        "--expect", nargs=3, type=int, metavar=("NOTICES", "CUSTOM", "MODE"),
        help="required row counts for --apply, in the displayed order",
    )
    args = parser.parse_args()
    if args.apply and args.expect is None:
        parser.error("--apply requires --expect NOTICES CUSTOM MODE")
    migrate(apply=args.apply, expected=tuple(args.expect) if args.expect else None)


if __name__ == "__main__":
    main()
