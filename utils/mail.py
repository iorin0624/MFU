import logging
import re
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, parseaddr
from pathlib import Path

from app.utils.logs import write_smtp_log
from app.utils.mail_delivery import generate_message_id, record_mail_submission


# 署名ファイルのパス（このファイルと同じディレクトリ）
SIGNATURE_FILE = Path(__file__).parent / "signature.txt"

# モジュール読み込み時に署名を1回だけ読み込む
try:
    DEFAULT_SIGNATURE = SIGNATURE_FILE.read_text(encoding="utf-8").rstrip("\n")
    if DEFAULT_SIGNATURE:
        DEFAULT_SIGNATURE = "\n\n" + DEFAULT_SIGNATURE.lstrip("\n")
except Exception as e:
    DEFAULT_SIGNATURE = ""
    logging.getLogger("mfu.smtp").warning(
        f"署名ファイルの読み込みに失敗しました: {SIGNATURE_FILE} - {e}"
    )


def send_mail(
    to,
    subject: str,
    body: str,
    *,
    event_uuid: str | None = None,    # 後方互換性のため残す
    event_name: str | None = None,
    smtp_host: str = "192.168.103.15",
    smtp_port: int = 25,
    timeout: int = 45,
    debug: bool = False,
    starttls: bool | None = None,
    ignore_quit_errors: bool = True,
    external_login_user_id: int | None = None,
    mail_kind: str | None = None,
) -> None:
    """
    メール送信ユーティリティ。

    - From: noreply@mail.iori0624.jp（表示名はイベント名 or システム名）
    - Reply-To: admin@mail.iori0624.jp（固定）
    - 本文末尾に signature.txt の内容を自動追加（存在する場合）
    """
    log = logging.getLogger("mfu.smtp")

    def _summary_log(ok: bool, to_text: str, subj: str, extra: str | None = None):
        status = "OK" if ok else "NG"
        msg = f'送信{status}: to="{to_text}" subj="{subj}"'
        if extra:
            msg += f" detail={extra}"

        log.info(msg)
        line = f"[SMTP] {msg}"
        print(line)
        try:
            write_smtp_log(line)
        except Exception:
            log.warning("write_smtp_log failed", exc_info=True)

    from_address = "noreply@mail.iori0624.jp"

    # 宛先処理
    if isinstance(to, (list, tuple, set)):
        to_list = [str(x).strip() for x in to if str(x).strip()]
        to_header = ", ".join(to_list)
    else:
        to_list = [str(to).strip()] if to else []
        to_header = to_list[0] if to_list else ""

    if not to_list:
        _summary_log(False, "(none)", subject or "", "no recipients")
        return

    # 表示名の決定
    is_event = event_uuid or event_name
    display_name = None

    if is_event:
        # 【【講座】テストイベント0002】のような複合形式に対応
        m = re.match(r"^(【[^】]*】[^】]*】)", subject or "")
        if m:
            display_name = Header(m.group(1), "utf-8").encode()
        else:
            m = re.match(r"^【([^】]+)】", subject or "")
            if m:
                display_name = Header(m.group(1), "utf-8").encode()
            else:
                display_name = Header(event_name or "イベント", "utf-8").encode()
    else:
        display_name = Header("IORI0624_MFUシステム", "utf-8").encode()

    # 本文に署名を追加
    final_body = body + DEFAULT_SIGNATURE

    # メッセージ作成
    msg = MIMEText(final_body, "plain", "utf-8")
    msg["Subject"] = subject or ""
    msg["From"] = formataddr((display_name, from_address))
    msg["To"] = to_header
    msg["Reply-To"] = "admin@mail.iori0624.jp"  # 固定
    msg["Date"] = formatdate(localtime=True)
    mfu_mail_uuid, message_id = generate_message_id()
    message_id_header = f"<{message_id}>"
    if msg.get("Message-ID"):
        msg.replace_header("Message-ID", message_id_header)
    else:
        msg["Message-ID"] = message_id_header
    if msg.get("X-MFU-Mail-ID"):
        msg.replace_header("X-MFU-Mail-ID", mfu_mail_uuid)
    else:
        msg["X-MFU-Mail-ID"] = mfu_mail_uuid

    # STARTTLS 自動判定
    if starttls is None:
        starttls = (smtp_port == 587)

    smtp = None
    sent_ok = False
    try:
        smtp = smtplib.SMTP(
            host=smtp_host,
            port=smtp_port,
            timeout=timeout,
            local_hostname="se02.local"
        )
        if debug:
            smtp.set_debuglevel(1)

        code, resp = smtp.ehlo()
        if debug:
            print("[SMTP] EHLO:", code, resp.decode() if isinstance(resp, bytes) else resp)

        if starttls:
            code, resp = smtp.starttls(context=ssl.create_default_context())
            if debug:
                print("[SMTP] STARTTLS:", code, resp.decode() if isinstance(resp, bytes) else resp)
            code, resp = smtp.ehlo()
            if debug:
                print("[SMTP] EHLO(after TLS):", code, resp.decode() if isinstance(resp, bytes) else resp)

        result = smtp.sendmail(from_address, to_list, msg.as_string())
        if result:
            detail = "; ".join(f"{rcpt}:{info}" for rcpt, info in result.items())
            _summary_log(False, to_header, subject or "", detail)
            raise smtplib.SMTPRecipientsRefused(result)

        sent_ok = True
        _summary_log(True, to_header, subject or "")
        try:
            record_mail_submission(
                mfu_mail_uuid=mfu_mail_uuid,
                message_id=message_id,
                to_addresses=to_header,
                subject=subject or "",
                submit_status="sent",
                last_delivery_status="queued",
                external_login_user_id=external_login_user_id,
                mail_kind=mail_kind,
            )
        except Exception:
            log.warning("mail submission log failed", exc_info=True)

        try:
            smtp.quit()
        except Exception as e_quit:
            if ignore_quit_errors and sent_ok:
                log.info(f"SMTP QUIT error ignored: {e_quit}")
            else:
                raise

    except smtplib.SMTPException as e:
        if sent_ok and ignore_quit_errors:
            log.info(f"SMTP post-send error ignored: {e}")
        else:
            _summary_log(False, to_header, subject or "", repr(e))
            try:
                record_mail_submission(
                    mfu_mail_uuid=mfu_mail_uuid,
                    message_id=message_id,
                    to_addresses=to_header,
                    subject=subject or "",
                    submit_status="failed",
                    last_delivery_status="failed",
                    last_delivery_detail=repr(e),
                    external_login_user_id=external_login_user_id,
                    mail_kind=mail_kind,
                )
            except Exception:
                log.warning("mail submission log failed", exc_info=True)
            raise
    except Exception as e:
        if sent_ok and ignore_quit_errors:
            log.info(f"SMTP post-send generic error ignored: {e}")
        else:
            _summary_log(False, to_header, subject or "", repr(e))
            try:
                record_mail_submission(
                    mfu_mail_uuid=mfu_mail_uuid,
                    message_id=message_id,
                    to_addresses=to_header,
                    subject=subject or "",
                    submit_status="failed",
                    last_delivery_status="failed",
                    last_delivery_detail=repr(e),
                    external_login_user_id=external_login_user_id,
                    mail_kind=mail_kind,
                )
            except Exception:
                log.warning("mail submission log failed", exc_info=True)
            raise
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass


