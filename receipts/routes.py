# -*- coding: utf-8 -*-
import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import (
    request, render_template, redirect, url_for, flash, session, abort,
    send_file, current_app
)

from . import receipts_bp
from app.utils.db import get_db
from app.utils.mail import send_mail

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
        return render_template("new.html", csrf_token=_new_csrf_token())

    if not _check_csrf(request.form.get("csrf_token")):
        abort(400)

    form = request.form
    issue_date = datetime.strptime(form.get("issue_date"), "%Y-%m-%d")
    pay_date = datetime.strptime(form.get("pay_date"), "%Y-%m-%d")
    recipient_name = form.get("recipient_name", "").strip()
    recipient_email = form.get("recipient_email", "").strip()
    amount = int(form.get("amount"))
    description = form.get("description", "").strip()
    payment_method = form.get("payment_method", "").strip()

    db = get_db()
    cur = db.cursor()
    try:
        receipt_no = _make_receipt_no(cur, issue_date)
        cur.execute(
            """
            INSERT INTO receipts
            (receipt_no, issuer_user_id, recipient_name, recipient_email, issue_date, pay_date,
             amount, description, payment_method, status, created_at, updated_at, is_deleted)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, 0)
            """,
            (
                receipt_no,
                session.get("user"),
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
    signature_data = request.form.get("signature_data", "").strip()

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

        signature_type = "typed"
        image_path = None
        signature_bytes = None
        if signature_data and signature_data.startswith("data:image/"):
            try:
                _, b64data = signature_data.split(",", 1)
                signature_bytes = base64.b64decode(b64data)
            except Exception:
                signature_bytes = None

        if signature_bytes and len(signature_bytes) <= 512:
            signature_bytes = None

        if not signer_name and not signature_bytes:
            flash("署名文字列または手書き署名のいずれかを入力してください。", "warning")
            return redirect(url_for("receipts.receipts_sign", token=token))

        if signature_bytes:
            signature_type = "drawn" if not signer_name else "both"
            if len(signature_bytes) > 1024 * 200:
                flash("署名画像が大きすぎます。", "warning")
                return redirect(url_for("receipts.receipts_sign", token=token))
            base_dir = os.path.join(
                RECEIPTS_ROOT,
                receipt["receipt_no"],
                f"v{version['version_no']}",
                "signatures",
            )
            _ensure_dir(base_dir)
            filename = f"sign_{uuid4().hex}.png"
            image_path = os.path.join(base_dir, filename)
            with open(image_path, "wb") as f:
                f.write(signature_bytes)

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
                signature_type,
                image_path,
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

        audit_page_html = render_template(
            "pdf_audit.html",
            receipt=receipt,
            version=version,
            signed_at=signed_at,
            signer_name=signer_name,
            signer_email=receipt["recipient_email"],
            ip=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
            op_id=op_id,
        )
        audit_path = os.path.join(
            RECEIPTS_ROOT,
            receipt["receipt_no"],
            f"v{version['version_no']}",
            "audit.pdf",
        )
        _render_pdf(audit_page_html, audit_path, base_url=request.url_root)

        final_path = os.path.join(
            RECEIPTS_ROOT,
            receipt["receipt_no"],
            f"v{version['version_no']}",
            "final.pdf",
        )
        _merge_pdf(version["original_pdf_path"], audit_path, final_path)
        hash_final = _sha256_file(final_path)

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
        db.commit()
    finally:
        db.close()

    return redirect(url_for("receipts.receipts_sign_done", token=token))


@receipts_bp.route("/receipts/sign/<token>/done")
def receipts_sign_done(token: str):
    return render_template("sign_done.html", message="署名が完了しました。ご協力ありがとうございます。")
