#!/usr/bin/env python3
"""Send a Discord notification for a call blocked by the MFU whitelist."""

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
DONE_DIR = Path("/var/lib/asterisk/mfu_blocked_call_notify_done")
JST = ZoneInfo("Asia/Tokyo")
MARKER_RETENTION = timedelta(days=31)
TOKEN_LIFETIME_SECONDS = 2 * 60 * 60
DEFAULT_BLACKLIST_ACTION_URL = "https://mfu.iori0624.jp/phone-blacklist/register"
DEFAULT_WHITELIST_ACTION_URL = "https://mfu.iori0624.jp/phone-whitelist/register"
DEFAULT_CLICK_TO_CALL_ACTION_URL = "https://mfu.iori0624.jp/phone-click-to-call"


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


def build_content(caller: str, now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    lines = [
        "**📞 ホワイトリスト外からの着信**",
        "",
        f"日時 : {now.strftime('%Y/%m/%d %H:%M:%S')}",
    ]
    if caller:
        lines.append(f"相手 : {caller}（tel:{caller}）")
    else:
        lines.append("相手 : 非通知")
    return "\n".join(lines)


def build_components(
    caller: str,
    blacklist_url: str,
    whitelist_url: str,
    click_to_call_url: str,
) -> list[dict[str, object]]:
    if not caller:
        return []
    buttons: list[dict[str, object]] = [
        {
            "type": 2,
            "style": 5,
            "label": "📖 電話帳ナビ",
            "url": f"https://www.telnavi.jp/phone/{caller}",
        }
    ]
    if click_to_call_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "📞 折り返し発信",
                "url": click_to_call_url,
            }
        )
    if whitelist_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "✅ ホワイトリストへ登録",
                "url": whitelist_url,
            }
        )
    if blacklist_url:
        buttons.append(
            {
                "type": 2,
                "style": 5,
                "label": "🚫 ブラックリストへ登録",
                "url": blacklist_url,
            }
        )
    return [{"type": 1, "components": buttons}]


def build_discord_payload(
    caller: str,
    blacklist_url: str = "",
    whitelist_url: str = "",
    click_to_call_url: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"content": build_content(caller, now=now)}
    components = build_components(caller, blacklist_url, whitelist_url, click_to_call_url)
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
    parser.add_argument("caller", nargs="?", default="")
    parser.add_argument("unique_id", nargs="?", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    caller = normalize_caller(args.caller)

    if args.dry_run:
        print(json.dumps(build_discord_payload(caller), ensure_ascii=False, indent=2))
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
        if caller and action_secret:
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
        payload = build_discord_payload(caller, blacklist_url, whitelist_url, click_to_call_url)
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
