from __future__ import annotations

import json
import os
import subprocess
import unicodedata
from typing import Any


MAIL_FILTER_HOST = os.getenv("MAIL_FILTER_HOST", "192.168.103.15").strip()
MAIL_FILTER_USER = os.getenv("MAIL_FILTER_USER", "mfu-mail-filter").strip()
MAIL_FILTER_KEY = os.getenv("MAIL_FILTER_SSH_KEY", "/mnt/mfu/ssh/mfu_mail_filter").strip()
MAIL_FILTER_KNOWN_HOSTS = os.getenv(
    "MAIL_FILTER_KNOWN_HOSTS",
    "/mnt/mfu/ssh/known_hosts",
).strip()
MAIL_FILTER_TIMEOUT = max(5, min(int(os.getenv("MAIL_FILTER_TIMEOUT", "30")), 120))


class MailFilterAgentError(RuntimeError):
    pass


def normalize_mailbox(value: Any) -> str:
    mailbox = unicodedata.normalize("NFC", str(value or "")).strip().lower()
    if (
        not mailbox
        or len(mailbox) > 255
        or mailbox.count("@") != 1
        or any(ord(char) < 32 for char in mailbox)
    ):
        raise MailFilterAgentError("メールボックス名が不正です")
    return mailbox


def _ssh_command() -> list[str]:
    return [
        "/usr/bin/ssh",
        "-T",
        "-i",
        MAIL_FILTER_KEY,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={MAIL_FILTER_KNOWN_HOSTS}",
        "-o",
        f"ConnectTimeout={min(MAIL_FILTER_TIMEOUT, 15)}",
        f"{MAIL_FILTER_USER}@{MAIL_FILTER_HOST}",
        "mfu-mail-filter-agent",
    ]


def call_agent(payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
    timeout_seconds = max(5, min(int(timeout or MAIL_FILTER_TIMEOUT), 120))
    try:
        completed = subprocess.run(
            _ssh_command(),
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MailFilterAgentError("メールサーバーとの通信がタイムアウトしました") from exc
    except OSError as exc:
        raise MailFilterAgentError("メールサーバー接続を開始できませんでした") from exc
    stdout = (completed.stdout or "").strip()
    if not stdout:
        detail = (completed.stderr or "").strip()[-500:]
        raise MailFilterAgentError(
            f"メールサーバーから応答がありません{': ' + detail if detail else ''}"
        )
    try:
        response = json.loads(stdout.splitlines()[-1])
    except Exception as exc:
        raise MailFilterAgentError("メールサーバーの応答形式が不正です") from exc
    if completed.returncode != 0 or not response.get("ok"):
        message = str(response.get("error") or "メールフィルター操作に失敗しました")
        raise MailFilterAgentError(message)
    return response


def list_mailboxes() -> list[dict[str, Any]]:
    return list(call_agent({"action": "list"}).get("mailboxes") or [])


def get_mailbox(mailbox: str) -> dict[str, Any]:
    return call_agent({"action": "get", "mailbox": normalize_mailbox(mailbox)})


def validate_script(mailbox: str, script: str) -> dict[str, Any]:
    return call_agent(
        {
            "action": "validate",
            "mailbox": normalize_mailbox(mailbox),
            "script": script,
        }
    )


def deploy_script(
    mailbox: str,
    script: str,
    *,
    expected_hash: str,
    actor: str,
) -> dict[str, Any]:
    return call_agent(
        {
            "action": "deploy",
            "mailbox": normalize_mailbox(mailbox),
            "script": script,
            "expected_hash": str(expected_hash or ""),
            "actor": str(actor or "")[:128],
        },
        timeout=60,
    )


def preview_rule(mailbox: str, rule: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    return call_agent(
        {
            "action": "manual_preview",
            "mailbox": normalize_mailbox(mailbox),
            "rule": rule,
            "scope": scope,
        },
        timeout=120,
    )


def execute_rule(
    mailbox: str,
    rule: dict[str, Any],
    scope: dict[str, Any],
    *,
    preview_token: str,
    execute_all: bool = False,
    allow_redirect: bool = False,
    allow_discard: bool = False,
) -> dict[str, Any]:
    return call_agent(
        {
            "action": "manual_execute",
            "mailbox": normalize_mailbox(mailbox),
            "rule": rule,
            "scope": scope,
            "preview_token": str(preview_token or ""),
            "execute_all": bool(execute_all),
            "allow_redirect": bool(allow_redirect),
            "allow_discard": bool(allow_discard),
        },
        timeout=300,
    )
