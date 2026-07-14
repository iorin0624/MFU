"""Expiry notification and automatic normal-upload deletion service."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.utils.db import get_db
from app.utils.mail import send_mail
from app.utils.upload_deletion import delete_normal_upload
from app.utils.upload_notifications import send_discord_upload_notification


JST = timezone(timedelta(hours=9))
NOTICE_TIME = time(9, 0)
DELETE_TIME = time(0, 15)
LATE_NOTICE_GRACE = timedelta(hours=24)
RETRY_DELAY = timedelta(minutes=30)
MAX_NOTICE_ATTEMPTS = 5
DEFAULT_LIMIT = 500
ACTION_NOTICE_GATE = "notice_gate"
ACTION_NOTICE_NONE = "notice_none"
ACTION_NOTICE_DISCORD = "notice_discord"
ACTION_NOTICE_EMAIL = "notice_email"
ACTION_DELETE = "delete"
LOCK_NAME = "mfu_upload_expiry"


@dataclass(frozen=True)
class ExpirySchedule:
    notice_at: datetime
    delete_at: datetime


def coerce_expiry_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def schedule_for(expire_at) -> ExpirySchedule:
    expiry_date = coerce_expiry_date(expire_at)
    return ExpirySchedule(
        notice_at=datetime.combine(expiry_date - timedelta(days=1), NOTICE_TIME, tzinfo=JST),
        delete_at=datetime.combine(expiry_date + timedelta(days=1), DELETE_TIME, tzinfo=JST),
    )


def deletion_not_before(expire_at, notice_started_at: datetime) -> datetime:
    started = notice_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=JST)
    return max(schedule_for(expire_at).delete_at, started.astimezone(JST) + LATE_NOTICE_GRACE)


def notification_channels(notify_method: str | None) -> tuple[str, ...]:
    method = (notify_method or "none").strip().lower()
    if method == "discord":
        return (ACTION_NOTICE_DISCORD,)
    if method == "email":
        return (ACTION_NOTICE_EMAIL,)
    if method == "both":
        return (ACTION_NOTICE_DISCORD, ACTION_NOTICE_EMAIL)
    return (ACTION_NOTICE_NONE,)


def build_expiry_notice(
    upload: dict,
    *,
    public_base_url: str,
    notice_started_at: datetime | None = None,
) -> tuple[str, str]:
    expiry_date = coerce_expiry_date(upload["expire_at"])
    delete_at = (
        deletion_not_before(expiry_date, notice_started_at)
        if notice_started_at is not None
        else schedule_for(expiry_date).delete_at
    )
    title = str(upload.get("title") or "（タイトルなし）")
    list_url = f"{public_base_url.rstrip('/')}/upload_list"
    subject = "【MFU】アップロード有効期限のお知らせ"
    notice_date = _as_jst(notice_started_at).date() if notice_started_at is not None else None
    if notice_date is not None and notice_date > expiry_date:
        introduction = (
            "アップロードしたデータの有効期限を過ぎています。\n"
            "初回導入時の安全措置として、この通知から24時間以上の猶予を設けています。"
        )
    elif notice_date == expiry_date:
        introduction = (
            "アップロードしたデータの有効期限は本日までです。\n"
            "前日通知が遅れたため、この通知から24時間以上の猶予を設けています。"
        )
    else:
        introduction = "アップロードしたデータの有効期限が明日までとなっています。"
    body = (
        f"{introduction}\n\n"
        f"タイトル: {title}\n"
        f"有効期限: {expiry_date:%Y年%m月%d日} 23:59（日本時間）\n"
        f"自動削除予定: {delete_at:%Y年%m月%d日 %H:%M}以降（日本時間）\n"
        f"確認: {list_url}\n\n"
        "自動削除されるのは通常アップロードのファイルです。\n"
        "レイヤーアップロードのデータは削除されません。"
    )
    return subject, body


def _mysql_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(JST).replace(tzinfo=None)


def _as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def ensure_upload_expiry_schema(*, db_factory: Callable = get_db) -> None:
    db = db_factory()
    cursor = db.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_expiry_actions (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                upload_id BIGINT NOT NULL,
                expire_at DATE NOT NULL,
                action VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                attempts INT UNSIGNED NOT NULL DEFAULT 0,
                next_attempt_at DATETIME NULL,
                started_at DATETIME NULL,
                completed_at DATETIME NULL,
                last_error TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_upload_expiry_action (upload_id, expire_at, action),
                KEY idx_upload_expiry_retry (status, next_attempt_at),
                KEY idx_upload_expiry_upload (upload_id, expire_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _fetch_candidates(cursor, *, through_date: date, limit: int) -> list[dict]:
    cursor.execute(
        """
        SELECT u.id, u.uuid, u.title, u.expire_at, u.username,
               usr.email, usr.webhook_url, usr.notify_method
          FROM uploads AS u
          LEFT JOIN users AS usr ON usr.username = u.username
         WHERE u.upload_deleted_at IS NULL
           AND u.expire_at IS NOT NULL
           AND u.expire_at <= %s
         ORDER BY u.expire_at DESC, u.id ASC
         LIMIT %s
        """,
        (through_date, int(limit)),
    )
    return list(cursor.fetchall())


def _fetch_actions(cursor, *, upload_id: int, expire_at: date) -> dict[str, dict]:
    cursor.execute(
        """
        SELECT * FROM upload_expiry_actions
         WHERE upload_id = %s AND expire_at = %s
        """,
        (upload_id, expire_at),
    )
    return {row["action"]: row for row in cursor.fetchall()}


def _ensure_notice_state(db, cursor, *, upload: dict, now: datetime) -> dict[str, dict]:
    expiry_date = coerce_expiry_date(upload["expire_at"])
    now_db = _mysql_datetime(now)
    cursor.execute(
        """
        INSERT IGNORE INTO upload_expiry_actions
            (upload_id, expire_at, action, status, attempts, started_at, completed_at,
             created_at, updated_at)
        VALUES (%s, %s, %s, 'succeeded', 1, %s, %s, %s, %s)
        """,
        (upload["id"], expiry_date, ACTION_NOTICE_GATE, now_db, now_db, now_db, now_db),
    )
    gate_created = cursor.rowcount == 1
    if gate_created:
        for action in notification_channels(upload.get("notify_method")):
            skipped = action == ACTION_NOTICE_NONE
            cursor.execute(
                """
                INSERT INTO upload_expiry_actions
                    (upload_id, expire_at, action, status, attempts, completed_at,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    upload["id"], expiry_date, action,
                    "skipped" if skipped else "pending", 0,
                    now_db if skipped else None, now_db, now_db,
                ),
            )
    db.commit()
    return _fetch_actions(cursor, upload_id=upload["id"], expire_at=expiry_date)


