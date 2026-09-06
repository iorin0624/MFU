# -*- coding: utf-8 -*-
from __future__ import annotations
import csv
import math
import os
import re
import textwrap
from pathlib import Path
from functools import lru_cache
from flask import request, jsonify
from email.header import Header
from email.utils import formataddr
from flask import current_app
from .utils import (
    _event_admin_can_view,
    _event_admin_can_manage,
    _ensure_event_invite_token,
    _event_invite_url,
    QR_TRADEMARK_NOTICE,
    _get_external_document_config,
    _get_current_privacy_policy_config,
    _get_current_commerce_law_config,
    _get_current_participant_terms_config,
    _is_privacy_policy_effective,
    _is_participant_terms_effective,
    _privacy_policy_date_label,
    DEFAULT_EVENT_THEME_COLOR,
    normalize_event_theme_color,
)

from app.utils.mail import send_mail
from app.utils.push import send_external_event_push
from .event_push import notify_member_payment_push, notify_member_status_push


from io import StringIO, BytesIO
from flask import request, session, redirect, url_for, render_template, abort, flash, make_response, send_file
from . import bp
from .utils import (
    _require_mfu_login_redirect, _admin_csrf_token, _uuid_bytes_to_str, _event_admin_can_view, update_event_member_status,
)
from .albums import create_event_album
from .album_naming import format_event_album_name
from .payments import _ensure_payment_uuid_for_event
from app.utils.db import get_db


def _normalize_document_admin_url(raw_url: str | None) -> str | None:
    value = (raw_url or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return None


def _parse_document_revised_date(raw_value: str | None):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _calculate_event_fee(studio_fee_yen: int | float | None,
                         fee_rate_percent: int | float | None,
                         admin_fee_yen: int | float | None,
                         payers: int | None,
                         fee_calc_method: str | None = "legacy") -> int | None:
    if studio_fee_yen in (None, "") or fee_rate_percent in (None, "") or payers in (None, 0):
        return None
    admin_fee_value = float(admin_fee_yen or 0)
    studio_fee_value = float(studio_fee_yen)
    fee_rate_value = float(fee_rate_percent)
    method = "new" if fee_calc_method == "new" else "legacy"

    per_person = studio_fee_value / payers
    if method == "new":
        base = per_person + admin_fee_value
        with_fee = base * (1 + (fee_rate_value / 100))
        total = math.ceil(with_fee / 10) * 10
    else:
        with_fee = per_person * (1 + (fee_rate_value / 100))
        total = math.ceil((with_fee + admin_fee_value) / 10) * 10
    return int(total)


def _calculate_square_net_amounts(final_fee_yen: int | None,
                                  square_fee_rate_percent: int | float | None,
                                  payers: int | None) -> dict:
    try:
        rate = float(square_fee_rate_percent) if square_fee_rate_percent not in (None, "") else 3.6
    except Exception:
        rate = 3.6

    result = {
        "square_fee_rate_percent": rate,
        "net_per_person": None,
        "square_fee_per_person": None,
        "net_total": None,
    }
    if final_fee_yen is None or int(final_fee_yen) <= 0:
        return result

    fee_value = int(final_fee_yen)
    net_per_person = math.floor(fee_value / (1 + rate / 100))
    square_fee_per_person = fee_value - net_per_person
    net_total = net_per_person * int(payers) if payers is not None and int(payers) > 0 else None

    result.update({
        "net_per_person": net_per_person,
        "square_fee_per_person": square_fee_per_person,
        "net_total": net_total,
    })
    return result


def _recalc_event_fee_if_auto(event_id: int) -> bool:
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT studio_fee_yen, fee_rate_percent, admin_fee_yen,
                   COALESCE(fee_auto_calc, 1) AS fee_auto_calc,
                   COALESCE(fee_calc_method, 'legacy') AS fee_calc_method,
                   square_fee_rate_percent
              FROM mfu_event
             WHERE id=%s
             LIMIT 1
        """, (event_id,))
        ev = cur.fetchone()
        if not ev:
            return False
        if not int(ev.get("fee_auto_calc") or 0):
            return False

        cur.execute("""
            SELECT COUNT(*) AS cnt
              FROM mfu_event_member
             WHERE event_id=%s
               AND COALESCE(require_payment, 1)=1
        """, (event_id,))
        row = cur.fetchone()
        payers = row.get("cnt") if row else 0

        total = _calculate_event_fee(
            ev.get("studio_fee_yen"),
            ev.get("fee_rate_percent"),
            ev.get("admin_fee_yen"),
            payers,
            ev.get("fee_calc_method") or "legacy",
        )
        if total is None:
            return False

        cur.execute("""
            UPDATE mfu_event
               SET fee_yen=%s
             WHERE id=%s
             LIMIT 1
        """, (total, event_id))
        db.commit()
        return True
    except Exception:
        try: db.rollback()
        except Exception: pass
        current_app.logger.exception("auto fee calc failed (event_id=%s)", event_id)
        return False
    finally:
        try: cur.close(); db.close()
        except Exception: pass
from urllib.parse import quote_plus
from flask import request, jsonify
from app.utils.db import get_db
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename


_AVATAR_ROOT = Path("/mnt/mfu/avatars")

# 既存の import 群と同じファイル内（関数定義の上の方）に追加
def _update_member_status_and_notify(event_id: int, user_id: int, new_status: str):
    """
    mfu_event_member.status を new_status に更新。
    変化があれば対象者へ「イベント名 <UUID@mail.iori0624.jp>」でメール通知。
    戻り: (ok: bool, msg: str, applied_status: str)
    """
    if new_status not in ("approved", "rejected", "pending"):
        return False, "invalid status", new_status

    starts_at = None
    db = get_db(); cur = db.cursor()

    # イベント基本情報（件名/From生成）
    cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id, ))
    ev_row = cur.fetchone()
    if not ev_row:
        cur.close(); db.close()
        return False, "event not found", new_status
    ev_title  = ev_row[0] if isinstance(ev_row, tuple) else ev_row["title"]
    ev_uuid_b = ev_row[1] if isinstance(ev_row, tuple) else ev_row["event_uuid"]
    ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""
    view_url = f"https://mfu.iori0624.jp/external-login/events/view/{ev_uuid_str}"

    # 対象メンバーの現状
    cur.execute("""
        SELECT m.status, u.email, u.nickname
          FROM mfu_event_member m
          JOIN external_login_user u ON u.id=m.user_id
         WHERE m.event_id=%s AND m.user_id=%s
         LIMIT 1
    """, (event_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        return False, "member not found", new_status
    old_status = row[0] if isinstance(row, tuple) else row["status"]
    to_email   = row[1] if isinstance(row, tuple) else row["email"]
    nickname   = row[2] if isinstance(row, tuple) else row["nickname"]

    # 変化なしならそのままOK
    if old_status == new_status:
        cur.close(); db.close()
        return True, "no change", new_status

    # 更新（承認遷移時の System 自動投稿は共通関数で処理）
    update_event_member_status(event_id=event_id, user_id=user_id, new_status=new_status)

    # メール通知（宛先・UUIDがあれば）→ send_mail 統一
    if to_email and ev_uuid_str:
        try:
            from flask import current_app
            # ★ 日本語ラベルへの変換
            STATUS_JA = {"approved": "承認", "rejected": "拒否", "pending": "保留"}
            old_j = STATUS_JA.get(old_status, old_status)
            new_j = STATUS_JA.get(new_status, new_status)

            subject = f"【{ev_title}】参加ステータスが更新されました"
            body = (
                f"{nickname or '参加者'} 様\n\n"
                f"イベント「{ev_title}」の参加ステータスが「{old_j}」から「{new_j}」に更新されました。\n"
                f"詳細は以下のページをご確認ください。\n{view_url}\n"
            )

            send_mail(
                to=to_email,
                subject=subject,
                body=body,
                event_uuid=ev_uuid_str,  # From: <UUID@mail.iori0624.jp>
                from_display_name=f"{ev_title} by Mimoria",
            )
        except Exception as e:
            current_app.logger.exception("status notify mail failed to %s: %s", to_email, e)

    notify_member_status_push(
        event_id=event_id,
        user_id=user_id,
        old_status=old_status,
        new_status=new_status,
    )

    cur.close(); db.close()
    return True, "ok", new_status


def _ensure_member_memo_columns():
    from app.utils.db import get_db
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM mfu_event_member")
        cols = { (r[0] if isinstance(r, tuple) else r.get("Field")) for r in (cur.fetchall() or []) }
        def _add(sql):
            try:
                cur.execute(sql); db.commit()
            except Exception:
                try: db.rollback()
                except Exception: pass
        if "contact_memo" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN contact_memo TEXT NULL")
        if "admin_note" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN admin_note TEXT NULL")
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _ensure_event_soft_delete_columns():
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM mfu_event")
        cols = {(r[0] if isinstance(r, tuple) else r.get("Field")) for r in (cur.fetchall() or [])}

        def _add(sql):
            try:
                cur.execute(sql)
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        if "deleted_at" not in cols:
            _add("ALTER TABLE mfu_event ADD COLUMN deleted_at DATETIME NULL")
        if "deleted_by" not in cols:
            _add("ALTER TABLE mfu_event ADD COLUMN deleted_by VARCHAR(80) NULL")
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

def _pick_first(d: Dict[str, Any], keys: Tuple[str, ...], default=None):
    """辞書 d から候補 keys のうち最初に見つかったキーの値を返す（無ければ default）。"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _derive_pay_status(member: Dict[str, Any]) -> Tuple[str, str]:
    """
    参加者1件の支払ステータスを「未／確認／済」に正規化して (label, bootstrap_color) を返す。
    - label: '未' | '確認' | '済'
    - bootstrap_color: 'danger' | 'warning' | 'success'
    判定はよくあるカラム名を幅広く見る。存在しないカラムは無視される。
    """

    # 支払方法（想定される候補名を順に拾う）
    method = str(_pick_first(member, (
        'payment_method', 'pay_method', 'method', 'selected_payment_method',
    ), '') or '').lower()

    # 「決済が完了している」可能性のあるフラグ・日時
    paid_truthy = bool(_pick_first(member, (
        'is_paid', 'paid', 'payment_ok', 'payment_done', 'paid_flag', 'pay_done',
        'square_paid', 'card_paid', 'card_ok', 'paid_at',
        'payment_completed_at', 'completed_at',
    ), False))

    # 「入金申告／送金申告があり、確認待ち」になり得るフラグ
    confirm_pending_truthy = bool(_pick_first(member, (
        'bank_notified', 'bank_notice', 'transfer_notified', 'transfer_notice',
        'paypay_notified', 'paypay_notice', 'manual_review_pending',
        'requires_staff_check', 'need_check', 'pending_review',
        'proof_uploaded', 'remittance_reported',
    ), False))

    # 「手動確認が完了」になり得るフラグ
    manual_confirmed_truthy = bool(_pick_first(member, (
        'bank_confirmed', 'transfer_confirmed', 'paypay_confirmed',
        'manual_confirmed', 'checked', 'verified',
    ), False))

    # 最終的な支払済み判定（カード系は自動、銀行/PayPay は手動確認でも可）
    is_paid = paid_truthy or manual_confirmed_truthy

    # 銀行振込 or PayPay では申告があり確認待ち → 「確認」
    if not is_paid and method in ('bank', 'bank_transfer', '振込', 'paypay', 'paypay_friend', 'paypay_friend_send'):
        if confirm_pending_truthy:
            return ('確認', 'warning')

    # 既に支払済み
    if is_paid:
        return ('済', 'success')

    # それ以外は「未」
    return ('未', 'danger')

def _normalize_role_for_db(db, role: str) -> tuple[str, bool]:
    """
    DBのENUM許容値を見て、入れようとしているroleが許容されない場合は安全値へ退避する。
    戻り値: (保存するrole, degradedフラグ)
    """
    role = (role or "none").strip().lower()
    try:
        cur = db.cursor()
        cur.execute("SHOW COLUMNS FROM mfu_event_member LIKE 'participant_role'")
        row = cur.fetchone()
        cur.close()
        if not row:
            # 列情報が取れない場合は保守的に許容
            return role, False
        # row[1] が Type。例: "enum('none','camera','assistant','cosplayer')"
        type_str = (row[1] or "").lower()
        # 文字列からenum要素をざっくり抽出
        if type_str.startswith("enum(") and type_str.endswith(")"):
            inside = type_str[5:-1]
            allowed = [s.strip().strip("'").strip('"') for s in inside.split(",")]
        else:
            allowed = []

        if role in allowed:
            return role, False

        # 許容されない → フォールバック方針：
        # - other は UI 上「メモ欄ON」の意味を保ちたいので、最も近い 'cosplayer' に寄せる
        # - それも未許容なら 'none'
        if role == "other":
            if "cosplayer" in allowed:
                return "cosplayer", True
            elif "none" in allowed:
                return "none", True
            else:
                # 何も無ければそのまま（最悪再度エラーするが、その方が気づける）
                return role, True
        # その他の未知値は 'none' があれば退避
        if "none" in allowed:
            return "none", True
        return role, True
    except Exception:
        # エラー時は何もいじらない（落ちないことを優先）
        return role, False



@bp.route("/admin")
def admin_home():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    # 直接イベント一覧へ飛ばす
    return redirect(url_for("external_login_user.admin_events_list"))


@bp.route("/admin/privacy-policy", methods=["GET", "POST"])
def admin_privacy_policy():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    admin_csrf = _admin_csrf_token()
    config = _get_external_document_config()
    form = {
        "privacy_policy_url": config.get("privacy_policy_url") or "",
        "privacy_policy_revised_date": (
            config["privacy_policy_revised_date"].isoformat()
            if config.get("privacy_policy_revised_date") else ""
        ),
        "commerce_law_url": config.get("commerce_law_url") or "",
        "participant_terms_url": config.get("participant_terms_url") or "",
        "participant_terms_revised_date": (
            config["participant_terms_revised_date"].isoformat()
            if config.get("participant_terms_revised_date") else ""
        ),
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        token = (request.form.get("csrf_token") or "").strip()
        if not token or token != admin_csrf:
            flash("CSRFエラーのため保存できませんでした。", "danger")
            return redirect(url_for("external_login_user.admin_privacy_policy"))

        form["privacy_policy_url"] = (request.form.get("privacy_policy_url") or "").strip()
        form["privacy_policy_revised_date"] = (request.form.get("privacy_policy_revised_date") or "").strip()
        form["commerce_law_url"] = (request.form.get("commerce_law_url") or "").strip()
        form["participant_terms_url"] = (request.form.get("participant_terms_url") or "").strip()
        form["participant_terms_revised_date"] = (request.form.get("participant_terms_revised_date") or "").strip()

        normalized_privacy_url = _normalize_document_admin_url(form["privacy_policy_url"])
        privacy_revised_date = _parse_document_revised_date(form["privacy_policy_revised_date"])
        normalized_commerce_law_url = _normalize_document_admin_url(form["commerce_law_url"])
        normalized_participant_terms_url = _normalize_document_admin_url(form["participant_terms_url"])
        participant_terms_revised_date = _parse_document_revised_date(form["participant_terms_revised_date"])

        if form["privacy_policy_url"] and not normalized_privacy_url:
            errors["privacy_policy_url"] = "URLは http:// または https:// で始まる形式で入力してください。"
        if form["privacy_policy_revised_date"] and not privacy_revised_date:
            errors["privacy_policy_revised_date"] = "改定日は yyyy-mm-dd 形式で入力してください。"
        if form["commerce_law_url"] and not normalized_commerce_law_url:
            errors["commerce_law_url"] = "URLは http:// または https:// で始まる形式で入力してください。"
        if form["participant_terms_url"] and not normalized_participant_terms_url:
            errors["participant_terms_url"] = "URLは http:// または https:// で始まる形式で入力してください。"
        if form["participant_terms_revised_date"] and not participant_terms_revised_date:
            errors["participant_terms_revised_date"] = "改定日は yyyy-mm-dd 形式で入力してください。"

        if not errors:
            db = get_db(); cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO mfu_external_privacy_policy_config
                      (
                        privacy_policy_url,
                        privacy_policy_revised_date,
                        commerce_law_url,
                        participant_terms_url,
                        participant_terms_revised_date,
                        updated_by
                      )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        normalized_privacy_url,
                        privacy_revised_date,
                        normalized_commerce_law_url,
                        normalized_participant_terms_url,
                        participant_terms_revised_date,
                        (session.get("user") or "").strip() or None,
                    ),
                )
                db.commit()
                flash("外部ログイン向けドキュメント設定を保存しました。", "success")
                return redirect(url_for("external_login_user.admin_privacy_policy"))
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                current_app.logger.exception("external document admin save failed")
                errors["form"] = "保存に失敗しました。時間をおいて再度お試しください。"
            finally:
                try:
                    cur.close(); db.close()
                except Exception:
                    pass

    current_config = _get_external_document_config()
    privacy_config = _get_current_privacy_policy_config()
    commerce_law_config = _get_current_commerce_law_config()
    participant_terms_config = _get_current_participant_terms_config()
    return render_template(
        "admin_privacy_policy.html",
        admin_csrf=admin_csrf,
        form=form,
        errors=errors,
        current_config=current_config,
        privacy_policy_effective=_is_privacy_policy_effective(privacy_config),
        privacy_policy_revised_date_label=_privacy_policy_date_label(privacy_config.get("privacy_policy_revised_date")),
        commerce_law_config=commerce_law_config,
        commerce_law_effective=bool(commerce_law_config.get("commerce_law_url")),
        participant_terms_effective=_is_participant_terms_effective(participant_terms_config),
        participant_terms_revised_date_label=_privacy_policy_date_label(participant_terms_config.get("participant_terms_revised_date")),
    )