def send_mime(
    msg,
    *,
    smtp_host: str = "192.168.103.15",
    smtp_port: int = 25,
    timeout: int = 45,
    debug: bool = False,
    starttls: bool | None = None,
    ignore_quit_errors: bool = True,
) -> None:
    """構築済みのMIMEメッセージをそのまま送信（署名は追加しない）"""
    log = logging.getLogger("mfu.smtp")

    def _summary_log(ok: bool, to_text: str, subj: str, extra: str | None = None):
        status = "OK" if ok else "NG"
        s = f'送信{status}: to="{to_text}" subj="{subj}"'
        if extra:
            s += f" detail={extra}"
        log.info(s)
        print(f"[SMTP] {s}")

    envelope_from = parseaddr(msg.get("From", ""))[1] or "noreply@mail.iori0624.jp"

    mfu_mail_uuid, message_id = generate_message_id()
    message_id_header = f"<{message_id}>"
    if msg.get("Message-ID"):
        msg.replace_header("Message-ID", message_id_header)
    else:
        msg["Message-ID"] = message_id_header
    if msg.get("X-MFU-Mail-ID"):
        msg.replace_header("X-MFU-Mail-ID", mfu_mail_uuid)
    else:
        msg["X-MFU-Mail-ID"] = mfu_mail_uuid

    to_rcpts = [
        addr.strip() for _, addr in getaddresses([msg.get("To")]) if addr and addr.strip()
    ] if msg.get("To") else []
    cc_rcpts = [
        addr.strip() for _, addr in getaddresses([msg.get("Cc")]) if addr and addr.strip()
    ] if msg.get("Cc") else []
    bcc_rcpts = [
        addr.strip() for _, addr in getaddresses([msg.get("Bcc")]) if addr and addr.strip()
    ] if msg.get("Bcc") else []
    rcpts: list[str] = [*to_rcpts, *cc_rcpts, *bcc_rcpts]

    if "Bcc" in msg:
        del msg["Bcc"]

    subj = msg.get("Subject", "")

    if not rcpts:
        _summary_log(False, "(none)", subj, "no recipients")
        return

    if starttls is None:
        starttls = (smtp_port == 587)

    smtp = None
    sent_ok = False
    try:
        smtp = smtplib.SMTP(host=smtp_host, port=smtp_port, timeout=timeout)
        if debug:
            smtp.set_debuglevel(1)

        code, resp = smtp.ehlo()
        if debug:
            print("[SMTP] EHLO:", code, resp.decode() if isinstance(resp, bytes) else resp)

        if starttls:
            code, resp = smtp.starttls(context=ssl.create_default_context())
            if debug:
                print("[SMTP] STARTTLS:", code, resp.decode() if isinstance(resp, bytes) else resp)
            code, resp = smtp.ehlo()
            if debug:
                print("[SMTP] EHLO(after TLS):", code, resp.decode() if isinstance(resp, bytes) else resp)

        result = smtp.sendmail(envelope_from, rcpts, msg.as_string())
        if result:
            detail = "; ".join(f"{rcpt}:{info}" for rcpt, info in result.items())
            _summary_log(False, ", ".join(rcpts), subj, detail)
            raise smtplib.SMTPRecipientsRefused(result)

        sent_ok = True
        _summary_log(True, ", ".join(rcpts), subj)
        try:
            record_mail_submission(
                mfu_mail_uuid=mfu_mail_uuid,
                message_id=message_id,
                to_addresses=to_rcpts,
                cc_addresses=cc_rcpts,
                bcc_addresses=bcc_rcpts,
                subject=subj or "",
                submit_status="queued",
                last_delivery_status="queued",
            )
        except Exception:
            log.warning("mail submission log failed", exc_info=True)

        try:
            smtp.quit()
        except Exception as e_quit:
            if ignore_quit_errors and sent_ok:
                log.info(f"SMTP QUIT error ignored: {e_quit}")
            else:
                raise

    except smtplib.SMTPException as e:
        if sent_ok and ignore_quit_errors:
            log.info(f"SMTP post-send error ignored: {e}")
        else:
            _summary_log(False, ", ".join(rcpts), subj, repr(e))
            try:
                record_mail_submission(
                    mfu_mail_uuid=mfu_mail_uuid,
                    message_id=message_id,
                    to_addresses=to_rcpts,
                    cc_addresses=cc_rcpts,
                    bcc_addresses=bcc_rcpts,
                    subject=subj or "",
                    submit_status="failed",
                    last_delivery_status="failed",
                    last_delivery_detail=repr(e),
                )
            except Exception:
                log.warning("mail submission log failed", exc_info=True)
            raise
    except Exception as e:
        if sent_ok and ignore_quit_errors:
            log.info(f"SMTP post-send generic error ignored: {e}")
        else:
            _summary_log(False, ", ".join(rcpts), subj, repr(e))
            try:
                record_mail_submission(
                    mfu_mail_uuid=mfu_mail_uuid,
                    message_id=message_id,
                    to_addresses=to_rcpts,
                    cc_addresses=cc_rcpts,
                    bcc_addresses=bcc_rcpts,
                    subject=subj or "",
                    submit_status="failed",
                    last_delivery_status="failed",
                    last_delivery_detail=repr(e),
                )
            except Exception:
                log.warning("mail submission log failed", exc_info=True)
            raise
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass
