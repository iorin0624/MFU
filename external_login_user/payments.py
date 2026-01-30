# /mnt/mfu/app/external_login_user/payments.py
from __future__ import annotations

import os
import base64
import uuid
from datetime import datetime, timedelta

from flask import (
    request, session, redirect, url_for, flash, abort, render_template, current_app
)

from . import bp
from .utils import (
    _require_ext_login, _get_ext_user_by_social, _event_by_uuid_str,
    _membership_status, _member_payment_status, _get_member_require_payment,
    PAYMENT_ENTRY_BASE,
)
from app.utils.db import get_db
from app.utils.mail import send_mail
from flask import current_app
from app.utils.mail import send_mail

from urllib.parse import quote

import requests  # Discord通知で使用（存在しなければ無視されるよう例外処理）

# ============================================================
# ヘルパ
# ============================================================

def _ensure_payment_uuid_for_event(event_id: int) -> str:
    """イベントに payment_uuid が無ければ発行して保存、返す"""
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT payment_uuid FROM mfu_event WHERE id=%s", (event_id,))
    row = cur.fetchone()
    if row and (row[0] if isinstance(row, tuple) else row["payment_uuid"]):
        v = row[0] if isinstance(row, tuple) else row["payment_uuid"]
        cur.close(); db.close()
        return v
    token = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii").rstrip("=")  # 約22字
    cur.execute("UPDATE mfu_event SET payment_uuid=%s WHERE id=%s", (token, event_id))
    db.commit(); cur.close(); db.close()
    return token


def _as_dt(v):
    """MySQL DATETIME / 文字列の両対応で datetime に変換"""
    if not v:
        return None
    try:
        return v if hasattr(v, "year") else datetime.fromisoformat(str(v).replace(" ", "T"))
    except Exception:
        return None


def _event_attach_uuid_str(ev: dict, event_uuid_str: str) -> dict:
    """テンプレ用に ev.event_uuid_str を付与"""
    try:
        ev["event_uuid_str"] = event_uuid_str
    except Exception:
        pass
    return ev


# --- PayPay: p2pリンク解決（イベント > 環境変数 > 既定値） ---
_DEFAULT_PAYPAY_P2P = "https://qr.paypay.ne.jp/p2p01_doQNfoJkfvJAAo8l"

def _paypay_p2p_url(ev: dict) -> str:
    # イベント個別（カラムがあれば）
    ev_url = (ev.get("paypay_p2p_url") or ev.get("paypay_qr_url") or "").strip() if isinstance(ev.get("paypay_p2p_url") or ev.get("paypay_qr_url"), str) else ""
    if ev_url:
        return ev_url
    # 環境変数
    env_url = (os.environ.get("PAYPAY_P2P_URL") or "").strip()
    if env_url:
        return env_url
    # 既定
    return _DEFAULT_PAYPAY_P2P


def _fetch_event_banks_active(event_id: int):
    """
    参加者向け：is_active=1 の複数口座（id 含む）を表示順で取得。
    戻りはタプル列: (id, label, bank_name, branch_name, account_kind, account_number, account_holder, memo)
    """
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT id, label, bank_name, branch_name, account_kind, account_number, account_holder, memo
              FROM mfu_event_bank
             WHERE event_id=%s AND is_active=1
             ORDER BY COALESCE(sort_order, 0) ASC, id ASC
        """, (event_id,))
        rows = cur.fetchall() or []
        return rows
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass


def _enabled_methods(ev: dict) -> dict[str, bool]:
    """
    支払い手段の有効化判定。
    ★テンプレと一致させるためキーは card / paypay / bank に統一。
    ★選択画面では allow_* の ON/OFF のみを見る（PayPay期限チェックは撤廃）。
    """
    card_enabled = bool(
        ev.get("allow_square") or
        ev.get("allow_card") or
        ev.get("allow_credit") or
        ev.get("allow_credit_card") or
        ev.get("enable_card") or
        ev.get("card_enabled") or
        ev.get("payment_card") or
        ev.get("square_enabled")
    )
    return {
        "card":   card_enabled,
        "paypay": bool(ev.get("allow_paypay")),
        "bank":   bool(ev.get("allow_bank")),
    }


def _ensure_payment_notice_table():
    """支払通知ログを格納するテーブルを自動作成"""
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mfu_payment_notice (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              event_id BIGINT UNSIGNED NOT NULL,
              user_id  BIGINT UNSIGNED NOT NULL,
              method   ENUM('square','paypay','bank') NOT NULL,
              bank_id  BIGINT UNSIGNED NULL,
              note     VARCHAR(255) NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              KEY idx_evt_user_created (event_id, user_id, created_at),
              KEY idx_bank (bank_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        db.commit()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

def _is_lecture_event(ev: dict) -> bool:
    try:
        return "【講座】" in str(ev.get("title") or "")
    except Exception:
        return False

def _lecture_auto_approve_from_iv_session(event_uuid: str) -> bool:
    try:
        return bool((session.get("lecture_auto_approve_by_iv") or {}).get(event_uuid))
    except Exception:
        return False

def _clear_lecture_auto_approve_iv_session(event_uuid: str) -> None:
    try:
        store = session.get("lecture_auto_approve_by_iv") or {}
        if event_uuid in store:
            store.pop(event_uuid, None)
            session["lecture_auto_approve_by_iv"] = store
    except Exception:
        pass

def _resolve_member_fee(event_id: int, user_id: int, default_fee: int) -> int:
    """ユーザー別の金額があれば優先し、なければイベント金額を返す。"""
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT custom_fee_yen
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             ORDER BY id DESC
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone() or {}
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    custom_fee = row.get("custom_fee_yen") if isinstance(row, dict) else None
    try:
        custom_fee_int = int(custom_fee) if custom_fee is not None else None
    except Exception:
        custom_fee_int = None
    if custom_fee_int is not None and custom_fee_int > 0:
        return custom_fee_int
    return int(default_fee or 0)

def _create_payment_request(
    event_id: int,
    user_id: int,
    amount_yen: int,
    *,
    event_uuid: str | None,
    nickname: str | None,
    x_id: str | None,
    instagram_id: str | None,
    lecture_auto_approve: bool = False,
) -> str:
    """支払いリクエストを発行し、トークンを返す。"""
    token = str(uuid.uuid4())
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO mfu_payment_request (
              token, event_id, event_uuid, user_id, nickname, x_id, instagram_id, amount_yen, lecture_auto_approve
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            token,
            event_id,
            event_uuid,
            user_id,
            nickname,
            x_id,
            instagram_id,
            int(amount_yen),
            1 if lecture_auto_approve else 0,
        ))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return token

def _amount_from_payment_token(event_id: int, user_id: int, token: str | None) -> int | None:
    if not token:
        return None
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT amount_yen
              FROM mfu_payment_request
             WHERE token=%s AND event_id=%s AND user_id=%s
               AND status='pending'
             ORDER BY id DESC
             LIMIT 1
        """, (token, event_id, user_id))
        row = cur.fetchone() or {}
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    try:
        return int(row.get("amount_yen")) if row.get("amount_yen") is not None else None
    except Exception:
        return None


def _mask_payment_token(token: str | None, *, show: int = 4) -> str:
    if not token:
        return "(none)"
    token = str(token)
    if len(token) <= show:
        return "*" * len(token)
    return f"***{token[-show:]}"


