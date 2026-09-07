#!/usr/bin/env python3
"""Send one compact Discord card for every inbound MFU-managed call."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ENV_PATH = Path("/etc/asterisk/vm-watch-10610.env")
ACTION_ENV_PATH = Path("/etc/asterisk/mfu-blacklist-action.env")
PHONE_LIST_PATH = Path("/etc/asterisk/caller_whitelist.txt")
DONE_DIR = Path("/var/lib/asterisk/mfu_blocked_call_notify_done")
JST = ZoneInfo("Asia/Tokyo")
MARKER_RETENTION = timedelta(days=31)
TOKEN_LIFETIME_SECONDS = 2 * 60 * 60
DEFAULT_BLACKLIST_ACTION_URL = "https://mfu.iori0624.jp/phone-blacklist/register"
DEFAULT_WHITELIST_ACTION_URL = "https://mfu.iori0624.jp/phone-whitelist/register"
DEFAULT_CLICK_TO_CALL_ACTION_URL = "https://mfu.iori0624.jp/phone-click-to-call"
CLASSIFICATIONS = {
    "whitelist": {
        "title": "✅ ホワイトリストからの着信",
        "color": 0x2ECC71,
    },
    "unregistered": {
        "title": "📞 ホワイトリスト外からの着信",
        "color": 0xF39C12,
    },
    "blacklist": {
        "title": "🚫 ブラックリストからの着信",
        "color": 0xE74C3C,
    },
    "anonymous": {
        "title": "🔒 非通知着信",
        "color": 0x95A5A6,
    },
    "call_through": {
        "title": "🔁 コールスルー着信",
        "color": 0x3498DB,
    },
}
PROCESS_LABELS = {
    "ring_group_106": "Ring Groups 106へ転送",
    "voicemail_announce": "VoiceMail_Announceへ転送",
    "hangup_21": "Hangup(21)で即時拒否",
    "blacklist_ring_until": "発信者側へ呼出音のみ（相手が切るまで）",
    "blacklist_ring_15": "発信者側へ呼出音のみ（15秒後に終話）",
    "blacklist_busy": "話中として拒否",
    "call_through_pin": "コールスルーPIN認証へ接続",
}


def read_env_value(path: Path, key: str) -> str:
    """Read one value without executing the environment file as shell code."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


def normalize_caller(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("81") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issue_action_token(caller: str, secret_hex: str, now: int | None = None) -> str:
    if not re.fullmatch(r"0\d{9,10}", caller):
        raise ValueError("unsupported caller number")
    secret = bytes.fromhex(secret_hex.strip())
    if len(secret) < 32:
        raise ValueError("action secret is not configured")
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "p": caller,
        "iat": issued_at,
        "exp": issued_at + TOKEN_LIFETIME_SECONDS,
        "n": secrets.token_urlsafe(16),
    }
    body = _b64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _b64url_encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def build_registration_urls(
    caller: str,
    secret_hex: str,
    blacklist_base_url: str,
    whitelist_base_url: str,
    click_to_call_base_url: str = DEFAULT_CLICK_TO_CALL_ACTION_URL,
    now: int | None = None,
) -> tuple[str, str, str]:
    registration_token = issue_action_token(caller, secret_hex, now=now)
    click_to_call_token = issue_action_token(caller, secret_hex, now=now)
    return (
        f"{blacklist_base_url.strip().rstrip('#')}#{registration_token}",
        f"{whitelist_base_url.strip().rstrip('#')}#{registration_token}",
        f"{click_to_call_base_url.strip().rstrip('#')}#{click_to_call_token}",
    )


def load_phone_names(path: Path = PHONE_LIST_PATH) -> tuple[dict[str, str], dict[str, str]]:
    whitelist: dict[str, str] = {}
    blacklist: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return whitelist, blacklist
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# MFU_NOTIFY_NAME|"):
            parts = line.split("|", 3)
            if len(parts) != 4 or parts[1] not in {"W", "B"}:
                continue
            number, encoded_name = parts[2], parts[3]
            if not re.fullmatch(r"0\d{9,10}", number):
                continue
            try:
                name = base64.b64decode(encoded_name, validate=True).decode("utf-8") if encoded_name else ""
            except (ValueError, UnicodeDecodeError):
                name = ""
            (blacklist if parts[1] == "B" else whitelist)[number] = name
            continue
        if line.startswith("#"):
            continue
        parts = line.split("|")
        is_blacklist = len(parts) == 3 and parts[0] == "B"
        if is_blacklist:
            number, encoded_name = parts[1], parts[2]
        elif len(parts) == 2:
            number, encoded_name = parts
        else:
            continue
        if not re.fullmatch(r"0\d{9,10}", number):
            continue
        try:
            name = base64.b64decode(encoded_name, validate=True).decode("utf-8") if encoded_name else ""
        except (ValueError, UnicodeDecodeError):
            name = ""
        target = blacklist if is_blacklist else whitelist
        target.setdefault(number, name)
    return whitelist, blacklist


def resolve_name(
    classification: str,
    caller: str,
    whitelist: dict[str, str] | None = None,
    blacklist: dict[str, str] | None = None,
) -> str:
    if classification == "anonymous":
        return "―"
    if classification == "unregistered" or not caller:
        return "未登録"
    if whitelist is None or blacklist is None:
        whitelist, blacklist = load_phone_names()
    if classification == "blacklist":
        return blacklist.get(caller) or "名称未登録"
    return whitelist.get(caller) or "未登録"