def _claim_notice_action(db, cursor, *, action: dict, now: datetime) -> bool:
    if action["action"] not in (ACTION_NOTICE_DISCORD, ACTION_NOTICE_EMAIL):
        return False
    if action["status"] in ("succeeded", "skipped"):
        return False
    if int(action.get("attempts") or 0) >= MAX_NOTICE_ATTEMPTS:
        return False
    next_attempt_at = action.get("next_attempt_at")
    if next_attempt_at and _as_jst(next_attempt_at) > now:
        return False
    if action["status"] == "processing" and _as_jst(action["updated_at"]) + RETRY_DELAY > now:
        return False

    cursor.execute(
        """
        UPDATE upload_expiry_actions
           SET status='processing', attempts=attempts+1, started_at=%s,
               next_attempt_at=NULL, last_error=NULL, updated_at=%s
         WHERE id=%s AND status NOT IN ('succeeded', 'skipped') AND attempts < %s
        """,
        (_mysql_datetime(now), _mysql_datetime(now), action["id"], MAX_NOTICE_ATTEMPTS),
    )
    changed = cursor.rowcount == 1
    db.commit()
    return changed


def _finish_action(db, cursor, *, action_id: int, now: datetime, success: bool, error: str | None = None) -> None:
    cursor.execute(
        """
        UPDATE upload_expiry_actions
           SET status=%s, completed_at=%s, next_attempt_at=%s,
               last_error=%s, updated_at=%s
         WHERE id=%s
        """,
        (
            "succeeded" if success else "failed",
            _mysql_datetime(now) if success else None,
            None if success else _mysql_datetime(now + RETRY_DELAY),
            None if success else (error or "notification failed")[:4000],
            _mysql_datetime(now), action_id,
        ),
    )
    db.commit()