# admin.py の admin_events_list() を置換
@bp.route("/admin/events")
def admin_events_list():
    guard = _require_mfu_login_redirect()
    if guard: 
        return guard

    # MFUログイン中のユーザー名（セッション 'user'）を使う
    from flask import session
    username = (session.get("user") or "").strip()

    _ensure_event_soft_delete_columns()

    db = get_db(); cur = db.cursor()
    try:
        if username == "admin":
            # admin は全件：開始日が近い順（NULLは一番下）、同日内は id 昇順で安定化
            cur.execute("""
              SELECT
                e.id,
                e.title,
                e.event_uuid,
                e.starts_at,
                e.pay_from,
                e.pay_until,
                (SELECT COUNT(*)
                   FROM mfu_event_member m
                  WHERE m.event_id=e.id
                    AND (m.status='approved' OR m.status IS NULL)
                    AND COALESCE(m.is_canceled,0)=0
                ) AS members
              FROM mfu_event e
              WHERE e.deleted_at IS NULL
              ORDER BY
                (e.starts_at IS NULL) ASC,
                e.starts_at ASC,
                e.id ASC
              LIMIT 200
            """)
        else:
            # ACL 許可イベントのみ：同じく開始日が近い順（NULLは下）
            try:
                cur.execute("""
                  SELECT
                    e.id,
                    e.title,
                    e.event_uuid,
                    e.starts_at,
                    e.pay_from,
                    e.pay_until,
                    (SELECT COUNT(*)
                       FROM mfu_event_member m
                      WHERE m.event_id=e.id
                        AND (m.status='approved' OR m.status IS NULL)
                        AND COALESCE(m.is_canceled,0)=0
                    ) AS members
                  FROM mfu_event e
                  JOIN mfu_event_admin_acl a ON a.event_id = e.id
                  WHERE a.username = %s
                    AND e.deleted_at IS NULL
                  ORDER BY
                    (e.starts_at IS NULL) ASC,
                    e.starts_at ASC,
                    e.id ASC
                  LIMIT 200
                """, (username,))
            except Exception:
                # ACLテーブル未作成などでも落ちないように空配列返却
                cur.fetchall()  # drain if needed
                return render_template("admin_events_list.html", events=[])
        raws = cur.fetchall()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    events = []
    for r in raws or []:
        # tuple/dict 両対応で取り出し
        id_        = r[0] if isinstance(r, tuple) else r["id"]
        title      = r[1] if isinstance(r, tuple) else r["title"]
        ev_uuid_b  = r[2] if isinstance(r, tuple) else r["event_uuid"]
        starts_at  = r[3] if isinstance(r, tuple) else r["starts_at"]
        pay_from   = r[4] if isinstance(r, tuple) else r.get("pay_from")
        pay_until  = r[5] if isinstance(r, tuple) else r.get("pay_until")
        members    = r[6] if isinstance(r, tuple) else r["members"]
        events.append({
            "id": id_,
            "title": title,
            "event_uuid_str": _uuid_bytes_to_str(ev_uuid_b),
            "starts_at": starts_at,
            "pay_from": pay_from,
            "pay_until": pay_until,
            "members": members,
        })
    return render_template("admin_events_list.html", events=events)


def _ensure_test_account_columns() -> None:
    db = get_db(); cur = db.cursor()
    try:
        for column_name, definition in (
            ("is_test_account", "is_test_account TINYINT(1) NOT NULL DEFAULT 0 AFTER email_verified_at"),
            ("test_account_enabled", "test_account_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER is_test_account"),
            ("last_login_at", "last_login_at DATETIME NULL AFTER test_account_enabled"),
        ):
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.COLUMNS
                     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='external_login_user' AND COLUMN_NAME=%s""",
                (column_name,),
            )
            if int(cur.fetchone()[0] or 0) == 0:
                cur.execute(f"ALTER TABLE external_login_user ADD COLUMN {definition}")
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass


@bp.route("/admin/test-accounts", methods=["GET", "POST"])
def admin_test_accounts():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if (session.get("user") or "").strip() != "admin":
        abort(403, "テストアカウントはadminのみ管理できます。")

    _ensure_test_account_columns()
    if request.method == "POST":
        if request.form.get("csrf_token") != session.get("admin_csrf"):
            abort(400, "CSRF token mismatch")
        nickname = (request.form.get("nickname") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        if not nickname or len(nickname) > 50:
            flash("表示名は1～50文字で入力してください。", "warning")
        elif not email or "@" not in email or len(email) > 191:
            flash("メールアドレスを正しく入力してください。", "warning")
        else:
            db = get_db(); cur = db.cursor(dictionary=True)
            user_id = 0
            try:
                cur.execute("SELECT id FROM external_login_user WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
                if cur.fetchone():
                    flash("このメールアドレスは既に別のアカウントで使用されています。", "warning")
                else:
                    social_id = "email_test:" + os.urandom(16).hex()
                    cur.execute(
                        """INSERT INTO external_login_user
                             (mfu_uuid, social_id, nickname, email, email_verified_at,
                              is_test_account, test_account_enabled)
                           VALUES (UNHEX(REPLACE(UUID(),'-','')), %s, %s, %s, UTC_TIMESTAMP(), 1, 1)""",
                        (social_id, nickname, email),
                    )
                    user_id = int(cur.lastrowid)
                    db.commit()
                    flash(f"テスト用アカウント「{nickname}」を発行しました。", "success")
            except Exception:
                db.rollback()
                current_app.logger.exception("global test account creation failed email=%s", email)
                flash("テスト用アカウントの発行に失敗しました。", "danger")
            finally:
                cur.close(); db.close()
            if user_id:
                return redirect(url_for("external_login_user.admin_test_accounts"))

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """SELECT u.id, u.nickname, u.email, u.test_account_enabled, u.last_login_at,
                      u.created_at,
                      (SELECT COUNT(*) FROM mfu_event_member m
                        WHERE m.user_id=u.id AND COALESCE(m.is_canceled,0)=0) AS event_count,
                      (SELECT GROUP_CONCAT(e.title ORDER BY e.starts_at SEPARATOR ' / ')
                         FROM mfu_event_member m
                         JOIN mfu_event e ON e.id=m.event_id
                        WHERE m.user_id=u.id AND COALESCE(m.is_canceled,0)=0
                          AND e.deleted_at IS NULL) AS event_titles
                 FROM external_login_user u
                WHERE COALESCE(u.is_test_account,0)=1 AND COALESCE(u.is_deleted,0)=0
                ORDER BY u.created_at DESC, u.id DESC""",
        )
        accounts = cur.fetchall() or []
        cur.execute(
            """SELECT id, title, starts_at FROM mfu_event
                WHERE deleted_at IS NULL
                ORDER BY (starts_at IS NULL), starts_at DESC, id DESC LIMIT 200"""
        )
        events = cur.fetchall() or []
    finally:
        cur.close(); db.close()
    return render_template(
        "admin_event_test_accounts.html",
        accounts=accounts,
        events=events,
        admin_csrf=_admin_csrf_token(),
        login_url=url_for("external_login_user.user_vue_portal", _external=True),
    )


@bp.get("/admin/events/<int:event_id>/test-accounts")
def admin_event_test_accounts(event_id: int):
    """旧イベント単位URLは共通管理画面へ転送する。"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    return redirect(url_for("external_login_user.admin_test_accounts"))


@bp.post("/admin/test-accounts/<int:user_id>/action")
def admin_test_account_action(user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if (session.get("user") or "").strip() != "admin":
        abort(403)
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")
    _ensure_test_account_columns()
    action = (request.form.get("action") or "").strip()

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """SELECT u.id, u.email, u.nickname, u.test_account_enabled
                 FROM external_login_user u
                WHERE u.id=%s AND COALESCE(u.is_test_account,0)=1
                  AND COALESCE(u.is_deleted,0)=0 LIMIT 1""",
            (user_id,),
        )
        account = cur.fetchone()
    finally:
        cur.close(); db.close()
    if not account:
        abort(404)

    if action == "toggle":
        enabled = 0 if int(account.get("test_account_enabled") or 0) else 1
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("UPDATE external_login_user SET test_account_enabled=%s WHERE id=%s LIMIT 1", (enabled, user_id))
            db.commit()
        finally:
            cur.close(); db.close()
        if enabled:
            from .session_revocation import mark_external_user_active
            mark_external_user_active(user_id)
        else:
            from .session_revocation import revoke_external_user_sessions
            revoke_external_user_sessions(
                user_id,
                message="このテスト用アカウントは管理者により無効化されました。",
            )
        flash("アカウントを有効化しました。" if enabled else "アカウントを無効化しました。", "success")
    elif action == "send_pin":
        from .users import _issue_pin
        ok, message = _issue_pin(account.get("email") or "")
        flash(message, "success" if ok else "warning")
    elif action == "assign":
        try:
            event_id = int(request.form.get("event_id") or 0)
        except Exception:
            event_id = 0
        db = get_db(); cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM mfu_event WHERE id=%s AND deleted_at IS NULL LIMIT 1", (event_id,))
            if not cur.fetchone():
                flash("割り当て先イベントが見つかりません。", "warning")
            else:
                cur.execute(
                    """INSERT INTO mfu_event_member
                         (event_id,user_id,role,status,payment_status,require_payment,joined_at)
                       VALUES (%s,%s,'viewer','approved','unpaid',1,UTC_TIMESTAMP())
                       ON DUPLICATE KEY UPDATE status='approved', is_canceled=0,
                         canceled_at=NULL, canceled_by=NULL""",
                    (event_id, user_id),
                )
                db.commit()
                flash("テストアカウントをイベントへ承認済み参加者として割り当てました。", "success")
        finally:
            cur.close(); db.close()
    elif action == "delete":
        db = get_db(); cur = db.cursor()
        try:
            actor = (session.get("user") or "admin")[:80]
            cur.execute(
                """UPDATE mfu_event_member
                      SET is_canceled=1, canceled_at=UTC_TIMESTAMP(), canceled_by=%s
                    WHERE user_id=%s AND COALESCE(is_canceled,0)=0""",
                (actor, user_id),
            )
            cur.execute(
                """UPDATE external_login_user
                      SET is_deleted=1, deleted_at=UTC_TIMESTAMP(), deleted_by=%s,
                          deletion_reason='test account deleted', test_account_enabled=0
                    WHERE id=%s LIMIT 1""",
                (actor, user_id),
            )
            db.commit()
        finally:
            cur.close(); db.close()
        from .session_revocation import revoke_external_user_sessions
        revoke_external_user_sessions(
            user_id,
            message="このテスト用アカウントは管理者により削除されました。",
        )
        flash("テスト用アカウントを削除しました。", "success")
    else:
        abort(400, "invalid action")
    return redirect(url_for("external_login_user.admin_test_accounts"))


@bp.get("/admin/events/deleted")
def admin_events_deleted_list():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    from flask import session
    if (session.get("user") or "").strip() != "admin":
        abort(403)

    _ensure_event_soft_delete_columns()

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
          SELECT
            e.id,
            e.title,
            e.event_uuid,
            e.starts_at,
            e.deleted_at,
            e.deleted_by
          FROM mfu_event e
          WHERE e.deleted_at IS NOT NULL
          ORDER BY e.deleted_at DESC, e.id DESC
          LIMIT 200
        """)
        raws = cur.fetchall()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    events = []
    for r in raws or []:
        events.append({
            "id": r[0] if isinstance(r, tuple) else r["id"],
            "title": r[1] if isinstance(r, tuple) else r["title"],
            "event_uuid_str": _uuid_bytes_to_str(r[2] if isinstance(r, tuple) else r["event_uuid"]),
            "starts_at": r[3] if isinstance(r, tuple) else r["starts_at"],
            "deleted_at": r[4] if isinstance(r, tuple) else r["deleted_at"],
            "deleted_by": r[5] if isinstance(r, tuple) else r["deleted_by"],
        })

    return render_template("admin_events_deleted_list.html", events=events)


@bp.post("/admin/events/<int:event_id>/delete")
def admin_event_soft_delete(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    from flask import session
    username = (session.get("user") or "").strip()
    if username != "admin":
        abort(403)

    token = request.form.get("csrf_token", "")
    if not token or token != session.get("admin_csrf"):
        abort(400, "invalid csrf")

    _ensure_event_soft_delete_columns()

    db = get_db(); cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE mfu_event
               SET deleted_at=NOW(),
                   deleted_by=%s
             WHERE id=%s
               AND deleted_at IS NULL
             LIMIT 1
            """,
            (username, event_id),
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    flash("イベントを削除済み一覧へ移動しました（論理削除）。", "success")
    return redirect(url_for("external_login_user.admin_events_list"))

@bp.route("/admin/events/new", methods=["GET", "POST"])
def admin_event_new():
    guard = _require_mfu_login_redirect()
    if guard: return guard

    if request.method == "GET":
        return render_template("admin_event_new.html", form={"theme_color": DEFAULT_EVENT_THEME_COLOR}, errors={}, default_theme_color=DEFAULT_EVENT_THEME_COLOR)

    title     = (request.form.get("title") or "").strip()
    starts_at = (request.form.get("starts_at") or "").strip()
    fee_yen   = (request.form.get("fee_yen") or "").strip()
    place     = (request.form.get("place_name") or "").strip()
    address   = (request.form.get("address") or "").strip()
    maps_url  = (request.form.get("maps_url") or "").strip()
    theme_color_raw = (request.form.get("theme_color") or "").strip()
    theme_color = normalize_event_theme_color(theme_color_raw, default=None)
    # ▼ 新規：支払期間（未入力は None → NULL）
    pay_from  = (request.form.get("pay_from") or "").strip() or None
    pay_until = (request.form.get("pay_until") or "").strip() or None
    google_form_url = (request.form.get("google_form_url") or "").strip() or None

    errors = {}
    if not title:
        errors["title"] = "タイトルは必須です。"
    if fee_yen and not fee_yen.isdigit():
        errors["fee_yen"] = "参加費は半角数字（円）で入力してください。"
    if theme_color is None:
        errors["theme_color"] = "テーマカラーは #RRGGBB 形式で指定してください。"

    if errors:
        return render_template("admin_event_new.html",
                               form={"title": title, "starts_at": starts_at, "fee_yen": fee_yen,
                                     "place_name": place, "address": address, "maps_url": maps_url,
                                     "pay_from": pay_from, "pay_until": pay_until,
                                     "theme_color": theme_color_raw},
                               errors=errors, default_theme_color=DEFAULT_EVENT_THEME_COLOR), 400

    db = get_db(); cur = db.cursor()
    cur.execute("""
      INSERT INTO mfu_event (
        event_uuid, title, theme_color, owner_user_id, starts_at, fee_yen,
        pay_from, pay_until,
        place_name, address, maps_url,
        checkin_qr_enabled,
        fee_calc_method,
        square_fee_rate_percent
      )
      VALUES (UNHEX(REPLACE(UUID(),'-','')), %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, 1, 'new', 3.6)
    """, (title, theme_color, (starts_at or None), (int(fee_yen) if fee_yen else None),
          pay_from, pay_until,
          (place or None), (address or None), (maps_url or None)))
    db.commit()
    new_id = cur.lastrowid
    cur.close(); db.close()

    # アルバム自動作成
    album_id = create_event_album(title=title, event_id=new_id, starts_at=(starts_at or None))
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE mfu_event SET album_id=%s WHERE id=%s", (album_id, new_id))
    db.commit(); cur.close(); db.close()

    # 決済イベントUUIDも事前作成
    _ensure_payment_uuid_for_event(new_id)

    flash("イベントを作成しました（連携アルバム・決済UUIDを自動作成）", "success")
    return redirect(url_for("external_login_user.admin_event_view", event_id=new_id))


