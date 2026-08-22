#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any
from datetime import date, datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses

import mysql.connector


MAIL_ROOT = Path(os.getenv("MFU_MAIL_FILTER_ROOT", "/mnt/mfu/maildata")).resolve()
BACKUP_ROOT = Path(
    os.getenv("MFU_MAIL_FILTER_BACKUP_ROOT", "/mnt/mfu/mail-filter-backups")
).resolve()
DOVECOT_SQL_CONFIG = Path(
    os.getenv("MFU_DOVECOT_SQL_CONFIG", "/etc/dovecot/dovecot-sql.conf.ext")
)
SIEVEC = os.getenv("MFU_SIEVEC", "/usr/bin/sievec")
DOVEADM = os.getenv("MFU_DOVEADM", "/usr/bin/doveadm")
SENDMAIL = os.getenv("MFU_SENDMAIL", "/usr/sbin/sendmail")
PREVIEW_KEY_PATH = Path(
    os.getenv("MFU_MAIL_FILTER_PREVIEW_KEY", "/etc/mfu-mail-filter-preview.key")
)
LOCK_PATH = Path(os.getenv("MFU_MAIL_FILTER_LOCK", "/run/lock/mfu-mail-filter.lock"))
VMAIL_UID = int(os.getenv("MFU_VMAIL_UID", "5000"))
VMAIL_GID = int(os.getenv("MFU_VMAIL_GID", "5000"))
MAX_SCRIPT_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 768 * 1024
MAX_VERSIONS_PER_MAILBOX = 30
MAX_MANUAL_BODY_SCAN = 10000
MAX_MANUAL_MATCHES = 999
MAX_FULL_EXECUTE_MATCHES = 10000
MAX_PREVIEW_ITEMS = 100
PREVIEW_TOKEN_TTL = 10 * 60
MAILBOX_RE = re.compile(r"^[^@\s/\\\x00-\x1f\x7f]+@[^@\s/\\\x00-\x1f\x7f]+$")
FORBIDDEN_SIEVE_RE = re.compile(
    r"(?i)\bvnd\.dovecot\.execute\b|(?:^|[;{}\s])execute\s*(?:[;:\"]|$)"
)


class AgentError(RuntimeError):
    pass


class ConflictError(AgentError):
    pass


def _json_response(payload: dict[str, Any], status: int = 0) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return status


def _normalize_mailbox(value: Any) -> str:
    mailbox = unicodedata.normalize("NFC", str(value or "")).strip().lower()
    if len(mailbox) > 255 or not MAILBOX_RE.fullmatch(mailbox):
        raise AgentError("メールボックス名が不正です")
    return mailbox


def _parse_connect_settings() -> dict[str, str]:
    text = DOVECOT_SQL_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*connect\s*=\s*(.+?)\s*$", text)
    if not match:
        raise AgentError("DovecotのメールボックスDB設定を読み取れません")
    return dict(re.findall(r"(host|dbname|user|password)=([^ ]+)", match.group(1)))


def _db_connection():
    settings = _parse_connect_settings()
    required = {"host", "dbname", "user", "password"}
    if not required.issubset(settings):
        raise AgentError("DovecotのメールボックスDB設定が不足しています")
    return mysql.connector.connect(
        host=settings["host"],
        database=settings["dbname"],
        user=settings["user"],
        password=settings["password"],
        connection_timeout=5,
    )


def _mailbox_rows() -> list[dict[str, Any]]:
    db = _db_connection()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT username, domain, local_part, COALESCE(active, 0) AS active
              FROM mailbox
             WHERE COALESCE(active, 0)=1
             ORDER BY username
            """
        )
        return cur.fetchall() or []
    finally:
        db.close()


def _mailbox_row(mailbox: str) -> dict[str, Any]:
    db = _db_connection()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT username, domain, local_part, COALESCE(active, 0) AS active
              FROM mailbox
             WHERE username=%s AND COALESCE(active, 0)=1
             LIMIT 1
            """,
            (mailbox,),
        )
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        raise AgentError("メールボックスが見つかりません")
    return row


def _mailbox_home(mailbox: str) -> Path:
    row = _mailbox_row(mailbox)
    local_part = str(row.get("local_part") or "")
    if not local_part or local_part in {".", ".."} or "/" in local_part or "\\" in local_part:
        raise AgentError("メールボックスの保存先が不正です")
    home = (MAIL_ROOT / local_part).resolve()
    if home.parent != MAIL_ROOT:
        raise AgentError("メールボックスの保存先が許可範囲外です")
    if not home.is_dir():
        raise AgentError("メールボックスの保存領域が見つかりません")
    return home


def _safe_active_source(home: Path) -> tuple[Path | None, str]:
    active = home / ".dovecot.sieve"
    if active.is_symlink():
        target_text = os.readlink(active)
        target = (home / target_text).resolve()
        try:
            target.relative_to(home)
        except ValueError as exc:
            raise AgentError("有効なSieveリンクが保存領域外を参照しています") from exc
        if target.is_file():
            return target, target_text
    if active.is_file():
        return active, ".dovecot.sieve"
    candidates = [home / "sieve" / "mfu.sieve", home / "sieve" / "roundcube.sieve"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate, str(candidate.relative_to(home))
    return None, ""


def _read_current_script(home: Path) -> tuple[str, str, str]:
    source, active_target = _safe_active_source(home)
    if not source:
        script = ""
    else:
        raw = source.read_bytes()
        if len(raw) > MAX_SCRIPT_BYTES:
            raise AgentError("既存Sieveスクリプトが大きすぎます")
        try:
            script = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentError("既存SieveスクリプトがUTF-8ではありません") from exc
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    return script, digest, active_target


def _folder_list(mailbox: str) -> list[str]:
    completed = subprocess.run(
        [DOVEADM, "mailbox", "list", "-u", mailbox],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentError("メールフォルダー一覧を取得できません")
    folders: list[str] = []
    seen: set[str] = set()
    for raw_name in completed.stdout.splitlines():
        name = unicodedata.normalize("NFC", raw_name.strip())
        if (
            not name
            or name in seen
            or name.casefold().startswith("dovecot.")
            or any(ord(char) < 32 for char in name)
        ):
            continue
        status = subprocess.run(
            [DOVEADM, "mailbox", "status", "-u", mailbox, "uidvalidity", name],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if status.returncode != 0:
            continue
        seen.add(name)
        folders.append(name)
    folders.sort(key=lambda value: (0 if value == "INBOX" else 1, value.casefold()))
    return folders


def _normalize_script(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentError("Sieveスクリプトが文字列ではありません")
    script = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if script and not script.endswith("\n"):
        script += "\n"
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise AgentError("Sieveスクリプトが大きすぎます")
    if "\x00" in script:
        raise AgentError("SieveスクリプトにNUL文字は使用できません")
    if FORBIDDEN_SIEVE_RE.search(script):
        raise AgentError("外部コマンド実行を含むSieveルールは利用できません")
    if not script.strip():
        return "# MFU: no active filters\n"
    return script


def _compile_script(script: str, directory: Path | None = None) -> str:
    directory_text = str(directory) if directory else None
    with tempfile.TemporaryDirectory(prefix="mfu-sieve-", dir=directory_text) as temp_dir:
        source = Path(temp_dir) / "candidate.sieve"
        binary = Path(temp_dir) / "candidate.svbin"
        source.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [SIEVEC, str(source), str(binary)],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
            raise AgentError(f"Sieve構文エラー: {detail or '詳細不明'}")
        return (completed.stderr or completed.stdout or "").strip()


def _preview_secret() -> bytes:
    try:
        raw = PREVIEW_KEY_PATH.read_bytes().strip()
    except FileNotFoundError:
        PREVIEW_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_bytes(32)
        fd = os.open(PREVIEW_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw + b"\n")
    if len(raw) < 32:
        raise AgentError("手動実行確認キーが不正です")
    return raw


def _decode_header_text(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


def _normalize_manual_rule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("raw"):
        raise AgentError("高度な生Sieveルールは手動実行できません")
    conditions = value.get("conditions") or []
    actions = value.get("actions") or []
    if not value.get("enabled", True):
        raise AgentError("無効なフィルターは手動実行できません")
    if not isinstance(conditions, list) or not conditions:
        raise AgentError("手動実行する条件がありません")
    if not isinstance(actions, list) or not actions:
        raise AgentError("手動実行する処理がありません")
    mode = str(value.get("mode") or "all").lower()
    if mode not in {"all", "any"}:
        raise AgentError("条件結合方法が不正です")
    allowed_conditions = {"true", "header", "address", "envelope", "body"}
    allowed_operators = {"contains", "is", "matches", "regex"}
    normalized_conditions = []
    for condition in conditions[:20]:
        condition_type = str(condition.get("type") or "").lower()
        operator = str(condition.get("operator") or "contains").lower()
        fields = [str(item).strip() for item in condition.get("fields") or [] if str(item).strip()]
        values = [str(item) for item in condition.get("values") or [] if str(item)]
        if condition_type not in allowed_conditions or operator not in allowed_operators:
            raise AgentError("未対応の手動実行条件です")
        if condition_type != "true" and not values:
            raise AgentError("検索値が未入力です")
        if condition_type not in {"true", "body"} and not fields:
            raise AgentError("条件の対象が未入力です")
        for pattern in values:
            if operator == "regex":
                try:
                    re.compile(pattern, re.I)
                except re.error as exc:
                    raise AgentError(f"正規表現が不正です: {exc}") from exc
        normalized_conditions.append(
            {"type": condition_type, "operator": operator, "fields": fields, "values": values}
        )
    normalized_actions = []
    for action in actions[:20]:
        action_type = str(action.get("type") or "").lower()
        if action_type not in {"fileinto", "redirect", "keep", "stop", "discard", "setflag", "addflag", "reject"}:
            raise AgentError("未対応の手動実行処理です")
        normalized_actions.append(
            {"type": action_type, "value": str(action.get("value") or "").strip(), "copy": bool(action.get("copy"))}
        )
    return {
        "name": str(value.get("name") or "名称なし")[:200],
        "enabled": True,
        "mode": mode,
        "conditions": normalized_conditions,
        "actions": normalized_actions,
    }


def _normalize_manual_scope(value: Any, folders: list[str]) -> dict[str, Any]:
    scope = value if isinstance(value, dict) else {}
    source = unicodedata.normalize("NFC", str(scope.get("source_folder") or "INBOX").strip())
    if source not in folders:
        raise AgentError("対象フォルダーが存在しません")
    today = date.today()
    try:
        date_from = date.fromisoformat(str(scope.get("date_from") or (today - timedelta(days=7)).isoformat()))
        date_to = date.fromisoformat(str(scope.get("date_to") or today.isoformat()))
    except ValueError as exc:
        raise AgentError("対象期間が不正です") from exc
    try:
        oldest_allowed = date_to.replace(year=date_to.year - 20)
    except ValueError:
        oldest_allowed = date_to.replace(year=date_to.year - 20, day=28)
    if date_from > date_to or date_from < oldest_allowed:
        raise AgentError("対象期間は今日までの20年以内で指定してください")
    max_matches = int(scope.get("max_matches") or 500)
    if not 1 <= max_matches <= MAX_MANUAL_MATCHES:
        raise AgentError(f"最大処理件数は1～{MAX_MANUAL_MATCHES}件で指定してください")
    return {
        "source_folder": source,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "unread_only": bool(scope.get("unread_only")),
        "max_matches": max_matches,
    }


def _doveadm_json(arguments: list[str], *, timeout: int = 60) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [DOVEADM, "-f", "json", *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
        raise AgentError(f"メール検索に失敗しました: {detail or '詳細不明'}")
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AgentError("メール検索結果を読み取れません") from exc
    return payload if isinstance(payload, list) else []


def _manual_search_query(scope: dict[str, Any]) -> list[str]:
    date_to_exclusive = date.fromisoformat(scope["date_to"]) + timedelta(days=1)
    query = [
        "mailbox", scope["source_folder"],
        "since", scope["date_from"],
        "before", date_to_exclusive.isoformat(),
    ]
    if scope["unread_only"]:
        query.append("unseen")
    return query


def _header_fetch_fields(rule: dict[str, Any]) -> list[str]:
    names = {"from", "to", "cc", "bcc", "sender", "reply-to", "subject", "list-id", "return-path", "delivered-to"}
    for condition in rule["conditions"]:
        if condition["type"] in {"header", "address", "envelope"}:
            for field in condition["fields"]:
                lowered = field.strip().lower()
                if re.fullmatch(r"[a-z0-9-]{1,100}", lowered):
                    names.add(lowered)
    return [f"hdr.{name}" for name in sorted(names)]


def _fetch_manual_messages(mailbox: str, rule: dict[str, Any], scope: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    fields = ["uid", "mailbox", "date.received", "flags", *_header_fetch_fields(rule)]
    needs_body = any(item["type"] == "body" for item in rule["conditions"])
    if needs_body:
        fields.append("text")
    rows = _doveadm_json(
        ["fetch", "-u", mailbox, " ".join(fields), *_manual_search_query(scope)],
        timeout=120,
    )
    rows.sort(key=lambda item: (str(item.get("date.received") or ""), int(item.get("uid") or 0)), reverse=True)
    if needs_body:
        truncated = len(rows) > MAX_MANUAL_BODY_SCAN
        return rows[:MAX_MANUAL_BODY_SCAN], truncated
    # Header/address filters only fetch small metadata fields. Dovecot has
    # already returned the complete date range here, so discarding rows at an
    # arbitrary scan limit would make a long-period preview incomplete.
    return rows, False


def _message_body(row: dict[str, Any]) -> str:
    raw = row.get("text")
    if raw is None:
        return ""
    try:
        message = BytesParser(policy=policy.default).parsebytes(str(raw).encode("utf-8", "replace"))
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "text" and part.get_content_disposition() != "attachment":
                try:
                    parts.append(str(part.get_content()))
                except Exception:
                    pass
        return "\n".join(parts)
    except Exception:
        return str(raw)


def _matches_value(actual: str, expected: str, operator: str) -> bool:
    actual_folded = actual.casefold()
    expected_folded = expected.casefold()
    if operator == "contains":
        return expected_folded in actual_folded
    if operator == "is":
        return actual_folded == expected_folded
    if operator == "matches":
        return fnmatch.fnmatchcase(actual_folded, expected_folded)
    if operator == "regex":
        return re.search(expected, actual, re.I) is not None
    return False


def _condition_matches(condition: dict[str, Any], row: dict[str, Any]) -> bool:
    condition_type = condition["type"]
    if condition_type == "true":
        return True
    actual_values: list[str] = []
    if condition_type == "body":
        actual_values = [_message_body(row)]
    else:
        for field in condition["fields"]:
            lowered = field.lower()
            if condition_type == "envelope":
                lowered = "return-path" if lowered == "from" else "delivered-to"
            header_value = _decode_header_text(row.get(f"hdr.{lowered}") or "")
            if condition_type == "address":
                actual_values.extend(address for _, address in getaddresses([header_value]) if address)
            else:
                actual_values.append(header_value)
    return any(
        _matches_value(actual, expected, condition["operator"])
        for actual in actual_values
        for expected in condition["values"]
    )


def _rule_matches(rule: dict[str, Any], row: dict[str, Any]) -> bool:
    results = [_condition_matches(condition, row) for condition in rule["conditions"]]
    return all(results) if rule["mode"] == "all" else any(results)


def _manual_action_labels(rule: dict[str, Any]) -> list[str]:
    labels = []
    for action in rule["actions"]:
        action_type = action["type"]
        if action_type == "fileinto":
            labels.append(("コピー: " if action["copy"] else "移動: ") + action["value"])
        elif action_type == "redirect":
            labels.append("転送: " + action["value"])
        elif action_type in {"setflag", "addflag"}:
            labels.append("フラグ: " + action["value"])
        elif action_type == "discard":
            labels.append("ごみ箱へ移動")
        elif action_type == "reject":
            labels.append("受信拒否（手動実行不可）")
    return labels or ["変更なし"]


def _manual_signature(mailbox: str, rule: dict[str, Any], scope: dict[str, Any], uids: list[int], issued_at: int) -> str:
    data = json.dumps(
        {"mailbox": mailbox, "rule": rule, "scope": scope, "uids": uids, "issued_at": issued_at},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(_preview_secret(), data, hashlib.sha256).hexdigest()
    return f"{issued_at}.{digest}"


def _manual_preview(mailbox: str, rule_value: Any, scope_value: Any) -> dict[str, Any]:
    folders = _folder_list(mailbox)
    rule = _normalize_manual_rule(rule_value)
    scope = _normalize_manual_scope(scope_value, folders)
    if any(action["type"] == "reject" for action in rule["actions"]):
        raise AgentError("受信拒否は受信済みメールへ手動実行できません")
    rows, scan_truncated = _fetch_manual_messages(mailbox, rule, scope)
    matched = [row for row in rows if _rule_matches(rule, row)]
    match_truncated = len(matched) > scope["max_matches"]
    selected = matched[: scope["max_matches"]]
    issued_at = int(time.time())
    uids = [int(row.get("uid") or 0) for row in selected]
    all_uids = [int(row.get("uid") or 0) for row in matched]
    token = _manual_signature(mailbox, rule, scope, uids, issued_at)
    full_execute_allowed = len(all_uids) <= MAX_FULL_EXECUTE_MATCHES
    full_token = (
        _manual_signature(mailbox, rule, scope, all_uids, issued_at)
        if full_execute_allowed
        else ""
    )
    items = []
    for row in selected[:MAX_PREVIEW_ITEMS]:
        items.append(
            {
                "uid": int(row.get("uid") or 0),
                "received_at": str(row.get("date.received") or ""),
                "from": _decode_header_text(row.get("hdr.from") or "")[:300],
                "subject": _decode_header_text(row.get("hdr.subject") or "（件名なし）")[:500],
                "flags": str(row.get("flags") or ""),
            }
        )
    return {
        "mailbox": mailbox,
        "rule": rule,
        "scope": scope,
        "scanned_count": len(rows),
        "matched_count": len(selected),
        "total_matched_count": len(matched),
        "scan_truncated": scan_truncated,
        "match_truncated": match_truncated,
        "items": items,
        "matched_uids": uids,
        "all_matched_uids": all_uids,
        "actions": _manual_action_labels(rule),
        "preview_token": token,
        "full_preview_token": full_token,
        "full_execute_allowed": full_execute_allowed,
        "full_execute_limit": MAX_FULL_EXECUTE_MATCHES,
    }


def _uid_batches(uids: list[int], size: int = 100):
    for offset in range(0, len(uids), size):
        yield ",".join(str(uid) for uid in uids[offset : offset + size])


def _run_doveadm(arguments: list[str], *, timeout: int = 120) -> None:
    completed = subprocess.run([DOVEADM, *arguments], text=True, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
        raise AgentError(f"メール処理に失敗しました: {detail or '詳細不明'}")


def _manual_execute(
    mailbox: str,
    rule_value: Any,
    scope_value: Any,
    token: str,
    *,
    execute_all: bool,
    allow_redirect: bool,
    allow_discard: bool,
) -> dict[str, Any]:
    preview = _manual_preview(mailbox, rule_value, scope_value)
    try:
        issued_at = int(str(token).split(".", 1)[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise AgentError("プレビュー確認情報が不正です") from exc
    if int(time.time()) - issued_at > PREVIEW_TOKEN_TTL:
        raise AgentError("プレビューの有効期限が切れました。もう一度確認してください")
    if execute_all and not preview["full_execute_allowed"]:
        raise AgentError(
            f"全件実行は{MAX_FULL_EXECUTE_MATCHES}件までです。先頭処理を使用してください"
        )
    target_uids = preview["all_matched_uids"] if execute_all else preview["matched_uids"]
    expected = _manual_signature(
        mailbox, preview["rule"], preview["scope"],
        [int(uid) for uid in target_uids], issued_at,
    )
    if not hmac.compare_digest(str(token), expected):
        raise ConflictError("対象メールまたはフィルターが変わりました。もう一度プレビューしてください")
    if preview["scan_truncated"]:
        raise AgentError("検索件数が安全上限を超えています。期間を狭めてください")
    rule = preview["rule"]
    actions = rule["actions"]
    if any(action["type"] == "redirect" for action in actions) and not allow_redirect:
        raise AgentError("転送を含むため、転送許可の確認が必要です")
    if any(action["type"] == "discard" for action in actions) and not allow_discard:
        raise AgentError("破棄を含むため、ごみ箱移動の確認が必要です")
    uids = [int(uid) for uid in target_uids]
    if not uids:
        return {
            **preview,
            "executed_count": 0,
            "execute_all": execute_all,
            "remaining_count": int(preview["total_matched_count"]),
        }
    source = preview["scope"]["source_folder"]
    folders = _folder_list(mailbox)
    keep_original = any(action["type"] == "keep" for action in actions)
    final_destination = None
    discard_requested = any(action["type"] == "discard" for action in actions)
    for action in actions:
        if action["type"] == "fileinto" and action["value"] not in folders:
            raise AgentError(f"振り分け先フォルダーが存在しません: {action['value']}")
        if action["type"] in {"setflag", "addflag"}:
            operation = "replace" if action["type"] == "setflag" else "add"
            for uid_set in _uid_batches(uids):
                _run_doveadm(["flags", operation, "-u", mailbox, action["value"], "mailbox", source, "uid", uid_set])
        if action["type"] == "fileinto":
            if action["copy"] or keep_original:
                for uid_set in _uid_batches(uids):
                    _run_doveadm(["copy", "-u", mailbox, action["value"], "mailbox", source, "uid", uid_set])
            elif final_destination is None:
                final_destination = action["value"]
            else:
                for uid_set in _uid_batches(uids):
                    _run_doveadm(["copy", "-u", mailbox, action["value"], "mailbox", source, "uid", uid_set])
        if action["type"] == "redirect":
            if not re.fullmatch(r"[^@\s]+@[^@\s]+", action["value"]):
                raise AgentError("転送先メールアドレスが不正です")
            for uid in uids:
                fetched = _doveadm_json(["fetch", "-u", mailbox, "text", "mailbox", source, "uid", str(uid)])
                if not fetched:
                    continue
                raw = str(fetched[0].get("text") or "").encode("utf-8", "replace")
                sent = subprocess.run([SENDMAIL, "--", action["value"]], input=raw, capture_output=True, timeout=30, check=False)
                if sent.returncode != 0:
                    raise AgentError("メール転送に失敗しました")
    if discard_requested:
        trash = next((name for name in ("Trash", "INBOX.Trash", "ごみ箱") if name in folders), None)
        if not trash:
            raise AgentError("ごみ箱フォルダーが見つかりません")
        final_destination = trash
    if final_destination and not keep_original:
        for uid_set in _uid_batches(uids):
            _run_doveadm(["move", "-u", mailbox, final_destination, "mailbox", source, "uid", uid_set])
    return {
        **preview,
        "preview_token": "",
        "full_preview_token": "",
        "executed_count": len(uids),
        "execute_all": execute_all,
        "remaining_count": max(int(preview["total_matched_count"]) - len(uids), 0),
    }


def _public_manual_result(result: dict[str, Any]) -> dict[str, Any]:
    """Remove internal matching material from an agent response."""
    public = dict(result)
    public.pop("matched_uids", None)
    public.pop("all_matched_uids", None)
    public.pop("rule", None)
    return public


def _backup_current(home: Path, mailbox: str, script: str, digest: str, active_target: str) -> str:
    safe_mailbox = re.sub(r"[^A-Za-z0-9_.@-]", "_", mailbox)
    backup_dir = BACKUP_ROOT / safe_mailbox
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"{stamp}_{digest[:12] or 'empty'}.sieve"
    backup_path = backup_dir / backup_name
    backup_path.write_text(script, encoding="utf-8")
    os.chmod(backup_path, 0o600)
    metadata_path = backup_path.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(
            {
                "mailbox": mailbox,
                "script_hash": digest,
                "active_target": active_target,
                "created_at": stamp,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(metadata_path, 0o600)
    backups = sorted(backup_dir.glob("*.sieve"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[MAX_VERSIONS_PER_MAILBOX:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return str(backup_path.relative_to(BACKUP_ROOT))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _deploy(mailbox: str, script: str, expected_hash: str) -> dict[str, Any]:
    normalized = _normalize_script(script)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    home = _mailbox_home(mailbox)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current_script, current_hash, active_target = _read_current_script(home)
        if expected_hash != current_hash:
            raise ConflictError(
                "メールサーバー側のフィルターが更新されています。再取得してください。"
            )
        _compile_script(normalized)
        backup = _backup_current(home, mailbox, current_script, current_hash, active_target)
        sieve_dir = home / "sieve"
        sieve_dir.mkdir(mode=0o700, exist_ok=True)
        os.chown(sieve_dir, VMAIL_UID, VMAIL_GID)
        os.chmod(sieve_dir, 0o700)
        version_name = f"mfu-{int(time.time())}-{digest[:12]}.sieve"
        temp_source = sieve_dir / f".{version_name}.tmp"
        final_source = sieve_dir / version_name
        with temp_source.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp_source, VMAIL_UID, VMAIL_GID)
        os.chmod(temp_source, 0o600)
        os.replace(temp_source, final_source)
        compiled_temp = home / ".dovecot.svbin.mfu-tmp"
        compiled_temp.unlink(missing_ok=True)
        completed = subprocess.run(
            [SIEVEC, str(final_source), str(compiled_temp)],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            final_source.unlink(missing_ok=True)
            raise AgentError(
                "Sieveの反映前コンパイルに失敗しました: "
                + (completed.stderr or completed.stdout or "詳細不明").strip()[-1000:]
            )
        os.chown(compiled_temp, VMAIL_UID, VMAIL_GID)
        os.chmod(compiled_temp, 0o600)
        relative_target = f"sieve/{version_name}"
        temp_link = home / ".dovecot.sieve.mfu-tmp"
        temp_link.unlink(missing_ok=True)
        os.symlink(relative_target, temp_link)
        os.lchown(temp_link, VMAIL_UID, VMAIL_GID)
        os.replace(temp_link, home / ".dovecot.sieve")
        os.replace(compiled_temp, home / ".dovecot.svbin")
        os.chown(home / ".dovecot.svbin", VMAIL_UID, VMAIL_GID)
        os.chmod(home / ".dovecot.svbin", 0o600)
        _fsync_directory(sieve_dir)
        _fsync_directory(home)
        version_files = sorted(
            sieve_dir.glob("mfu-*.sieve"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for old in version_files[MAX_VERSIONS_PER_MAILBOX:]:
            if old != final_source:
                old.unlink(missing_ok=True)
        return {
            "script_hash": digest,
            "backup": backup,
            "active_target": relative_target,
        }


def _handle(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    if action == "list":
        output: list[dict[str, Any]] = []
        for row in _mailbox_rows():
            mailbox = _normalize_mailbox(row["username"])
            try:
                home = _mailbox_home(mailbox)
                script, digest, active_target = _read_current_script(home)
                output.append(
                    {
                        "mailbox": mailbox,
                        "has_filter": bool(script.strip()),
                        "script_hash": digest,
                        "active_target": active_target,
                        "storage_exists": True,
                    }
                )
            except AgentError:
                output.append(
                    {
                        "mailbox": mailbox,
                        "has_filter": False,
                        "script_hash": "",
                        "active_target": "",
                        "storage_exists": False,
                    }
                )
        return {"ok": True, "mailboxes": output}
    mailbox = _normalize_mailbox(payload.get("mailbox"))
    if action == "get":
        home = _mailbox_home(mailbox)
        script, digest, active_target = _read_current_script(home)
        return {
            "ok": True,
            "mailbox": mailbox,
            "script": script,
            "script_hash": digest,
            "active_target": active_target,
            "folders": _folder_list(mailbox),
        }
    if action == "validate":
        script = _normalize_script(payload.get("script"))
        _mailbox_home(mailbox)
        message = _compile_script(script)
        return {
            "ok": True,
            "mailbox": mailbox,
            "script_hash": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "message": message or "Sieve構文は正常です",
        }
    if action == "deploy":
        expected_hash = str(payload.get("expected_hash") or "")
        result = _deploy(mailbox, str(payload.get("script") or ""), expected_hash)
        return {"ok": True, "mailbox": mailbox, **result}
    if action == "manual_preview":
        return _public_manual_result({
            "ok": True,
            **_manual_preview(mailbox, payload.get("rule"), payload.get("scope")),
        })
    if action == "manual_execute":
        return _public_manual_result({
            "ok": True,
            **_manual_execute(
                mailbox,
                payload.get("rule"),
                payload.get("scope"),
                str(payload.get("preview_token") or ""),
                execute_all=bool(payload.get("execute_all")),
                allow_redirect=bool(payload.get("allow_redirect")),
                allow_discard=bool(payload.get("allow_discard")),
            ),
        })
    raise AgentError("未対応の操作です")


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise AgentError("リクエストが大きすぎます")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise AgentError("リクエスト形式が不正です")
        return _json_response(_handle(payload), 0)
    except ConflictError as exc:
        return _json_response({"ok": False, "error": str(exc), "code": "conflict"}, 2)
    except (AgentError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _json_response({"ok": False, "error": str(exc)}, 1)
    except Exception:
        return _json_response({"ok": False, "error": "メールフィルター処理で内部エラーが発生しました"}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
