# -*- coding: utf-8 -*-
import hashlib
import os
import secrets
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import (
    request, render_template, redirect, url_for, flash, session, abort,
    send_file, current_app
)

from . import receipts_bp
from app.utils.db import get_db
from app.utils.mail import send_mail, send_mime

JST = timezone(timedelta(hours=9))
RECEIPTS_ROOT = os.environ.get("MFU_RECEIPTS_ROOT", "/mnt/mfu/app/receipts")
OTP_EXPIRES_MIN = 10
OTP_MAX_FAIL = 5
TOKEN_EXPIRES_HOURS = 48


def _fetchone_dict(cur):
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetchall_dict(cur):
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_with_salt(value: str, salt: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def _new_csrf_token() -> str:
    token = secrets.token_urlsafe(16)
    session["receipts_csrf"] = token
    return token


def _check_csrf(token: str | None) -> bool:
    saved = session.get("receipts_csrf")
    return bool(token) and bool(saved) and token == saved


def _new_sign_csrf() -> str:
    token = secrets.token_urlsafe(16)
    session["receipts_sign_csrf"] = token
    return token


def _check_sign_csrf(token: str | None) -> bool:
    saved = session.get("receipts_sign_csrf")
    return bool(token) and bool(saved) and token == saved


def _pad(n: int, width: int = 6) -> str:
    return str(n).zfill(width)


def _make_receipt_no(cur, issue_date: datetime) -> str:
    year = issue_date.year
    prefix = f"{year}-"
    cur.execute(
        "SELECT receipt_no FROM receipts WHERE receipt_no LIKE %s ORDER BY receipt_no DESC LIMIT 1",
        (f"{prefix}%",),
    )
    row = cur.fetchone()
    last_no = 0
    if row:
        receipt_no = row[0] if not isinstance(row, dict) else row.get("receipt_no")
        if receipt_no and "-" in receipt_no:
            try:
                last_no = int(receipt_no.split("-")[-1])
            except ValueError:
                last_no = 0
    return f"{year}-{_pad(last_no + 1)}"


def _rate_limit(cur, token_id: int | None, action: str, window_sec: int, max_count: int) -> bool:
    if token_id is None:
        return False
    since = _now_jst() - timedelta(seconds=window_sec)
    cur.execute(
        """
        SELECT COUNT(*) FROM audit_logs
        WHERE token_id = %s AND action = %s AND at >= %s
        """,
        (token_id, action, since),
    )
    count = cur.fetchone()[0]
    return count >= max_count


def _latest_audit_hash(cur) -> str | None:
    cur.execute("SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row.get("hash")


def _append_audit_log(
    cur,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    receipt_id: int | None = None,
    version_id: int | None = None,
    token_id: int | None = None,
    result: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    at = _now_jst()
    prev_hash = _latest_audit_hash(cur)
    payload = {
        "at": at.isoformat(),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "receipt_id": receipt_id,
        "version_id": version_id,
        "token_id": token_id,
        "result": result,
        "ip": ip,
        "user_agent": user_agent,
        "prev_hash": prev_hash,
    }
    hash_value = hashlib.sha256(
        (prev_hash or "")
        .encode("utf-8")
        + str(payload).encode("utf-8")
    ).hexdigest()
    cur.execute(
        """
        INSERT INTO audit_logs
        (at, actor_type, actor_id, action, receipt_id, version_id, token_id,
         result, ip, user_agent, prev_hash, hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            at,
            actor_type,
            actor_id,
            action,
            receipt_id,
            version_id,
            token_id,
            result,
            ip,
            user_agent,
            prev_hash,
            hash_value,
        ),
    )


def _render_pdf(html: str, out_path: str, base_url: str | None = None) -> None:
    try:
        from weasyprint import HTML
    except Exception as e:
        raise RuntimeError("WeasyPrint が利用できません") from e
    _ensure_dir(os.path.dirname(out_path))
    HTML(string=html, base_url=base_url).write_pdf(out_path)


def _get_user_email(cur, username: str | None) -> str | None:
    if not username:
        return None
    cur.execute("SELECT email FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row.get("email")


def _get_payer_profile(cur, issuer_user_id: str | None):
    if not issuer_user_id:
        return None
    cur.execute("SELECT * FROM payer_profiles WHERE issuer_user_id = %s", (issuer_user_id,))
    return _fetchone_dict(cur)


def _save_payer_profile(cur, issuer_user_id: str, payload: dict) -> None:
    cur.execute(
        """
        INSERT INTO payer_profiles
        (issuer_user_id, payer_name, payer_address, payer_phone, payer_email,
         payer_invoice_no, payer_bank_name, payer_bank_branch, payer_bank_account,
         payer_bank_holder, payer_note, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          payer_name = VALUES(payer_name),
          payer_address = VALUES(payer_address),
          payer_phone = VALUES(payer_phone),
          payer_email = VALUES(payer_email),
          payer_invoice_no = VALUES(payer_invoice_no),
          payer_bank_name = VALUES(payer_bank_name),
          payer_bank_branch = VALUES(payer_bank_branch),
          payer_bank_account = VALUES(payer_bank_account),
          payer_bank_holder = VALUES(payer_bank_holder),
          payer_note = VALUES(payer_note),
          updated_at = VALUES(updated_at)
        """,
        (
            issuer_user_id,
            payload["payer_name"],
            payload["payer_address"],
            payload["payer_phone"],
            payload.get("payer_email"),
            payload.get("payer_invoice_no"),
            payload.get("payer_bank_name"),
            payload.get("payer_bank_branch"),
            payload.get("payer_bank_account"),
            payload.get("payer_bank_holder"),
            payload.get("payer_note"),
            _now_jst(),
            _now_jst(),
        ),
    )


def _send_pdf_notice(to_email: str, subject: str, body: str, pdf_path: str) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr((Header("IORI0624_MFUシステム", "utf-8").encode(), "noreply@mail.iori0624.jp"))
    msg["To"] = to_email
    msg["Reply-To"] = "admin@mail.iori0624.jp"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-Id"] = make_msgid(domain="mail.iori0624.jp")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(pdf_path))
    msg.attach(part)

    send_mime(msg)


def _merge_pdf(original_path: str, audit_path: str, out_path: str) -> None:
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except Exception as e:
        raise RuntimeError("PyPDF2 が利用できません") from e
    writer = PdfWriter()
    for path in (original_path, audit_path):
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
    _ensure_dir(os.path.dirname(out_path))
    with open(out_path, "wb") as f:
        writer.write(f)


@receipts_bp.before_request
def _receipts_login_required():
    p = request.path or ""
    if p.startswith("/receipts/sign/"):
        return
    if "user" not in session:
        return redirect(url_for("login"))


@receipts_bp.route("/receipts")
def receipts_list():
    db = get_db()
    cur = db.cursor()
    params = []
    where = ["is_deleted = 0"]

    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()

    if q:
        where.append("(recipient_name LIKE %s OR recipient_email LIKE %s OR receipt_no LIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if status:
        where.append("status = %s")
        params.append(status)
    if start:
        where.append("issue_date >= %s")
        params.append(start)
    if end:
        where.append("issue_date <= %s")
        params.append(end)

    sql = "SELECT * FROM receipts WHERE " + " AND ".join(where) + " ORDER BY id DESC"
    cur.execute(sql, params)
    receipts = _fetchall_dict(cur)

    return render_template("list.html", receipts=receipts, q=q, status=status, start=start, end=end)


@receipts_bp.route("/receipts/new", methods=["GET", "POST"])
def receipts_new():
    if request.method == "GET":
        db = get_db()
        cur = db.cursor()
        profile = _get_payer_profile(cur, session.get("user"))
        db.close()
        return render_template("new.html", csrf_token=_new_csrf_token(), profile=profile or {})

    if not _check_csrf(request.form.get("csrf_token")):
        abort(400)

    form = request.form
    action = form.get("action", "create")
    payer_name = form.get("payer_name", "").strip()
    payer_address = form.get("payer_address", "").strip()
    payer_phone = form.get("payer_phone", "").strip()
    payer_email = form.get("payer_email", "").strip()
    payer_invoice_no = form.get("payer_invoice_no", "").strip()
    payer_bank_name = form.get("payer_bank_name", "").strip()
    payer_bank_branch = form.get("payer_bank_branch", "").strip()
    payer_bank_account = form.get("payer_bank_account", "").strip()
    payer_bank_holder = form.get("payer_bank_holder", "").strip()
    payer_note = form.get("payer_note", "").strip()
    profile_payload = {
        "payer_name": payer_name,
        "payer_address": payer_address,
        "payer_phone": payer_phone,
        "payer_email": payer_email or None,
        "payer_invoice_no": payer_invoice_no or None,
        "payer_bank_name": payer_bank_name or None,
        "payer_bank_branch": payer_bank_branch or None,
        "payer_bank_account": payer_bank_account or None,
        "payer_bank_holder": payer_bank_holder or None,
        "payer_note": payer_note or None,
    }

    if action == "save_profile":
        if not all([payer_name, payer_address, payer_phone]):
            flash("支払者プロフィールの必須項目（氏名/住所/電話）を入力してください。", "warning")
            return redirect(url_for("receipts.receipts_new"))
        db = get_db()
        cur = db.cursor()
        try:
            _save_payer_profile(cur, session.get("user"), profile_payload)
            db.commit()
        finally:
            db.close()
        flash("支払者プロフィールを保存しました。", "success")
        return redirect(url_for("receipts.receipts_new"))

    issue_date = datetime.strptime(form.get("issue_date"), "%Y-%m-%d")
    pay_date = datetime.strptime(form.get("pay_date"), "%Y-%m-%d")
    recipient_name = form.get("recipient_name", "").strip()
    recipient_email = form.get("recipient_email", "").strip()
    amount = int(form.get("amount"))
    description = form.get("description", "").strip()
    payment_method = form.get("payment_method", "").strip()
    update_profile = form.get("update_profile") == "on"

    db = get_db()
    cur = db.cursor()
    try:
        if update_profile:
            _save_payer_profile(cur, session.get("user"), profile_payload)

        receipt_no = _make_receipt_no(cur, issue_date)
        cur.execute(
            """
            INSERT INTO receipts
            (receipt_no, issuer_user_id, payer_name, payer_address, payer_phone, payer_email,
             payer_invoice_no, payer_bank_name, payer_bank_branch, payer_bank_account,
             payer_bank_holder, payer_note, recipient_name, recipient_email, issue_date,
             pay_date, amount, description, payment_method, status, created_at, updated_at, is_deleted)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, 0)
            """,
            (
                receipt_no,
                session.get("user"),
                payer_name,
                payer_address,
                payer_phone,
                payer_email,
                payer_invoice_no,
                payer_bank_name,
                payer_bank_branch,
                payer_bank_account,
                payer_bank_holder,
                payer_note,
                recipient_name,
                recipient_email,
                issue_date,
                pay_date,
                amount,
                description,
                payment_method,
                _now_jst(),
                _now_jst(),
            ),
        )
        receipt_id = cur.lastrowid

        version_no = 1
        cur.execute(
            """
            INSERT INTO receipt_versions
            (receipt_id, version_no, original_pdf_path, final_pdf_path,
             hash_original, hash_final, created_at)
            VALUES (%s, %s, '', '', '', '', %s)
            """,
            (receipt_id, version_no, _now_jst()),
        )
        version_id = cur.lastrowid
        cur.execute(
            "UPDATE receipts SET current_version_id = %s WHERE id = %s",
            (version_id, receipt_id),
        )

        receipt_data = {
            "receipt_no": receipt_no,
            "issue_date": issue_date,
            "pay_date": pay_date,
            "payer_name": payer_name,
            "payer_address": payer_address,
            "payer_phone": payer_phone,
            "payer_email": payer_email,
            "payer_invoice_no": payer_invoice_no,
            "payer_bank_name": payer_bank_name,
            "payer_bank_branch": payer_bank_branch,
            "payer_bank_account": payer_bank_account,
            "payer_bank_holder": payer_bank_holder,
            "payer_note": payer_note,
            "recipient_name": recipient_name,
            "recipient_email": recipient_email,
            "amount": amount,
            "description": description,
            "payment_method": payment_method,
        }
        base_dir = os.path.join(RECEIPTS_ROOT, receipt_no, f"v{version_no}")
        original_path = os.path.join(base_dir, "original.pdf")
        html = render_template("pdf_original.html", receipt=receipt_data)
        _render_pdf(html, original_path, base_url=request.url_root)
        hash_original = _sha256_file(original_path)

        cur.execute(
            """
            UPDATE receipt_versions
            SET original_pdf_path = %s, hash_original = %s
            WHERE id = %s
            """,
            (original_path, hash_original, version_id),
        )

        _append_audit_log(
            cur,
            actor_type="issuer",
            actor_id=session.get("user"),
            action="create_draft",
            receipt_id=receipt_id,
            version_id=version_id,
            result="ok",
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    flash("受領書ドラフトを作成しました。", "success")
    return redirect(url_for("receipts.receipts_detail", receipt_id=receipt_id))


@receipts_bp.route("/receipts/<int:receipt_id>")
def receipts_detail(receipt_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM receipts WHERE id = %s AND is_deleted = 0", (receipt_id,))
    receipt = _fetchone_dict(cur)
    if not receipt:
        abort(404)
    cur.execute("SELECT * FROM receipt_versions WHERE id = %s", (receipt["current_version_id"],))
    version = _fetchone_dict(cur)
    return render_template("detail.html", receipt=receipt, version=version, csrf_token=_new_csrf_token())


@receipts_bp.route("/receipts/<int:receipt_id>/payer", methods=["POST"])
def receipts_update_payer(receipt_id: int):
    if not _check_csrf(request.form.get("csrf_token")):
        abort(400)

    payer_name = request.form.get("payer_name", "").strip()
    payer_address = request.form.get("payer_address", "").strip()
    payer_phone = request.form.get("payer_phone", "").strip()
    payer_email = request.form.get("payer_email", "").strip()
    payer_invoice_no = request.form.get("payer_invoice_no", "").strip()
    payer_bank_name = request.form.get("payer_bank_name", "").strip()
    payer_bank_branch = request.form.get("payer_bank_branch", "").strip()
    payer_bank_account = request.form.get("payer_bank_account", "").strip()
    payer_bank_holder = request.form.get("payer_bank_holder", "").strip()
    payer_note = request.form.get("payer_note", "").strip()

    if not all([payer_name, payer_address, payer_phone]):
        flash("支払者情報の必須項目（氏名/住所/電話）を入力してください。", "warning")
        return redirect(url_for("receipts.receipts_detail", receipt_id=receipt_id))

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE receipts
            SET payer_name = %s,
                payer_address = %s,
                payer_phone = %s,
                payer_email = %s,
                payer_invoice_no = %s,
                payer_bank_name = %s,
                payer_bank_branch = %s,
                payer_bank_account = %s,
                payer_bank_holder = %s,
                payer_note = %s,
                updated_at = %s
            WHERE id = %s AND is_deleted = 0
            """,
            (
                payer_name,
                payer_address,
                payer_phone,
                payer_email,
                payer_invoice_no,
                payer_bank_name,
                payer_bank_branch,
                payer_bank_account,
                payer_bank_holder,
                payer_note,
                _now_jst(),
                receipt_id,
            ),
        )
        _append_audit_log(
            cur,
            actor_type="issuer",
            actor_id=session.get("user"),
            action="update_payer",
            receipt_id=receipt_id,
            result="ok",
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    flash("支払者情報を更新しました。", "success")
    return redirect(url_for("receipts.receipts_detail", receipt_id=receipt_id))


@receipts_bp.route("/receipts/<int:receipt_id>/send", methods=["POST"])
def receipts_send(receipt_id: int):
    if not _check_csrf(request.form.get("csrf_token")):
        abort(400)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM receipts WHERE id = %s AND is_deleted = 0", (receipt_id,))
        receipt = _fetchone_dict(cur)
        if not receipt:
            abort(404)
        cur.execute("SELECT * FROM receipt_versions WHERE id = %s", (receipt["current_version_id"],))
        version = _fetchone_dict(cur)

        token = secrets.token_urlsafe(32)
        token_hash = _sha256_text(token)
        expires_at = _now_jst() + timedelta(hours=TOKEN_EXPIRES_HOURS)

        cur.execute(
            """
            INSERT INTO receipt_tokens
            (receipt_version_id, token_hash, expires_at, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (version["id"], token_hash, expires_at, _now_jst()),
        )
        token_id = cur.lastrowid

        sign_url = url_for("receipts.receipts_sign", token=token, _external=True)
        subject = f"【受領書署名依頼】{receipt['receipt_no']}"
        body = (
            f"{receipt['recipient_name']} 様\n\n"
            f"以下の受領書への署名をお願いします。\n"
            f"署名リンク（48時間有効）: {sign_url}\n\n"
            f"受領書番号: {receipt['receipt_no']}\n"
            f"金額: {receipt['amount']} 円\n"
            f"摘要: {receipt['description']}\n"
        )

        try:
            send_mail(receipt["recipient_email"], subject, body)
            mail_result = "ok"
        except Exception as e:
            mail_result = f"ng:{e}"

        cur.execute(
            "UPDATE receipts SET status = 'sent', updated_at = %s WHERE id = %s",
            (_now_jst(), receipt_id),
        )

        _append_audit_log(
            cur,
            actor_type="issuer",
            actor_id=session.get("user"),
            action="send_link",
            receipt_id=receipt_id,
            version_id=version["id"],
            token_id=token_id,
            result=mail_result,
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    flash("署名リンクを送信しました。", "success")
    return redirect(url_for("receipts.receipts_detail", receipt_id=receipt_id))


@receipts_bp.route("/receipts/<int:receipt_id>/reissue", methods=["POST"])
def receipts_reissue(receipt_id: int):
    if not _check_csrf(request.form.get("csrf_token")):
        abort(400)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM receipts WHERE id = %s AND is_deleted = 0", (receipt_id,))
        receipt = _fetchone_dict(cur)
        if not receipt:
            abort(404)

        cur.execute("SELECT MAX(version_no) FROM receipt_versions WHERE receipt_id = %s", (receipt_id,))
        row = cur.fetchone()
        last_ver = row[0] if row else 0
        version_no = (last_ver or 0) + 1

        cur.execute(
            """
            INSERT INTO receipt_versions
            (receipt_id, version_no, original_pdf_path, final_pdf_path,
             hash_original, hash_final, created_at)
            VALUES (%s, %s, '', '', '', '', %s)
            """,
            (receipt_id, version_no, _now_jst()),
        )
        version_id = cur.lastrowid

        cur.execute(
            "UPDATE receipts SET current_version_id = %s, status = 'reissued', updated_at = %s WHERE id = %s",
            (version_id, _now_jst(), receipt_id),
        )

        receipt_data = {
            "receipt_no": receipt["receipt_no"],
            "issue_date": receipt["issue_date"],
            "pay_date": receipt["pay_date"],
            "payer_name": receipt["payer_name"],
            "payer_address": receipt["payer_address"],
            "payer_phone": receipt["payer_phone"],
            "payer_email": receipt["payer_email"],
            "payer_invoice_no": receipt.get("payer_invoice_no"),
            "payer_bank_name": receipt.get("payer_bank_name"),
            "payer_bank_branch": receipt.get("payer_bank_branch"),
            "payer_bank_account": receipt.get("payer_bank_account"),
            "payer_bank_holder": receipt.get("payer_bank_holder"),
            "payer_note": receipt.get("payer_note"),
            "recipient_name": receipt["recipient_name"],
            "recipient_email": receipt["recipient_email"],
            "amount": receipt["amount"],
            "description": receipt["description"],
            "payment_method": receipt["payment_method"],
        }
        base_dir = os.path.join(RECEIPTS_ROOT, receipt["receipt_no"], f"v{version_no}")
        original_path = os.path.join(base_dir, "original.pdf")
        html = render_template("pdf_original.html", receipt=receipt_data)
        _render_pdf(html, original_path, base_url=request.url_root)
        hash_original = _sha256_file(original_path)

        cur.execute(
            """
            UPDATE receipt_versions
            SET original_pdf_path = %s, hash_original = %s
            WHERE id = %s
            """,
            (original_path, hash_original, version_id),
        )

        _append_audit_log(
            cur,
            actor_type="issuer",
            actor_id=session.get("user"),
            action="reissue",
            receipt_id=receipt_id,
            version_id=version_id,
            result="ok",
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    flash("再発行版を作成しました。必要に応じて送信してください。", "success")
    return redirect(url_for("receipts.receipts_detail", receipt_id=receipt_id))


@receipts_bp.route("/receipts/<int:receipt_id>/download")
def receipts_download(receipt_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM receipts WHERE id = %s AND is_deleted = 0", (receipt_id,))
    receipt = _fetchone_dict(cur)
    if not receipt:
        abort(404)
    cur.execute("SELECT * FROM receipt_versions WHERE id = %s", (receipt["current_version_id"],))
    version = _fetchone_dict(cur)
    if not version or not version.get("final_pdf_path"):
        abort(404)
    path = version["final_pdf_path"]
    if not os.path.exists(path):
        abort(404)

    _append_audit_log(
        cur,
        actor_type="issuer",
        actor_id=session.get("user"),
        action="download_final",
        receipt_id=receipt_id,
        version_id=version["id"],
        result="ok",
        ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )
    db.commit()
    db.close()

    return send_file(path, as_attachment=True)


def _get_token_record(cur, token: str):
    token_hash = _sha256_text(token)
    cur.execute(
        """
        SELECT rt.*, rv.receipt_id
        FROM receipt_tokens rt
        JOIN receipt_versions rv ON rt.receipt_version_id = rv.id
        WHERE rt.token_hash = %s
        """,
        (token_hash,),
    )
    return _fetchone_dict(cur)


def _get_receipt_version(cur, token_row):
    cur.execute("SELECT * FROM receipt_versions WHERE id = %s", (token_row["receipt_version_id"],))
    version = _fetchone_dict(cur)
    cur.execute("SELECT * FROM receipts WHERE id = %s", (token_row["receipt_id"],))
    receipt = _fetchone_dict(cur)
    return receipt, version


@receipts_bp.route("/receipts/sign/<token>")
def receipts_sign(token: str):
    db = get_db()
    cur = db.cursor()
    token_row = _get_token_record(cur, token)
    if not token_row:
        abort(404)
    if token_row.get("used_at"):
        return render_template("sign_done.html", message="このリンクは使用済みです。")
    if token_row["expires_at"] < _now_jst():
        return render_template("sign_done.html", message="リンクの有効期限が切れています。")

    receipt, version = _get_receipt_version(cur, token_row)
    cur.execute(
        "SELECT * FROM otp_sessions WHERE token_id = %s ORDER BY id DESC LIMIT 1",
        (token_row["id"],),
    )
    otp_session = _fetchone_dict(cur)
    otp_verified = bool(otp_session and otp_session.get("verified_at"))

    return render_template(
        "sign.html",
        receipt=receipt,
        version=version,
        token=token,
        otp_verified=otp_verified,
        csrf_token=_new_sign_csrf(),
    )


@receipts_bp.route("/receipts/sign/<token>/otp/send", methods=["POST"])
def receipts_send_otp(token: str):
    if not _check_sign_csrf(request.form.get("csrf_token")):
        abort(400)

    db = get_db()
    cur = db.cursor()
    try:
        token_row = _get_token_record(cur, token)
        if not token_row:
            abort(404)
        if token_row.get("used_at"):
            abort(400)
        if token_row["expires_at"] < _now_jst():
            abort(400)

        if _rate_limit(cur, token_row["id"], "otp_send", 60, 3):
            flash("OTP送信が多すぎます。少し時間をおいてください。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        otp = f"{secrets.randbelow(10 ** 6):06d}"
        salt = secrets.token_hex(8)
        otp_hash = _hash_with_salt(otp, salt)
        expires_at = _now_jst() + timedelta(minutes=OTP_EXPIRES_MIN)

        cur.execute(
            """
            INSERT INTO otp_sessions
            (token_id, otp_hash, otp_salt, expires_at, failed_count, locked_until, created_at)
            VALUES (%s, %s, %s, %s, 0, NULL, %s)
            """,
            (token_row["id"], otp_hash, salt, expires_at, _now_jst()),
        )
        otp_id = cur.lastrowid

        receipt, _ = _get_receipt_version(cur, token_row)
        subject = f"【受領書OTP】{receipt['receipt_no']}"
        body = (
            f"{receipt['recipient_name']} 様\n\n"
            "受領書署名のためのOTPです。\n"
            f"OTP: {otp}\n"
            f"有効期限: {OTP_EXPIRES_MIN}分\n"
        )

        try:
            send_mail(receipt["recipient_email"], subject, body)
            result = "ok"
        except Exception as e:
            result = f"ng:{e}"

        _append_audit_log(
            cur,
            actor_type="recipient",
            actor_id=receipt["recipient_email"],
            action="otp_send",
            receipt_id=receipt["id"],
            version_id=token_row["receipt_version_id"],
            token_id=token_row["id"],
            result=result,
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    flash("OTPを送信しました。", "success")
    return redirect(url_for("receipts.receipts_sign", token=token))


@receipts_bp.route("/receipts/sign/<token>/otp/verify", methods=["POST"])
def receipts_verify_otp(token: str):
    if not _check_sign_csrf(request.form.get("csrf_token")):
        abort(400)

    otp_input = request.form.get("otp", "").strip()

    db = get_db()
    cur = db.cursor()
    try:
        token_row = _get_token_record(cur, token)
        if not token_row:
            abort(404)
        if token_row.get("used_at"):
            abort(400)
        if token_row["expires_at"] < _now_jst():
            abort(400)

        if _rate_limit(cur, token_row["id"], "otp_verify", 60, 5):
            flash("OTP検証が多すぎます。少し時間をおいてください。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        cur.execute(
            "SELECT * FROM otp_sessions WHERE token_id = %s ORDER BY id DESC LIMIT 1",
            (token_row["id"],),
        )
        otp_session = _fetchone_dict(cur)
        if not otp_session:
            flash("OTPが未発行です。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        if otp_session.get("locked_until") and otp_session["locked_until"] > _now_jst():
            flash("OTPがロックされています。", "danger")
            return redirect(url_for("receipts.receipts_sign", token=token))

        if otp_session["expires_at"] < _now_jst():
            flash("OTPの有効期限が切れています。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        ok = _hash_with_salt(otp_input, otp_session["otp_salt"]) == otp_session["otp_hash"]
        if not ok:
            failed = otp_session["failed_count"] + 1
            locked_until = None
            if failed >= OTP_MAX_FAIL:
                locked_until = _now_jst() + timedelta(minutes=OTP_EXPIRES_MIN)
            cur.execute(
                """
                UPDATE otp_sessions
                SET failed_count = %s, locked_until = %s
                WHERE id = %s
                """,
                (failed, locked_until, otp_session["id"]),
            )
            _append_audit_log(
                cur,
                actor_type="recipient",
                actor_id=None,
                action="otp_verify",
                receipt_id=token_row["receipt_id"],
                version_id=token_row["receipt_version_id"],
                token_id=token_row["id"],
                result="ng",
                ip=request.remote_addr,
                user_agent=request.headers.get("User-Agent"),
            )
            db.commit()
            flash("OTPが一致しません。", "danger")
            return redirect(url_for("receipts.receipts_sign", token=token))

        cur.execute(
            "UPDATE otp_sessions SET verified_at = %s WHERE id = %s",
            (_now_jst(), otp_session["id"]),
        )

        _append_audit_log(
            cur,
            actor_type="recipient",
            actor_id=None,
            action="otp_verify",
            receipt_id=token_row["receipt_id"],
            version_id=token_row["receipt_version_id"],
            token_id=token_row["id"],
            result="ok",
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    flash("OTPを確認しました。署名を入力してください。", "success")
    return redirect(url_for("receipts.receipts_sign", token=token))


@receipts_bp.route("/receipts/sign/<token>/submit", methods=["POST"])
def receipts_submit_sign(token: str):
    if not _check_sign_csrf(request.form.get("csrf_token")):
        abort(400)

    signer_name = request.form.get("signer_name", "").strip()
    signer_agree = request.form.get("agree") == "on"

    if not signer_agree:
        flash("同意にチェックしてください。", "warning")
        return redirect(url_for("receipts.receipts_sign", token=token))

    db = get_db()
    cur = db.cursor()
    try:
        token_row = _get_token_record(cur, token)
        if not token_row:
            abort(404)
        if token_row.get("used_at"):
            abort(400)
        if token_row["expires_at"] < _now_jst():
            abort(400)

        receipt, version = _get_receipt_version(cur, token_row)

        if _rate_limit(cur, token_row["id"], "sign_submit", 60, 5):
            flash("署名が多すぎます。少し時間をおいてください。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        cur.execute(
            """
            SELECT * FROM otp_sessions
            WHERE token_id = %s AND verified_at IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (token_row["id"],),
        )
        otp_session = _fetchone_dict(cur)
        if not otp_session:
            flash("OTP認証が必要です。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))
        if otp_session["expires_at"] < _now_jst():
            flash("OTPの有効期限が切れています。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        if not signer_name:
            flash("署名者氏名を入力してください。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        op_id = uuid4().hex
        signed_at = _now_jst()

        cur.execute(
            """
            INSERT INTO signatures
            (receipt_version_id, signed_at, signer_name_input, signer_email,
             signature_type, signature_image_path, ip, user_agent, op_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version["id"],
                signed_at,
                signer_name,
                receipt["recipient_email"],
                "typed",
                None,
                request.remote_addr,
                request.headers.get("User-Agent"),
                op_id,
            ),
        )
        signature_id = cur.lastrowid

        cur.execute(
            "UPDATE receipts SET status = 'signed', updated_at = %s WHERE id = %s",
            (_now_jst(), receipt["id"]),
        )

        base_dir = os.path.join(
            RECEIPTS_ROOT,
            receipt["receipt_no"],
            f"v{version['version_no']}",
        )
        audit_path = os.path.join(base_dir, "audit.pdf")
        final_path = os.path.join(base_dir, "final.pdf")
        temp_final_path = os.path.join(base_dir, "final_tmp.pdf")

        audit_page_html = render_template(
            "pdf_audit.html",
            receipt=receipt,
            version={**version, "hash_final": ""},
            signed_at=signed_at,
            signer_name=signer_name,
            signer_email=receipt["recipient_email"],
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
            op_id=op_id,
        )
        _render_pdf(audit_page_html, audit_path, base_url=request.url_root)
        _merge_pdf(version["original_pdf_path"], audit_path, temp_final_path)
        hash_final = _sha256_file(temp_final_path)

        audit_page_html = render_template(
            "pdf_audit.html",
            receipt=receipt,
            version={**version, "hash_final": hash_final},
            signed_at=signed_at,
            signer_name=signer_name,
            signer_email=receipt["recipient_email"],
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
            op_id=op_id,
        )
        _render_pdf(audit_page_html, audit_path, base_url=request.url_root)
        _merge_pdf(version["original_pdf_path"], audit_path, final_path)
        hash_final = _sha256_file(final_path)
        if os.path.exists(temp_final_path):
            os.remove(temp_final_path)

        cur.execute(
            """
            UPDATE receipt_versions
            SET final_pdf_path = %s, hash_final = %s
            WHERE id = %s
            """,
            (final_path, hash_final, version["id"]),
        )
        cur.execute(
            "UPDATE receipts SET status = 'finalized', updated_at = %s WHERE id = %s",
            (_now_jst(), receipt["id"]),
        )
        cur.execute(
            "UPDATE receipt_tokens SET used_at = %s WHERE id = %s",
            (_now_jst(), token_row["id"]),
        )

        _append_audit_log(
            cur,
            actor_type="recipient",
            actor_id=receipt["recipient_email"],
            action="sign_submit",
            receipt_id=receipt["id"],
            version_id=version["id"],
            token_id=token_row["id"],
            result=f"ok:{signature_id}",
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )

        issuer_email = _get_user_email(cur, receipt.get("issuer_user_id"))
        recipient_subject = f"【受領書確定】{receipt['receipt_no']}"
        recipient_body = (
            f"{receipt['recipient_name']} 様\n\n"
            "受領書の署名が完了しました。控えのPDFを添付します。\n\n"
            f"受領書番号: {receipt['receipt_no']}\n"
            f"金額: {receipt['amount']} 円\n"
            f"摘要: {receipt['description']}\n"
            f"確定PDF SHA-256: {hash_final}\n"
        )
        issuer_subject = f"【受領書署名完了】{receipt['receipt_no']}"
        issuer_body = (
            f"受領書の署名が完了しました。\n\n"
            f"受領書番号: {receipt['receipt_no']}\n"
            f"相手: {receipt['recipient_name']}（{receipt['recipient_email']}）\n"
            f"金額: {receipt['amount']} 円\n"
            f"摘要: {receipt['description']}\n"
            f"確定PDF SHA-256: {hash_final}\n"
        )

        recipient_result = "skip"
        issuer_result = "skip"
        try:
            if os.path.exists(final_path):
                _send_pdf_notice(receipt["recipient_email"], recipient_subject, recipient_body, final_path)
                recipient_result = "ok"
            else:
                recipient_result = "ng:missing_pdf"
        except Exception as e:
            recipient_result = f"ng:{e}"

        try:
            if issuer_email:
                send_mail(issuer_email, issuer_subject, issuer_body)
                issuer_result = "ok"
        except Exception as e:
            issuer_result = f"ng:{e}"

        _append_audit_log(
            cur,
            actor_type="system",
            actor_id=issuer_email,
            action="notify_issuer_final",
            receipt_id=receipt["id"],
            version_id=version["id"],
            token_id=token_row["id"],
            result=issuer_result,
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        _append_audit_log(
            cur,
            actor_type="system",
            actor_id=receipt["recipient_email"],
            action="notify_recipient_final",
            receipt_id=receipt["id"],
            version_id=version["id"],
            token_id=token_row["id"],
            result=recipient_result,
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        )
        db.commit()
    finally:
        db.close()

    return redirect(url_for("receipts.receipts_sign_done", token=token))


@receipts_bp.route("/receipts/sign/<token>/done")
def receipts_sign_done(token: str):
    return render_template("sign_done.html", message="署名が完了しました。ご協力ありがとうございます。")