@bp.route("/admin/events/<int:event_id>")
def admin_event_view(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_view(event_id):
        abort(403, "このイベントへのアクセス権がありません。")

    guard = _require_mfu_login_redirect()
    if guard: return guard

    db = get_db(); cur = db.cursor()
    # イベント本体は従来どおり
    cur.execute("SELECT * FROM mfu_event WHERE id=%s", (event_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        abort(404, "イベントが見つかりません")

    col_names = [d[0] for d in cur.description] if getattr(cur, "description", None) else []
    ev = dict(zip(col_names, row)) if isinstance(row, tuple) else dict(row)
    cur.close(); db.close()
    ev["event_uuid_str"] = _uuid_bytes_to_str(ev.get("event_uuid"))

    # 付帯リンク生成（自動承認時は iv 付き招待リンクを優先）
    if int(ev.get("auto_approve_by_invite") or 0):
        ev["invite_token"] = _ensure_event_invite_token(event_id)
        join_url = _event_invite_url(ev)
    else:
        join_url = url_for("external_login_user.join_event", event_uuid=ev["event_uuid_str"], _external=True)
    maps_link = ev.get("maps_url") or (f"https://www.google.com/maps/search/?api=1&query={quote_plus(ev['address'])}"
                                       if ev.get("address") else None)
    album_url = url_for("album.album_access", album_id=ev["album_id"], _external=True) if ev.get("album_id") else None
    pay_admin_url = f"/payment/admin/events/uuid/{ev['payment_uuid']}" if ev.get("payment_uuid") else None

    # ★ メモ列を確保してからメンバー取得
    _ensure_member_memo_columns()

    # ===== ここを修正：並び順を 役割優先 → ニックネーム（あいうえお順） → joined_at に =====
    db = get_db(); cur = db.cursor()
    cur.execute("""
      SELECT
        m.user_id,
        u.nickname, u.x_id, u.instagram_id, u.email, COALESCE(u.is_test_account,0) AS is_test_account,
        u.avatar_file, u.avatar_url, u.updated_at,
        m.status, m.payment_status, m.paid_at, m.receipt_url, m.joined_at,
        m.checkin_at,
        COALESCE(m.require_payment, 1)        AS require_payment,
        COALESCE(m.process, 0)                AS process,
        COALESCE(m.is_host, 0)                AS is_host,
        COALESCE(m.is_subhost, 0)             AS is_subhost,
        COALESCE(m.participant_role, 'none')  AS participant_role,
        m.costume_label,
        m.paid_amount_yen,
        m.contact_memo,
        m.admin_note,
        m.receipt_note,
        COALESCE(m.is_canceled,0) AS is_canceled,
        m.canceled_at,
        m.canceled_by,
        CASE
          WHEN COALESCE(m.is_host, 0)=1 THEN 0
          WHEN COALESCE(m.is_subhost, 0)=1 THEN 1
          WHEN LOWER(COALESCE(m.participant_role,''))='camera' THEN 2
          WHEN LOWER(COALESCE(m.participant_role,''))='assistant' THEN 3
          WHEN LOWER(COALESCE(m.participant_role,''))='cosplayer' THEN 4
          ELSE 5
        END AS role_rank
      FROM mfu_event_member m
      JOIN external_login_user u ON u.id = m.user_id
      WHERE m.event_id=%s
      ORDER BY role_rank ASC, u.nickname ASC, m.joined_at ASC
    """, (event_id,))
    rows = cur.fetchall()
    cur.close(); db.close()

    def _badge(label: str, color: str) -> str:
        return f'<span class="badge bg-{color}">{label}</span>'

    # pending系を「確認（オレンジ）」に寄せるマップ（既存）
    PENDING_STATUSES = {
        "pending", "confirm", "confirming", "checking",
        "verifying", "awaiting", "processing", "authorized"
    }

    members = []
    total_member_count = 0
    camera_count = 0
    assistant_count = 0
    cosplayer_count = 0
    other_count = 0
    require_payment_count = 0
    paid_count = 0
    for r in rows:
        if isinstance(r, tuple):
            (user_id, nickname, x_id, instagram_id, email, is_test_account,
             avatar_file, avatar_url, updated_at,
             status, payment_status, paid_at, receipt_url, joined_at, checkin_at,
             require_payment, process, is_host, is_subhost, participant_role, costume_label,
             paid_amount_yen, contact_memo, admin_note, receipt_note,
             is_canceled, canceled_at, canceled_by, _role_rank) = r
        else:
            user_id          = r["user_id"]
            nickname         = r["nickname"]
            x_id             = r["x_id"]
            instagram_id     = r["instagram_id"]
            email            = r["email"]
            is_test_account  = int(r.get("is_test_account") or 0)
            avatar_file      = r["avatar_file"]
            avatar_url       = r["avatar_url"]
            updated_at       = r["updated_at"]
            status           = r["status"]
            payment_status   = r["payment_status"]
            paid_at          = r["paid_at"]
            receipt_url      = r["receipt_url"]
            joined_at        = r["joined_at"]
            checkin_at       = r.get("checkin_at")
            require_payment  = r["require_payment"]
            process          = r["process"]
            is_host          = r["is_host"]
            is_subhost       = r["is_subhost"]
            participant_role = r["participant_role"]
            costume_label    = r["costume_label"]
            paid_amount_yen  = r.get("paid_amount_yen")
            contact_memo     = r.get("contact_memo")
            admin_note       = r.get("admin_note")
            receipt_note     = r.get("receipt_note")
            is_canceled      = int(r.get("is_canceled") or 0)
            canceled_at      = r.get("canceled_at")
            canceled_by      = r.get("canceled_by")

        if int(is_canceled or 0) == 0:
            total_member_count += 1
            normalized_role = str(participant_role or "none").strip().lower()
            if normalized_role == "camera":
                camera_count += 1
            elif normalized_role == "assistant":
                assistant_count += 1
            elif normalized_role == "cosplayer":
                cosplayer_count += 1
            else:
                other_count += 1

        # --- 支払状況バッジ（既存ロジック） ---
        status_s = (payment_status or "").strip().lower()
        if paid_at:  # 支払日時が入っていれば最優先で「済」
            pay_status_html = _badge("済", "success")
        elif status_s == "paid":
            pay_status_html = _badge("済", "success")
        elif status_s in PENDING_STATUSES:
            pay_status_html = _badge("確認", "warning")
        else:
            pay_status_html = _badge("未", "danger")

        require_payment_flag = 1 if (require_payment is None) else int(require_payment)
        if int(is_canceled or 0) == 0 and require_payment_flag == 1:
            require_payment_count += 1
            if paid_at or status_s == "paid":
                paid_count += 1

        members.append({
            "user_id": user_id,
            "nickname": nickname,
            "x_id": x_id,
            "instagram_id": instagram_id,
            "email": email,
            "is_test_account": int(is_test_account or 0),
            "avatar_file": avatar_file,
            "avatar_url": avatar_url,
            "updated_at": updated_at,
            "status": status,
            "payment_status": payment_status,
            "paid_at": paid_at,
            "receipt_url": receipt_url,
            "joined_at": joined_at,
            "checkin_at": checkin_at,
            "checked_in": bool(checkin_at),
            "require_payment": require_payment_flag,
            "process": 1 if process else 0,
            "is_host": 1 if is_host else 0,
            "is_subhost": 1 if is_subhost else 0,
            "participant_role": (participant_role or "none"),
            "costume_label": costume_label,
            "paid_amount_yen": paid_amount_yen,
            "contact_memo": contact_memo,
            "admin_note": admin_note,
            "receipt_note": receipt_note,
            "is_canceled": int(is_canceled or 0),
            "canceled_at": canceled_at,
            "canceled_by": canceled_by,
            "pay_status_html": pay_status_html,
        })

    admin_csrf = _admin_csrf_token()
    return render_template(
        "admin_event_view.html",
        ev=ev, members=members,
        join_url=join_url, maps_link=maps_link,
        album_url=album_url, pay_admin_url=pay_admin_url,
        line_openchat_url=ev.get("line_openchat_url"),
        line_openchat_pass=ev.get("line_openchat_pass"),
        google_form_url=ev.get("google_form_url"),
        total_member_count=total_member_count,
        camera_count=camera_count,
        assistant_count=assistant_count,
        cosplayer_count=cosplayer_count,
        other_count=other_count,
        require_payment_count=require_payment_count,
        paid_count=paid_count,
        admin_csrf=admin_csrf,
        qr_trademark_notice=QR_TRADEMARK_NOTICE
    )


@bp.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"], endpoint="admin_event_edit")
def admin_event_edit(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    admin_csrf = _admin_csrf_token()

    from datetime import datetime
    def _parse_dt_local(s: str | None):
        if not s: return None
        try: return datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M")
        except Exception:
            try: return datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%S")
            except Exception: return "INVALID"

    def _fmt_dt_local(v):
        if not v: return ""
        try:
            if isinstance(v, str):
                v = v.replace(" ", "T")
                return v[:16] if len(v) >= 16 else v
            return v.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return ""

    # --- イベント取得（編集前の memo_all を保持して差分判定に使う） ---
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              id, title, theme_color, event_uuid,
              starts_at, fee_yen,
              pay_from, pay_until,
              place_name, address, maps_url,
              sns_hashtag,
              line_openchat_url, line_openchat_pass,
              google_form_url,
              album_id, payment_uuid,
              memo_all,
              studio_fee_yen,
              fee_rate_percent,
              admin_fee_yen,
              COALESCE(fee_auto_calc, 1) AS fee_auto_calc,
              COALESCE(fee_calc_method, 'legacy') AS fee_calc_method,
              COALESCE(square_fee_rate_percent, 3.6) AS square_fee_rate_percent,
              COALESCE(allow_square, 1) AS allow_square,
              COALESCE(allow_paypay, 0) AS allow_paypay,
              COALESCE(allow_bank,   0) AS allow_bank,
              COALESCE(tip_enabled, 0) AS tip_enabled,
              paypay_display,
              COALESCE(auto_approve_by_invite, 0) AS auto_approve_by_invite,
              invite_token,
              COALESCE(checkin_qr_enabled, 0) AS checkin_qr_enabled,
              checkin_qr_token,
              checkin_qr_expires_at
            FROM mfu_event
            WHERE id=%s
            LIMIT 1
        """, (event_id,))
        ev = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if not ev:
        abort(404, "イベントが見つかりません")

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT COUNT(*)
              FROM mfu_event_member
             WHERE event_id=%s
               AND COALESCE(require_payment, 1)=1
        """, (event_id,))
        require_payment_count_row = cur.fetchone()
        require_payment_count = (require_payment_count_row[0] if isinstance(require_payment_count_row, tuple)
                                 else (require_payment_count_row.get("COUNT(*)") if require_payment_count_row else 0))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    try:
        ev["event_uuid_str"] = _uuid_bytes_to_str(ev.get("event_uuid"))
    except Exception:
        ev["event_uuid_str"] = ev.get("event_uuid")

    form_iso = {
        "starts_at": _fmt_dt_local(ev.get("starts_at")),
        "pay_from":  _fmt_dt_local(ev.get("pay_from")),
        "pay_until": _fmt_dt_local(ev.get("pay_until")),
    }
    form = {
        "title": ev.get("title"),
        "theme_color": normalize_event_theme_color(ev.get("theme_color")),
        "fee_yen": ev.get("fee_yen"),
        "place_name": ev.get("place_name"),
        "address": ev.get("address"),
        "maps_url": ev.get("maps_url"),
        "sns_hashtag": ev.get("sns_hashtag"),
        "line_openchat_url": ev.get("line_openchat_url"),
        "line_openchat_pass": ev.get("line_openchat_pass"),
        "google_form_url": ev.get("google_form_url"),
        "album_id": ev.get("album_id"),
        "payment_uuid": ev.get("payment_uuid"),
        "memo_all": ev.get("memo_all"),
        "studio_fee_yen": ev.get("studio_fee_yen"),
        "fee_rate_percent": ev.get("fee_rate_percent"),
        "admin_fee_yen": ev.get("admin_fee_yen"),
        "fee_auto_calc": int(ev.get("fee_auto_calc") or 0),
        "fee_calc_method": ev.get("fee_calc_method") or "legacy",
        "square_fee_rate_percent": ev.get("square_fee_rate_percent") if ev.get("square_fee_rate_percent") is not None else 3.6,
        "require_payment_count": require_payment_count,
        "allow_square": int(ev.get("allow_square") or 0),
        "allow_paypay": int(ev.get("allow_paypay") or 0),
        "allow_bank":   int(ev.get("allow_bank") or 0),
        "tip_enabled": int(ev.get("tip_enabled") or 0),
        "paypay_display": ev.get("paypay_display") or "",
    }
    errors: dict[str, str] = {}

    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or token != admin_csrf:
            abort(400, "invalid csrf token")

        title       = (request.form.get("title") or "").strip()
        theme_color_raw = (request.form.get("theme_color") or "").strip()
        theme_color = normalize_event_theme_color(theme_color_raw, default=None)
        starts_at_in= request.form.get("starts_at") or ""
        pay_from_in = request.form.get("pay_from")  or ""
        pay_until_in= request.form.get("pay_until") or ""
        fee_yen_in  = request.form.get("fee_yen")
        studio_fee_yen_in = request.form.get("studio_fee_yen")
        fee_rate_percent_in = request.form.get("fee_rate_percent")
        admin_fee_yen_in = request.form.get("admin_fee_yen")
        require_payment_count_in = request.form.get("require_payment_count")
        fee_auto_calc = 1 if request.form.get("fee_auto_calc") else 0
        fee_calc_method_in = (request.form.get("fee_calc_method") or "legacy").strip()
        square_fee_rate_percent_in = request.form.get("square_fee_rate_percent")

        place_name  = (request.form.get("place_name") or "").strip() or None
        address     = (request.form.get("address") or "").strip() or None
        maps_url    = (request.form.get("maps_url") or "").strip() or None
        sns_hashtag = (request.form.get("sns_hashtag") or "").strip() or None

        line_openchat_url  = (request.form.get("line_openchat_url")  or "").strip() or None
        line_openchat_pass = (request.form.get("line_openchat_pass") or "").strip() or None
        google_form_url    = (request.form.get("google_form_url")    or "").strip() or None

        album_id    = (request.form.get("album_id") or "").strip() or None
        memo_all    = (request.form.get("memo_all") or "").strip() or None

        allow_square   = 1 if request.form.get("allow_square") else 0
        allow_paypay   = 1 if request.form.get("allow_paypay") else 0
        allow_bank     = 1 if request.form.get("allow_bank")   else 0
        paypay_display = (request.form.get("paypay_display") or "").strip() or None
        tip_enabled = 1 if request.form.get("tip_enabled") else 0

        if not title:
            errors["title"] = "タイトルは必須です。"
        if theme_color is None:
            errors["theme_color"] = "テーマカラーは #RRGGBB 形式で指定してください。"

        fee_yen = None
        if fee_yen_in not in (None, ""):
            try:
                fee_yen = int(fee_yen_in)
                if fee_yen < 0: raise ValueError()
            except Exception:
                errors["fee_yen"] = "参加費は0以上の整数で指定してください。"

        studio_fee_yen = None
        if studio_fee_yen_in not in (None, ""):
            try:
                studio_fee_yen = int(studio_fee_yen_in)
                if studio_fee_yen < 0: raise ValueError()
            except Exception:
                errors["studio_fee_yen"] = "スタジオ代金は0以上の整数で指定してください。"

        fee_rate_percent = None
        if fee_rate_percent_in not in (None, ""):
            try:
                fee_rate_percent = float(fee_rate_percent_in)
                if fee_rate_percent < 0: raise ValueError()
            except Exception:
                errors["fee_rate_percent"] = "手数料は0以上の数値で指定してください。"

        admin_fee_yen = None
        if admin_fee_yen_in not in (None, ""):
            try:
                admin_fee_yen = int(admin_fee_yen_in)
                if admin_fee_yen < 0: raise ValueError()
            except Exception:
                errors["admin_fee_yen"] = "事務手数料は0以上の整数で指定してください。"

        fee_calc_method = fee_calc_method_in if fee_calc_method_in in ("legacy", "new") else "legacy"

        square_fee_rate_percent = 3.6
        try:
            if square_fee_rate_percent_in not in (None, ""):
                square_fee_rate_percent = float(square_fee_rate_percent_in)
            if square_fee_rate_percent < 0:
                raise ValueError()
        except Exception:
            errors["square_fee_rate_percent"] = "Square手数料は0以上の数値で指定してください。"

        starts_at = _parse_dt_local(starts_at_in) if starts_at_in else None
        pay_from  = _parse_dt_local(pay_from_in)  if pay_from_in  else None
        pay_until = _parse_dt_local(pay_until_in) if pay_until_in else None
        if starts_at == "INVALID": errors["starts_at"] = "開始日時の形式が不正です。"; starts_at = None
        if pay_from  == "INVALID": errors["pay_from"]  = "支払開始の形式が不正です。"; pay_from = None
        if pay_until == "INVALID": errors["pay_until"] = "支払終了の形式が不正です。"; pay_until = None

        form.update({
            "title": title, "fee_yen": fee_yen_in,
            "theme_color": theme_color_raw,
            "place_name": place_name or "", "address": address or "", "maps_url": maps_url or "",
            "sns_hashtag": sns_hashtag or "",
            "line_openchat_url": line_openchat_url or "", "line_openchat_pass": line_openchat_pass or "",
            "google_form_url": google_form_url or "", "album_id": album_id or "", "memo_all": memo_all or "",
            "studio_fee_yen": studio_fee_yen_in or "",
            "fee_rate_percent": fee_rate_percent_in or "",
            "admin_fee_yen": admin_fee_yen_in or "",
            "require_payment_count": require_payment_count_in or "",
            "fee_auto_calc": fee_auto_calc,
            "fee_calc_method": fee_calc_method,
            "square_fee_rate_percent": square_fee_rate_percent_in or "3.6",
            "allow_square": allow_square, "allow_paypay": allow_paypay, "allow_bank": allow_bank,
            "tip_enabled": tip_enabled,
            "paypay_display": paypay_display or "",
        })
        form_iso.update({"starts_at": starts_at_in, "pay_from": pay_from_in, "pay_until": pay_until_in})

        if errors:
            flash("入力に誤りがあります。確認してください。", "warning")
            return render_template("admin_event_edit.html",
                ev=ev, form=form, form_iso=form_iso, errors=errors,
                require_payment_count=require_payment_count, admin_csrf=admin_csrf,
                qr_trademark_notice=QR_TRADEMARK_NOTICE, default_theme_color=DEFAULT_EVENT_THEME_COLOR)

        # === 保存処理 ===
        event_album_name = format_event_album_name(title=title, starts_at=starts_at)
        dbu = get_db(); curu = dbu.cursor()
        try:
            curu.execute("""
                UPDATE mfu_event
                   SET title=%s, theme_color=%s, starts_at=%s, fee_yen=%s,
                       studio_fee_yen=%s, fee_rate_percent=%s, admin_fee_yen=%s,
                       fee_auto_calc=%s,
                       fee_calc_method=%s, square_fee_rate_percent=%s,
                       pay_from=%s, pay_until=%s,
                       place_name=%s, address=%s, maps_url=%s, sns_hashtag=%s,
                       line_openchat_url=%s, line_openchat_pass=%s, google_form_url=%s,
                       album_id=%s, memo_all=%s,
                       allow_square=%s, allow_paypay=%s, allow_bank=%s, tip_enabled=%s, paypay_display=%s
                 WHERE id=%s
                 LIMIT 1
            """, (title, theme_color, starts_at, fee_yen,
                  studio_fee_yen, fee_rate_percent, admin_fee_yen,
                  fee_auto_calc, fee_calc_method, square_fee_rate_percent,
                  pay_from, pay_until,
                  place_name, address, maps_url, sns_hashtag,
                  line_openchat_url, line_openchat_pass, google_form_url,
                  album_id, memo_all,
                  allow_square, allow_paypay, allow_bank, tip_enabled, paypay_display,
                  event_id))

            curu.execute("""
                UPDATE albums
                   SET album_name=%s
                 WHERE event_id=%s
                   AND access_mode='event'
            """, (event_album_name, event_id))

            dbu.commit()
        finally:
            try: curu.close(); dbu.close()
            except Exception: pass

        # === ここから追加：全体メモ変更時のメール通知 ===
        try:
            before = (ev.get("memo_all") or "").strip()
            after  = (memo_all or "").strip()
            if before != after:
                # 宛先：承認済み + 主催/副主催（status IS NULL）に送る
                dbn = get_db(); curn = dbn.cursor()
                try:
                    curn.execute("""
                        SELECT DISTINCT u.id AS user_id, u.email
                         FROM mfu_event_member m
                          JOIN external_login_user u ON u.id = m.user_id
                         WHERE m.event_id=%s
                           AND (m.status='approved' OR m.status IS NULL)
                           AND COALESCE(m.is_canceled,0)=0
                           AND COALESCE(u.is_deleted,0)=0
                    """, (event_id,))
                    rows = curn.fetchall() or []
                finally:
                    try: curn.close(); dbn.close()
                    except Exception: pass

                contacts = [
                    {
                        "user_id": int(r[0] if isinstance(r, tuple) else r["user_id"]),
                        "email": (r[1] if isinstance(r, tuple) else r.get("email")),
                    }
                    for r in rows if r
                ]
                emails = [c["email"] for c in contacts if c.get("email")]

                subject  = f"【{title or ev.get('title')}】全体メモが更新されました"
                push_body = "イベントの全体メモが更新されました。イベント詳細をご確認ください。"
                for contact in contacts:
                    send_external_event_push(
                        user_id=contact["user_id"],
                        event_id=event_id,
                        event_uuid=ev["event_uuid_str"],
                        kind="event_memo_updated",
                        title=subject,
                        body=push_body,
                        sender_label="イベント",
                    )

                if emails:
                    view_url = url_for("external_login_user.view_event", event_uuid=ev["event_uuid_str"], _external=True)
                    body     = (
                        "全体メモが更新されました。\n\n"
                        "―― 新しい全体メモ ――\n"
                        f"{after}\n\n"
                        f"イベントページ: {view_url}\n"
                    )
                    sent = 0
                    for addr in emails:
                        try:
                            send_mail(
                                to=addr,
                                subject=subject,
                                body=body,
                                event_uuid=ev["event_uuid_str"],
                                from_display_name=f"{title or ev.get('title') or 'イベント'} by Mimoria",
                            )
                            sent += 1
                        except Exception:
                            current_app.logger.exception("failed to send memo_all update mail to %s (event_id=%s)", addr, event_id)
                    flash(f"イベントを保存しました（全体メモ更新の通知 {sent} 件）", "success")
                else:
                    flash("イベントを保存しました（通知対象のメールアドレスなし）", "success")
            else:
                flash("イベントを保存しました。", "success")
        except Exception:
            current_app.logger.exception("memo_all update mail failed (event_id=%s)", event_id)
            flash("イベントを保存しました（通知時に一部エラー）", "warning")

        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    return render_template("admin_event_edit.html",
        ev=ev, form=form, form_iso=form_iso, errors=errors,
        require_payment_count=require_payment_count, admin_csrf=admin_csrf,
        qr_trademark_notice=QR_TRADEMARK_NOTICE, default_theme_color=DEFAULT_EVENT_THEME_COLOR)


def _fetch_event_members_in_admin_order(event_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
          SELECT
            m.user_id,
            u.nickname,
            u.x_id,
            u.instagram_id,
            u.avatar_file,
            u.avatar_url,
            u.updated_at,
            COALESCE(u.is_deleted, 0) AS is_deleted,
            COALESCE(m.is_host, 0)              AS is_host,
            COALESCE(m.is_subhost, 0)           AS is_subhost,
            COALESCE(m.participant_role, 'none') AS participant_role,
            m.costume_label,
            m.status,
            COALESCE(m.is_canceled, 0)          AS is_canceled,
            m.joined_at,
            CASE
              WHEN COALESCE(m.is_host, 0)=1 THEN 0
              WHEN COALESCE(m.is_subhost, 0)=1 THEN 1
              WHEN LOWER(COALESCE(m.participant_role,''))='camera' THEN 2
              WHEN LOWER(COALESCE(m.participant_role,''))='assistant' THEN 3
              WHEN LOWER(COALESCE(m.participant_role,''))='cosplayer' THEN 4
              ELSE 5
            END AS role_rank
          FROM mfu_event_member m
          JOIN external_login_user u ON u.id = m.user_id
         WHERE m.event_id=%s
         ORDER BY role_rank ASC, u.nickname ASC, m.joined_at ASC
        """, (event_id,))
        return cur.fetchall() or []
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def _compose_member_role_label(member: dict) -> str:
    admin_prefix = ""
    if int(member.get("is_host") or 0) == 1:
        admin_prefix = "主催"
    elif int(member.get("is_subhost") or 0) == 1:
        admin_prefix = "副主催"

    role_raw = str(member.get("participant_role") or "").strip().lower()
    costume_label = (member.get("costume_label") or "").strip()
    role_label = ""
    if role_raw == "camera":
        role_label = "カメラマン"
    elif role_raw == "assistant":
        role_label = "アシスタント"
    elif role_raw == "cosplayer":
        role_label = f"衣装（{costume_label}）" if costume_label else "衣装"
    elif role_raw == "other":
        role_label = f"その他（{costume_label}）" if costume_label else "その他"

    if admin_prefix and role_label:
        return f"{admin_prefix}＆{role_label}"
    if admin_prefix:
        return admin_prefix
    if role_label:
        return role_label
    return "その他"


def _safe_png_download_filename(event_id: int, title: str | None) -> str:
    base = (title or "").strip()
    if not base:
        return f"participants_{event_id}.png"
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", base)
    safe = re.sub(r"\s+", "_", safe).strip("._")
    if not safe:
        safe = f"event_{event_id}"
    return f"{safe}_参加者一覧.png"


PNG_FONT_PATH = "/mnt/mfu/app/PDF_Font/BIZUDPGothic-Regular.ttf"
TWEMOJI_CACHE_DIR = "/mnt/mfu/app/static/twemoji_cache"
# twitter/twemoji は更新停止のため、メンテ継続中の jdecked/twemoji に移行する。
TWEMOJI_VERSION = "17.0.2"
TWEMOJI_CDN_BASE = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@{TWEMOJI_VERSION}/assets/72x72"


@lru_cache(maxsize=1)
def _get_png_font_path() -> str:
    if os.path.exists(PNG_FONT_PATH):
        return PNG_FONT_PATH
    current_app.logger.error("PNG Japanese font file not found: %s", PNG_FONT_PATH)
    raise RuntimeError(f"PNG日本語フォントが見つかりません: {PNG_FONT_PATH}")


@lru_cache(maxsize=16)
def _get_png_text_font(size: int):
    font_path = _get_png_font_path()
    try:
        return ImageFont.truetype(font_path, size=size)
    except Exception:
        current_app.logger.exception("Failed to load PNG Japanese font: path=%s size=%s", font_path, size)
        raise RuntimeError(f"PNG日本語フォントの読込に失敗しました: {font_path}")


def _iter_text_clusters(text: str):
    """
    絵文字連結（VS16, ZWJ, 絵文字修飾子）を最低限まとめたクラスターを返す。
    完全な grapheme cluster ではないが、参加者名に含まれる単純絵文字には十分。
    """
    chars = list(str(text or ""))
    i = 0
    while i < len(chars):
        cluster = chars[i]
        i += 1
        while i < len(chars):
            cp = ord(chars[i])
            prev_cp = ord(cluster[-1])
            is_vs16 = cp == 0xFE0F
            is_zwj = cp == 0x200D
            is_emoji_modifier = 0x1F3FB <= cp <= 0x1F3FF
            continue_after_zwj = prev_cp == 0x200D
            if is_vs16 or is_zwj or is_emoji_modifier or continue_after_zwj:
                cluster += chars[i]
                i += 1
                continue
            break
        yield cluster


def _is_emoji_codepoint(cp: int) -> bool:
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return True
    if 0x1F300 <= cp <= 0x1FAFF:
        return True
    return cp in {
        0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139,
        0x2194, 0x2195, 0x2196, 0x2197, 0x2198, 0x2199, 0x21A9, 0x21AA,
        0x231A, 0x231B, 0x2328, 0x23CF, 0x23E9, 0x23EA, 0x23ED, 0x23EE, 0x23EF,
        0x23F0, 0x23F1, 0x23F2, 0x23F3, 0x23F8, 0x23F9, 0x23FA,
        0x24C2, 0x25AA, 0x25AB, 0x25B6, 0x25C0, 0x25FB, 0x25FC, 0x25FD, 0x25FE,
        0x2600, 0x2601, 0x2602, 0x2603, 0x2604, 0x260E, 0x2611, 0x2614, 0x2615,
        0x2618, 0x261D, 0x2620, 0x2622, 0x2623, 0x2626, 0x262A, 0x262E, 0x262F,
        0x2638, 0x2639, 0x263A, 0x2640, 0x2642, 0x2648, 0x2649, 0x264A, 0x264B,
        0x264C, 0x264D, 0x264E, 0x264F, 0x2650, 0x2651, 0x2652, 0x2653, 0x265F,
        0x2660, 0x2663, 0x2665, 0x2666, 0x2668, 0x267B, 0x267E, 0x267F, 0x2692,
        0x2693, 0x2694, 0x2695, 0x2696, 0x2697, 0x2699, 0x269B, 0x269C, 0x26A0,
        0x26A1, 0x26A7, 0x26AA, 0x26AB, 0x26B0, 0x26B1, 0x26BD, 0x26BE, 0x26C4,
        0x26C5, 0x26C8, 0x26CE, 0x26CF, 0x26D1, 0x26D3, 0x26D4, 0x26E9, 0x26EA,
        0x26F0, 0x26F1, 0x26F2, 0x26F3, 0x26F4, 0x26F5, 0x26F7, 0x26F8, 0x26F9,
        0x26FA, 0x26FD, 0x2702, 0x2705, 0x2708, 0x2709, 0x270A, 0x270B, 0x270C,
        0x270D, 0x270F, 0x2712, 0x2714, 0x2716, 0x271D, 0x2721, 0x2728, 0x2733,
        0x2734, 0x2744, 0x2747, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757,
        0x2763, 0x2764, 0x2795, 0x2796, 0x2797, 0x27A1, 0x27B0, 0x27BF, 0x2934,
        0x2935, 0x2B05, 0x2B06, 0x2B07, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55, 0x3030,
        0x303D, 0x3297, 0x3299,
    }


def _is_emoji_cluster(cluster: str) -> bool:
    if not cluster:
        return False
    cps = [ord(ch) for ch in cluster]
    for cp in cps:
        if cp in (0x200D, 0xFE0F, 0x20E3) or (0x1F3FB <= cp <= 0x1F3FF):
            continue
        if _is_emoji_codepoint(cp):
            return True
    return False


def _split_text_and_emoji_runs(text: str):
    runs = []
    current_kind = None
    current_text = []
    for cluster in _iter_text_clusters(text):
        kind = "emoji" if _is_emoji_cluster(cluster) else "text"

        if kind != current_kind and current_text:
            key = "emoji" if current_kind == "emoji" else "text"
            runs.append({"kind": current_kind, key: "".join(current_text)})
            current_text = []
        current_kind = kind
        current_text.append(cluster)

    if current_text:
        key = "emoji" if current_kind == "emoji" else "text"
        runs.append({"kind": current_kind, key: "".join(current_text)})
    return runs


def _emoji_to_twemoji_codepoints(emoji_text: str) -> str:
    cps = []
    for ch in str(emoji_text or ""):
        cp = ord(ch)
        # Twemoji のファイル名規則に合わせて FE0F(VS16) は除外する。
        # 例: "☃️" は "2603"。ZWJ(200D) は結合絵文字識別に必要なため保持する。
        if cp == 0xFE0F:
            continue
        cps.append(f"{cp:x}")
    return "-".join(cps)


def _emoji_draw_size(text_font) -> int:
    return max(12, int(round(float(getattr(text_font, "size", 24)) * 1.05)))


def _load_twemoji_image_cached(emoji_text: str):
    if not emoji_text:
        return None
    codepoints = _emoji_to_twemoji_codepoints(emoji_text)
    if not codepoints:
        return None
    remote_url = f"{TWEMOJI_CDN_BASE}/{codepoints}.png"

    try:
        os.makedirs(TWEMOJI_CACHE_DIR, exist_ok=True)
    except Exception:
        current_app.logger.warning("twemoji cache dir create failed: %s", TWEMOJI_CACHE_DIR)

    local_path = os.path.join(TWEMOJI_CACHE_DIR, f"{codepoints}.png")

    def _download_to_local() -> bool:
        try:
            response = requests.get(remote_url, timeout=2.0)
            response.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(response.content)
            return True
        except Exception as exc:
            current_app.logger.warning(
                "twemoji download failed: emoji=%s codepoints=%s remote_url=%s reason=%s",
                emoji_text, codepoints, remote_url, exc
            )
            return False

    if not os.path.exists(local_path):
        if not _download_to_local():
            return None

    def _load_local_image():
        with Image.open(local_path) as img:
            return img.convert("RGBA")

    try:
        return _load_local_image()
    except Exception as exc:
        current_app.logger.warning(
            "twemoji cache load failed: emoji=%s codepoints=%s remote_url=%s path=%s reason=%s",
            emoji_text, codepoints, remote_url, local_path, exc
        )
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            current_app.logger.debug("twemoji cache remove failed: path=%s", local_path)
        if not _download_to_local():
            return None
        try:
            return _load_local_image()
        except Exception as exc2:
            current_app.logger.warning(
                "twemoji cache reload failed: emoji=%s codepoints=%s remote_url=%s path=%s reason=%s",
                emoji_text, codepoints, remote_url, local_path, exc2
            )
        return None


def _get_twemoji_image(emoji_text: str, size: int):
    base = _load_twemoji_image_cached(emoji_text)
    if base is None:
        current_app.logger.debug("twemoji image unavailable, fallback required: emoji=%s size=%s", emoji_text, size)
        return None
    try:
        return base.resize((size, size), Image.Resampling.LANCZOS)
    except Exception as exc:
        current_app.logger.warning("twemoji resize failed: emoji=%s size=%s err=%s", emoji_text, size, exc)
        return None


def _measure_text_with_twemoji(draw: ImageDraw.ImageDraw, text: str, text_font) -> float:
    width = 0.0
    emoji_size = _emoji_draw_size(text_font)
    for run in _split_text_and_emoji_runs(text):
        if run["kind"] == "text":
            width += draw.textlength(run["text"], font=text_font)
        else:
            for cluster in _iter_text_clusters(run["emoji"]):
                width += emoji_size if _is_emoji_cluster(cluster) else draw.textlength(cluster, font=text_font)
    return width


def _draw_text_with_twemoji(draw: ImageDraw.ImageDraw, base_image: Image.Image, xy, text: str, text_font, fill):
    x, y = xy
    emoji_size = _emoji_draw_size(text_font)
    metrics = text_font.getmetrics() if hasattr(text_font, "getmetrics") else (text_font.size, 0)
    ascent = metrics[0] if isinstance(metrics, tuple) and metrics else text_font.size
    emoji_y = int(y + ascent - emoji_size * 0.84)
    for run in _split_text_and_emoji_runs(text):
        if run["kind"] == "text":
            run_text = run["text"]
            if not run_text:
                continue
            draw.text((x, y), run_text, fill=fill, font=text_font)
            x += draw.textlength(run_text, font=text_font)
            continue

        for cluster in _iter_text_clusters(run["emoji"]):
            if not _is_emoji_cluster(cluster):
                draw.text((x, y), cluster, fill=fill, font=text_font)
                x += draw.textlength(cluster, font=text_font)
                continue
            emoji_img = _get_twemoji_image(cluster, emoji_size)
            if emoji_img is None:
                fallback_advance = 0.0
                fallback_drawn = False
                try:
                    draw.text((x, y), cluster, fill=fill, font=text_font)
                    fallback_advance = float(draw.textlength(cluster, font=text_font))
                    fallback_drawn = True
                except Exception as exc:
                    current_app.logger.warning("emoji fallback draw failed: emoji=%s reason=%s", cluster, exc)

                if not fallback_drawn:
                    for symbol in ("□", "〓"):
                        try:
                            draw.text((x, y), symbol, fill=fill, font=text_font)
                            fallback_advance = float(draw.textlength(symbol, font=text_font))
                            fallback_drawn = True
                            break
                        except Exception:
                            continue

                advance = max(float(emoji_size), fallback_advance)
                current_app.logger.warning(
                    "twemoji fallback rendering applied: emoji=%s codepoints=%s advance=%s drawn=%s",
                    cluster, _emoji_to_twemoji_codepoints(cluster), advance, fallback_drawn
                )
                x += advance
                continue
            base_image.paste(emoji_img, (int(x), emoji_y), emoji_img)
            x += emoji_size
    return x


def _split_multiline(draw: ImageDraw.ImageDraw, text: str, text_font, max_width: int):
    lines = []
    for raw_line in str(text or "").splitlines() or [""]:
        chunks = textwrap.wrap(raw_line, width=80, break_long_words=True, break_on_hyphens=False) or [raw_line]
        for c in chunks:
            if not c:
                lines.append("")
                continue
            line = c
            while line:
                if _measure_text_with_twemoji(draw, line, text_font) <= max_width:
                    lines.append(line)
                    break
                line_width = _measure_text_with_twemoji(draw, line, text_font)
                cut = max(1, int(len(line) * max_width / max(line_width, 1)))
                found = False
                for i in range(min(len(line), cut + 2), 0, -1):
                    piece = line[:i]
                    if _measure_text_with_twemoji(draw, piece, text_font) <= max_width:
                        lines.append(piece)
                        line = line[i:]
                        found = True
                        break
                if not found:
                    lines.append(line[:1])
                    line = line[1:]
    return lines or [""]


def _download_avatar_image(member: dict, size: int):
    avatar_image = None
    avatar_file = member.get("avatar_file")
    avatar_url = member.get("avatar_url")
    updated_at = member.get("updated_at")
    version = ""
    try:
        if updated_at:
            version = str(int(updated_at.timestamp()))
    except Exception:
        version = ""

    def _resolve_avatar_file_path(name: str | None) -> Path | None:
        raw_name = (name or "").strip()
        if not raw_name:
            return None
        safe_name = secure_filename(raw_name)
        if not safe_name or safe_name != raw_name:
            current_app.logger.warning("invalid avatar_file name skipped: %s", raw_name)
            return None
        root = _AVATAR_ROOT.resolve()
        candidate = (root / safe_name).resolve()
        if root not in candidate.parents:
            current_app.logger.warning("avatar_file path traversal blocked: %s", raw_name)
            return None
        return candidate

    def _open_local_avatar_image(path: Path | None, image_size: int):
        if not path or not path.exists() or not path.is_file():
            return None
        try:
            with Image.open(path) as img:
                normalized = ImageOps.exif_transpose(img).convert("RGB")
                return ImageOps.fit(normalized, (image_size, image_size), method=Image.Resampling.LANCZOS)
        except (OSError, UnidentifiedImageError, ValueError):
            current_app.logger.debug("local avatar image load failed: %s", path, exc_info=True)
            return None
        except Exception:
            current_app.logger.warning("local avatar image load failed unexpectedly: %s", path, exc_info=True)
            return None

    if avatar_file:
        avatar_image = _open_local_avatar_image(_resolve_avatar_file_path(avatar_file), size)

    candidate_urls = []
    if avatar_url:
        if version:
            sep = "&" if "?" in avatar_url else "?"
            candidate_urls.append(f"{avatar_url}{sep}v={version}")
        else:
            candidate_urls.append(avatar_url)

    for candidate in candidate_urls if avatar_image is None else []:
        try:
            response = requests.get(candidate, timeout=2.5)
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as img:
                normalized = ImageOps.exif_transpose(img).convert("RGB")
                avatar_image = ImageOps.fit(normalized, (size, size), method=Image.Resampling.LANCZOS)
            break
        except (requests.RequestException, OSError, UnidentifiedImageError, ValueError):
            continue
        except Exception:
            current_app.logger.exception("avatar fetch failed unexpectedly: %s", candidate)

    if avatar_image is None:
        avatar_image = Image.new("RGB", (size, size), (220, 226, 234))

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size, size), fill=255)
    rounded = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    rounded.paste(avatar_image, (0, 0), mask)
    return rounded


def _render_participants_png(event_title: str, members: list[dict]) -> BytesIO:
    width = 1200
    padding_x = 56
    top_pad = 44
    avatar_size = 68
    block_gap = 20
    line_gap = 8
    divider_gap_top = 14
    min_block_height = 110

    title_font = _get_png_text_font(42)
    summary_font = _get_png_text_font(28)
    name_font = _get_png_text_font(34)
    body_font = _get_png_text_font(25)
    role_font = _get_png_text_font(25)

    temp = Image.new("RGB", (width, 500), "white")
    draw = ImageDraw.Draw(temp)
    content_width = width - (padding_x * 2) - avatar_size - 28

    lines_cache = []
    total_height = top_pad + 30
    total_height += int(title_font.size * 1.8)
    total_height += int(summary_font.size * 2.0)

    for member in members:
        name_lines = _split_multiline(draw, member.get("nickname") or "（名前未設定）", name_font, content_width)
        sns_parts = []
        if member.get("x_id"):
            sns_parts.append(f"X: {member.get('x_id')}")
        if member.get("instagram_id"):
            sns_parts.append(f"IG: {member.get('instagram_id')}")
        sns_text = " / ".join(sns_parts)
        sns_lines = _split_multiline(draw, sns_text, body_font, content_width) if sns_text else []
        role_lines = _split_multiline(draw, _compose_member_role_label(member), role_font, content_width)
        lines_cache.append((name_lines, sns_lines, role_lines))

        block_h = 0
        block_h += len(name_lines) * (name_font.size + line_gap)
        block_h += len(sns_lines) * (body_font.size + line_gap)
        block_h += len(role_lines) * (role_font.size + line_gap)
        block_h += divider_gap_top + 8
        block_h = max(min_block_height, block_h)
        total_height += block_h + block_gap

    total_height = max(total_height + 24, 260)
    image = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(image)
    _draw_text_with_twemoji(draw, image, (padding_x, top_pad), event_title or "イベント参加者一覧", title_font, "#111111")
    summary_text = f"参加者 {len(members)} 名（approved / 非キャンセル）"
    _draw_text_with_twemoji(draw, image, (padding_x, top_pad + 64), summary_text, summary_font, "#444444")

    y = top_pad + 120
    avatar_x = padding_x
    text_x = avatar_x + avatar_size + 28
    for idx, member in enumerate(members):
        name_lines, sns_lines, role_lines = lines_cache[idx]
        avatar = _download_avatar_image(member, avatar_size)
        image.paste(avatar, (avatar_x, y + 4), avatar)

        text_y = y
        for line in name_lines:
            _draw_text_with_twemoji(draw, image, (text_x, text_y), line, name_font, "#111111")
            text_y += name_font.size + line_gap
        for line in sns_lines:
            _draw_text_with_twemoji(draw, image, (text_x, text_y), line, body_font, "#4b5563")
            text_y += body_font.size + line_gap
        for line in role_lines:
            _draw_text_with_twemoji(draw, image, (text_x, text_y), line, role_font, "#1f2937")
            text_y += role_font.size + line_gap

        block_bottom = max(y + min_block_height, text_y + divider_gap_top)
        draw.line((padding_x, block_bottom, width - padding_x, block_bottom), fill="#e5e7eb", width=2)
        y = block_bottom + block_gap

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


# 以降（CSV出力・役割/支払金額の更新・他）は既存そのまま
@bp.route("/admin/events/<int:event_id>/export.csv")
def admin_event_export_csv(event_id: int):
    """
    CSV: 指定カラムのみ（日本語見出し）
      ニックネーム, X ID, IG ID, 役割, 衣装, 要支払, 支払状況, 支払金額, 個別連絡メモ, 管理者備考（非公開）
    役割: camera→カメラマン / assistant→アシスタント / cosplayer→衣装 / その他→空欄
    要支払: 1→要 / 0→不要（NULLは要扱い）
    支払状況: 'paid' or paid_atあり→済 / pending系→確認 / それ以外→未払い
    """
    guard = _require_mfu_login_redirect()
    if guard: 
        return guard

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT starts_at FROM mfu_event WHERE id=%s", (event_id,))
        ev_row = cur.fetchone()
        starts_at = ev_row[0] if isinstance(ev_row, tuple) else (ev_row.get("starts_at") if ev_row else None)

        cur.execute("""
          SELECT
            u.nickname,
            u.x_id,
            u.instagram_id,
            COALESCE(m.participant_role, 'none') AS participant_role,
            m.costume_label,
            COALESCE(m.require_payment, 1)       AS require_payment,
            COALESCE(m.payment_status, 'unpaid') AS payment_status,
            m.paid_at,
            m.paid_amount_yen,
            m.contact_memo,
            m.admin_note,
            COALESCE(m.is_host, 0)               AS is_host,
            COALESCE(m.is_subhost, 0)            AS is_subhost,
            m.joined_at,
            CASE
              WHEN COALESCE(m.is_host, 0)=1 THEN 0
              WHEN COALESCE(m.is_subhost, 0)=1 THEN 1
              WHEN LOWER(COALESCE(m.participant_role,''))='camera' THEN 2
              WHEN LOWER(COALESCE(m.participant_role,''))='assistant' THEN 3
              WHEN LOWER(COALESCE(m.participant_role,''))='cosplayer' THEN 4
              ELSE 5
            END AS role_rank
          FROM mfu_event_member m
          JOIN external_login_user u ON u.id = m.user_id
         WHERE m.event_id=%s
         ORDER BY role_rank ASC, u.nickname ASC, m.joined_at ASC
        """, (event_id,))
        rows = cur.fetchall() or []
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 役割マッピング
    ROLE_JA = {
        "camera": "カメラマン",
        "assistant": "アシスタント",
        "cosplayer": "衣装",
    }
    # pending系（確認扱い）
    PENDING_STATUSES = {
        "pending", "confirm", "confirming", "checking",
        "verifying", "awaiting", "processing", "authorized"
    }

    def _col(r, k, idx):
        # tuple/dict 両対応
        return (r[idx] if isinstance(r, tuple) else r.get(k))

    # CSV生成
    sio = StringIO()
    w = csv.writer(sio)
    # 見出し
    w.writerow(["ニックネーム","X ID","IG ID","役割","衣装","要支払","支払状況","支払金額","個別連絡メモ","管理者備考（非公開）"])

    for r in rows:
        nickname        = _col(r, "nickname",         0) or ""
        x_id            = _col(r, "x_id",             1) or ""
        ig_id           = _col(r, "instagram_id",     2) or ""
        role_raw        = (_col(r, "participant_role",3) or "none").lower()
        costume_label   = _col(r, "costume_label",    4) or ""
        req_pay_raw     = _col(r, "require_payment",  5)
        pay_status_raw  = (_col(r, "payment_status",  6) or "unpaid").lower()
        paid_at         = _col(r, "paid_at",          7)
        amount          = _col(r, "paid_amount_yen",  8)
        contact_memo    = _col(r, "contact_memo",     9) or ""
        admin_note      = _col(r, "admin_note",      10) or ""

        # 役割
        role_ja = ROLE_JA.get(role_raw, "")

        # 要支払
        req_pay = 1 if (req_pay_raw is None) else int(bool(req_pay_raw))
        req_pay_ja = "要" if req_pay == 1 else "不要"

        # 支払状況
        if paid_at or pay_status_raw == "paid":
            pay_ja = "済"
        elif pay_status_raw in PENDING_STATUSES:
            pay_ja = "確認"
        else:
            pay_ja = "未払い"

        # 金額はそのまま（None→空）
        amount_out = "" if amount in (None, "") else amount

        w.writerow([
            nickname, x_id, ig_id, role_ja, costume_label,
            req_pay_ja, pay_ja, amount_out, contact_memo, admin_note
        ])

    # Excel向けにBOM付与
    csv_data = "\ufeff" + sio.getvalue()

    resp = make_response(csv_data)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    filename_date = None
    if starts_at:
        try:
            filename_date = starts_at.strftime("%Y%m%d")
        except Exception:
            from datetime import datetime as _dt
            filename_date = _dt.fromisoformat(str(starts_at).replace(" ", "T")).strftime("%Y%m%d")
    if filename_date:
        filename = f"event_{filename_date}_members.csv"
    else:
        filename = f"event_{event_id}_members.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@bp.route("/admin/events/<int:event_id>/participants.png", endpoint="admin_event_export_participants_png")
def admin_event_export_participants_png(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, title FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        ev = cur.fetchone()
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass
    if not ev:
        abort(404, "イベントが見つかりません")
    if not _event_admin_can_view(event_id):
        abort(403, "このイベントへのアクセス権がありません。")

    rows = _fetch_event_members_in_admin_order(event_id)
    target_members = [
        r for r in rows
        if str(r.get("status") or "").strip().lower() == "approved" and int(r.get("is_canceled") or 0) == 0
    ]
    try:
        png_data = _render_participants_png(ev.get("title") or "", target_members)
    except Exception:
        current_app.logger.exception("participants png export failed (event_id=%s)", event_id)
        abort(500, "PNG生成に失敗しました")

    download_name = _safe_png_download_filename(event_id, ev.get("title"))
    return send_file(
        png_data,
        mimetype="image/png",
        as_attachment=True,
        download_name=download_name,
    )

@bp.post("/admin/events/<int:event_id>/members/<int:user_id>/toggle-payment")
def admin_event_member_toggle_payment(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")

    db = get_db(); cur = db.cursor()
    try:
        # user_id(=external_login_user.id) でも member_id(=mfu_event_member.id) でもヒットさせる
        cur.execute("""
            UPDATE mfu_event_member
               SET require_payment = CASE WHEN COALESCE(require_payment,1)=1 THEN 0 ELSE 1 END
             WHERE event_id=%s AND (user_id=%s OR id=%s)
             LIMIT 1
        """, (event_id, user_id, user_id))
        db.commit()
        if cur.rowcount == 0:
            flash("対象が見つかりませんでした。IDの指定を確認してください。", "warning")
        else:
            flash("支払い要/不要を切り替えました。", "success")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    _recalc_event_fee_if_auto(event_id)

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))


@bp.post("/admin/events/<int:event_id>/members/<int:user_id>/set-admin-role")
def admin_set_admin_role(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")

    kind = (request.form.get("kind") or "").strip().lower()
    is_host = 1 if kind == "host" else 0
    is_sub = 1 if kind == "subhost" else 0

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            UPDATE mfu_event_member
               SET is_host=%s,
                   is_subhost=%s
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (is_host, is_sub, event_id, user_id))
        db.commit()
        flash("主催/副主催の設定を更新しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("主催/副主催の設定に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

def _parse_optional_int(raw_value: str, field_label: str):
    raw = (raw_value or "").replace(",", "").strip()
    if raw == "":
        return None, None
    if not raw.isdigit():
        return None, f"{field_label}は半角数字で入力してください。"
    return int(raw), None

@bp.post("/admin/events/<int:event_id>/members/<int:user_id>/set-paid-amount")
def admin_set_paid_amount(event_id: int, user_id: int):
    """支払金額入力に応じて paid/unpaid 切替＋メール通知（send_mail統一）"""
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")

    receipt_url = (request.form.get("receipt_url") or "").strip() or None

    amount, err = _parse_optional_int(request.form.get("paid_amount_yen"), "支払金額")
    if err:
        flash(err, "warning")
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT m.payment_status, u.email
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id = m.user_id
             WHERE m.event_id=%s AND m.user_id=%s
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        cur_status = (row[0] if isinstance(row, tuple) else (row and row.get("payment_status"))) or "unpaid"
        to_email   = row[1] if row and isinstance(row, tuple) else (row.get("email") if row else None)

        cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        ev = cur.fetchone()
        if not ev:
            abort(404, "event not found")
        ev_title = ev[0] if isinstance(ev, tuple) else ev["title"]
        ev_uuid_b = ev[1] if isinstance(ev, tuple) else ev["event_uuid"]
        ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""

        next_status = "paid" if (amount is not None and amount > 0) else "unpaid"

        if next_status == "paid":
            cur.execute("""
                UPDATE mfu_event_member
                   SET paid_amount_yen=%s,
                       payment_status=%s,
                       paid_at=COALESCE(paid_at, NOW()),
                       receipt_url=COALESCE(%s, receipt_url)
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (amount, next_status, receipt_url, event_id, user_id))
        else:
            # ★ 未払いに戻すときは 3項目を NULL に
            cur.execute("""
                UPDATE mfu_event_member
                   SET paid_amount_yen=NULL,
                       payment_status='unpaid',
                       receipt_url=COALESCE(%s, receipt_url),
                       paid_at=NULL,
                       payment_row_id=NULL
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (receipt_url, event_id, user_id))

        db.commit()
        flash("支払金額を保存しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("支払金額の保存に失敗しました。", "danger")
        try: cur.close(); db.close()
        except Exception: pass
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # === メール通知（ステータスが変わった時のみ） ===
    try:
        if to_email and ev_uuid_str and cur_status != next_status and next_status in ("paid", "unpaid"):
            MSG = {
                "paid":   "お支払いありがとうございます！💕当日の参加お待ちしております💕",
                "unpaid": "お支払いが確認出来ませんでした🙇　後ほど主催から個別に連絡致します。",
            }
            body = MSG.get(next_status)
            if body:
                send_mail(
                    to=to_email,
                    subject=f"【{ev_title}】お支払い状況の更新",
                    body=body,
                    event_uuid=ev_uuid_str,
                    from_display_name=f"{ev_title} by Mimoria",
                )
    except Exception:
        current_app.logger.exception("failed to send set-paid-amount mail (user_id=%s, event_id=%s)", user_id, event_id)

    notify_member_payment_push(
        event_id=event_id,
        user_id=user_id,
        payment_status=next_status,
        kind=("event_payment_status" if cur_status != next_status else "event_payment_details_updated"),
        body=(
            None
            if cur_status != next_status
            else "実際の支払金額または領収書情報が更新されました。イベント詳細をご確認ください。"
        ),
    )

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))


@bp.post("/admin/events/<int:event_id>/members/<int:user_id>/set-participant-role")
def admin_set_participant_role(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")

    ui_role = (request.form.get("participant_role") or "none").strip().lower()
    # UIで許す値（otherもOK）
    if ui_role not in ("none", "camera", "assistant", "cosplayer", "other"):
        ui_role = "none"

    costume = (request.form.get("costume_label") or "").strip() or None
    # メモ欄は「衣装 or その他」でON（UI仕様）
    keep_costume = ui_role in ("cosplayer", "other")

    db = get_db()
    save_role, degraded = _normalize_role_for_db(db, ui_role)

    # フォールバックで 'cosplayer' などに変換された場合でも、
    # 「メモは保持」仕様を尊重（UIで入力できた＝保持対象）
    if not keep_costume:
        costume = None

    if costume and len(costume) > 120:
        flash("メモは120文字以内で入力してください。", "warning")
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE mfu_event_member
               SET participant_role=%s,
                   costume_label=%s
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (save_role, costume, event_id, user_id))
        db.commit()
        if degraded:
            flash("注意：DBスキーマが 'other' を未対応のため、近い役割にフォールバックして保存しました。後日ENUMに 'other' を追加してください。", "warning")
        else:
            flash("役割・メモを更新しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("役割・メモの更新に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

@bp.route("/admin/events/<int:event_id>/members/<int:user_id>/<action>", methods=["POST"])
def admin_event_member_action(event_id: int, user_id: int, action: str):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")
    if action == "uncancel":
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("""
                UPDATE mfu_event_member
                   SET is_canceled=0,
                       canceled_at=NULL,
                       canceled_by=NULL
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (event_id, user_id))
            db.commit()
        finally:
            cur.close(); db.close()
        _recalc_event_fee_if_auto(event_id)
        notify_member_status_push(
            event_id=event_id, user_id=user_id, old_status="canceled", new_status="active"
        )
        flash("参加キャンセルを解除しました。", "success")
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    if action not in ("approve", "reject"):
        if action == "remove":
            db = get_db(); cur = db.cursor()
            try:
                canceled_by = (session.get("user") or "admin")
                cur.execute("""
                    UPDATE mfu_event_member
                       SET is_canceled=1,
                           canceled_at=NOW(),
                           canceled_by=%s
                     WHERE event_id=%s AND user_id=%s
                     LIMIT 1
                """, (canceled_by, event_id, user_id))
                cur.execute(
                    """
                    DELETE FROM mfu_notifications
                     WHERE user_kind='external'
                       AND user_id=%s
                       AND kind='chat_message'
                       AND COALESCE(chat_event_id, event_id)=%s
                       AND read_at IS NULL
                    """,
                    (user_id, event_id),
                )
                db.commit()
            finally:
                cur.close(); db.close()
            _recalc_event_fee_if_auto(event_id)
            notify_member_status_push(
                event_id=event_id, user_id=user_id, old_status="active", new_status="canceled"
            )
            flash("参加者をキャンセル済みにしました。", "success")
            return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))
        abort(400, "unsupported action")

    new_status = "approved" if action == "approve" else "rejected"
    STATUS_JA = {"approved": "承認", "rejected": "拒否", "pending": "保留"}

    db = get_db(); cur = db.cursor()
    try:
        # イベント情報
        cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        ev = cur.fetchone()
        if not ev:
            abort(404, "event not found")
        ev_title = ev[0] if isinstance(ev, tuple) else ev["title"]
        ev_uuid_b = ev[1] if isinstance(ev, tuple) else ev["event_uuid"]
        ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""
        view_url = f"https://mfu.iori0624.jp/external-login/events/view/{ev_uuid_str}"

        # 対象メンバー
        cur.execute("""
            SELECT m.status, u.email, u.nickname
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id = m.user_id
             WHERE m.event_id=%s AND m.user_id=%s
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        if not row:
            abort(404, "member not found")

        old_status = row[0] if isinstance(row, tuple) else row["status"]
        to_email   = row[1] if isinstance(row, tuple) else row["email"]
        nickname   = row[2] if isinstance(row, tuple) else row["nickname"]

        # 変化があるときのみ更新＆通知
        if old_status != new_status:
            update_event_member_status(event_id, user_id, new_status)

            if to_email and ev_uuid_str:
                try:
                    old_j = STATUS_JA.get(old_status, old_status)
                    new_j = STATUS_JA.get(new_status, new_status)

                    # 既存文面・件名は維持（テンプレ名変更なし）
                    subject = f"[{ev_title}] 参加ステータスが更新されました"
                    body = (
                        f"{nickname or '参加者'} 様\n\n"
                        f"イベント「{ev_title}」の参加ステータスが「{old_j}」から「{new_j}」に更新されました。\n"
                        f"詳細は以下のページをご確認ください。\n{view_url}\n"
                    )

                    send_mail(
                        to=to_email,
                        subject=subject,
                        body=body,
                        event_uuid=ev_uuid_str,  # From: <UUID@mail.iori0624.jp>
                        from_display_name=f"{ev_title} by Mimoria",
                    )
                except Exception as e:
                    current_app.logger.exception("status notify mail failed to %s: %s", to_email, e)
            notify_member_status_push(
                event_id=event_id,
                user_id=user_id,
                old_status=old_status,
                new_status=new_status,
            )
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    flash("更新しました。", "success")
    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))


from flask import request

@bp.route("/admin/events/discord-action", methods=["GET", "POST"])
def admin_discord_action():
    """
    Discord の承認/拒否ボタン用アクション。
    ※ GET は確認画面のみ（ここでは状態変更しない）
       POST で DB 更新（pending のときのみ適用）＋通知
    ※ いずれか一方が実行されたら、以後は両リンクとも無効（single-use）
    """
    from .utils import _verify_discord_action
    token = (request.values.get("t") or "").strip()

    v = _verify_discord_action(token)
    if not v:
        abort(400, "invalid or expired token")
    event_id, user_id, act = v  # act: "approve" | "reject" 想定

    # 表示用：イベント名／ユーザー名
    ev_title = "（不明なイベント）"
    nickname = "（不明なユーザー）"
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT title FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        row = cur.fetchone()
        if row:
            ev_title = row[0] if isinstance(row, tuple) else row.get("title")

        # ★ 参加者名（user_id だけで引く）
        cur.execute("SELECT nickname FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if row:
            nickname = row[0] if isinstance(row, tuple) else row.get("nickname")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 現在のステータスを取得（pending 以外ならリンクは無効扱い）
    def _current_status() -> str | None:
        dbs = get_db(); curs = dbs.cursor()
        try:
            curs.execute("""
                SELECT status
                  FROM mfu_event_member
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (event_id, user_id))
            r = curs.fetchone()
            if not r:
                return None
            return r[0] if isinstance(r, tuple) else r.get("status")
        finally:
            try: curs.close(); dbs.close()
            except Exception: pass

    STATUS_JA = {"approved": "承認", "rejected": "拒否", "pending": "保留"}

    # --- GET: 確認画面（single-use 事前判定） ---
    if request.method == "GET":
        from flask import render_template_string
        admin_event_url = url_for("external_login_user.admin_event_view", event_id=event_id, _external=True)
        verb = "承認" if act == "approve" else "拒否"

        cur_stat = _current_status()
        if cur_stat is None:
            abort(404, "参加者が見つかりません")
        if cur_stat != "pending":
            decided = STATUS_JA.get(cur_stat, cur_stat)
            return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>リンクは無効です</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <h1 style="font-size:20px;margin:0 0 12px">このワンタイムリンクは無効です。</h1>
  <p style="margin:4px 0 0">イベント：<strong>{{ ev_title }}</strong></p>
  <p style="margin:2px 0 8px">申請者：<strong>{{ nickname }}</strong></p>
  <p style="margin:2px 0 16px;color:#555">理由：すでに<strong>{{ decided }}</strong> 済みです。</p>
  <a href="{{ admin_event_url }}" style="padding:10px 16px;display:inline-block;text-decoration:none;border:1px solid #ccc;border-radius:6px">管理画面を開く</a>
</div>
            """, ev_title=ev_title, nickname=nickname, decided=decided, admin_event_url=admin_event_url)

        # まだ pending のときだけ確認画面を出す
        return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>参加申請 {{ verb }} 確認</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <h1 style="font-size:20px;margin:0 0 12px">この操作を実行しますか？（{{ verb }}）</h1>
  <p style="margin:4px 0 0">イベント：<strong>{{ ev_title }}</strong></p>
  <p style="margin:2px 0 16px">申請者：<strong>{{ nickname }}</strong></p>

  <form method="post" style="display:inline-block;margin-right:8px">
    <input type="hidden" name="t" value="{{ token }}">
    <button type="submit" style="padding:10px 16px">実行する</button>
  </form>
  <a href="{{ admin_event_url }}" style="padding:10px 16px;display:inline-block;text-decoration:none;border:1px solid #ccc;border-radius:6px">管理画面を開く</a>

  <p style="margin-top:20px;color:#666;font-size:13px">
    ※この確認画面はリンクプレビュー等の自動アクセス時に誤って状態変更されないようにするためのものです。
  </p>
</div>
        """, verb=verb, ev_title=ev_title, nickname=nickname, token=token, admin_event_url=admin_event_url)

    # --- POST: 実行（single-use 本判定） ---
    if act not in ("approve", "reject"):
        abort(400, "unsupported action")

    cur_stat = _current_status()
    from flask import render_template_string
    admin_event_url = url_for("external_login_user.admin_event_view", event_id=event_id, _external=True)

    # pending 以外なら無効（＝一度実行済み）
    if cur_stat is None:
        abort(404, "参加者が見つかりません")
    if cur_stat != "pending":
        decided = STATUS_JA.get(cur_stat, cur_stat)
        return render_template_string("""
<!doctype html><meta charset="utf-8">
<title>リンクは無効です</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <p style="margin:0 0 8px">このワンタイムリンクは無効です（既に {{ decided }} 済み）。</p>
  <p style="margin:0 0 4px">イベント：<strong>{{ ev_title }}</strong></p>
  <p style="margin:0 0 12px">申請者：<strong>{{ nickname }}</strong></p>
  <a href="{{ admin_event_url }}" style="padding:10px 16px;display:inline-block;text-decoration:none;border:1px solid #ccc;border-radius:6px">管理画面を開く</a>
</div>
        """, ev_title=ev_title, nickname=nickname, decided=decided, admin_event_url=admin_event_url)

    # ここまで来たら pending → 実行
    new_status = "approved" if act == "approve" else "rejected"
    ok, msg, applied = _update_member_status_and_notify(event_id, user_id, new_status)

    applied_ja = STATUS_JA.get(applied, applied)
    if ok:
        done = f"{'承認しました。' if applied == 'approved' else '拒否しました。'}"
        note = "メール通知を送信しました。" if msg != "no change" else "（変更なし：通知は送信されていません）"
    else:
        done = "更新に失敗しました。"
        note = f"理由: {msg}"

    return render_template_string(f"""
<!doctype html><meta charset="utf-8">
<title>完了</title>
<div style="padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial">
  <p style="margin:0 0 8px">{done}</p>
  <p style="margin:0 0 4px">イベント：<strong>{{{{ ev_title }}}}</strong></p>
  <p style="margin:0 0 12px">申請者：<strong>{{{{ nickname }}}}</strong> / ステータス：<strong>{applied_ja}</strong></p>
  <p style="margin:0 0 16px;color:#555">{note}</p>
  <a href="{{{{ admin_event_url }}}}" style="padding:10px 16px;display:inline-block;text-decoration:none;border:1px solid #ccc;border-radius:6px">管理画面を開く</a>
</div>
    """, ev_title=ev_title, nickname=nickname, admin_event_url=admin_event_url)