def _send_notice(
    *, upload: dict, action: str, subject: str, body: str, logger,
    mail_sender: Callable, discord_sender: Callable,
) -> tuple[bool, str | None]:
    try:
        if action == ACTION_NOTICE_EMAIL:
            email = (upload.get("email") or "").strip()
            if not email:
                return False, "email address is not configured"
            mail_sender(email, subject, body, mail_kind="upload_expiry_notice")
            return True, None

        if action == ACTION_NOTICE_DISCORD:
            webhook = (upload.get("webhook_url") or "").strip()
            if not webhook:
                return False, "Discord webhook is not configured"
            ok = discord_sender(
                logger=logger,
                username=upload.get("username") or "",
                notify_method="discord",
                webhook_url=webhook,
                upload_id=upload.get("uuid") or "",
                message=f"{subject}\n\n{body}",
                context_label="[upload-expiry]",
            )
            return (True, None) if ok else (False, "Discord notification failed")
    except Exception as exc:
        logger.exception("upload expiry notification failed: uuid=%s action=%s", upload.get("uuid"), action)
        return False, repr(exc)
    return False, f"unsupported notice action: {action}"


def _ensure_delete_action(db, cursor, *, upload: dict, now: datetime) -> dict:
    expiry_date = coerce_expiry_date(upload["expire_at"])
    now_db = _mysql_datetime(now)
    cursor.execute(
        """
        INSERT IGNORE INTO upload_expiry_actions
            (upload_id, expire_at, action, status, attempts, created_at, updated_at)
        VALUES (%s, %s, %s, 'pending', 0, %s, %s)
        """,
        (upload["id"], expiry_date, ACTION_DELETE, now_db, now_db),
    )
    db.commit()
    return _fetch_actions(cursor, upload_id=upload["id"], expire_at=expiry_date)[ACTION_DELETE]


def _claim_delete_action(db, cursor, *, action: dict, now: datetime) -> bool:
    if action["status"] in ("succeeded", "skipped"):
        return False
    next_attempt_at = action.get("next_attempt_at")
    if next_attempt_at and _as_jst(next_attempt_at) > now:
        return False
    if action["status"] == "processing" and _as_jst(action["updated_at"]) + RETRY_DELAY > now:
        return False
    cursor.execute(
        """
        UPDATE upload_expiry_actions
           SET status='processing', attempts=attempts+1, started_at=%s,
               next_attempt_at=NULL, last_error=NULL, updated_at=%s
         WHERE id=%s AND status NOT IN ('succeeded', 'skipped')
        """,
        (_mysql_datetime(now), _mysql_datetime(now), action["id"]),
    )
    changed = cursor.rowcount == 1
    db.commit()
    return changed


def _finish_delete_action(db, cursor, *, action_id: int, now: datetime, success: bool, error: str | None = None) -> None:
    cursor.execute(
        """
        UPDATE upload_expiry_actions
           SET status=%s, completed_at=%s, next_attempt_at=%s,
               last_error=%s, updated_at=%s
         WHERE id=%s
        """,
        (
            "succeeded" if success else "failed",
            _mysql_datetime(now) if success else None,
            None if success else _mysql_datetime(now + RETRY_DELAY),
            None if success else (error or "deletion failed")[:4000],
            _mysql_datetime(now), action_id,
        ),
    )
    db.commit()


def _action_created_at(action: dict) -> datetime:
    return _as_jst(action["created_at"])


