from __future__ import annotations

import json
import re
import threading
import uuid as uuid_lib
from datetime import date, datetime
from typing import Any

from app.utils.db import get_db


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_UUID_PATTERN = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)


def normalize_uuid(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            if len(raw) == 16:
                return str(uuid_lib.UUID(bytes=raw)).lower()
            value = raw.decode("ascii", errors="ignore")
        return str(uuid_lib.UUID(str(value).strip())).lower()
    except (ValueError, TypeError, AttributeError):
        return ""


def ensure_uuid_registry_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS central_uuid_registry (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  uuid_value CHAR(36) NOT NULL,
                  resource_type VARCHAR(40) NOT NULL,
                  title_initial VARCHAR(255) NOT NULL DEFAULT '',
                  title_current VARCHAR(255) NOT NULL DEFAULT '',
                  source_table VARCHAR(64) NOT NULL DEFAULT '',
                  source_pk VARCHAR(128) NOT NULL DEFAULT '',
                  parent_uuid CHAR(36) NULL,
                  owner_key VARCHAR(255) NOT NULL DEFAULT '',
                  canonical_path VARCHAR(512) NOT NULL DEFAULT '',
                  status VARCHAR(32) NOT NULL DEFAULT 'active',
                  metadata_json LONGTEXT NULL,
                  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  deleted_at DATETIME NULL,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_central_uuid_value (uuid_value),
                  KEY idx_central_uuid_type_status (resource_type, status),
                  KEY idx_central_uuid_title (title_current),
                  KEY idx_central_uuid_source (source_table, source_pk),
                  KEY idx_central_uuid_parent (parent_uuid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS central_uuid_title_history (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  registry_id BIGINT UNSIGNED NOT NULL,
                  title VARCHAR(255) NOT NULL DEFAULT '',
                  change_type VARCHAR(32) NOT NULL DEFAULT 'rename',
                  recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (id),
                  KEY idx_central_uuid_history_registry (registry_id, recorded_at),
                  CONSTRAINT fk_central_uuid_history_registry
                    FOREIGN KEY (registry_id) REFERENCES central_uuid_registry(id)
                    ON DELETE RESTRICT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute("SHOW COLUMNS FROM logs")
            log_columns = {str(row[0]) for row in cur.fetchall()}
            additions = {
                "resource_registry_id": "ALTER TABLE logs ADD COLUMN resource_registry_id BIGINT UNSIGNED NULL AFTER latency_ms",
                "resource_type": "ALTER TABLE logs ADD COLUMN resource_type VARCHAR(40) NOT NULL DEFAULT '' AFTER resource_registry_id",
                "resource_uuid": "ALTER TABLE logs ADD COLUMN resource_uuid CHAR(36) NOT NULL DEFAULT '' AFTER resource_type",
                "resource_title": "ALTER TABLE logs ADD COLUMN resource_title VARCHAR(255) NOT NULL DEFAULT '' AFTER resource_uuid",
                "resource_status": "ALTER TABLE logs ADD COLUMN resource_status VARCHAR(32) NOT NULL DEFAULT '' AFTER resource_title",
            }
            for column, ddl in additions.items():
                if column not in log_columns:
                    try:
                        cur.execute(ddl)
                    except Exception as exc:
                        if getattr(exc, "errno", None) != 1060:
                            raise
            cur.execute("SHOW INDEX FROM logs WHERE Key_name='idx_logs_resource_uuid'")
            if not cur.fetchone():
                try:
                    cur.execute("CREATE INDEX idx_logs_resource_uuid ON logs(resource_uuid)")
                except Exception as exc:
                    if getattr(exc, "errno", None) != 1061:
                        raise
            cur.execute("SHOW INDEX FROM logs WHERE Key_name='idx_logs_resource_title'")
            if not cur.fetchone():
                try:
                    cur.execute("CREATE INDEX idx_logs_resource_title ON logs(resource_title)")
                except Exception as exc:
                    if getattr(exc, "errno", None) != 1061:
                        raise
            db.commit()
            _SCHEMA_READY = True
        finally:
            cur.close()
            db.close()


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_text(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _upsert(
    cur,
    *,
    uuid_value: Any,
    resource_type: str,
    title: str = "",
    source_table: str = "",
    source_pk: Any = "",
    parent_uuid: Any = None,
    owner_key: str = "",
    canonical_path: str = "",
    status: str = "active",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_uuid(uuid_value)
    if not normalized:
        return None
    title = _clean(title, 255)
    resource_type = _clean(resource_type, 40).upper() or "UNKNOWN"
    status = _clean(status, 32).lower() or "active"
    parent = normalize_uuid(parent_uuid) or None
    cur.execute(
        "SELECT id, title_current, status FROM central_uuid_registry WHERE uuid_value=%s FOR UPDATE",
        (normalized,),
    )
    existing = cur.fetchone()
    if isinstance(existing, tuple):
        existing = {"id": existing[0], "title_current": existing[1], "status": existing[2]}
    if not existing:
        cur.execute(
            """
            INSERT INTO central_uuid_registry (
              uuid_value, resource_type, title_initial, title_current,
              source_table, source_pk, parent_uuid, owner_key, canonical_path,
              status, metadata_json, first_seen_at, last_seen_at, deleted_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s)
            """,
            (
                normalized, resource_type, title, title,
                _clean(source_table, 64), _clean(source_pk, 128), parent,
                _clean(owner_key, 255), _clean(canonical_path, 512), status,
                _json_text(metadata), datetime.now() if status == "deleted" else None,
            ),
        )
        registry_id = int(cur.lastrowid)
        cur.execute(
            "INSERT INTO central_uuid_title_history (registry_id, title, change_type) VALUES (%s,%s,'created')",
            (registry_id, title),
        )
    else:
        registry_id = int(existing["id"])
        previous_title = str(existing.get("title_current") or "")
        next_title = title or previous_title
        if title and title != previous_title:
            cur.execute(
                "INSERT INTO central_uuid_title_history (registry_id, title, change_type) VALUES (%s,%s,'rename')",
                (registry_id, title),
            )
        cur.execute(
            """
            UPDATE central_uuid_registry
               SET resource_type=%s,
                   title_current=%s,
                   source_table=%s,
                   source_pk=%s,
                   parent_uuid=%s,
                   owner_key=%s,
                   canonical_path=%s,
                   status=%s,
                   metadata_json=COALESCE(%s, metadata_json),
                   last_seen_at=NOW(),
                   deleted_at=CASE WHEN %s='deleted' THEN COALESCE(deleted_at,NOW()) ELSE NULL END
             WHERE id=%s
            """,
            (
                resource_type, next_title, _clean(source_table, 64), _clean(source_pk, 128),
                parent, _clean(owner_key, 255), _clean(canonical_path, 512), status,
                _json_text(metadata), status, registry_id,
            ),
        )
    return {
        "id": registry_id,
        "uuid": normalized,
        "resource_type": resource_type,
        "title": title,
        "status": status,
    }


def register_uuid(**kwargs) -> dict[str, Any] | None:
    ensure_uuid_registry_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        result = _upsert(cur, **kwargs)
        db.commit()
        return result
    finally:
        cur.close()
        db.close()


def mark_uuid_deleted(uuid_value: Any, *, reason: str = "source_deleted") -> None:
    normalized = normalize_uuid(uuid_value)
    if not normalized:
        return
    ensure_uuid_registry_schema()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE central_uuid_registry
               SET status='deleted', deleted_at=COALESCE(deleted_at,NOW()),
                   last_seen_at=NOW(),
                   metadata_json=JSON_SET(COALESCE(metadata_json, '{}'), '$.delete_reason', %s)
             WHERE uuid_value=%s
            """,
            (_clean(reason, 255), normalized),
        )
        db.commit()
    finally:
        cur.close()
        db.close()


def _status_from_dates(*, deleted_at: Any = None, expires_at: Any = None) -> str:
    if deleted_at:
        return "deleted"
    if expires_at:
        value = expires_at.date() if isinstance(expires_at, datetime) else expires_at
        if isinstance(value, date) and value < date.today():
            return "expired"
    return "active"


def sync_known_uuid_resources() -> dict[str, int]:
    """Backfill every durable business UUID without deleting registry rows."""
    ensure_uuid_registry_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    counts: dict[str, int] = {}

    def sync_rows(resource_type: str, query: str, mapper) -> None:
        cur.execute(query)
        rows = cur.fetchall() or []
        for row in rows:
            payload = mapper(row)
            if payload and _upsert(cur, resource_type=resource_type, **payload):
                counts[resource_type] = counts.get(resource_type, 0) + 1

    try:
        sync_rows(
            "UPLOAD",
            "SELECT id, uuid, title, username, created_at, expire_at, upload_deleted_at FROM uploads",
            lambda r: {
                "uuid_value": r["uuid"], "title": r.get("title") or "",
                "source_table": "uploads", "source_pk": r["id"], "owner_key": r.get("username") or "",
                "canonical_path": f"/view/{r['uuid']}",
                "status": _status_from_dates(deleted_at=r.get("upload_deleted_at"), expires_at=r.get("expire_at")),
            },
        )
        sync_rows(
            "ALBUM",
            "SELECT id, album_name, owner, event_id FROM albums",
            lambda r: {
                "uuid_value": r["id"], "title": r.get("album_name") or "",
                "source_table": "albums", "source_pk": r["id"], "owner_key": r.get("owner") or "",
                "canonical_path": f"/album/{r['id']}/", "status": "active",
                "metadata": {"event_id": r.get("event_id")},
            },
        )
        sync_rows(
            "ALBUM_CHILD",
            """
            SELECT c.id, c.album_id, c.name, a.album_name, a.owner
              FROM album_children c JOIN albums a ON a.id=c.album_id
            """,
            lambda r: {
                "uuid_value": r["id"], "title": f"{r.get('album_name') or ''} / {r.get('name') or ''}".strip(" /"),
                "source_table": "album_children", "source_pk": r["id"], "parent_uuid": r.get("album_id"),
                "owner_key": r.get("owner") or "", "canonical_path": f"/album/{r['album_id']}/view/{r['id']}",
                "status": "active",
            },
        )
        sync_rows(
            "EVENT",
            "SELECT id, event_uuid, title, owner_user_id, payment_uuid, created_at, deleted_at FROM mfu_event",
            lambda r: {
                "uuid_value": r["event_uuid"], "title": r.get("title") or "",
                "source_table": "mfu_event", "source_pk": r["id"], "owner_key": r.get("owner_user_id") or "",
                "canonical_path": f"/external-login/app/events/{normalize_uuid(r['event_uuid'])}",
                "status": _status_from_dates(deleted_at=r.get("deleted_at")),
            },
        )
        sync_rows(
            "PAYMENT",
            "SELECT id, event_uuid, payment_uuid, title, deleted_at FROM mfu_event WHERE payment_uuid IS NOT NULL AND payment_uuid<>''",
            lambda r: {
                "uuid_value": r["payment_uuid"], "title": r.get("title") or "",
                "source_table": "mfu_event", "source_pk": r["id"], "parent_uuid": r.get("event_uuid"),
                "canonical_path": f"/payment/admin/events/uuid/{r['payment_uuid']}",
                "status": _status_from_dates(deleted_at=r.get("deleted_at")),
            },
        )
        sync_rows(
            "LAYER_REPLY",
            """
            SELECT r.id, r.reply_uuid, r.title_snapshot, r.upload_id, r.posted_at,
                   u.uuid AS upload_uuid, u.title AS upload_title, u.username
              FROM layer_upload_replies r JOIN uploads u ON u.id=r.upload_id
            """,
            lambda r: {
                "uuid_value": r["reply_uuid"], "title": r.get("title_snapshot") or r.get("upload_title") or "",
                "source_table": "layer_upload_replies", "source_pk": r["id"], "parent_uuid": r.get("upload_uuid"),
                "owner_key": r.get("username") or "", "canonical_path": f"/layer_reply/{r['reply_uuid']}",
                "status": "active",
            },
        )
        sync_rows(
            "DM",
            """
            SELECT c.id, c.uuid, c.pair_key,
                   GROUP_CONCAT(NULLIF(p.display_name_cache,'') ORDER BY p.id SEPARATOR ' / ') AS display_names
              FROM chat_dm_conversations c
              LEFT JOIN chat_dm_participants p ON p.conversation_id=c.id
             GROUP BY c.id, c.uuid, c.pair_key
            """,
            lambda r: {
                "uuid_value": r["uuid"], "title": r.get("display_names") or "DM会話",
                "source_table": "chat_dm_conversations", "source_pk": r["id"],
                "canonical_path": f"/chat/dm/room/{r['uuid']}", "status": "active",
            },
        )
        sync_rows(
            "TICKET_BATCH",
            "SELECT id, event_name, created_at, expires_at FROM ticket_batches",
            lambda r: {
                "uuid_value": r["id"], "title": r.get("event_name") or "",
                "source_table": "ticket_batches", "source_pk": r["id"],
                "canonical_path": f"/tickets/{r['id']}",
                "status": _status_from_dates(expires_at=r.get("expires_at")),
            },
        )
        db.commit()
        return counts
    finally:
        cur.close()
        db.close()


def backfill_uuid_access_logs(*, batch_size: int = 2000) -> dict[str, int]:
    """Attach registry snapshots to legacy access logs without rewriting log text."""
    ensure_uuid_registry_schema()
    batch_size = max(100, min(int(batch_size or 2000), 10000))
    read_db = get_db()
    write_db = get_db()
    read_cur = read_db.cursor(dictionary=True)
    write_cur = write_db.cursor()
    try:
        read_cur.execute(
            """
            SELECT id, uuid_value, resource_type,
                   COALESCE(NULLIF(title_initial,''), title_current) AS historical_title,
                   status
              FROM central_uuid_registry
            """
        )
        registry = {
            str(row["uuid_value"]).replace("-", "").lower(): row
            for row in (read_cur.fetchall() or [])
        }
        last_id = 0
        scanned = matched = updated = 0
        pattern = r"[0-9A-Fa-f]{32}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
        while True:
            read_cur.execute(
                """
                SELECT id, path
                  FROM logs
                 WHERE id > %s AND resource_uuid='' AND path REGEXP %s
                 ORDER BY id
                 LIMIT %s
                """,
                (last_id, pattern, batch_size),
            )
            rows = read_cur.fetchall() or []
            if not rows:
                break
            last_id = int(rows[-1]["id"])
            scanned += len(rows)
            updates = []
            for row in rows:
                selected = None
                candidates = _UUID_PATTERN.findall(str(row.get("path") or ""))
                for candidate in reversed(candidates):
                    key = normalize_uuid(candidate).replace("-", "")
                    if key in registry:
                        selected = registry[key]
                        break
                if not selected:
                    continue
                matched += 1
                updates.append(
                    (
                        selected["id"], selected["resource_type"], selected["uuid_value"],
                        selected.get("historical_title") or "", selected.get("status") or "",
                        row["id"],
                    )
                )
            if updates:
                write_cur.executemany(
                    """
                    UPDATE logs
                       SET resource_registry_id=%s, resource_type=%s, resource_uuid=%s,
                           resource_title=%s, resource_status=%s
                     WHERE id=%s AND resource_uuid=''
                    """,
                    updates,
                )
                write_db.commit()
                updated += max(int(write_cur.rowcount or 0), 0)
        return {"scanned": scanned, "matched": matched, "updated": updated}
    finally:
        read_cur.close()
        write_cur.close()
        read_db.close()
        write_db.close()


def _lookup_registry(cur, normalized: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT id, uuid_value, resource_type, title_current, status, canonical_path,
               source_table, source_pk, parent_uuid
          FROM central_uuid_registry WHERE uuid_value=%s LIMIT 1
        """,
        (normalized,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]), "uuid": row["uuid_value"],
        "resource_type": row["resource_type"], "title": row["title_current"],
        "status": row["status"], "canonical_path": row.get("canonical_path") or "",
        "source_table": row.get("source_table") or "", "source_pk": row.get("source_pk") or "",
        "parent_uuid": row.get("parent_uuid") or "",
    }


def _discover_uuid(cur, normalized: str) -> dict[str, Any] | None:
    """Register a newly-created source row on its first UUID access."""
    compact = normalized.replace("-", "")
    discoveries = [
        (
            "SELECT id,uuid,title,username,expire_at,upload_deleted_at FROM uploads WHERE REPLACE(uuid,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["uuid"], resource_type="UPLOAD", title=r.get("title") or "",
                source_table="uploads", source_pk=r["id"], owner_key=r.get("username") or "",
                canonical_path=f"/view/{r['uuid']}",
                status=_status_from_dates(deleted_at=r.get("upload_deleted_at"), expires_at=r.get("expire_at")),
            ),
        ),
        (
            "SELECT id,album_name,owner,event_id FROM albums WHERE REPLACE(id,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["id"], resource_type="ALBUM", title=r.get("album_name") or "",
                source_table="albums", source_pk=r["id"], owner_key=r.get("owner") or "",
                canonical_path=f"/album/{r['id']}/", status="active",
            ),
        ),
        (
            "SELECT c.id,c.album_id,c.name,a.album_name,a.owner FROM album_children c JOIN albums a ON a.id=c.album_id WHERE REPLACE(c.id,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["id"], resource_type="ALBUM_CHILD",
                title=f"{r.get('album_name') or ''} / {r.get('name') or ''}".strip(" /"),
                source_table="album_children", source_pk=r["id"], parent_uuid=r.get("album_id"),
                owner_key=r.get("owner") or "", canonical_path=f"/album/{r['album_id']}/view/{r['id']}", status="active",
            ),
        ),
        (
            "SELECT id,event_uuid,title,owner_user_id,deleted_at FROM mfu_event WHERE event_uuid=UNHEX(%s) LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["event_uuid"], resource_type="EVENT", title=r.get("title") or "",
                source_table="mfu_event", source_pk=r["id"], owner_key=r.get("owner_user_id") or "",
                canonical_path=f"/external-login/app/events/{normalize_uuid(r['event_uuid'])}",
                status=_status_from_dates(deleted_at=r.get("deleted_at")),
            ),
        ),
        (
            "SELECT id,event_uuid,payment_uuid,title,deleted_at FROM mfu_event WHERE REPLACE(payment_uuid,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["payment_uuid"], resource_type="PAYMENT", title=r.get("title") or "",
                source_table="mfu_event", source_pk=r["id"], parent_uuid=r.get("event_uuid"),
                canonical_path=f"/payment/admin/events/uuid/{r['payment_uuid']}",
                status=_status_from_dates(deleted_at=r.get("deleted_at")),
            ),
        ),
        (
            "SELECT r.id,r.reply_uuid,r.title_snapshot,u.uuid AS upload_uuid,u.title AS upload_title,u.username FROM layer_upload_replies r JOIN uploads u ON u.id=r.upload_id WHERE REPLACE(r.reply_uuid,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["reply_uuid"], resource_type="LAYER_REPLY",
                title=r.get("title_snapshot") or r.get("upload_title") or "",
                source_table="layer_upload_replies", source_pk=r["id"], parent_uuid=r.get("upload_uuid"),
                owner_key=r.get("username") or "", canonical_path=f"/layer_reply/{r['reply_uuid']}", status="active",
            ),
        ),
        (
            "SELECT id,uuid FROM chat_dm_conversations WHERE REPLACE(uuid,'-','')=%s LIMIT 1",
            (compact,),
            lambda r: dict(
                uuid_value=r["uuid"], resource_type="DM", title="DM会話",
                source_table="chat_dm_conversations", source_pk=r["id"],
                canonical_path=f"/chat/dm/room/{r['uuid']}", status="active",
            ),
        ),
    ]
    for query, params, mapper in discoveries:
        cur.execute(query, params)
        row = cur.fetchone()
        if row:
            return _upsert(cur, **mapper(row))
    return None