def _payment_request_id_from_token(event_id: int, user_id: int, token: str | None) -> int | None:
    if not token:
        return None
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id
              FROM mfu_payment_request
             WHERE token=%s AND event_id=%s AND user_id=%s
             ORDER BY id DESC
             LIMIT 1
        """, (token, event_id, user_id))
        row = cur.fetchone()
        if row:
            return row["id"] if isinstance(row, dict) else row[0]
    except Exception:
        current_app.logger.exception(
            "payment request lookup failed token=%s event_id=%s user_id=%s",
            _mask_payment_token(token),
            event_id,
            user_id,
        )
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return None


def _resolve_payment_request_id(
    event_id: int,
    user_id: int,
    token: str | None,
    payment_row_id: int | None,
) -> int | None:
    payment_request_id = _payment_request_id_from_token(event_id, user_id, token)
    if payment_request_id is not None:
        return payment_request_id
    return payment_row_id


def _mark_payment_request_used(payment_request_id: int, token: str | None) -> None:
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            UPDATE mfu_payment_request
               SET status='used',
                   used_at=COALESCE(used_at, NOW())
             WHERE id=%s
        """, (payment_request_id,))
        db.commit()
    except Exception:
        current_app.logger.exception(
            "payment request update failed id=%s token=%s",
            payment_request_id,
            _mask_payment_token(token),
        )
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _build_member_receipt_pdf_url(event_uuid: str, event_id: int, user_id: int) -> str | None:
    try:
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("""
                SELECT id
                  FROM mfu_event_member
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (event_id, user_id))
            row = cur.fetchone()
            if not row:
                return None
            member_id = row[0] if isinstance(row, tuple) else row.get("id")
            if not member_id:
                return None
        finally:
            try: cur.close(); db.close()
            except Exception: pass
        return url_for(
            "external_login_user.member_receipt_pdf",
            event_uuid=event_uuid,
            member_id=member_id,
            _external=True,
        )
    except Exception:
        return None

# ============================================================
# users.py を踏襲した 通知系ヘルパ
# ============================================================

def _users_table_columns() -> set[str]:
    """users テーブルのカラム名をセットで取得（存在しない環境でも安全に）"""
    cols: set[str] = set()
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
        """)
        rows = cur.fetchall() or []
        for r in rows:
            cols.add(r[0] if isinstance(r, tuple) else r.get("COLUMN_NAME"))
    except Exception:
        pass
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return cols


def _fetch_admin_contacts() -> tuple[str | None, str | None]:
    """
    admin のメール/Discord Webhook を取得。
    - users テーブルの候補カラムを動的に COALESCE
    - メールは環境変数/設定でフォールバック
    戻り: (admin_email | None, admin_webhook | None)
    """
    cols = _users_table_columns()
    email_candidates = [c for c in ("notify_email","email","mail","email_address") if c in cols]
    wh_candidates    = [c for c in ("webhook_url","discord_webhook_url") if c in cols]

    admin_email, admin_webhook = None, None
    db = get_db(); cur = db.cursor()
    try:
        sel_email = "'' AS email"
        if email_candidates:
            sel_email = "COALESCE(" + ",".join([c for c in email_candidates]) + ", '') AS email"
        sel_wh = "'' AS webhook_url"
        if wh_candidates:
            sel_wh = f"COALESCE({wh_candidates[0]}, '') AS webhook_url"

        cur.execute(f"SELECT {sel_email}, {sel_wh} FROM users WHERE username=%s LIMIT 1", ("admin",))
        row = cur.fetchone()
        if row:
            if isinstance(row, tuple):
                admin_email   = (row[0] or "").strip() or None
                admin_webhook = (row[1] or "").strip() or None
            else:
                admin_email   = (row.get("email") or "").strip() or None
                admin_webhook = (row.get("webhook_url") or "").strip() or None
    except Exception:
        current_app.logger.exception("fetch admin contacts failed")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # メールはフォールバック
    if not admin_email:
        admin_email = (current_app.config.get("ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL") or "").strip() or None

    return admin_email, admin_webhook


def _fetch_acl_contacts(event_id: int) -> list[dict]:
    """
    ACL（adminは除外）を users と join してメール/Discord Webhook を取得。
    戻り: [{"username": str, "email": str|None, "webhook_url": str|None}, ...]
    """
    cols = _users_table_columns()
    email_candidates = [c for c in ("notify_email","email","mail","email_address") if c in cols]
    wh_candidates    = [c for c in ("webhook_url","discord_webhook_url") if c in cols]

    out: list[dict] = []
    db = get_db(); cur = db.cursor()
    try:
        sel_email = "'' AS email"
        if email_candidates:
            sel_email = "COALESCE(" + ",".join([f"u.{c}" for c in email_candidates]) + ", '') AS email"
        sel_wh = "'' AS webhook_url"
        if wh_candidates:
            sel_wh = f"COALESCE(u.{wh_candidates[0]}, '') AS webhook_url"

        sql = f"""
            SELECT a.username, {sel_email}, {sel_wh}
              FROM mfu_event_admin_acl a
         LEFT JOIN users u ON u.username = a.username
             WHERE a.event_id=%s
        """
        cur.execute(sql, (event_id,))
        rows = cur.fetchall() or []
        for r in rows:
            if isinstance(r, tuple):
                username = (r[0] or "").strip()
                if username == "admin":  # admin は二重送信しない
                    continue
                out.append({
                    "username": username,
                    "email": (r[1] or "").strip() or None,
                    "webhook_url": (r[2] or "").strip() or None
                })
            else:
                username = (r.get("username") or "").strip()
                if username == "admin":
                    continue
                out.append({
                    "username": username,
                    "email": (r.get("email") or "").strip() or None,
                    "webhook_url": (r.get("webhook_url") or "").strip() or None
                })
    except Exception:
        current_app.logger.exception("fetch acl contacts failed")
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    return out


def _post_discord(webhook_url: str | None, content: str) -> None:
    """Discordへシンプル通知（失敗してもアプリ動作は阻害しない）"""
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": content}, timeout=5)
    except Exception:
        current_app.logger.exception("discord notify failed")


# /mnt/mfu/app/external_login_user/payments.py
# ↓ この関数ブロックだけ置き換え
def _send_mail_safe(to: str | list[str], subject: str, body: str, *, event_uuid: str | None) -> None:
    """メール送信は app.utils.mail.send_mail に統一。SMTP設定は mail.py 側の既定を使用。"""
    try:
        send_mail(
            to=to,
            subject=subject,
            body=body,
            event_uuid=event_uuid,
            # smtp_host / smtp_port / timeout は渡さない（mail.py の既定に統一）
        )
    except Exception:
        current_app.logger.exception("mail send failed to=%s", to)

def _notify_payment_to_admin_and_acl(
    ev: dict,
    *,
    event_uuid_str: str | None,
    subject: str,
    mail_lines: list[str],
    discord_lines: list[str] | None = None,
) -> None:
    """
    支払い系の通知を admin と ACL に送信する（users.pyの流儀）。
    - admin は必ず送る（連絡先があれば）
    - ACL は admin を除外して送る
    - Discord は連絡先が取得できた場合のみ送る（メール優先）
    """
    admin_email, admin_webhook = _fetch_admin_contacts()
    acl = _fetch_acl_contacts(int(ev["id"]))

    body = "\n".join([s for s in (mail_lines or []) if s])
    discord_text = "\n".join([s for s in (discord_lines or mail_lines or []) if s])

    # --- Adminへ ---
    if admin_email:
        _send_mail_safe(admin_email, subject, body, event_uuid=event_uuid_str or ev.get("event_uuid_str"))
    if admin_webhook:
        _post_discord(admin_webhook, discord_text)

    # --- ACLへ（adminは除外済み）---
    for rcpt in acl:
        if rcpt.get("email"):
            _send_mail_safe(rcpt["email"], subject, body, event_uuid=event_uuid_str or ev.get("event_uuid_str"))
        if rcpt.get("webhook_url"):
            _post_discord(rcpt["webhook_url"], discord_text)


# ============================================================
# Square開始（※多重設定時は options に誘導）＋ POSTでの手段分岐
# ============================================================