def run_upload_expiry(
    *, dry_run: bool = False, now: datetime | None = None, limit: int = DEFAULT_LIMIT,
    storage_root: str | Path = "/mnt/mfu/uploads",
    public_base_url: str = "https://mfu.iori0624.jp",
    db_factory: Callable = get_db, mail_sender: Callable = send_mail,
    discord_sender: Callable = send_discord_upload_notification,
    deletion_func: Callable = delete_normal_upload, logger=None,
) -> dict:
    logger = logger or logging.getLogger("mfu.upload_expiry")
    current = _as_jst(now or datetime.now(JST))
    result = {
        "dry_run": dry_run, "now": current.isoformat(), "candidates": 0,
        "notice_due": 0, "notice_sent": 0, "notice_failed": 0,
        "notice_skipped": 0, "delete_due": 0, "deleted": 0,
        "delete_failed": 0, "grace_deferred": 0, "locked": False,
        "details": [],
    }

    db = db_factory()
    cursor = db.cursor(dictionary=True)
    lock_acquired = False
    try:
        if not dry_run:
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LOCK_NAME,))
            lock_acquired = bool((cursor.fetchone() or {}).get("acquired"))
            if not lock_acquired:
                result["locked"] = True
                return result

        candidates = _fetch_candidates(
            cursor,
            through_date=current.date() + timedelta(days=1),
            limit=max(1, min(int(limit), 5000)),
        )
        result["candidates"] = len(candidates)

        for upload in candidates:
            expiry_date = coerce_expiry_date(upload["expire_at"])
            schedule = schedule_for(expiry_date)
            if current < schedule.notice_at:
                continue
            result["notice_due"] += 1
            actions = _fetch_actions(cursor, upload_id=upload["id"], expire_at=expiry_date)
            gate = actions.get(ACTION_NOTICE_GATE)

            if dry_run:
                detail = {
                    "uuid": upload["uuid"], "username": upload.get("username"),
                    "expire_at": expiry_date.isoformat(),
                    "would_initialize_notice": gate is None,
                    "channels": [item.removeprefix("notice_") for item in notification_channels(upload.get("notify_method"))],
                }
                if gate:
                    eligible_at = deletion_not_before(expiry_date, _action_created_at(gate))
                    detail["delete_not_before"] = eligible_at.isoformat()
                    detail["would_delete"] = current >= eligible_at
                    if current >= eligible_at:
                        result["delete_due"] += 1
                elif current >= schedule.delete_at:
                    detail["would_delete"] = False
                    detail["reason"] = "late notice requires 24-hour grace"
                    result["grace_deferred"] += 1
                result["details"].append(detail)
                continue

            if gate is None:
                actions = _ensure_notice_state(db, cursor, upload=upload, now=current)
                gate = actions[ACTION_NOTICE_GATE]
                if ACTION_NOTICE_NONE in actions:
                    result["notice_skipped"] += 1

            subject, body = build_expiry_notice(
                upload,
                public_base_url=public_base_url,
                notice_started_at=_action_created_at(gate),
            )
            for action_name in (ACTION_NOTICE_DISCORD, ACTION_NOTICE_EMAIL):
                action = actions.get(action_name)
                if not action or not _claim_notice_action(db, cursor, action=action, now=current):
                    continue
                ok, error = _send_notice(
                    upload=upload, action=action_name, subject=subject, body=body,
                    logger=logger, mail_sender=mail_sender, discord_sender=discord_sender,
                )
                _finish_action(db, cursor, action_id=action["id"], now=current, success=ok, error=error)
                result["notice_sent" if ok else "notice_failed"] += 1

            eligible_at = deletion_not_before(expiry_date, _action_created_at(gate))
            if current < eligible_at:
                if current >= schedule.delete_at:
                    result["grace_deferred"] += 1
                continue

            result["delete_due"] += 1
            delete_action = actions.get(ACTION_DELETE) or _ensure_delete_action(db, cursor, upload=upload, now=current)
            if not _claim_delete_action(db, cursor, action=delete_action, now=current):
                continue
            try:
                deletion_func(
                    upload_id=upload["id"], uuid=upload["uuid"],
                    storage_root=storage_root, db_factory=db_factory,
                )
                _finish_delete_action(db, cursor, action_id=delete_action["id"], now=current, success=True)
                result["deleted"] += 1
                result["details"].append({"uuid": upload["uuid"], "deleted": True})
            except Exception as exc:
                logger.exception("automatic upload deletion failed: uuid=%s", upload.get("uuid"))
                _finish_delete_action(
                    db, cursor, action_id=delete_action["id"], now=current,
                    success=False, error=repr(exc),
                )
                result["delete_failed"] += 1
                result["details"].append({"uuid": upload["uuid"], "deleted": False, "error": repr(exc)})
    finally:
        if lock_acquired:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            except Exception:
                logger.warning("failed to release upload expiry advisory lock", exc_info=True)
        db.close()

    return result


def configured_storage_root(app) -> Path:
    return Path(app.config.get("STORAGE_ROOT", os.environ.get("UPLOAD_STORAGE_ROOT", "/mnt/mfu/uploads"))).resolve()
