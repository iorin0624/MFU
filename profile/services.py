from __future__ import annotations

from threading import Lock

from app.utils.db import get_db

from .formatting import DEFAULT_PROFILE_QA_TEXT, split_legacy_profile_qa

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
                    qa_text MEDIUMTEXT NULL,
                    qa_updated_at DATETIME NULL,
                    avatar_file VARCHAR(64) NULL,
                    x_url VARCHAR(500) NULL,
                    instagram_url VARCHAR(500) NULL,
                    portfolio_url VARCHAR(500) NULL,
                    is_public TINYINT(1) NOT NULL DEFAULT 1,
                    show_known_works TINYINT(1) NOT NULL DEFAULT 1,
                    show_sns_links TINYINT(1) NOT NULL DEFAULT 1,
                    social_links_migrated TINYINT(1) NOT NULL DEFAULT 0,
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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_social_link (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    profile_id BIGINT UNSIGNED NOT NULL,
                    label VARCHAR(100) NOT NULL,
                    url VARCHAR(1000) NOT NULL,
                    icon_mode VARCHAR(16) NOT NULL DEFAULT 'bootstrap',
                    icon_name VARCHAR(64) NULL,
                    icon_file VARCHAR(64) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_visible TINYINT(1) NOT NULL DEFAULT 1,
                    legacy_key VARCHAR(32) NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_profile_social_order (profile_id, sort_order, id),
                    UNIQUE KEY uq_profile_social_legacy (profile_id, legacy_key)
                )
                """
            )

            if not _column_exists(cur, "profile_master", "slug"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN slug VARCHAR(64) NOT NULL DEFAULT 'main'")

            if not _column_exists(cur, "profile_master", "qa_text"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN qa_text MEDIUMTEXT NULL")
                cur.execute("SELECT id, request_notes_text FROM profile_master WHERE slug='main' LIMIT 1")
                existing = cur.fetchone()
                if existing:
                    try:
                        legacy = split_legacy_profile_qa(existing[1])
                    except ValueError:
                        legacy = None
                    if legacy:
                        notes_text, qa_text = legacy
                        cur.execute(
                            "UPDATE profile_master SET request_notes_text=%s, qa_text=%s WHERE id=%s",
                            (notes_text, qa_text, existing[0]),
                        )
                    elif not existing[1] or "Q&A" not in existing[1]:
                        cur.execute(
                            "UPDATE profile_master SET qa_text=%s WHERE id=%s",
                            (DEFAULT_PROFILE_QA_TEXT, existing[0]),
                        )

            if not _column_exists(cur, "profile_master", "avatar_file"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN avatar_file VARCHAR(64) NULL")

            if not _column_exists(cur, "profile_master", "qa_updated_at"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN qa_updated_at DATETIME NULL")

            if not _column_exists(cur, "profile_master", "social_links_migrated"):
                cur.execute("ALTER TABLE profile_master ADD COLUMN social_links_migrated TINYINT(1) NOT NULL DEFAULT 0")

            cur.execute(
                "SELECT id, x_url, instagram_url, portfolio_url, social_links_migrated "
                "FROM profile_master WHERE slug='main' LIMIT 1 FOR UPDATE"
            )
            main = cur.fetchone()
            if main and not main[4]:
                legacy_links = (
                    ("x", "X", main[1], "bi-twitter-x", 10),
                    ("instagram", "Instagram", main[2], "bi-instagram", 20),
                    ("portfolio", "ポートフォリオ", main[3], "bi-globe", 30),
                )
                for key, label, url, icon_name, sort_order in legacy_links:
                    if not url:
                        continue
                    cur.execute(
                        """
                        INSERT INTO profile_social_link
                            (profile_id, label, url, icon_mode, icon_name, icon_file,
                             sort_order, is_visible, legacy_key, created_at, updated_at)
                        VALUES (%s, %s, %s, 'bootstrap', %s, NULL, %s, 1, %s, NOW(), NOW())
                        ON DUPLICATE KEY UPDATE legacy_key=legacy_key
                        """,
                        (main[0], label, url, icon_name, sort_order, key),
                    )
                cur.execute("UPDATE profile_master SET social_links_migrated=1 WHERE id=%s", (main[0],))

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
                    slug, page_title, display_name, subtitle, intro_text, request_notes_text, qa_text,
                    x_url, instagram_url, portfolio_url,
                    is_public, show_known_works, show_sns_links,
                    created_at, updated_at
                ) VALUES (
                    'main', 'プロフィール', '表示名', NULL, NULL, NULL, %s,
                    NULL, NULL, NULL,
                    1, 1, 1,
                    NOW(), NOW()
                )
                """,
                (DEFAULT_PROFILE_QA_TEXT,),
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
                   qa_updated_at=CASE
                       WHEN NOT (qa_text <=> %s) THEN UTC_TIMESTAMP()
                       ELSE qa_updated_at
                   END,
                   qa_text=%s,
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
                payload["qa_text"],
                payload["qa_text"],
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


