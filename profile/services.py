from __future__ import annotations

from threading import Lock

from app.utils.db import get_db

_SCHEMA_LOCK = Lock()
_SCHEMA_INITIALIZED = False


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = DATABASE()
           AND table_name = %s
           AND column_name = %s
         LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def ensure_profile_schema() -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED:
            return

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_master (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    slug VARCHAR(64) NOT NULL DEFAULT 'main',
                    page_title VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255) NOT NULL,
                    subtitle VARCHAR(255) NULL,
                    intro_text MEDIUMTEXT NULL,
                    request_notes_text MEDIUMTEXT NULL,
                    x_url VARCHAR(500) NULL,
                    instagram_url VARCHAR(500) NULL,
                    portfolio_url VARCHAR(500) NULL,
                    is_public TINYINT(1) NOT NULL DEFAULT 1,
                    show_known_works TINYINT(1) NOT NULL DEFAULT 1,
                    show_sns_links TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE KEY uq_profile_master_slug (slug)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_known_work (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    profile_id BIGINT UNSIGNED NOT NULL,
                    category_name VARCHAR(100) NOT NULL,
                    item_name VARCHAR(255) NOT NULL,
                    category_sort INT NOT NULL DEFAULT 0,
                    item_sort INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_profile_known_work_profile (profile_id),
                    KEY idx_profile_known_work_sort (profile_id, category_sort, item_sort, id)
                )
                """
            )

            if not _column_exists(cur, "profile_master", "slug"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN slug VARCHAR(64) NOT NULL DEFAULT 'main'")

            db.commit()
            _SCHEMA_INITIALIZED = True
        finally:
            cur.close()
            db.close()


def create_default_main_profile_if_missing() -> None:
    ensure_profile_schema()

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT id FROM profile_master WHERE slug=%s LIMIT 1", ("main",))
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                INSERT INTO profile_master (
                    slug, page_title, display_name, subtitle, intro_text, request_notes_text,
                    x_url, instagram_url, portfolio_url,
                    is_public, show_known_works, show_sns_links,
                    created_at, updated_at
                ) VALUES (
                    'main', 'プロフィール', '表示名', NULL, NULL, NULL,
                    NULL, NULL, NULL,
                    1, 1, 1,
                    NOW(), NOW()
                )
                """
            )
            db.commit()
    finally:
        cur.close()
        db.close()


def get_main_profile() -> dict | None:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM profile_master WHERE slug=%s LIMIT 1", ("main",))
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def update_main_profile(payload: dict) -> None:
    create_default_main_profile_if_missing()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE profile_master
               SET page_title=%s,
                   display_name=%s,
                   subtitle=%s,
                   intro_text=%s,
                   request_notes_text=%s,
                   x_url=%s,
                   instagram_url=%s,
                   portfolio_url=%s,
                   is_public=%s,
                   show_known_works=%s,
                   show_sns_links=%s,
                   updated_at=NOW()
             WHERE slug='main'
            """,
            (
                payload["page_title"],
                payload["display_name"],
                payload["subtitle"],
                payload["intro_text"],
                payload["request_notes_text"],
                payload["x_url"],
                payload["instagram_url"],
                payload["portfolio_url"],
                payload["is_public"],
                payload["show_known_works"],
                payload["show_sns_links"],
            ),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def list_known_works(profile_id: int) -> list[dict]:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, profile_id, category_name, item_name, category_sort, item_sort, created_at, updated_at
              FROM profile_known_work
             WHERE profile_id=%s
             ORDER BY category_sort ASC, category_name ASC, item_sort ASC, id ASC
            """,
            (profile_id,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def add_known_work(profile_id: int, category_name: str, item_name: str, category_sort: int, item_sort: int) -> None:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO profile_known_work
                (profile_id, category_name, item_name, category_sort, item_sort, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (profile_id, category_name, item_name, category_sort, item_sort),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def update_known_work(work_id: int, profile_id: int, category_name: str, item_name: str, category_sort: int, item_sort: int) -> bool:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE profile_known_work
               SET category_name=%s,
                   item_name=%s,
                   category_sort=%s,
                   item_sort=%s,
                   updated_at=NOW()
             WHERE id=%s
               AND profile_id=%s
            """,
            (category_name, item_name, category_sort, item_sort, work_id, profile_id),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        db.close()


def delete_known_work(work_id: int, profile_id: int) -> bool:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "DELETE FROM profile_known_work WHERE id=%s AND profile_id=%s",
            (work_id, profile_id),
        )
        db.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        db.close()