# 追記：メモ用のカラムを緩やかに追加（存在すれば何もしない）
def _ensure_member_memo_columns():
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM mfu_event_member")
        rows = cur.fetchall() or []
        cols = set()
        for r in rows:
            try:
                cols.add(r[0])
            except Exception:
                cols.add(r.get("Field"))

        def _add(sql):
            try:
                cur.execute(sql); db.commit()
            except Exception:
                try: db.rollback()
                except Exception: pass

        if "contact_memo" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN contact_memo TEXT NULL")
        if "admin_note" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN admin_note TEXT NULL")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

@bp.route("/admin/events/<int:event_id>/members/<int:user_id>/status", methods=["POST"])
def admin_event_member_status_update(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    # form でも JSON でも受けます
    new_status = (request.form.get("status") or
                  (request.json.get("status") if request.is_json else "")).strip()

    ok, msg, applied = _update_member_status_and_notify(event_id, user_id, new_status)
    code = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg, "status": applied}), code

# --- 追加: メンバー編集ページ(GET) ---
@bp.get("/admin/events/<int:event_id>/members/<int:user_id>/edit", endpoint="admin_member_edit")
def admin_member_edit(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    # === イベント ===
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT id, title, event_uuid, fee_yen FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
    ev_row = cur.fetchone()
    if not ev_row:
        abort(404, "イベントが見つかりません")
    ev = {"id": ev_row[0], "title": ev_row[1], "event_uuid": ev_row[2], "fee_yen": ev_row[3]} if isinstance(ev_row, tuple) else dict(ev_row)
    ev["event_uuid_str"] = _uuid_bytes_to_str(ev.get("event_uuid"))

    # === 参加者 ===
    cur.close()
    cur = db.cursor(dictionary=True)
    cur.execute("""
      SELECT
        m.id AS id,
        m.user_id,
        u.nickname, u.x_id, u.instagram_id, u.email,
        u.avatar_file, u.avatar_url, u.updated_at,
        COALESCE(m.status, 'pending')         AS status,
        COALESCE(m.payment_status, 'unpaid')  AS payment_status,
        m.paid_at, m.receipt_url, m.joined_at,
        COALESCE(m.require_payment, 1)        AS require_payment,
        COALESCE(m.process, 0)                AS process,
        COALESCE(m.is_host, 0)                AS is_host,
        COALESCE(m.is_subhost, 0)             AS is_subhost,
        COALESCE(m.is_canceled, 0)            AS is_canceled,
        COALESCE(m.participant_role, 'none')  AS participant_role,
        m.costume_label,
        m.paid_amount_yen,
        m.custom_fee_yen,
        m.contact_memo,
        m.admin_note,
        m.receipt_note,
        m.bank_transfer, m.bank_dest_name, m.bank_remitter_name, m.bank_deposit_date,
        m.paypay_transfer, m.paypay_sender_name, m.paypay_sent_date
      FROM mfu_event_member m
      JOIN external_login_user u ON u.id = m.user_id
     WHERE m.event_id=%s AND m.user_id=%s
     LIMIT 1
    """, (event_id, user_id))
    r = cur.fetchone()
    cur.close(); db.close()
    if not r:
        abort(404, "参加者が見つかりません")

    m = dict(r)
    # ★ 修正点：0 を 1 に潰さない（None のときだけ 1）
    _rp = m.get("require_payment")
    m["require_payment"] = 1 if (_rp is None) else int(_rp)
    m["is_host"]         = int(bool(m.get("is_host")))
    m["is_subhost"]      = int(bool(m.get("is_subhost")))
    m["process"]         = int(bool(m.get("process")))
    m["is_canceled"]     = int(bool(m.get("is_canceled")))
    m["participant_role"]= m.get("participant_role") or "none"
    m["bank_transfer"]   = int(m.get("bank_transfer") or 0)
    m["paypay_transfer"] = int(m.get("paypay_transfer") or 0)

    # 支払申告ログ（最新20件）
    db2 = get_db(); cur2 = db2.cursor(dictionary=True)
    try:
        cur2.execute("""
            SELECT method, bank_id, note, created_at
              FROM mfu_payment_notice
             WHERE event_id=%s AND user_id=%s
               AND method IN ('paypay','bank')
             ORDER BY id DESC
             LIMIT 20
        """, (event_id, m["user_id"]))
        notices = cur2.fetchall()
    finally:
        try: cur2.close(); db2.close()
        except Exception: pass

    admin_csrf = _admin_csrf_token()
    card = None

    return render_template(
        "admin_member_edit.html",
        ev=ev, m=m, admin_csrf=admin_csrf,
        notices=notices, card=card
    )

# admin.py に追記：ACL一覧＋追加
@bp.post("/admin/events/<int:event_id>/acl/add")
def admin_event_acl_add(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    from .utils import _event_admin_can_manage
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    username = (request.form.get("username") or "").strip()
    role = (request.form.get("role") or "viewer").strip()
    if role not in ("viewer","manager"):
        role = "viewer"

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
          INSERT INTO mfu_event_admin_acl (event_id, username, role)
          VALUES (%s, %s, %s)
          ON DUPLICATE KEY UPDATE role=VALUES(role)
        """, (event_id, username, role))
        db.commit()
        flash("ACLを更新しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("ACLの更新に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

# 削除
@bp.post("/admin/events/<int:event_id>/acl/delete")
def admin_event_acl_delete(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    from .utils import _event_admin_can_manage
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    username = (request.form.get("username") or "").strip()
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("DELETE FROM mfu_event_admin_acl WHERE event_id=%s AND username=%s LIMIT 1",
                    (event_id, username))
        db.commit()
        flash("ACLを削除しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        flash("ACLの削除に失敗しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

@bp.route("/admin/events/<int:event_id>/acl", methods=["GET", "POST"])
def admin_event_acl(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # ← ここを増やす：スイッチ＆トークン＆UUIDをJinjaへ渡す
        cur.execute("""
            SELECT
              id, title, event_uuid,
              COALESCE(auto_approve_by_invite, 0) AS auto_approve_by_invite,
              invite_token,
              COALESCE(checkin_qr_enabled, 0) AS checkin_qr_enabled,
              checkin_qr_token,
              checkin_qr_expires_at
            FROM mfu_event
            WHERE id=%s
            LIMIT 1
        """, (event_id,))
        ev = cur.fetchone()
        if not ev:
            abort(404, "イベントが見つかりません")
        # UUIDの文字列化
        try:
            ev["event_uuid_str"] = _uuid_bytes_to_str(ev.get("event_uuid"))
        except Exception:
            ev["event_uuid_str"] = ev.get("event_uuid")

        if request.method == "POST":
            # CSRF
            if request.form.get("csrf_token") != session.get("admin_csrf"):
                abort(400, "CSRF token mismatch")

            # 送られてきた role[username] を全部適用
            cur2 = db.cursor()
            try:
                roles = {}
                for k, v in request.form.items():
                    if not k.startswith("role[") or not k.endswith("]"):
                        continue
                    username = k[5:-1].strip()
                    role = (v or "").strip()
                    if role not in ("", "viewer", "manager"):
                        role = ""
                    roles[username] = role

                for username, role in roles.items():
                    if not username:
                        continue
                    if role == "":
                        cur2.execute(
                            "DELETE FROM mfu_event_admin_acl WHERE event_id=%s AND username=%s LIMIT 1",
                            (event_id, username)
                        )
                    else:
                        cur2.execute("""
                            INSERT INTO mfu_event_admin_acl (event_id, username, role)
                            VALUES (%s, %s, %s)
                            ON DUPLICATE KEY UPDATE role=VALUES(role)
                        """, (event_id, username, role))
                db.commit()
                flash("権限を更新しました。", "success")
                # ★ 修正：保存後はイベント一覧へ戻す
                return redirect(url_for("external_login_user.admin_events_list"))
            finally:
                try: cur2.close()
                except Exception: pass

        # GET: 一覧表示用データ
        cur.execute("SELECT username FROM users ORDER BY username ASC")
        all_users = [ r["username"] for r in (cur.fetchall() or []) ]

        cur.execute("SELECT username, role FROM mfu_event_admin_acl WHERE event_id=%s", (event_id,))
        acl_rows = cur.fetchall() or []
        acl = { r["username"]: r["role"] for r in acl_rows }

        admin_csrf = _admin_csrf_token()
        return render_template("admin_event_acl.html",
                               ev=ev, all_users=all_users, acl=acl, admin_csrf=admin_csrf)
    finally:
        try: cur.close(); db.close()
        except Exception: pass


@bp.route("/admin/events/<int:event_id>/banks", methods=["GET", "POST"])
def admin_event_banks(event_id: int):
    # 管理ログイン必須
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    # 権限
    try:
        if not _event_admin_can_manage(event_id):
            abort(403, "このイベントを管理する権限がありません。")
    except Exception:
        # ガード関数が未定義でも落ちないように
        pass

    # --- イベントを dict 化で取得（Jinja 側で ev.id を使えるように） ---
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        row = cur.fetchone()
        if not row:
            abort(404, "イベントが見つかりません")
        col_names = [d[0] for d in cur.description] if getattr(cur, "description", None) else []
        ev = dict(zip(col_names, row)) if isinstance(row, tuple) else dict(row)
        # 表示用 UUID 文字列
        try:
            ev["event_uuid_str"] = _uuid_bytes_to_str(ev.get("event_uuid"))
        except Exception:
            pass

        # --- 振込テーブルが無ければ作成（自動保証） ---
        try:
            from .schema import _ensure_event_bank_table  # 追記した関数
            _ensure_event_bank_table(cur, db)
        except Exception:
            # 失敗してもページが落ちないようにする
            pass

        # --- POST: 追加/削除 ---
        if request.method == "POST":
            if request.form.get("csrf_token") != session.get("admin_csrf"):
                abort(400, "CSRF token mismatch")

            action = (request.form.get("action") or "").strip()
            if action == "add":
                label          = (request.form.get("label") or "").strip()
                bank_name      = (request.form.get("bank_name") or "").strip()
                branch_name    = (request.form.get("branch_name") or "").strip() or None
                account_kind   = (request.form.get("account_type") or "普通").strip()
                account_number = (request.form.get("account_no") or "").strip()
                account_holder = (request.form.get("holder") or "").strip()
                memo           = (request.form.get("note") or "").strip() or None

                if not (label and bank_name and account_number and account_holder):
                    flash("必須項目（表示名/銀行名/口座番号/名義）が不足しています。", "warning")
                else:
                    cur.execute("""
                        INSERT INTO mfu_event_bank
                          (event_id, label, bank_name, branch_name, account_kind,
                           account_number, account_holder, memo, sort_order, is_active)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, 0, 1)
                    """, (event_id, label, bank_name, branch_name, account_kind,
                          account_number, account_holder, memo))
                    db.commit()
                    flash("口座を追加しました。", "success")

                return redirect(url_for("external_login_user.admin_event_banks", event_id=event_id))

            elif action == "del":
                bid = request.form.get("bank_id")
                if bid and str(bid).isdigit():
                    cur.execute("DELETE FROM mfu_event_bank WHERE id=%s AND event_id=%s LIMIT 1", (int(bid), event_id))
                    db.commit()
                    flash("口座を削除しました。", "info")
                else:
                    flash("削除対象が不正です。", "warning")
                return redirect(url_for("external_login_user.admin_event_banks", event_id=event_id))

        # --- GET: 一覧取得（dict に整形） ---
        cur.execute("""
            SELECT id, label, bank_name, branch_name, account_kind, account_number, account_holder, memo, sort_order, is_active
              FROM mfu_event_bank
             WHERE event_id=%s
             ORDER BY sort_order ASC, id ASC
        """, (event_id,))
        rows = cur.fetchall() or []
        col_names = [d[0] for d in cur.description] if getattr(cur, "description", None) else []
        banks = [ (dict(zip(col_names, r)) if isinstance(r, tuple) else dict(r)) for r in rows ]

    finally:
        try: cur.close(); db.close()
        except Exception: pass

    admin_csrf = _admin_csrf_token()
    return render_template("admin_event_banks.html", ev=ev, banks=banks, admin_csrf=admin_csrf)

@bp.route("/admin/events/<int:event_id>/members/<int:member_id>/payment-status", methods=["POST"])
def admin_member_set_payment_status(event_id: int, member_id: int):
    """支払い確認フラグの更新（paid / pending）＋メール通知（send_mail統一）。"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    # 簡易CSRF
    token = request.form.get("csrf_token", "")
    if not token or token != _admin_csrf_token():
        abort(400, "invalid csrf token")

    set_status = (request.form.get("set_status") or "").strip()
    if set_status not in ("paid", "pending"):
        flash("不正なステータスです。", "error")
        return redirect(url_for("external_login_user.admin_member_edit", event_id=event_id, member_id=member_id))

    admin_note = (request.form.get("admin_note") or "").strip()

    db = get_db(); cur = db.cursor()
    try:
        # 事前に現在値＋通知に必要な情報を取得
        cur.execute("""
            SELECT m.payment_status, m.user_id, u.email, u.nickname
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id = m.user_id
             WHERE m.id=%s AND m.event_id=%s
             LIMIT 1
        """, (member_id, event_id))
        row = cur.fetchone()
        if not row:
            abort(404, "member not found")
        cur_status = row[0] if isinstance(row, tuple) else row["payment_status"]
        target_user_id = int(row[1] if isinstance(row, tuple) else row["user_id"])
        to_email   = row[2] if isinstance(row, tuple) else row["email"]
        # nickname はメール送信には使わない（send_mailはアドレスのみ）

        # イベント情報（件名/From用）
        cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        ev = cur.fetchone()
        if not ev:
            abort(404, "event not found")
        ev_title = ev[0] if isinstance(ev, tuple) else ev["title"]
        ev_uuid_b = ev[1] if isinstance(ev, tuple) else ev["event_uuid"]
        ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""

        # ステータス更新
        cur.execute("""
            UPDATE mfu_event_member
               SET payment_status=%s
             WHERE id=%s AND event_id=%s
             LIMIT 1
        """, (set_status, member_id, event_id))

        # 操作ログ（管理メモ）
        if admin_note:
            cur.execute("""
              INSERT INTO mfu_payment_notice (event_id, user_id, method, bank_id, note)
              SELECT %s, mem.user_id, 'admin', NULL, %s
                FROM mfu_event_member AS mem
               WHERE mem.id=%s AND mem.event_id=%s
               LIMIT 1
            """, (event_id, admin_note, member_id, event_id))

        db.commit()
        flash("支払いステータスを更新しました。", "success")
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    # === メール通知（変化があった場合のみ） ===
    try:
        if cur_status != set_status and to_email:
            MSG = {
                "paid":   "お支払いありがとうございます！  当日の参加お待ちしております💕",
                "pending":"お支払い確認しております。もう少々お待ちください。",
            }
            body = MSG.get(set_status)
            if body:
                send_mail(
                    to=to_email,
                    subject=f"【{ev_title}】お支払い状況の更新",
                    body=body,
                    event_uuid=ev_uuid_str,  # From: <UUID>@mail.iori0624.jp
                    from_display_name=f"{ev_title} by Mimoria",
                )
    except Exception:
        current_app.logger.exception("failed to send payment-status mail (member_id=%s, event_id=%s)", member_id, event_id)

    if cur_status != set_status:
        notify_member_payment_push(
            event_id=event_id,
            user_id=target_user_id,
            payment_status=set_status,
        )

    return redirect(url_for("external_login_user.admin_member_edit", event_id=event_id, member_id=member_id))

@bp.post("/admin/events/<int:event_id>/members/<int:user_id>/bulk", endpoint="admin_member_bulk_update")
def admin_member_bulk_update(event_id: int, user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    if request.form.get("csrf_token") != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")

    custom_fee_raw = (request.form.get("custom_fee_yen") or "").replace(",", "").strip()
    if custom_fee_raw == "":
        custom_fee_yen = None
    elif not custom_fee_raw.isdigit():
        flash("個別参加費は半角数字で入力してください", "warning")
        ref = request.headers.get("Referer")
        if ref:
            return redirect(ref)
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))
    else:
        custom_fee_yen = int(custom_fee_raw)
        if custom_fee_yen == 0:
            flash("個別参加費は1円以上。空欄で標準参加費です", "warning")
            ref = request.headers.get("Referer")
            if ref:
                return redirect(ref)
            return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    status = (request.form.get("status") or "").strip()
    if status not in ("approved", "pending", "rejected"):
        status = None

    admin_kind = (request.form.get("admin_kind") or "none").strip().lower()
    is_host    = 1 if admin_kind == "host"    else 0
    is_subhost = 1 if admin_kind == "subhost" else 0

    ui_role = (request.form.get("participant_role") or "none").strip().lower()
    if ui_role not in ("none", "camera", "assistant", "cosplayer", "other"):
        ui_role = "none"

    costume = (request.form.get("costume_label") or "").strip() or None
    keep_costume = ui_role in ("cosplayer", "other")

    require_payment = 1 if request.form.get("require_payment") == "1" else 0
    process_flag = 1 if (request.form.get("process") or "").strip().lower() in {"1", "on", "true", "yes"} else 0

    contact_memo = (request.form.get("contact_memo") or "").strip() or None
    admin_note   = (request.form.get("admin_note")   or "").strip() or None

    # 先に承認ステータス（通知付き）
    if status is not None:
        try:
            ok, msg, applied = _update_member_status_and_notify(event_id, user_id, status)
            if ok:
                flash(f"承認ステータスを「{ {'approved':'承認','pending':'保留','rejected':'拒否'}.get(applied,'—') }」に更新し、参加者へ通知しました。", "success")
            else:
                flash(f"承認ステータス更新でエラー: {msg}", "warning")
            status = None
        except Exception:
            current_app.logger.exception("bulk_update: status notify failed (event_id=%s user_id=%s)", event_id, user_id)
            flash("承認ステータスの通知でエラーが発生しました。", "warning")

    db = get_db(); cur = db.cursor()

    # DBのENUMに合わせてフォールバック
    save_role, degraded = _normalize_role_for_db(db, ui_role)

    # メモの扱い（UIでONだったときのみ保持）
    if not keep_costume:
        costume = None

    try:
        cur.execute("""
            SELECT COALESCE(is_canceled,0) AS is_canceled
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        is_canceled = int((row[0] if isinstance(row, tuple) else (row or {}).get("is_canceled") or 0))
        sets = [
            "is_host=%s",
            "is_subhost=%s",
            "participant_role=%s",
            "costume_label=%s",
            "require_payment=%s",
            "process=%s",
            "custom_fee_yen=%s",
            "contact_memo=%s",
            "admin_note=%s",
        ]
        save_process = process_flag
        if is_canceled == 1:
            save_process = int(bool(request.form.get("current_process") or 0))
            flash("キャンセル済みのため、加工回し必要設定は変更できません。", "warning")
        params = [is_host, is_subhost, save_role, costume, require_payment, save_process, custom_fee_yen, contact_memo, admin_note]

        sql = f"""
            UPDATE mfu_event_member
               SET {", ".join(sets)}
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """
        params.extend([event_id, user_id])
        cur.execute(sql, tuple(params))
        db.commit()
        if degraded:
            flash("注意：DBスキーマが 'other' を未対応のため、近い役割にフォールバックして保存しました。後日ENUMに 'other' を追加してください。", "warning")
        else:
            flash("参加者情報を保存しました。", "success")
    except Exception:
        try: db.rollback()
        except Exception: pass
        current_app.logger.exception("bulk_update failed (event_id=%s user_id=%s)", event_id, user_id)
        flash("保存時にエラーが発生しました。", "danger")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    _recalc_event_fee_if_auto(event_id)

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

@bp.post("/admin/events/<int:event_id>/members/<int:member_id>/payment-details")
def admin_member_update_payment_details(event_id: int, member_id: int):
    """支払い詳細の更新（require_payment には触れない）＋メール通知（send_mail統一）"""
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403)

    def _int01(x):
        try: return 1 if str(x).strip() in ("1", "on", "true", "True") else 0
        except Exception: return 0
    def _none_if_blank(x):
        x = (x or "").strip()
        return x if x else None

    bank_transfer  = _int01(request.form.get("bank_transfer")) or 0
    bank_dest_name = _none_if_blank(request.form.get("bank_dest_name"))
    bank_remitter  = _none_if_blank(request.form.get("bank_remitter_name"))
    bank_deposit   = _none_if_blank(request.form.get("bank_deposit_date"))

    paypay_transfer = _int01(request.form.get("paypay_transfer")) or 0
    paypay_sender   = _none_if_blank(request.form.get("paypay_sender_name"))
    paypay_sent     = _none_if_blank(request.form.get("paypay_sent_date"))

    new_pstatus = _none_if_blank(request.form.get("payment_status"))
    if new_pstatus not in (None, "unpaid", "pending", "paid", "refunded"):
        new_pstatus = None

    paid_amount_yen, amount_err = _parse_optional_int(request.form.get("paid_amount_yen"), "支払金額")
    if amount_err:
        flash(amount_err, "warning")
        ref = request.headers.get("Referer")
        if ref:
            return redirect(ref)
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    payment_row_id, row_err = _parse_optional_int(request.form.get("payment_row_id"), "payment_row_id")
    if row_err:
        flash(row_err, "warning")
        ref = request.headers.get("Referer")
        if ref:
            return redirect(ref)
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    receipt_url = _none_if_blank(request.form.get("receipt_url"))

    custom_fee_raw = (request.form.get("custom_fee_yen") or "").replace(",", "").strip()
    if custom_fee_raw == "":
        custom_fee_yen = None
    elif not custom_fee_raw.isdigit():
        flash("個別参加費は半角数字で入力してください", "warning")
        ref = request.headers.get("Referer")
        if ref:
            return redirect(ref)
        return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))
    else:
        custom_fee_yen = int(custom_fee_raw)
        if custom_fee_yen == 0:
            flash("個別参加費は1円以上。空欄で標準参加費です", "warning")
            ref = request.headers.get("Referer")
            if ref:
                return redirect(ref)
            return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

    admin_note_present = "admin_note" in request.form
    admin_note = _none_if_blank(request.form.get("admin_note"))
    receipt_note_present = "receipt_note" in request.form
    receipt_note = _none_if_blank(request.form.get("receipt_note"))

    # 現在の支払状態 & 通知用
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT m.payment_status, m.paid_at, m.user_id, u.email, u.nickname
              FROM mfu_event_member m
              JOIN external_login_user u ON u.id = m.user_id
             WHERE m.event_id=%s AND m.id=%s
             LIMIT 1
        """, (event_id, member_id))
        row = cur.fetchone()
        cur_ps = (row[0] if isinstance(row, tuple) else row.get("payment_status")) if row else "unpaid"
        cur_paid_at = (row[1] if isinstance(row, tuple) else row.get("paid_at")) if row else None
        target_user_id = int(row[2] if isinstance(row, tuple) else row.get("user_id")) if row else 0
        to_email   = row[3] if row and isinstance(row, tuple) else (row.get("email") if row else None)

        cur.execute("SELECT title, event_uuid FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        ev = cur.fetchone()
        if not ev:
            abort(404, "event not found")
        ev_title = ev[0] if isinstance(ev, tuple) else ev["title"]
        ev_uuid_b = ev[1] if isinstance(ev, tuple) else ev["event_uuid"]
        ev_uuid_str = _uuid_bytes_to_str(ev_uuid_b) or ""
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if new_pstatus == "unpaid":
        bank_transfer = 0
        bank_dest_name = None
        bank_remitter = None
        bank_deposit = None
        paypay_transfer = 0
        paypay_sender = None
        paypay_sent = None
        paid_amount_yen = None
        receipt_url = None
        payment_row_id = None

    # 更新クエリ（require_payment には触れない）
    sets = [
        "bank_transfer=%s",
        "bank_dest_name=%s",
        "bank_remitter_name=%s",
        "bank_deposit_date=%s",
        "paypay_transfer=%s",
        "paypay_sender_name=%s",
        "paypay_sent_date=%s",
        "paid_amount_yen=%s",
        "receipt_url=%s",
        "payment_row_id=%s",
        "custom_fee_yen=%s",
    ]
    params = [
        bank_transfer, bank_dest_name, bank_remitter, bank_deposit,
        paypay_transfer, paypay_sender, paypay_sent,
        paid_amount_yen, receipt_url, payment_row_id,
        custom_fee_yen,
    ]

    if admin_note_present:
        sets.append("admin_note=%s")
        params.append(admin_note)
    if receipt_note_present:
        sets.append("receipt_note=%s")
        params.append(receipt_note)

    # payment_status の更新と paid_at の自動付与/解除
    will_change = False
    if new_pstatus is not None and new_pstatus != cur_ps:
        will_change = True
        sets.append("payment_status=%s")
        params.append(new_pstatus)
        if new_pstatus == "paid" and not cur_paid_at:
            sets.append("paid_at=NOW()")
        if new_pstatus in ("unpaid", "pending", "refunded"):
            sets.append("paid_at=NULL")

    sql = f"UPDATE mfu_event_member SET {', '.join(sets)} WHERE event_id=%s AND id=%s LIMIT 1"
    params.extend([event_id, member_id])

    db = get_db(); cur = db.cursor()
    try:
        cur.execute(sql, params)
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # === メール通知（必要時） ===
    if will_change and new_pstatus in ("paid", "pending", "unpaid") and to_email:
        try:
            MSG = {
                "paid":   "お支払いありがとうございます！💕当日の参加お待ちしております💕",
                "pending":"お支払い確認しております。もう少々お待ちください。",
                "unpaid":"お支払いが確認出来ませんでした🙇　後ほど主催から個別に連絡致します。",
            }
            body = MSG.get(new_pstatus)
            if body:
                send_mail(
                    to=to_email,
                    subject=f"【{ev_title}】お支払い状況の更新",
                    body=body,
                    event_uuid=ev_uuid_str,
                    from_display_name=f"{ev_title} by Mimoria",
                )
        except Exception:
            current_app.logger.exception("failed to send payment-details mail (member_id=%s, event_id=%s)", member_id, event_id)

    if target_user_id > 0:
        effective_status = new_pstatus or cur_ps or "unpaid"
        notify_member_payment_push(
            event_id=event_id,
            user_id=target_user_id,
            payment_status=effective_status,
            kind=("event_payment_status" if will_change else "event_payment_details_updated"),
            body=(
                None
                if will_change
                else "支払金額・支払方法・領収書などの支払詳細が更新されました。"
            ),
        )

    flash("支払い詳細を更新しました。", "success")
    ref = request.headers.get("Referer")
    if ref:
        return redirect(ref)
    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

# 先頭の import 付近に追記
import os
from flask import request, jsonify, abort, session
from . import bp
from app.utils.db import get_db
from .utils import _require_mfu_login_redirect, _event_admin_can_manage

def _assert_admin_csrf():
    token = request.headers.get("X-CSRF-Token")
    if not token or token != session.get("admin_csrf"):
        abort(400, "CSRF token mismatch")


def _build_checkin_qr_url(token: str | None) -> str:
    tok = (token or "").strip()
    if not tok:
        return ""
    return url_for("external_login_user.event_qr_checkin", token=tok, _external=True)

# 自動承認 ON/OFF（ON時は未発行なら即トークン生成→JSONで返す）
@bp.post("/external-login/admin/event/<int:event_id>/auto-approve")
def admin_toggle_auto_approve(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")
    _assert_admin_csrf()

    enable = 1 if (request.form.get("enable") == "1") else 0

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT invite_token FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
        row = cur.fetchone() or {}
        token = (row.get("invite_token") or "")

        if enable == 1 and (not token or len(token) != 64):
            token = os.urandom(32).hex()  # 256bit → 64桁hex(小文字)
            cur.execute(
                "UPDATE mfu_event SET auto_approve_by_invite=%s, invite_token=%s WHERE id=%s",
                (enable, token, event_id)
            )
        else:
            cur.execute(
                "UPDATE mfu_event SET auto_approve_by_invite=%s WHERE id=%s",
                (enable, event_id)
            )
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({
        "ok": True,
        "auto_approve_by_invite": enable,
        "invite_token": (token if enable == 1 else None)
    })


# トークン発行/再発行（スイッチOFFのままでも可）
@bp.post("/external-login/admin/event/<int:event_id>/invite/rotate")
def admin_rotate_invite_token(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")
    _assert_admin_csrf()

    new_tok = os.urandom(32).hex()  # 256bit → 64桁hex(小文字)
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("UPDATE mfu_event SET invite_token=%s WHERE id=%s", (new_tok, event_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({"ok": True, "invite_token": new_tok})


@bp.post("/external-login/admin/event/<int:event_id>/checkin-qr/rotate")
def admin_rotate_checkin_qr_token(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")
    _assert_admin_csrf()

    new_tok = os.urandom(32).hex()
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("UPDATE mfu_event SET checkin_qr_enabled=1, checkin_qr_token=%s WHERE id=%s", (new_tok, event_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({
        "ok": True,
        "checkin_qr_token": new_tok,
        "checkin_qr_url": _build_checkin_qr_url(new_tok),
        "qr_trademark_notice": QR_TRADEMARK_NOTICE,
    })


@bp.post("/external-login/admin/event/<int:event_id>/checkin-qr/expires")
def admin_update_checkin_qr_expires(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")
    _assert_admin_csrf()

    raw = (request.form.get("expires_at") or "").strip()
    expires_at = None
    if raw:
        from datetime import datetime as _dt
        try:
            expires_at = _dt.strptime(raw, "%Y-%m-%dT%H:%M")
        except Exception:
            return jsonify({"ok": False, "error": "期限の形式が不正です。"}), 400

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("UPDATE mfu_event SET checkin_qr_expires_at=%s WHERE id=%s", (expires_at, event_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({
        "ok": True,
        "checkin_qr_expires_at": (expires_at.strftime("%Y-%m-%d %H:%M:%S") if expires_at else None),
    })

@bp.route("/admin/events/<int:event_id>/members/<int:member_id>/status", methods=["POST"], endpoint="admin_member_update_status")
def admin_member_update_status(event_id: int, member_id: int):
    """
    管理画面：参加者の承認ステータス変更 + 本人へメール通知
    フォーム: status=approved|pending|rejected
    """
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403, "このイベントを管理する権限がありません。")

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in {"approved", "pending", "rejected"}:
        abort(400, "不正なステータスです。")

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # 対象メンバーとイベント情報を取得（メールテンプレ材料）
        cur.execute("""
            SELECT m.id AS member_id, m.status AS old_status, m.user_id,
                   e.id AS event_id, e.title, e.event_uuid,
                   u.nickname, u.email
              FROM mfu_event_member AS m
              JOIN mfu_event AS e ON e.id = m.event_id
              JOIN external_login_user AS u ON u.id = m.user_id
             WHERE m.id=%s AND m.event_id=%s
             LIMIT 1
        """, (member_id, event_id))
        row = cur.fetchone()
        if not row:
            abort(404, "参加者が見つかりません。")

        old_status = ((row.get("old_status") or "") if row.get("old_status") is not None else "").strip().lower()
        target_user_id = int(row.get("user_id") or 0)
        if target_user_id <= 0:
            abort(404, "参加者が見つかりません。")
        if old_status == new_status:
            # ステータスが変わらない場合は更新せず完了
            flash("ステータスに変更はありません（メール送信なし）。", "info")
            return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

        # ステータス更新（承認遷移時のSystem自動投稿を含む共通処理）
        update_event_member_status(event_id=event_id, user_id=target_user_id, new_status=new_status)
    finally:
        try: cur.close()
        except Exception: pass

    # ===== メール送信 =====
    nickname = row.get("nickname") or "参加者"
    email    = (row.get("email") or "").strip()
    title    = row.get("title") or "イベント"
    # event_uuid は bytes の可能性があるので文字列化
    try:
        event_uuid_str = _uuid_bytes_to_str(row.get("event_uuid"))
    except Exception:
        event_uuid_str = row.get("event_uuid")

    view_url = url_for("external_login_user.view_event", event_uuid=event_uuid_str, _external=True)

    # 日本語テンプレ
    jp = {
        "approved": {
            "subject": f"【{title}】参加申請のステータス：承認",
            "body": f"""{nickname} 様

イベント「{title}」への参加が承認されました。
当日のご参加をお待ちしております！
{view_url}
""",
        },
        "pending": {
            "subject": f"【{title}】参加申請のステータス：保留",
            "body": f"""{nickname} 様

イベント「{title}」への参加が保留されました。
確認事項があります。後ほど主催から個別にご連絡いたします。
{view_url}
""",
        },
        "rejected": {
            "subject": f"【{title}】参加申請のステータス：拒否",
            "body": f"""{nickname} 様

イベント「{title}」への参加が拒否されました。
確認事項があります。後ほど主催から個別にご連絡いたします。
{view_url}
""",
        },
    }

    # メールがあれば送る（なければフラッシュだけ）
    if email:
        try:
            send_mail(
                to=email,
                subject=jp[new_status]["subject"],
                body=jp[new_status]["body"],
                event_uuid=event_uuid_str,
                from_display_name=f"{title} by Mimoria",
            )
            flash("ステータスを更新し、参加者へメール通知しました。", "success")
        except Exception:
            current_app.logger.exception("failed to send status mail (member_id=%s, event_id=%s)", member_id, event_id)
            flash("ステータスは更新しましたが、メール送信でエラーが発生しました。", "warning")
    else:
        flash("ステータスを更新しました（メールアドレス未設定のため通知なし）。", "warning")

    notify_member_status_push(
        event_id=event_id,
        user_id=target_user_id,
        old_status=old_status,
        new_status=new_status,
    )

    return redirect(url_for("external_login_user.admin_event_view", event_id=event_id))

@bp.post("/admin/events/<int:event_id>/copy")
def admin_event_copy(event_id: int):
    guard = _require_mfu_login_redirect()
    if guard: return guard
    if not _event_admin_can_manage(event_id):
        abort(403)

    db = get_db(); cur = db.cursor(dictionary=True)

    # 元イベント取得
    cur.execute("""
        SELECT
          title, fee_yen,
          place_name, address, maps_url,
          pay_from, pay_until,
          line_openchat_url, line_openchat_pass,
          google_form_url,
          memo_all,
          allow_square, allow_paypay, allow_bank,
          paypay_display
        FROM mfu_event
        WHERE id=%s
        LIMIT 1
    """, (event_id,))
    src = cur.fetchone()
    if not src:
        abort(404)

    # 新規作成（UUIDは新規）
    new_title = f"{src['title']}（コピー）"

    cur.execute("""
        INSERT INTO mfu_event (
          event_uuid, title, owner_user_id,
          fee_yen,
          place_name, address, maps_url,
          pay_from, pay_until,
          checkin_qr_enabled,
          line_openchat_url, line_openchat_pass,
          google_form_url,
          memo_all,
          allow_square, allow_paypay, allow_bank,
          paypay_display
        )
        VALUES (
          UNHEX(REPLACE(UUID(),'-','')),
          %s, NULL,
          %s,
          %s, %s, %s,
          %s, %s,
          1,
          %s, %s,
          %s,
          %s,
          %s, %s, %s,
          %s
        )
    """, (
        new_title,
        src["fee_yen"],
        src["place_name"], src["address"], src["maps_url"],
        src["pay_from"], src["pay_until"],
        src["line_openchat_url"], src["line_openchat_pass"],
        src["google_form_url"],
        src["memo_all"],
        src["allow_square"], src["allow_paypay"], src["allow_bank"],
        src["paypay_display"],
    ))

    db.commit()
    new_event_id = cur.lastrowid
    cur.close(); db.close()

    # 新規アルバム＆決済UUID
    album_id = create_event_album(title=new_title, event_id=new_event_id)
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE mfu_event SET album_id=%s WHERE id=%s", (album_id, new_event_id))
    db.commit()
    cur.close(); db.close()

    _ensure_payment_uuid_for_event(new_event_id)

    flash("イベントをコピーしました。日時だけ設定してください。", "success")
    return redirect(url_for("external_login_user.admin_event_edit", event_id=new_event_id))
