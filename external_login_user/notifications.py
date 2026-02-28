from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import abort, current_app, jsonify, render_template, request, session

from . import bp
from .utils import _require_ext_login
from app.utils.db import get_db


_NOTIFICATION_DDL = """
CREATE TABLE IF NOT EXISTS mfu_notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_kind VARCHAR(16) NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  kind VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  body TEXT NULL,
  target_url VARCHAR(512) NOT NULL,
  event_id BIGINT UNSIGNED NULL,
  created_at DATETIME NOT NULL,
  read_at DATETIME NULL,
  dedup_key VARCHAR(191) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_mfu_notifications_dedup (user_kind, user_id, dedup_key),
  KEY idx_mfu_notifications_unread (user_kind, user_id, read_at, created_at),
  KEY idx_mfu_notifications_created (user_kind, user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


@bp.record_once
def _ensure_notification_table(state) -> None:
    app = state.app
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(_NOTIFICATION_DDL)
            db.commit()
        except Exception:
            app.logger.exception("ensure mfu_notifications failed")
        finally:
            cur.close()
            db.close()


def create_notification_external(
    user_id: int,
    kind: str,
    title: str,
    body: str,
    target_url: str,
    dedup_key: str,
    event_id: int | None = None,
) -> bool:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        dedup = (dedup_key or "").strip()[:191]
        cur.execute(
            """
            SELECT id, created_at
              FROM mfu_notifications
             WHERE user_kind=%s AND user_id=%s AND dedup_key=%s
             LIMIT 1
            """,
            ("external", int(user_id), dedup),
        )
        existing = cur.fetchone()

        cur.execute(
            """
            INSERT INTO mfu_notifications (
              user_kind, user_id, kind, title, body, target_url,
              event_id, dedup_key, created_at, read_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
            ON DUPLICATE KEY UPDATE id=id
            """,
            (
                "external",
                int(user_id),
                (kind or "").strip() or "general",
                (title or "").strip() or "お知らせ",
                (body or "").strip(),
                (target_url or "").strip() or "/external-login/",
                int(event_id) if event_id else None,
                dedup,
                datetime.utcnow(),
            ),
        )
        db.commit()
        current_app.logger.info(
            "notifications insert user_id=%s event_id=%s kind=%s dedup_key=%s inserted=%s existing_id=%s",
            int(user_id),
            int(event_id) if event_id else None,
            (kind or "").strip() or "general",
            dedup,
            cur.rowcount == 1,
            existing["id"] if existing else None,
        )
        return cur.rowcount == 1
    except Exception:
        current_app.logger.warning("create_notification_external failed user_id=%s", user_id, exc_info=True)
        return False
    finally:
        cur.close()
        db.close()


@bp.get("/notifications")
def notifications_page():
    guard = _require_ext_login()
    if guard:
        return guard
    return render_template("notifications.html")


@bp.get("/api/notifications/unread-count")
def api_notifications_unread_count():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM mfu_notifications
             WHERE user_kind='external'
               AND user_id=%s
               AND read_at IS NULL
            """,
            (uid,),
        )
        row = cur.fetchone() or {}
        count = int(row.get("cnt") or 0)
        current_app.logger.info("notifications unread-count user_id=%s read_at_is_null=true count=%s", uid, count)
        resp = jsonify({"count": count})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    finally:
        cur.close()
        db.close()


@bp.get("/api/notifications")
def api_notifications_list():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    unread_only = (request.args.get("unread") or "").strip() in {"1", "true", "yes"}
    page = max(int(request.args.get("page") or 1), 1)
    per_page = 20
    offset = (page - 1) * per_page

    where = ["user_kind='external'", "user_id=%s"]
    params: list[Any] = [uid]
    if unread_only:
        where.append("read_at IS NULL")

    where_sql = " AND ".join(where)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT id, kind, title, body, target_url, event_id, created_at, read_at
              FROM mfu_notifications
             WHERE {where_sql}
             ORDER BY created_at DESC, id DESC
             LIMIT %s OFFSET %s
            """,
            tuple(params + [per_page, offset]),
        )
        rows = cur.fetchall() or []

        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM mfu_notifications WHERE {where_sql}",
            tuple(params),
        )
        total = int((cur.fetchone() or {}).get("cnt") or 0)

        items = []
        for row in rows:
            items.append(
                {
                    "id": int(row["id"]),
                    "kind": row.get("kind"),
                    "title": row.get("title"),
                    "body": row.get("body") or "",
                    "target_url": row.get("target_url") or "/external-login/",
                    "event_id": row.get("event_id"),
                    "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
                    "read_at": row.get("read_at").isoformat() if row.get("read_at") else None,
                }
            )

        latest_id = items[0]["id"] if items else None
        latest_created_at = items[0]["created_at"] if items else None
        current_app.logger.info(
            "notifications list user_id=%s page=%s unread_only=%s returned=%s total=%s latest_id=%s latest_created_at=%s",
            uid,
            page,
            unread_only,
            len(items),
            total,
            latest_id,
            latest_created_at,
        )

        resp = jsonify(
            {
                "items": items,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "has_next": page * per_page < total,
                },
            }
        )
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/<int:notification_id>/read")
def api_notifications_mark_read(notification_id: int):
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=COALESCE(read_at, %s)
             WHERE id=%s
               AND user_kind='external'
               AND user_id=%s
            """,
            (datetime.utcnow(), notification_id, uid),
        )
        db.commit()
        if cur.rowcount == 0:
            abort(404)
        return jsonify({"ok": True})
    finally:
        cur.close()
        db.close()


@bp.post("/api/notifications/read-all")
def api_notifications_mark_all_read():
    guard = _require_ext_login()
    if guard:
        return guard

    uid = int(session.get("ext_user_id") or 0)
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_notifications
               SET read_at=%s
             WHERE user_kind='external'
               AND user_id=%s
               AND read_at IS NULL
            """,
            (datetime.utcnow(), uid),
        )
        updated = int(cur.rowcount or 0)
        db.commit()
        return jsonify({"ok": True, "updated": updated})
    finally:
        cur.close()
        db.close()
