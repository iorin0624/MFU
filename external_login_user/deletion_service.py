# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from flask import current_app

from app.utils.db import get_db
from .identity_lock import lock_deleted_identity

_AVATAR_ROOT = Path("/mnt/mfu/avatars")


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME=%s
        """,
        (table,),
    )
    rows = cur.fetchall() or []
    cols: set[str] = set()
    for row in rows:
        if isinstance(row, tuple):
            cols.add(str(row[0]))
        else:
            cols.add(str(row.get("COLUMN_NAME")))
    return cols


def anonymize_external_user(*, user_id: int, executed_by: str, reason: str | None = None) -> dict[str, Any]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    now_token = int(time.time())
    avatar_file_for_delete = None
    changed = {
        "identity_locked": 0,
        "cards_soft_deleted": 0,
        "push_deleted": 0,
        "resume_tokens_deleted": 0,
        "email_verifications_deleted": 0,
        "notifications_deleted": 0,
        "album_process_deleted": 0,
        "payment_requests_canceled": 0,
        "future_memberships_canceled": 0,
        "past_memberships_scrubbed": 0,
    }
    try:
        cur.execute("START TRANSACTION")

        cur.execute("SELECT * FROM external_login_user WHERE id=%s FOR UPDATE", (int(user_id),))
        user = cur.fetchone()
        if not user:
            raise ValueError("external user not found")

        user_cols = _table_columns(cur, "external_login_user")
        if int(user.get("is_deleted") or 0) == 1:
            db.commit()
            return {
                "ok": True,
                "already_deleted": True,
                "user_id": int(user_id),
                "anonymized_nickname": f"退会済みユーザー#{int(user_id)}",
                "changed": changed,
            }

        anonymized_nickname = f"退会済みユーザー#{int(user_id)}"
        deleted_social_id = f"deleted:{int(user_id)}:{now_token}"
        original_social_id = str(user.get("social_id") or "").strip()
        avatar_file_for_delete = (user.get("avatar_file") or "").strip() or None
        deletion_note = f"[anonymized {time.strftime('%Y-%m-%d %H:%M:%S')} by {executed_by}]"

        lock_deleted_identity(
            cur,
            provider="line",
            social_id=original_social_id,
            user_id=int(user_id),
            deleted_by=(executed_by or "admin"),
            reason=reason,
        )
        changed["identity_locked"] = 1

        set_sql = [
            "nickname=%s",
            "social_id=%s",
            "x_id=NULL",
            "instagram_id=NULL",
            "email=NULL",
            "avatar_file=NULL",
            "avatar_url=NULL",
            "chat_admin_alias=0",
            "notification_unread_reminder_last_sent_at=NULL",
            "privacy_policy_agreed_at=NULL",
            "privacy_policy_agreed_revised_date=NULL",
            "is_deleted=1",
            "deleted_at=NOW()",
            "deleted_by=%s",
            "deletion_reason=%s",
            "anonymized_at=NOW()",
        ]
        vals: list[Any] = [anonymized_nickname, deleted_social_id, (executed_by or "admin")[:80], (reason or None)]
        if "admin_note" in user_cols:
            set_sql.append("admin_note=%s")
            vals.append(deletion_note)
        if "email_verified_at" in user_cols:
            set_sql.append("email_verified_at=NULL")
        if "notify_album_upload" in user_cols:
            set_sql.append("notify_album_upload=0")
        if "notify_album_process" in user_cols:
            set_sql.append("notify_album_process=0")
        if "payment_mode" in user_cols:
            set_sql.append("payment_mode='manual'")
        if "updated_at" in user_cols:
            set_sql.append("updated_at=NOW()")
        vals.append(int(user_id))
        cur.execute(
            f"UPDATE external_login_user SET {', '.join(set_sql)} WHERE id=%s LIMIT 1",
            tuple(vals),
        )

        card_cols = _table_columns(cur, "external_login_user_card_data")
        card_set = ["deleted_at=NOW()"]
        if "is_default" in card_cols:
            card_set.append("is_default=0")
        cur.execute(
            f"""
            UPDATE external_login_user_card_data
               SET {", ".join(card_set)}
             WHERE user_id=%s
               AND deleted_at IS NULL
            """,
            (int(user_id),),
        )
        changed["cards_soft_deleted"] = int(cur.rowcount or 0)

        cur.execute(
            """
            DELETE FROM chat_push_subscriptions
             WHERE actor_type='line'
               AND actor_id=CAST(%s AS CHAR)
            """,
            (int(user_id),),
        )
        changed["push_deleted"] = int(cur.rowcount or 0)

        cur.execute("DELETE FROM external_login_resume_token WHERE ext_user_id=%s", (int(user_id),))
        changed["resume_tokens_deleted"] = int(cur.rowcount or 0)

        cur.execute("DELETE FROM mfu_email_verification WHERE user_id=%s", (int(user_id),))
        changed["email_verifications_deleted"] = int(cur.rowcount or 0)

        cur.execute("DELETE FROM mfu_notifications WHERE user_kind='external' AND user_id=%s", (int(user_id),))
        changed["notifications_deleted"] = int(cur.rowcount or 0)

        cur.execute("DELETE FROM album_process WHERE ext_user_id=%s", (int(user_id),))
        changed["album_process_deleted"] = int(cur.rowcount or 0)

        cur.execute(
            """
            UPDATE mfu_payment_request
               SET nickname=%s,
                   x_id=NULL,
                   instagram_id=NULL,
                   buyer_email=NULL,
                   status=CASE WHEN status='pending' THEN 'canceled' ELSE status END
             WHERE user_id=%s
            """,
            (anonymized_nickname, int(user_id)),
        )
        changed["payment_requests_canceled"] = int(cur.rowcount or 0)

        member_cols = _table_columns(cur, "mfu_event_member")
        scrub_parts = []
        if "contact_memo" in member_cols:
            scrub_parts.append("contact_memo=NULL")
        if "admin_note" in member_cols:
            scrub_parts.append("admin_note=NULL")
        if "receipt_note" in member_cols:
            scrub_parts.append("receipt_note=NULL")
        if "costume_label" in member_cols:
            scrub_parts.append("costume_label=NULL")
        if scrub_parts:
            cur.execute(
                f"UPDATE mfu_event_member SET {', '.join(scrub_parts)} WHERE user_id=%s",
                (int(user_id),),
            )
            changed["past_memberships_scrubbed"] = int(cur.rowcount or 0)

        cancel_parts = []
        if "is_canceled" in member_cols:
            cancel_parts.append("m.is_canceled=1")
        if "canceled_at" in member_cols:
            cancel_parts.append("m.canceled_at=NOW()")
        if "canceled_by" in member_cols:
            cancel_parts.append("m.canceled_by='user_deleted'")
        if cancel_parts:
            cur.execute(
                f"""
                UPDATE mfu_event_member AS m
                JOIN mfu_event AS e ON e.id=m.event_id
                   SET {', '.join(cancel_parts)}
                 WHERE m.user_id=%s
                   AND (e.starts_at IS NULL OR e.starts_at >= NOW())
                """,
                (int(user_id),),
            )
            changed["future_memberships_canceled"] = int(cur.rowcount or 0)

        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.exception("external user anonymize failed user_id=%s", user_id)
        raise
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    if avatar_file_for_delete:
        try:
            path = (_AVATAR_ROOT / avatar_file_for_delete).resolve()
            if path.exists() and str(path).startswith(str(_AVATAR_ROOT.resolve())):
                path.unlink(missing_ok=True)
        except Exception:
            current_app.logger.warning("avatar unlink failed user_id=%s file=%s", user_id, avatar_file_for_delete)

    current_app.logger.info(
        "external user anonymized user_id=%s executed_by=%s changed=%s",
        user_id,
        executed_by,
        changed,
    )
    return {
        "ok": True,
        "already_deleted": False,
        "user_id": int(user_id),
        "anonymized_nickname": f"退会済みユーザー#{int(user_id)}",
        "changed": changed,
    }