def update_main_profile_avatar(file_name: str | None) -> str | None:
    """Replace the main profile image reference and return its previous name."""
    create_default_main_profile_if_missing()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT avatar_file FROM profile_master WHERE slug='main' FOR UPDATE")
        row = cur.fetchone()
        previous = row[0] if row else None
        cur.execute(
            "UPDATE profile_master SET avatar_file=%s, updated_at=NOW() WHERE slug='main'",
            (file_name,),
        )
        db.commit()
        return previous
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def list_social_links(profile_id: int, *, visible_only: bool = False) -> list[dict]:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        condition = " AND is_visible=1" if visible_only else ""
        cur.execute(
            "SELECT id, profile_id, label, url, icon_mode, icon_name, icon_file, "
            "sort_order, is_visible FROM profile_social_link "
            f"WHERE profile_id=%s{condition} ORDER BY sort_order, id",
            (profile_id,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        db.close()


def get_social_link(profile_id: int, link_id: int) -> dict | None:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, profile_id, label, url, icon_mode, icon_name, icon_file, "
            "sort_order, is_visible FROM profile_social_link WHERE profile_id=%s AND id=%s LIMIT 1",
            (profile_id, link_id),
        )
        return cur.fetchone()
    finally:
        cur.close()
        db.close()


def save_social_link(profile_id: int, payload: dict, *, link_id: int | None = None) -> str | None:
    """Save a link and return its replaced icon filename, if any."""
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        previous_icon = None
        if link_id is None:
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM profile_social_link WHERE profile_id=%s",
                (profile_id,),
            )
            sort_order = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO profile_social_link
                    (profile_id, label, url, icon_mode, icon_name, icon_file,
                     sort_order, is_visible, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (profile_id, payload["label"], payload["url"], payload["icon_mode"],
                 payload["icon_name"], payload["icon_file"], sort_order, payload["is_visible"]),
            )
        else:
            cur.execute(
                "SELECT icon_file FROM profile_social_link WHERE profile_id=%s AND id=%s FOR UPDATE",
                (profile_id, link_id),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("対象のリンクが見つかりません。")
            previous_icon = row[0]
            cur.execute(
                """
                UPDATE profile_social_link
                   SET label=%s, url=%s, icon_mode=%s, icon_name=%s,
                       icon_file=%s, is_visible=%s, updated_at=NOW()
                 WHERE profile_id=%s AND id=%s
                """,
                (payload["label"], payload["url"], payload["icon_mode"],
                 payload["icon_name"], payload["icon_file"], payload["is_visible"],
                 profile_id, link_id),
            )
        db.commit()
        return previous_icon
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def delete_social_link(profile_id: int, link_id: int) -> str | None:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT icon_file FROM profile_social_link WHERE profile_id=%s AND id=%s FOR UPDATE",
            (profile_id, link_id),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("対象のリンクが見つかりません。")
        cur.execute("DELETE FROM profile_social_link WHERE profile_id=%s AND id=%s", (profile_id, link_id))
        db.commit()
        return row[0]
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def move_social_link(profile_id: int, link_id: int, direction: int) -> bool:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id FROM profile_social_link WHERE profile_id=%s ORDER BY sort_order, id FOR UPDATE",
            (profile_id,),
        )
        ids = [row[0] for row in cur.fetchall()]
        if link_id not in ids or direction not in {-1, 1}:
            db.rollback()
            return False
        index = ids.index(link_id)
        next_index = index + direction
        if not 0 <= next_index < len(ids):
            db.rollback()
            return False
        ids[index], ids[next_index] = ids[next_index], ids[index]
        for order, item_id in enumerate(ids, start=1):
            cur.execute(
                "UPDATE profile_social_link SET sort_order=%s WHERE profile_id=%s AND id=%s",
                (order * 10, profile_id, item_id),
            )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()
        db.close()


def social_icon_is_visible(file_name: str, *, include_hidden: bool = False) -> bool:
    ensure_profile_schema()
    db = get_db()
    cur = db.cursor()
    try:
        visibility_filter = "" if include_hidden else (
            "AND link.is_visible=1 AND profile.is_public=1 AND profile.show_sns_links=1"
        )
        cur.execute(
            f"""
            SELECT 1 FROM profile_social_link link
            JOIN profile_master profile ON profile.id=link.profile_id
            WHERE link.icon_file=%s AND link.icon_mode='custom'
              {visibility_filter}
            LIMIT 1
            """,
            (file_name,),
        )
        return cur.fetchone() is not None
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