def build_components(
    classification: str,
    caller: str,
    blacklist_url: str,
    whitelist_url: str,
    click_to_call_url: str,
) -> list[dict[str, object]]:
    if not caller:
        return []
    if classification not in {"whitelist", "unregistered"}:
        return []
    buttons: list[dict[str, object]] = []
    if click_to_call_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "📞 折り返し発信",
                "url": click_to_call_url,
            }
        )
    if classification == "unregistered" and whitelist_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "✅ ホワイトリストへ登録",
                "url": whitelist_url,
            }
        )
    if classification == "unregistered" and blacklist_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "🚫 ブラックリストへ登録",
                "url": blacklist_url,
            }
        )
    buttons.append(
        {
            "type": 2,
            "style": 5,
            "label": "📖 電話帳ナビ",
            "url": f"https://www.telnavi.jp/phone/{caller}",
        }
    )
    return [{"type": 1, "components": buttons}] if buttons else []


def build_discord_payload(
    caller: str,
    classification: str = "unregistered",
    caller_name: str = "",
    did: str = "",
    process: str = "voicemail_announce",
    blacklist_url: str = "",
    whitelist_url: str = "",
    click_to_call_url: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(JST)
    meta = CLASSIFICATIONS.get(classification, CLASSIFICATIONS["unregistered"])
    display_caller = caller or "非通知"
    display_name = caller_name or resolve_name(classification, caller)
    display_did = did or "不明"
    display_process = PROCESS_LABELS.get(process, process or "不明")
    payload: dict[str, object] = {
        "embeds": [
            {
                "title": meta["title"],
                "color": meta["color"],
                "fields": [
                    {
                        "name": "日時",
                        "value": now.strftime("%Y/%m/%d %H:%M:%S"),
                        "inline": False,
                    },
                    {"name": "相手", "value": display_caller, "inline": True},
                    {"name": "名称", "value": display_name, "inline": True},
                    {
                        "name": "着信先　｜　処理",
                        "value": f"{display_did}　｜　{display_process}",
                        "inline": False,
                    },
                ],
            }
        ]
    }
    components = build_components(
        classification, caller, blacklist_url, whitelist_url, click_to_call_url,
    )
    if components:
        payload["components"] = components
    return payload


def marker_path(unique_id: str) -> Path:
    digest = hashlib.sha256(unique_id.encode("utf-8", errors="replace")).hexdigest()
    return DONE_DIR / f"{digest}.done"


def claim(unique_id: str) -> Path | None:
    DONE_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    marker = marker_path(unique_id)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("pending\n")
    return marker


def prune_markers(now: datetime | None = None) -> None:
    now = now or datetime.now(JST)
    cutoff = now.timestamp() - MARKER_RETENTION.total_seconds()
    try:
        markers = DONE_DIR.glob("*.done")
        for marker in markers:
            try:
                if marker.stat().st_mtime < cutoff:
                    marker.unlink()
            except OSError:
                continue
    except OSError:
        return


def discord_post(webhook_url: str, payload: dict[str, object]) -> None:
    response = requests.post(
        webhook_url,
        params={"with_components": "true"},
        json=payload,
        timeout=8,
    )
    response.raise_for_status()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("classification", choices=sorted(CLASSIFICATIONS))
    parser.add_argument("caller", nargs="?", default="")
    parser.add_argument("unique_id", nargs="?", default="")
    parser.add_argument("did", nargs="?", default="")
    parser.add_argument("process", nargs="?", choices=sorted(PROCESS_LABELS), default="voicemail_announce")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    caller = normalize_caller(args.caller)
    did = normalize_caller(args.did)

    if args.dry_run:
        print(json.dumps(build_discord_payload(
            caller,
            classification=args.classification,
            did=did,
            process=args.process,
        ), ensure_ascii=False, indent=2))
        return 0

    unique_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", args.unique_id.strip())
    if not unique_id:
        print("Missing Asterisk unique ID", file=sys.stderr)
        return 2

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        webhook_url = read_env_value(ENV_PATH, "DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is empty", file=sys.stderr)
        return 2

    marker = claim(unique_id)
    if marker is None:
        return 0

    try:
        blacklist_url = ""
        whitelist_url = ""
        click_to_call_url = ""
        action_secret = read_env_value(ACTION_ENV_PATH, "PHONE_BLACKLIST_ACTION_SECRET")
        blacklist_base_url = (
            read_env_value(ACTION_ENV_PATH, "PHONE_BLACKLIST_ACTION_URL")
            or DEFAULT_BLACKLIST_ACTION_URL
        )
        whitelist_base_url = (
            read_env_value(ACTION_ENV_PATH, "PHONE_WHITELIST_ACTION_URL")
            or DEFAULT_WHITELIST_ACTION_URL
        )
        click_to_call_base_url = (
            read_env_value(ACTION_ENV_PATH, "PHONE_CLICK_TO_CALL_ACTION_URL")
            or DEFAULT_CLICK_TO_CALL_ACTION_URL
        )
        if caller and action_secret and args.classification in {"whitelist", "unregistered"}:
            try:
                blacklist_url, whitelist_url, click_to_call_url = build_registration_urls(
                    caller,
                    action_secret,
                    blacklist_base_url,
                    whitelist_base_url,
                    click_to_call_base_url,
                )
            except (ValueError, TypeError):
                print("Phone list registration link generation failed", file=sys.stderr)
        whitelist_names, blacklist_names = load_phone_names()
        caller_name = resolve_name(
            args.classification, caller, whitelist_names, blacklist_names,
        )
        payload = build_discord_payload(
            caller,
            classification=args.classification,
            caller_name=caller_name,
            did=did,
            process=args.process,
            blacklist_url=blacklist_url,
            whitelist_url=whitelist_url,
            click_to_call_url=click_to_call_url,
        )
        discord_post(webhook_url, payload)
        marker.write_text("sent\n", encoding="utf-8")
        prune_markers()
    except Exception as exc:
        try:
            marker.unlink()
        except OSError:
            pass
        print(f"Discord notification failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
