from __future__ import annotations

from functools import wraps
from threading import Lock

from flask import abort, current_app, redirect, request, session, url_for

from app.utils.db import get_db

_SCHEMA_LOCK = Lock()
_SCHEMA_INITIALIZED = False


def _safe_log(message: str, *, exc: Exception | None = None) -> None:
    try:
        if exc:
            current_app.logger.warning(message, exc_info=exc)
        else:
            current_app.logger.warning(message)
    except Exception:
        if exc:
            print(f"[feature_access] {message}: {exc}")
        else:
            print(f"[feature_access] {message}")


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


def _ensure_columns(cur, table: str, columns: dict[str, str]) -> None:
    for column, ddl in columns.items():
        if not _column_exists(cur, table, column):
            cur.execute(ddl)


def ensure_feature_access_schema() -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED:
            return
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mfu_features (
                    feature_key VARCHAR(64) PRIMARY KEY,
                    label VARCHAR(128) NOT NULL,
                    is_enabled_global TINYINT(1) NOT NULL DEFAULT 1,
                    category VARCHAR(64) NOT NULL DEFAULT 'other',
                    order_no INT NOT NULL DEFAULT 0,
                    description VARCHAR(255) NULL,
                    deprecated TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
            _ensure_columns(
                cur,
                "mfu_features",
                {
                    "category": "ALTER TABLE mfu_features ADD COLUMN category VARCHAR(64) NOT NULL DEFAULT 'other'",
                    "order_no": "ALTER TABLE mfu_features ADD COLUMN order_no INT NOT NULL DEFAULT 0",
                    "description": "ALTER TABLE mfu_features ADD COLUMN description VARCHAR(255) NULL",
                    "deprecated": "ALTER TABLE mfu_features ADD COLUMN deprecated TINYINT(1) NOT NULL DEFAULT 0",
                },
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mfu_user_features (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    feature_key VARCHAR(64) NOT NULL,
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_user_feature (user_id, feature_key),
                    INDEX (feature_key)
                )
                """
            )
            _ensure_columns(
                cur,
                "mfu_user_features",
                {
                    "user_id": "ALTER TABLE mfu_user_features ADD COLUMN user_id VARCHAR(64) NOT NULL",
                    "feature_key": "ALTER TABLE mfu_user_features ADD COLUMN feature_key VARCHAR(64) NOT NULL",
                    "is_enabled": "ALTER TABLE mfu_user_features ADD COLUMN is_enabled TINYINT(1) NOT NULL DEFAULT 1",
                    "created_at": "ALTER TABLE mfu_user_features ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
                },
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mfu_nav_items (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    parent_id INT NULL,
                    label VARCHAR(128) NOT NULL,
                    url VARCHAR(255) NOT NULL,
                    order_no INT NOT NULL DEFAULT 0,
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
                    feature_key VARCHAR(64) NULL,
                    open_in_new_tab TINYINT(1) NOT NULL DEFAULT 0,
                    is_external TINYINT(1) NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    INDEX (parent_id),
                    INDEX (order_no),
                    INDEX (feature_key)
                )
                """
            )
            _ensure_columns(
                cur,
                "mfu_nav_items",
                {
                    "parent_id": "ALTER TABLE mfu_nav_items ADD COLUMN parent_id INT NULL",
                    "label": "ALTER TABLE mfu_nav_items ADD COLUMN label VARCHAR(128) NOT NULL",
                    "url": "ALTER TABLE mfu_nav_items ADD COLUMN url VARCHAR(255) NOT NULL",
                    "order_no": "ALTER TABLE mfu_nav_items ADD COLUMN order_no INT NOT NULL DEFAULT 0",
                    "is_enabled": "ALTER TABLE mfu_nav_items ADD COLUMN is_enabled TINYINT(1) NOT NULL DEFAULT 1",
                    "feature_key": "ALTER TABLE mfu_nav_items ADD COLUMN feature_key VARCHAR(64) NULL",
                    "open_in_new_tab": "ALTER TABLE mfu_nav_items ADD COLUMN open_in_new_tab TINYINT(1) NOT NULL DEFAULT 0",
                    "is_external": "ALTER TABLE mfu_nav_items ADD COLUMN is_external TINYINT(1) NOT NULL DEFAULT 0",
                },
            )
            db.commit()
            db.close()
            _SCHEMA_INITIALIZED = True
        except Exception as exc:
            _safe_log("feature access schema init failed", exc=exc)


def _redirect_login():
    path = request.full_path or request.path or "/"
    return redirect(url_for("login", next=path))


def user_has_feature(user_id: str, feature_key: str) -> bool:
    if not user_id or not feature_key:
        return False
    if user_id == "admin":
        return True
    ensure_feature_access_schema()
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            SELECT 1
              FROM mfu_features f
              JOIN mfu_user_features uf
                ON uf.feature_key = f.feature_key
             WHERE uf.user_id=%s
               AND uf.feature_key=%s
               AND uf.is_enabled=1
               AND f.is_enabled_global=1
             LIMIT 1
            """,
            (user_id, feature_key),
        )
        ok = cur.fetchone() is not None
        db.close()
        return ok
    except Exception as exc:
        _safe_log("user_has_feature failed", exc=exc)
        return False


def get_allowed_features(user_id: str | None) -> set[str]:
    if not user_id:
        return set()
    ensure_feature_access_schema()
    try:
        db = get_db()
        cur = db.cursor()
        if user_id == "admin":
            cur.execute("SELECT feature_key FROM mfu_features WHERE is_enabled_global=1")
            rows = cur.fetchall()
        else:
            cur.execute(
                """
                SELECT uf.feature_key
                  FROM mfu_user_features uf
                  JOIN mfu_features f ON f.feature_key = uf.feature_key
                 WHERE uf.user_id=%s
                   AND uf.is_enabled=1
                   AND f.is_enabled_global=1
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        db.close()
        return {row[0] for row in rows} if rows else set()
    except Exception as exc:
        _safe_log("get_allowed_features failed", exc=exc)
        return set()


def has_feature(feature_key: str) -> bool:
    user_id = session.get("user")
    return user_has_feature(user_id, feature_key) if user_id else False


def enforce_feature_access(feature_key: str):
    user_id = session.get("user")
    if not user_id:
        return _redirect_login()
    if user_id == "admin":
        return None
    if user_has_feature(user_id, feature_key):
        return None
    abort(403)


def require_feature(feature_key: str):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            response = enforce_feature_access(feature_key)
            if response is not None:
                return response
            return view(*args, **kwargs)

        return wrapper

    return decorator


def _is_allowed_url(url_value: str, is_external: int) -> bool:
    if not url_value:
        return False
    if is_external:
        return True
    return url_value.startswith("/")


def get_nav_items_for_user(user_id: str | None) -> list[dict]:
    if not user_id:
        return []
    ensure_feature_access_schema()
    allowed_features = get_allowed_features(user_id)
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, parent_id, label, url, order_no, is_enabled,
                   feature_key, open_in_new_tab, is_external
              FROM mfu_nav_items
             WHERE is_enabled=1
             ORDER BY order_no, id
            """
        )
        rows = cur.fetchall()
        db.close()
    except Exception as exc:
        _safe_log("get_nav_items_for_user failed", exc=exc)
        return []

    filtered = []
    for row in rows:
        if not row.get("is_enabled"):
            continue
        feature_key = row.get("feature_key")
        if user_id != "admin":
            if feature_key and feature_key not in allowed_features:
                continue
        if not _is_allowed_url(row.get("url") or "", int(row.get("is_external") or 0)):
            continue
        filtered.append(row)

    parents = []
    children_map: dict[int, list[dict]] = {}
    for row in filtered:
        parent_id = row.get("parent_id")
        if parent_id is None:
            parents.append(row)
        else:
            children_map.setdefault(parent_id, []).append(row)

    parents.sort(key=lambda x: (x.get("order_no", 0), x["id"]))
    for parent in parents:
        children = children_map.get(parent["id"], [])
        children.sort(key=lambda x: (x.get("order_no", 0), x["id"]))
        parent["children"] = children
    return parents