# === 既存: pay_start の中を一部置き換え ===
@bp.route("/pay/start/<event_uuid>", methods=["GET", "POST"])
def pay_start(event_uuid: str):
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    methods = _enabled_methods(ev)
    force = (request.args.get("force") == "1")  # ★ Square強制遷移フラグ

    # ─ 支払期間ガード ─（従来のまま）
    now = datetime.now()
    pf = _as_dt(ev.get("pay_from"))
    pu = _as_dt(ev.get("pay_until"))
    if (pf and now < pf) or (pu and now > pu):
        if pf and pu:
            flash(f"支払い可能期間は {pf:%Y-%m-%d %H:%M} 〜 {pu:%Y-%m-%d %H:%M} です。", "warning")
        elif pf:
            flash(f"支払いは {pf:%Y-%m-%d %H:%M} から可能です。", "warning")
        else:
            flash(f"支払いは {pu:%Y-%m-%d %H:%M} までに行ってください。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # ─ ここから講座モード分岐 ─
    lecture = _is_lecture_event(ev)

    # 非講座は従来どおり「承認済みのみ支払い可」
    if not lecture:
        if _membership_status(ev["id"], me["id"]) != "approved":  # type: ignore
            flash("承認済みの参加者のみ支払いできます。", "warning")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # 講座モード：メンバー行が無ければ pending で作っておく（支払い先行）
    if lecture:
        db_tmp = get_db(); cur_tmp = db_tmp.cursor()
        try:
            cur_tmp.execute("""
                SELECT id, COALESCE(payment_status,'unpaid') AS ps
                  FROM mfu_event_member
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (ev["id"], me["id"]))  # type: ignore
            row = cur_tmp.fetchone()
            if not row:
                cur_tmp.execute("""
                    INSERT INTO mfu_event_member
                      (event_id, user_id, status, payment_status, require_payment, joined_at)
                    VALUES (%s,%s,'pending','unpaid',1,NOW())
                """, (ev["id"], me["id"]))  # type: ignore
                db_tmp.commit()
        finally:
            try: cur_tmp.close(); db_tmp.close()
            except Exception: pass

    # ─ 参加費ガード ─（従来のまま）
    if _get_member_require_payment(ev["id"], me["id"]) == 0:  # type: ignore
        flash("このイベントはあなたは『支払い不要』に設定されています。", "info")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore
    if not fee or int(fee) <= 0:
        flash("このイベントに参加費は設定されていません。", "info")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # ─ 支払済み/確認中ガードの扱いを講座で緩和 ─
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              COALESCE(payment_status,'unpaid') AS payment_status,
              COALESCE(bank_transfer,0)   AS bank_transfer,
              COALESCE(paypay_transfer,0) AS paypay_transfer
            FROM mfu_event_member
            WHERE event_id=%s AND user_id=%s
            LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        ms = cur.fetchone() or {}
    finally:
        try: cur.close()
        except Exception: pass

    cur_ps = (ms.get("payment_status") or "unpaid").strip()
    if cur_ps not in ("unpaid", "pending", "paid"):
        cur_ps = "unpaid"

    # pending の偽ペン補正（従来のまま）
    if cur_ps == "pending" and int(ms.get("bank_transfer", 0)) == 0 and int(ms.get("paypay_transfer", 0)) == 0:
        cur2 = get_db().cursor()
        try:
            cur2.execute("""
                UPDATE mfu_event_member
                   SET payment_status='unpaid'
                 WHERE event_id=%s AND user_id=%s
                 LIMIT 1
            """, (ev["id"], me["id"]))  # type: ignore
            get_db().commit()
            cur_ps = "unpaid"
        finally:
            try: cur2.close()
            except Exception: pass

    # 非講座：従来どおり
    if not lecture:
        if cur_ps == "paid":
            flash("すでに支払い済みです。", "info")
            return redirect(url_for("external_login_user.index"))
        if cur_ps == "pending":
            flash("主催の入金確認中です。確認完了までお支払いの再実行はできません。", "info")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))
    else:
        # 講座：paid は止める／pending はカード再実行は不可、PayPay/銀行の“申告”はテンプレ側で案内
        if cur_ps == "paid":
            flash("すでにお支払い済みです。参加申請へお進みください。", "info")
            return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))

    # ─ 以下、支払方法分岐は従来どおり（省略なしで原文維持） ─
    lecture_auto_approve = False
    iv = (request.args.get("iv") or request.args.get("vi") or "").strip()
    if not iv:
        iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid) or ""
    if iv:
        store = session.get("lecture_invite_tokens") or {}
        store[event_uuid] = iv
        session["lecture_invite_tokens"] = store
    auto_approve_hit = bool(
        lecture
        and int(ev.get("auto_approve_by_invite") or 0) == 1
        and ev.get("invite_token")
        and iv
        and iv == ev.get("invite_token")
    )
    if auto_approve_hit:
        store = session.get("lecture_auto_approve_by_iv") or {}
        store[event_uuid] = True
        session["lecture_auto_approve_by_iv"] = store
    if lecture and (auto_approve_hit or _lecture_auto_approve_from_iv_session(event_uuid)):
        lecture_auto_approve = True

    if request.method == "POST":
        picked = (request.form.get("method") or "").strip()
        if picked == "card":
            if not methods.get("card"):
                flash("このイベントではクレジットカード決済は利用できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            pay_ev_uuid = ev.get("payment_uuid") or _ensure_payment_uuid_for_event(ev["id"])  # type: ignore
            payment_token = _create_payment_request(
                ev["id"],
                me["id"],
                int(fee),
                event_uuid=event_uuid,
                nickname=me.get("nickname"),
                x_id=me.get("x_id"),
                instagram_id=me.get("instagram_id"),
                lecture_auto_approve=lecture_auto_approve,
            )  # type: ignore
            if lecture_auto_approve:
                _clear_lecture_auto_approve_iv_session(event_uuid)
            session["pay_ctx"] = {
                "mfu_event_id": ev["id"],
                "mfu_event_uuid": event_uuid,
                "ext_user_id": me["id"],
                "nickname": me.get("nickname"),
                "x_id": me.get("x_id"),
                "instagram_id": me.get("instagram_id"),
                "email": me.get("email"),
                "expected_amount_yen": int(fee) if fee else None,
                "return_url": url_for(
                    "external_login_user.lecture_return" if lecture else "external_login_user.pay_return",
                    event_uuid=event_uuid,
                    payment_token=payment_token,
                    iv=iv or None,
                    _external=True,
                ),
                "payment_token": payment_token,
            }
            return_url = url_for(
                "external_login_user.lecture_return" if lecture else "external_login_user.pay_return",
                event_uuid=event_uuid,
                payment_token=payment_token,
                iv=iv or None,
                _external=True,
            )
            dest = (
                f"{PAYMENT_ENTRY_BASE()}{pay_ev_uuid}"
                f"?autofill=1&payment_token={payment_token}&return_url={quote(return_url, safe='')}"
            )
            return redirect(dest)

        if picked == "paypay":
            if not methods.get("paypay"):
                flash("このイベントではPayPay送金は選択できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            return redirect(url_for("external_login_user.pay_paypay", event_uuid=event_uuid))

        if picked == "bank":
            if not methods.get("bank"):
                flash("このイベントでは銀行振込は選択できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            return redirect(url_for("external_login_user.pay_bank", event_uuid=event_uuid))

        flash("支払方法を選択してください。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    if not force and sum(1 for v in methods.values() if v) >= 2:
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    if methods.get("paypay") and not (methods.get("card") or methods.get("bank")):
        return redirect(url_for("external_login_user.pay_paypay", event_uuid=event_uuid))
    if methods.get("bank") and not (methods.get("card") or methods.get("paypay")):
        return redirect(url_for("external_login_user.pay_bank", event_uuid=event_uuid))

    pay_ev_uuid = ev.get("payment_uuid") or _ensure_payment_uuid_for_event(ev["id"])  # type: ignore
    payment_token = _create_payment_request(
        ev["id"],
        me["id"],
        int(fee),
        event_uuid=event_uuid,
        nickname=me.get("nickname"),
        x_id=me.get("x_id"),
        instagram_id=me.get("instagram_id"),
        lecture_auto_approve=lecture_auto_approve,
    )  # type: ignore
    if lecture_auto_approve:
        _clear_lecture_auto_approve_iv_session(event_uuid)
    session["pay_ctx"] = {
        "mfu_event_id": ev["id"],
        "mfu_event_uuid": event_uuid,
        "ext_user_id": me["id"],
        "nickname": me.get("nickname"),
        "x_id": me.get("x_id"),
        "instagram_id": me.get("instagram_id"),
        "email": me.get("email"),
        "expected_amount_yen": int(fee) if fee else None,
        "return_url": url_for(
            "external_login_user.lecture_return" if lecture else "external_login_user.pay_return",
            event_uuid=event_uuid,
            payment_token=payment_token,
            iv=iv or None,
            _external=True,
        ),
        "payment_token": payment_token,
    }
    return_url = url_for(
        "external_login_user.lecture_return" if lecture else "external_login_user.pay_return",
        event_uuid=event_uuid,
        payment_token=payment_token,
        iv=iv or None,
        _external=True,
    )
    dest = (
        f"{PAYMENT_ENTRY_BASE()}{pay_ev_uuid}"
        f"?autofill=1&payment_token={payment_token}&return_url={quote(return_url, safe='')}"
    )
    return redirect(dest)

# ============================================================
# Square戻り
# ============================================================

# === 既存: pay_return の末尾ロジックを講座対応で補強 ===
@bp.route("/pay/return/<event_uuid>")
def pay_return(event_uuid: str):
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    lecture = _is_lecture_event(ev)

    # 承認済みのみ完了処理（← 非講座のみ厳格。講座は支払い先行OK）
    if not lecture:
        if _membership_status(ev["id"], me["id"]) != "approved":  # type: ignore
            flash("承認済み参加者のみ完了処理が可能です。", "warning")
            return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # --- Square 戻り値の受け口（既存のまま） ---
    q = request.args
    status = (q.get("status") or q.get("square_status") or "").strip().lower()
    receipt_url = (q.get("receipt") or q.get("receipt_url") or q.get("receiptUrl") or None)
    pr_id_raw = (q.get("payment_row_id") or q.get("paymentRowId") or q.get("row_id") or None)
    try:
        payment_row_id = int(pr_id_raw) if pr_id_raw is not None and str(pr_id_raw).strip() != "" else None
    except Exception:
        payment_row_id = None
    amt_raw = (q.get("amount_yen") or q.get("amount") or q.get("total_yen") or q.get("total") or None)
    token = (q.get("payment_token") or (session.get("pay_ctx") or {}).get("payment_token") or None)

    # ①URL → ②トークン → ③セッション → ④イベントfee_yen（既存のまま）
    paid_amount_yen = None
    if amt_raw is not None and str(amt_raw).strip() != "":
        try:
            paid_amount_yen = int(str(amt_raw).strip())
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            paid_amount_yen = _amount_from_payment_token(ev["id"], me["id"], token)  # type: ignore
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            sess_amt = (session.get("pay_ctx") or {}).get("expected_amount_yen")
            if sess_amt is not None and int(sess_amt) > 0:
                paid_amount_yen = int(sess_amt)
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            base_fee = ev.get("fee_yen")
            if base_fee is not None and int(base_fee) > 0:
                paid_amount_yen = int(base_fee)
        except Exception:
            paid_amount_yen = None

    is_success = status in ("", "ok", "success", "paid", "completed", "authorized", "approved")

    payment_request_id = _resolve_payment_request_id(ev["id"], me["id"], token, payment_row_id)  # type: ignore
    if token and payment_request_id is None:
        current_app.logger.warning(
            "payment request not found token=%s event_id=%s user_id=%s",
            _mask_payment_token(token),
            ev["id"],
            me["id"],
        )

    resolved_payment_row_id = payment_request_id if payment_request_id is not None else payment_row_id

    # 既存の paid 冪等チェック
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT COALESCE(payment_status,'unpaid') AS payment_status
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        row = cur.fetchone() or {}
        already = (row.get("payment_status") or "unpaid").strip().lower() == "paid"
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    if is_success and payment_request_id is not None:
        _mark_payment_request_used(payment_request_id, token)

    if is_success and not already:
        # 講座：メンバー行が無いケースに備えて upsert
        db2 = get_db(); cur2 = db2.cursor()
        try:
            if lecture:
                cur2.execute("""
                    INSERT INTO mfu_event_member (event_id, user_id, status, payment_status, require_payment, joined_at)
                    VALUES (%s,%s,'pending','paid',1,NOW())
                    ON DUPLICATE KEY UPDATE
                      payment_status='paid',
                      paid_at=COALESCE(paid_at, NOW()),
                      paid_amount_yen=%s,
                      receipt_url=COALESCE(%s, receipt_url),
                      payment_row_id=COALESCE(%s, payment_row_id)
                """, (ev["id"], me["id"], paid_amount_yen, receipt_url, resolved_payment_row_id))  # type: ignore
            else:
                cur2.execute("""
                    INSERT INTO mfu_event_member (event_id, user_id, status, payment_status, require_payment, joined_at)
                    VALUES (%s,%s,'pending','paid',1,NOW())
                    ON DUPLICATE KEY UPDATE
                      payment_status='paid',
                      paid_at=COALESCE(paid_at, NOW()),
                      paid_amount_yen=%s,
                      receipt_url=COALESCE(%s, receipt_url),
                      payment_row_id=COALESCE(%s, payment_row_id)
                """, (ev["id"], me["id"], paid_amount_yen, receipt_url, resolved_payment_row_id))  # type: ignore
            db2.commit()
        finally:
            try: cur2.close(); db2.close()
            except Exception: pass

        receipt_pdf_url = _build_member_receipt_pdf_url(event_uuid, ev["id"], me["id"])  # type: ignore
        receipt_label = receipt_pdf_url or "(領収書発行準備中)"

        # セッションクリア＆通知（既存ロジックそのまま）
        try:
            (session.get("pay_ctx") or {}).clear()
            session.pop("pay_ctx", None)
        except Exception:
            pass

        try:
            from datetime import datetime
            try:
                admin_link = url_for("external_login_user.admin_event_view", event_id=ev["id"], _external=True)  # type: ignore
            except Exception:
                admin_link = ""
            subject_admin = f"【{ev.get('title','イベント')}】クレジットカード決済が完了しました"
            lines = [
                f"イベント: {ev.get('title','(無題)')}",
                f"参加者: {me.get('nickname') or '(不明)'} (ID: {me.get('id')})",
                f"金額: {paid_amount_yen if paid_amount_yen is not None else '(未取得)'} 円",
                f"領収書PDF: {receipt_label}",
                f"管理画面: {admin_link}" if admin_link else "",
                f"日時: {datetime.now():%Y-%m-%d %H:%M:%S}",
            ]
            _notify_payment_to_admin_and_acl(
                ev,
                event_uuid_str=event_uuid,
                subject=subject_admin,
                mail_lines=[s for s in lines if s],
                discord_lines=None,
            )
        except Exception:
            current_app.logger.exception("notify (card) failed")

        # 参加者控えメール（既存のまま）
        try:
            user_email = (me.get("email") or "").strip()
            if user_email:
                from app.utils.mail import send_mail
                amount_line = f"{paid_amount_yen:,} 円" if isinstance(paid_amount_yen, int) else "— 円"
                subject_user = f"【{ev.get('title','イベント')}】お支払いありがとうございます！💕"
                body_user = (
                    f"{me.get('nickname') or '参加者'} 様\n\n"
                    "お忙しい中、お支払いいただきありがとうございます。\n"
                    "このメールを持って決済完了とさせていただきます。\n"
                    "領収書PDFは、下記のアドレスよりご確認よろしくお願いします。\n"
                    "※クレジットカード加盟店は「小松　伊織」と表示されますが、変更処理が間に合っておらず「チームイカ」と記載される場合があります。間違いではありませんが、ご面倒お掛けします。\n\n"
                    "当日、お会いできるのを楽しみにしております！\n\n"
                    f"イベント: {ev.get('title','(無題)')}\n"
                    f"金額: {amount_line}\n"
                    f"領収書PDF: {receipt_label}\n"
                    f"イベント詳細: https://mfu.iori0624.jp/external-login/events/view/{event_uuid}\n\n"
                    "--\n"
                    "小松　伊織\n"
                    "050-6874-1025\n"
                    "admin@mail.iori0624.jp\n"
                    "--\n"
                )
                send_mail(
                    to=user_email,
                    subject=subject_user,
                    body=body_user,
                    event_uuid=event_uuid,
                )
        except Exception:
            current_app.logger.exception("notify (user mail) failed")

        flash("お支払いが完了しました。ありがとうございます。", "success")

        # ★ 講座モード：支払い後は参加申請へ誘導
        if lecture:
            flash("続いて、参加申請（必要項目の入力）をお願いします。", "info")
            iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid)
            if iv:
                return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid, iv=iv))
            return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))

    elif is_success and already:
        flash("お支払いは反映済みです。", "info")
        if lecture:
            iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid)
            if iv:
                return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid, iv=iv))
            return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))
    else:
        flash("お支払い結果の反映を確認できませんでした。時間をおいて再読込してください。", "warning")

    return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

# ============================================================
# 支払い方法の分岐 / PayPay / 銀行振込
# ============================================================

# --- 支払方法の選択（単独時の自動遷移をPayPay表示に） ---
@bp.route("/pay/options/<event_uuid>")
def pay_options(event_uuid: str):
    guard = _require_ext_login()
    if guard:
        return guard

    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")
    ev = _event_attach_uuid_str(ev, event_uuid)

    methods = _enabled_methods(ev)
    enabled = [k for k, v in methods.items() if v]
    is_lecture = _is_lecture_event(ev)

    iv = (request.args.get("iv") or request.args.get("vi") or "").strip()
    if iv and is_lecture:
        store = session.get("lecture_invite_tokens") or {}
        store[event_uuid] = iv
        session["lecture_invite_tokens"] = store

    if is_lecture and enabled == ["card"]:
        return redirect(url_for("external_login_user.lecture_pay_start", event_uuid=event_uuid, iv=iv or None))

    # 1つだけなら自動遷移（PayPayはテンプレ表示へ。go=1 は使わない）
    if len(enabled) == 1:
        k = enabled[0]
        if k == "card":
            return redirect(url_for("external_login_user.pay_start", event_uuid=event_uuid))
        if k == "paypay":
            return redirect(url_for("external_login_user.pay_paypay", event_uuid=event_uuid))
        if k == "bank":
            return redirect(url_for("external_login_user.pay_bank", event_uuid=event_uuid))

    return render_template("pay_options.html", ev=ev, methods=methods, opts=methods)


@bp.route("/pay/paypay/<event_uuid>", methods=["GET", "POST"])
def pay_paypay(event_uuid: str):
    """
    PayPay友だち送金：
      - イベントに設定されたURLをテンプレートで表示（外部へ自動遷移しない）
      - 送金後は送金名を受け取り、mfu_event_member に記録＆ 'pending' 化
      - その時点の参加費を paid_amount_yen に保存
    """
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404)
    ev = _event_attach_uuid_str(ev, event_uuid)

    if not ev.get("allow_paypay"):
        flash("このイベントではPayPay送金は受付していません。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    def _resolve_p2p_url(ev: dict) -> str | None:
        import re, os
        for key in ("paypay_p2p_url", "paypay_qr_url"):
            v = ev.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        disp = ev.get("paypay_display")
        if isinstance(disp, str) and disp.strip():
            m = re.search(r'https?://[^\s<>"\']+', disp)
            if m:
                return m.group(0)
        e = (os.environ.get("PAYPAY_P2P_URL") or "").strip()
        return e or None

    p2p_url = _resolve_p2p_url(ev)
    if not p2p_url:
        flash("PayPay友だち送金リンクが未設定です。主催者にお問い合わせください。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    display_fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore

    # POST：送金申告 → mfu_event_member に記録（pending）
    if request.method == "POST":
        remitter = (request.form.get("remitter_name") or "").strip()
        if not remitter:
            flash("送金名を入力してください。", "warning")
            return render_template(
                "pay_paypay.html",
                ev=ev,
                paypay_url=p2p_url,
                paypay_display=(ev.get("paypay_display") or ""),
                fee_yen=display_fee,
            )

        # ★ その時点の参加費を保存
        fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore

        _ensure_payment_notice_table()
        db = get_db(); cur = db.cursor()
        try:
            # mfu_event_member に記録（paid_amount_yen も更新）
            cur.execute("""
              UPDATE mfu_event_member
                 SET payment_status='pending',
                     paid_amount_yen=%s,
                     paypay_transfer=1,
                     paypay_sent_date=CURDATE(),
                     paypay_sender_name=%s
               WHERE event_id=%s AND user_id=%s
               LIMIT 1
            """, (fee, remitter, ev["id"], me["id"]))  # type: ignore

            # ログ（任意）
            cur.execute("""
              INSERT INTO mfu_payment_notice (event_id, user_id, method, bank_id, note)
              VALUES (%s, %s, 'paypay', NULL, %s)
            """, (ev["id"], me["id"], f"ユーザーがPayPay送金を申告｜送金名:{remitter}｜金額:{fee}円"))  # type: ignore

            db.commit()
        finally:
            try: cur.close(); db.close()
            except Exception: pass

        # ===== 通知（admin/ACL：メール＋Discord） =====
        try:
            try:
                admin_link = url_for("external_login_user.admin_event_view", event_id=ev["id"], _external=True)  # type: ignore
            except Exception:
                admin_link = ""
            subject = f"【{ev.get('title','イベント')}】PayPay送金の申告がありました（pending）"
            lines = [
                f"イベント: {ev.get('title','(無題)')}",
                f"参加者: {me.get('nickname') or '(不明)'} (ID: {me.get('id')})",
                f"送金名: {remitter}",
                f"申告金額: {fee} 円",
                f"PayPayリンク: {p2p_url}",
                f"管理画面: {admin_link}" if admin_link else "",
                f"日時: {datetime.now():%Y-%m-%d %H:%M:%S}",
            ]
            _notify_payment_to_admin_and_acl(ev,
                event_uuid_str=event_uuid,
                subject=subject,
                mail_lines=[s for s in lines if s],
                discord_lines=None,
            )
        except Exception:
            current_app.logger.exception("notify (paypay) failed")

        flash("送金申告を受け付けました。主催の確認までお待ちください。", "success")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # GET：テンプレ表示
    return render_template(
        "pay_paypay.html",
        ev=ev,
        paypay_url=p2p_url,
        paypay_display=(ev.get("paypay_display") or ""),
        fee_yen=display_fee,
    )


@bp.route("/pay/bank/<event_uuid>", methods=["GET", "POST"])
def pay_bank(event_uuid: str):
    """
    銀行振込：
      - 選択式で振込先を選び、振込元名/着金日を申告
      - 申告時に mfu_event_member に詳細を記録して 'pending' 化
      - その時点の参加費を paid_amount_yen に保存
    """
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404)
    ev = _event_attach_uuid_str(ev, event_uuid)

    methods = _enabled_methods(ev)
    if not methods.get("bank"):
        flash("このイベントでは銀行振込は利用できません。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    # テーブル確保（口座）
    def _ensure_event_bank_table():
        db = get_db(); cur = db.cursor()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mfu_event_bank (
                  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  event_id       BIGINT UNSIGNED NOT NULL,
                  label          VARCHAR(120)  NOT NULL,
                  bank_name      VARCHAR(120)  NOT NULL,
                  branch_name    VARCHAR(120)  NOT NULL,
                  account_kind   VARCHAR(20)   NOT NULL,
                  account_number VARCHAR(32)   NOT NULL,
                  account_holder VARCHAR(120)  NOT NULL,
                  memo           VARCHAR(255)  NULL,
                  sort_order     INT           NOT NULL DEFAULT 0,
                  is_active      TINYINT(1)    NOT NULL DEFAULT 1,
                  created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (id),
                  KEY idx_event (event_id),
                  CONSTRAINT fk_event_bank_event
                    FOREIGN KEY (event_id) REFERENCES mfu_event(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """)
            db.commit()
        finally:
            try: cur.close(); db.close()
            except Exception: pass

    _ensure_event_bank_table()

    # 口座一覧取得
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, label, bank_name, branch_name, account_kind, account_number, account_holder, memo
              FROM mfu_event_bank
             WHERE event_id=%s AND is_active=1
             ORDER BY COALESCE(sort_order,0) ASC, id ASC
        """, (ev["id"],))  # type: ignore
        banks = cur.fetchall()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if not banks:
        flash("銀行振込口座が未設定です。主催者にお問い合わせください。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    display_fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore
    selected_bank_id = request.args.get("bank_id") or str(banks[0]["id"])

    # POST：申告 → mfu_event_member に記録
    if request.method == "POST":
        bank_id = (request.form.get("bank_id") or "").strip()
        remitter = (request.form.get("remitter_name") or "").strip()
        deposit_date = (request.form.get("deposit_date") or "").strip()  # YYYY-MM-DD

        if not bank_id:
            flash("振込先の銀行を選択してください。", "warning")
            return render_template(
                "pay_bank.html",
                ev=ev,
                banks=banks,
                selected_bank_id=selected_bank_id,
                fee_yen=display_fee,
            )
        sel = next((b for b in banks if str(b["id"]) == bank_id), None)
        if not sel:
            flash("選択した銀行が無効です。", "warning")
            return render_template(
                "pay_bank.html",
                ev=ev,
                banks=banks,
                selected_bank_id=selected_bank_id,
                fee_yen=display_fee,
            )
        if not remitter:
            flash("振込元名を入力してください。", "warning")
            return render_template(
                "pay_bank.html",
                ev=ev,
                banks=banks,
                selected_bank_id=bank_id,
                fee_yen=display_fee,
            )
        if not deposit_date:
            flash("振込先着金日を入力してください。", "warning")
            return render_template(
                "pay_bank.html",
                ev=ev,
                banks=banks,
                selected_bank_id=bank_id,
                fee_yen=display_fee,
            )

        dest_name = (sel.get("bank_name") or sel.get("label") or "").strip()
        # ★ その時点の参加費を保存
        fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore

        _ensure_payment_notice_table()
        db = get_db(); cur = db.cursor()
        try:
            # mfu_event_member に記録（paid_amount_yen も更新）
            cur.execute("""
              UPDATE mfu_event_member
                 SET payment_status='pending',
                     paid_amount_yen=%s,
                     bank_transfer=1,
                     bank_dest_name=%s,
                     bank_remitter_name=%s,
                     bank_deposit_date=%s
               WHERE event_id=%s AND user_id=%s
               LIMIT 1
            """, (fee, dest_name, remitter, deposit_date, ev["id"], me["id"]))  # type: ignore

            # ログ（任意）
            note = f"ユーザーが銀行振込を申告｜振込元名:{remitter}｜着金日:{deposit_date}｜先:{dest_name}｜金額:{fee}円"
            cur.execute("""
              INSERT INTO mfu_payment_notice (event_id, user_id, method, bank_id, note)
              VALUES (%s, %s, 'bank', %s, %s)
            """, (ev["id"], me["id"], bank_id, note))  # type: ignore

            db.commit()
        finally:
            try: cur.close(); db.close()
            except Exception: pass

        # ===== 通知（admin/ACL：メール＋Discord） =====
        try:
            try:
                admin_link = url_for("external_login_user.admin_event_view", event_id=ev["id"], _external=True)  # type: ignore
            except Exception:
                admin_link = ""
            subject = f"【{ev.get('title','イベント')}】銀行振込の申告がありました（pending）"
            lines = [
                f"イベント: {ev.get('title','(無題)')}",
                f"参加者: {me.get('nickname') or '(不明)'} (ID: {me.get('id')})",
                f"振込先: {dest_name}",
                f"振込元名: {remitter}",
                f"着金予定日: {deposit_date}",
                f"申告金額: {fee} 円",
                f"管理画面: {admin_link}" if admin_link else "",
                f"日時: {datetime.now():%Y-%m-%d %H:%M:%S}",
            ]
            _notify_payment_to_admin_and_acl(ev,
                event_uuid_str=event_uuid,
                subject=subject,
                mail_lines=[s for s in lines if s],
                discord_lines=None,
            )
        except Exception:
            current_app.logger.exception("notify (bank) failed")

        flash("振込申告を受け付けました。主催の確認までお待ちください。", "success")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # GET：テンプレ表示
    return render_template(
        "pay_bank.html",
        ev=ev,
        banks=banks,
        selected_bank_id=selected_bank_id,
        fee_yen=display_fee,
    )


# ============================================================
# 【講座専用】事前支払 → 参加申請 フロー（既存ルートは未改変）
# 使い方：
#   1) 案内リンク：/external-login/lecture/start/<event_uuid>
#   2) ログイン後：自動で /lecture/pay/<uuid> へ
#   3) Square完了戻り：/lecture/return/<uuid> → 参加申請ページへ
# ============================================================

def _is_lecture_event(ev: dict) -> bool:
    """イベント名に【講座】を含むか判定（全角のまま一致）"""
    title = (ev.get("title") or "").strip()
    return "【講座】" in title

@bp.route("/lecture/start/<event_uuid>")
def lecture_start(event_uuid: str):
    """
    講座の案内→LINEログインへ誘導（案内はflashで、テンプレ追加なし）
    """
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")
    if not _is_lecture_event(ev):
        flash("このページは講座専用です。通常の申請ページに移動しました。", "info")
        return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))

    # 案内（テンプレ増やさず flash で案内）
    flash("講座参加には事前支払が必要です。LINEログインして続行してください。", "info")

    iv = (request.args.get("iv") or request.args.get("vi") or "").strip()
    if iv:
        store = session.get("lecture_invite_tokens") or {}
        store[event_uuid] = iv
        session["lecture_invite_tokens"] = store

    # 未ログインなら、講座用支払ページを next にしてLINEログインへ
    if not session.get("ext_user_social_id"):
        next_url = url_for("external_login_user.lecture_pay_start", event_uuid=event_uuid, _external=False)
        session["ext_after_login_next"] = next_url
        return redirect(url_for("external_login_user.line_login", next=next_url))

    # 既にログイン済みならそのまま支払ステップへ
    return redirect(url_for("external_login_user.lecture_pay_start", event_uuid=event_uuid))


@bp.route("/lecture/pay/<event_uuid>", methods=["GET", "POST"])
def lecture_pay_start(event_uuid: str):
    """
    講座：支払ステップ（承認前でも支払を許可）。支払後に参加申請ページへ誘導。
    - 支払い期間ガードあり
    - 支払不要設定（require_payment=0）は尊重し、申請ページへ直行
    - 支払方法の分岐は既存の pay_* へ委譲
    """
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")
    if not _is_lecture_event(ev):
        flash("このページは講座専用です。通常の申請ページに移動しました。", "info")
        return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))

    iv = (request.args.get("iv") or request.args.get("vi") or "").strip()
    if not iv:
        iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid) or ""
    if iv:
        store = session.get("lecture_invite_tokens") or {}
        store[event_uuid] = iv
        session["lecture_invite_tokens"] = store
    auto_approve_hit = bool(
        int(ev.get("auto_approve_by_invite") or 0) == 1
        and ev.get("invite_token")
        and iv
        and iv == ev.get("invite_token")
    )
    if _lecture_auto_approve_from_iv_session(event_uuid):
        auto_approve_hit = True

    # 支払期間ガード（通常のカード決済と同じ判定）
    now = datetime.now()
    pf = _as_dt(ev.get("pay_from"))
    pu = _as_dt(ev.get("pay_until"))
    if (pf and now < pf) or (pu and now > pu):
        if pf and pu:
            flash(f"支払い可能期間は {pf:%Y-%m-%d %H:%M} 〜 {pu:%Y-%m-%d %H:%M} です。", "warning")
        elif pf:
            flash(f"支払いは {pf:%Y-%m-%d %H:%M} から可能です。", "warning")
        else:
            flash(f"支払いは {pu:%Y-%m-%d %H:%M} までに行ってください。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # 参加費チェック
    fee = _resolve_member_fee(ev["id"], me["id"], int(ev.get("fee_yen") or 0))  # type: ignore
    if fee <= 0:
        flash("この講座イベントに参加費が設定されていません。主催へお問い合わせください。", "warning")
        return redirect(url_for("external_login_user.view_event", event_uuid=event_uuid))

    # 参加レコードが無ければ仮エントリを作成しておく（承認前でもOK）
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT id, COALESCE(require_payment,1) AS rp
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        row = cur.fetchone()
        if not row:
            # status=pending / require_payment=1 で仮登録
            cur.execute("""
                INSERT INTO mfu_event_member (event_id, user_id, status, require_payment, joined_at)
                VALUES (%s, %s, 'pending', 1, NOW())
            """, (ev["id"], me["id"]))  # type: ignore
            db.commit()
            require_payment = 1
        else:
            require_payment = int(row[1] if isinstance(row, tuple) else row.get("rp") or 1)
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 支払不要は尊重 → 参加申請ページへ
    if require_payment == 0:
        flash("あなたは『支払い不要』に設定されています。参加申請ページへお進みください。", "info")
        return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))

    methods = _enabled_methods(ev)

    # POST: 選択分岐（既存の分岐に合わせる）
    if request.method == "POST":
        picked = (request.form.get("method") or "").strip()
        if picked == "card":
            if not methods.get("card"):
                flash("この講座ではクレジットカード決済は利用できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            # Square へ（戻りは講座専用の return へ）
            pay_ev_uuid = ev.get("payment_uuid") or _ensure_payment_uuid_for_event(ev["id"])  # type: ignore
            payment_token = _create_payment_request(
                ev["id"],
                me["id"],
                int(fee),
                event_uuid=event_uuid,
                nickname=me.get("nickname"),
                x_id=me.get("x_id"),
                instagram_id=me.get("instagram_id"),
                lecture_auto_approve=auto_approve_hit,
            )  # type: ignore
            if auto_approve_hit:
                _clear_lecture_auto_approve_iv_session(event_uuid)
            session["pay_ctx"] = {
                "mfu_event_id": ev["id"],
                "mfu_event_uuid": event_uuid,
                "ext_user_id": me["id"],
                "nickname": me.get("nickname"),
                "x_id": me.get("x_id"),
                "instagram_id": me.get("instagram_id"),
                "email": me.get("email"),
                "expected_amount_yen": fee,
                "return_url": url_for(
                    "external_login_user.lecture_return",
                    event_uuid=event_uuid,
                    payment_token=payment_token,
                    iv=iv or None,
                    _external=True,
                ),
                "payment_token": payment_token,
                "invite_token": iv or None,
            }
            return_url = url_for(
                "external_login_user.lecture_return",
                event_uuid=event_uuid,
                payment_token=payment_token,
                iv=iv or None,
                _external=True,
            )
            dest = (
                f"{PAYMENT_ENTRY_BASE()}{pay_ev_uuid}"
                f"?autofill=1&payment_token={payment_token}&return_url={quote(return_url, safe='')}"
            )
            return redirect(dest)

        if picked == "paypay":
            if not methods.get("paypay"):
                flash("この講座ではPayPay送金は選択できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            return redirect(url_for("external_login_user.pay_paypay", event_uuid=event_uuid))

        if picked == "bank":
            if not methods.get("bank"):
                flash("この講座では銀行振込は選択できません。", "warning")
                return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
            return redirect(url_for("external_login_user.pay_bank", event_uuid=event_uuid))

        flash("支払方法を選択してください。", "warning")
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))

    # GET: 単独手段なら自動遷移 / 複数なら既存の選択画面へ
    enabled = [k for k, v in methods.items() if v]
    if len(enabled) >= 2:
        # 既存の選択画面をそのまま利用（テンプレ改変なし）
        return redirect(url_for("external_login_user.pay_options", event_uuid=event_uuid))
    if methods.get("paypay") and not (methods.get("card") or methods.get("bank")):
        return redirect(url_for("external_login_user.pay_paypay", event_uuid=event_uuid))
    if methods.get("bank") and not (methods.get("card") or methods.get("paypay")):
        return redirect(url_for("external_login_user.pay_bank", event_uuid=event_uuid))

    # ここまで来たらカードのみ → Squareへ
    pay_ev_uuid = ev.get("payment_uuid") or _ensure_payment_uuid_for_event(ev["id"])  # type: ignore
    payment_token = _create_payment_request(
        ev["id"],
        me["id"],
        int(fee),
        event_uuid=event_uuid,
        nickname=me.get("nickname"),
        x_id=me.get("x_id"),
        instagram_id=me.get("instagram_id"),
        lecture_auto_approve=auto_approve_hit,
    )  # type: ignore
    if auto_approve_hit:
        _clear_lecture_auto_approve_iv_session(event_uuid)
    session["pay_ctx"] = {
        "mfu_event_id": ev["id"],
        "mfu_event_uuid": event_uuid,
        "ext_user_id": me["id"],
        "nickname": me.get("nickname"),
        "x_id": me.get("x_id"),
        "instagram_id": me.get("instagram_id"),
        "email": me.get("email"),
        "expected_amount_yen": fee,
        "return_url": url_for(
            "external_login_user.lecture_return",
            event_uuid=event_uuid,
            payment_token=payment_token,
            iv=iv or None,
            _external=True,
        ),
        "payment_token": payment_token,
        "invite_token": iv or None,
    }
    return_url = url_for(
        "external_login_user.lecture_return",
        event_uuid=event_uuid,
        payment_token=payment_token,
        iv=iv or None,
        _external=True,
    )
    dest = (
        f"{PAYMENT_ENTRY_BASE()}{pay_ev_uuid}"
        f"?autofill=1&payment_token={payment_token}&return_url={quote(return_url, safe='')}"
    )
    return redirect(dest)


@bp.route("/lecture/return/<event_uuid>")
def lecture_return(event_uuid: str):
    """
    講座：Square戻り（承認前でも支払反映）→ 参加申請ページへ。
    内容は既存 /pay/return を踏襲しつつ「承認済みガード」を外したもの。
    """
    guard = _require_ext_login()
    if guard:
        return guard

    me = _get_ext_user_by_social(session.get("ext_user_social_id"))  # type: ignore
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        abort(404, "イベントが見つかりません")

    # --- 戻り値の受け口（キー名ゆるく受理） ---
    q = request.args
    status = (q.get("status") or q.get("square_status") or "").strip().lower()
    receipt_url = (q.get("receipt") or q.get("receipt_url") or q.get("receiptUrl") or None)
    token = (q.get("payment_token") or (session.get("pay_ctx") or {}).get("payment_token") or None)
    iv = (q.get("iv") or q.get("vi") or (session.get("pay_ctx") or {}).get("invite_token") or "").strip()
    if not iv:
        iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid) or ""

    pr_id_raw = (q.get("payment_row_id") or q.get("paymentRowId") or q.get("row_id") or None)
    try:
        payment_row_id = int(pr_id_raw) if pr_id_raw is not None and str(pr_id_raw).strip() != "" else None
    except Exception:
        payment_row_id = None

    amt_raw = (q.get("amount_yen") or q.get("amount") or q.get("total_yen") or q.get("total") or None)

    # ①URL → ②トークン → ③セッション → ④イベントfee_yen の順に解決
    paid_amount_yen = None
    if amt_raw is not None and str(amt_raw).strip() != "":
        try:
            paid_amount_yen = int(str(amt_raw).strip())
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            paid_amount_yen = _amount_from_payment_token(ev["id"], me["id"], token)  # type: ignore
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            sess_amt = (session.get("pay_ctx") or {}).get("expected_amount_yen")
            if sess_amt is not None and int(sess_amt) > 0:
                paid_amount_yen = int(sess_amt)
        except Exception:
            paid_amount_yen = None
    if paid_amount_yen is None:
        try:
            base_fee = ev.get("fee_yen")
            if base_fee is not None and int(base_fee) > 0:
                paid_amount_yen = int(base_fee)
        except Exception:
            paid_amount_yen = None

    # 成功判定（空=OK を含む）
    is_success = status in ("", "ok", "success", "paid", "completed", "authorized", "approved")

    payment_request_id = _resolve_payment_request_id(ev["id"], me["id"], token, payment_row_id)  # type: ignore
    if token and payment_request_id is None:
        current_app.logger.warning(
            "payment request not found token=%s event_id=%s user_id=%s",
            _mask_payment_token(token),
            ev["id"],
            me["id"],
        )
    resolved_payment_row_id = payment_request_id if payment_request_id is not None else payment_row_id

    # すでに paid なら冪等（再反映しない）
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(payment_status,'unpaid') AS ps
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        row = cur.fetchone()
        already_paid = bool(row and ((row[0] if isinstance(row, tuple) else row.get("ps") or "unpaid") == "paid"))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    if is_success and payment_request_id is not None:
        _mark_payment_request_used(payment_request_id, token)

    if is_success and not already_paid:
        # 無ければ安全に作ってから反映（承認前でもOK）
        db2 = get_db(); cur2 = db2.cursor()
        try:
            cur2.execute("""
                INSERT IGNORE INTO mfu_event_member (event_id, user_id, status, require_payment, joined_at)
                VALUES (%s, %s, 'pending', 1, NOW())
            """, (ev["id"], me["id"]))  # type: ignore
            db2.commit()

            cur2.execute("""
                UPDATE mfu_event_member
                   SET payment_status='paid',
                       paid_at=COALESCE(paid_at, NOW()),
                       paid_amount_yen=%s,
                       receipt_url=COALESCE(%s, receipt_url),
                       payment_row_id=COALESCE(%s, payment_row_id)
                 WHERE event_id=%s AND user_id=%s
            """, (paid_amount_yen, receipt_url, resolved_payment_row_id, ev["id"], me["id"]))  # type: ignore
            db2.commit()
        finally:
            try: cur2.close(); db2.close()
            except Exception: pass

        receipt_pdf_url = _build_member_receipt_pdf_url(event_uuid, ev["id"], me["id"])  # type: ignore
        receipt_label = receipt_pdf_url or "(領収書発行準備中)"

        if iv:
            store = session.get("lecture_invite_tokens") or {}
            store[event_uuid] = iv
            session["lecture_invite_tokens"] = store

        # セッションのフォールバック情報はクリア
        try:
            (session.get("pay_ctx") or {}).clear()
            session.pop("pay_ctx", None)
        except Exception:
            pass

        # ===== 管理者/ACL 通知（既存ペイ系と同じ体裁） =====
        try:
            try:
                admin_link = url_for("external_login_user.admin_event_view", event_id=ev["id"], _external=True)  # type: ignore
            except Exception:
                admin_link = ""
            subject_admin = f"【{ev.get('title','イベント')}】クレジットカード決済が完了しました"
            lines = [
                f"イベント: {ev.get('title','(無題)')}",
                f"参加者: {me.get('nickname') or '(不明)'} (ID: {me.get('id')})",
                f"金額: {paid_amount_yen if paid_amount_yen is not None else '(未取得)'} 円",
                f"領収書PDF: {receipt_label}",
                f"管理画面: {admin_link}" if admin_link else "",
                f"日時: {datetime.now():%Y-%m-%d %H:%M:%S}",
            ]
            _notify_payment_to_admin_and_acl(
                ev,
                event_uuid_str=event_uuid,
                subject=subject_admin,
                mail_lines=[s for s in lines if s],
                discord_lines=None,
            )
        except Exception:
            current_app.logger.exception("notify (lecture card) failed")

        # ===== 参加者控えメール =====
        try:
            user_email = (me.get("email") or "").strip()
            if user_email:
                amount_line = f"{paid_amount_yen:,} 円" if isinstance(paid_amount_yen, int) else "— 円"
                subject_user = f"【{ev.get('title','イベント')}】お支払いありがとうございます！💕"
                body_user = (
                    f"{me.get('nickname') or '参加者'} 様\n\n"
                    "お忙しい中、お支払いいただきありがとうございます。\n"
                    "このメールを持って決済完了とさせていただきます。\n"
                    "領収書PDFは、下記のアドレスよりご確認よろしくお願いします。\n"
                    "※クレジットカード加盟店は「小松　伊織」と表示されますが、変更処理が間に合っておらず「チームイカ」と記載される場合があります。間違いではありませんが、ご面倒お掛けします。\n\n"
                    "当日、お会いできるのを楽しみにしております！\n\n"
                    f"イベント: {ev.get('title','(無題)')}\n"
                    f"金額: {amount_line}\n"
                    f"領収書PDF: {receipt_label}\n"
                    f"イベント詳細: https://mfu.iori0624.jp/external-login/events/view/{event_uuid}\n\n"
                    "--\n"
                    "小松　伊織\n"
                    "050-6874-1025\n"
                    "admin@mail.iori0624.jp\n"
                    "--\n"
                )
                send_mail(
                    to=user_email,
                    subject=subject_user,
                    body=body_user,
                    event_uuid=event_uuid,
                )
        except Exception:
            current_app.logger.exception("notify (lecture user mail) failed")

        flash("お支払いが完了しました。次に『参加申請ページ』で役割/衣装を入力してください。", "success")
    elif is_success and already_paid:
        flash("お支払いは反映済みです。参加申請ページへお進みください。", "info")
    else:
        flash("お支払い結果の反映を確認できませんでした。時間をおいて再読込してください。", "warning")

    # 支払後は必ず参加申請ページへ
    iv = (session.get("lecture_invite_tokens") or {}).get(event_uuid)
    if iv:
        return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid, iv=iv))
    return redirect(url_for("external_login_user.join_event", event_uuid=event_uuid))