def resolve_request_resource(flask_request) -> dict[str, Any] | None:
    """Resolve a durable UUID in the current route without registering random probes."""
    candidates: list[str] = []
    for raw in _UUID_PATTERN.findall(str(flask_request.path or "")):
        normalized = normalize_uuid(raw)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    for value in (flask_request.view_args or {}).values():
        normalized = normalize_uuid(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    event_id = (flask_request.view_args or {}).get("event_id")
    if not candidates and event_id is None:
        return None
    ensure_uuid_registry_schema()

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        # The final UUID in a nested route is normally the most specific
        # resource (child album/reply), while the first is its parent.
        for candidate in reversed(candidates):
            # Prefer the live source so renamed titles/status changes are
            # captured before this request is written to the access log.
            row = _discover_uuid(cur, candidate)
            if row:
                db.commit()
                return row
            # If the source was physically deleted, its immutable registry
            # row remains resolvable for audit purposes.
            row = _lookup_registry(cur, candidate)
            if row:
                cur.execute("UPDATE central_uuid_registry SET last_seen_at=NOW() WHERE id=%s", (row["id"],))
                db.commit()
                return row

        if event_id is not None:
            cur.execute(
                "SELECT id, event_uuid, title, owner_user_id, deleted_at FROM mfu_event WHERE id=%s LIMIT 1",
                (event_id,),
            )
            event = cur.fetchone()
            if event:
                row = _upsert(
                    cur, uuid_value=event["event_uuid"], resource_type="EVENT",
                    title=event.get("title") or "", source_table="mfu_event", source_pk=event["id"],
                    owner_key=event.get("owner_user_id") or "",
                    canonical_path=f"/external-login/app/events/{normalize_uuid(event['event_uuid'])}",
                    status=_status_from_dates(deleted_at=event.get("deleted_at")),
                )
                db.commit()
                return row
        return None
    finally:
        cur.close()
        db.close()
