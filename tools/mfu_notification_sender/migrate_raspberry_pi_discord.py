#!/usr/bin/env python3
"""Switch Raspberry Pi notification producers from Discord to the local bridge."""
from __future__ import annotations

import os
import re
from pathlib import Path


BRIDGE = "http://127.0.0.1:8765/discord"
SETTINGS = (
    (Path("/etc/p2p-eew-history/discord.conf"), "webhook_url", "earthquake_early_warning"),
    (Path("/etc/default/chiba_police_watcher"), "CHIBA_POLICE_DISCORD_WEBHOOK_URL", "chiba_police_incidents"),
    (Path("/etc/default/ichihara_bousai_watcher"), "ICHIHARA_DISCORD_WEBHOOK_URL", "ichihara_disaster_radio"),
    (Path("/etc/default/mail_summary_watcher"), "DISCORD_WEBHOOK_URL", "mail_summary"),
    (Path("/etc/default/mail_spam_monitor"), "MAIL_SPAM_MONITOR_DISCORD_WEBHOOK_URL", "mail_spam_report"),
    (Path("/etc/host_watch.env"), "WEBHOOK_URL", "host_health"),
    (Path("/usr/local/bin/update_all_debian.conf"), "DISCORD_WEBHOOK_URL", "debian_updates"),
    (Path("/etc/default/mfu-notify"), "DISCORD_WEBHOOK_URL_HARDCODE", "backup_status"),
)
VALIDATORS = (
    Path("/usr/local/bin/chiba_police_watcher.py"),
    Path("/usr/local/bin/ichihara_bousai_watcher.py"),
    Path("/usr/local/sbin/mail_spam_monitor.py"),
    Path("/usr/local/bin/update_all_debian.py"),
)


def atomic_write(path: Path, text: str) -> None:
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.mfu-migrate")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, stat.st_mode)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)


def set_value(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$", re.MULTILINE)
    changed, count = pattern.subn(rf'\g<1>"{value}"', text)
    if count != 1:
        raise RuntimeError(f"expected one {key} in {path}, found {count}")
    atomic_write(path, changed)


def allow_local_bridge(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old = 'parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}'
    new = "not ((parsed.scheme == \"https\" and parsed.hostname in {\"discord.com\", \"discordapp.com\"}) or (parsed.scheme == \"http\" and parsed.hostname == \"127.0.0.1\" and parsed.port == 8765))"
    if old not in text and new not in text:
        raise RuntimeError(f"webhook validator not found in {path}")
    text = text.replace(old, new)
    if path.name == "chiba_police_watcher.py":
        discord_path_check = 'if "/api/webhooks/" not in parsed.path:'
        bridge_aware_path_check = 'if parsed.hostname in {"discord.com", "discordapp.com"} and "/api/webhooks/" not in parsed.path:'
        if discord_path_check not in text and bridge_aware_path_check not in text:
            raise RuntimeError(f"Discord path validator not found in {path}")
        text = text.replace(discord_path_check, bridge_aware_path_check)
    atomic_write(path, text)


def main() -> int:
    for path in VALIDATORS:
        allow_local_bridge(path)
    for path, key, feature in SETTINGS:
        set_value(path, key, f"{BRIDGE}/{feature}")
    print(f"migrated={len(SETTINGS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
