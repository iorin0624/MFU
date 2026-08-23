#!/usr/bin/env python3
"""Move the hard-coded EEW Discord webhook into a protected config file."""

from datetime import datetime
from pathlib import Path
import configparser
import os
import pwd
import grp
import re
import shutil


SOURCE = Path("/home/admin/p2p_eew_history_watcher.py")
CONFIG = Path("/etc/p2p-eew-history/discord.conf")
BACKUP_DIR = Path("/root/p2p-eew-history-backups")
MARKER = 'DISCORD_CONFIG_FILE = "/etc/p2p-eew-history/discord.conf"'
PATTERN = re.compile(r'^DISCORD_WEBHOOK_URL\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
REPLACEMENT = '''DISCORD_CONFIG_FILE = "/etc/p2p-eew-history/discord.conf"

def _load_discord_webhook_url():
    parser = configparser.ConfigParser()
    if not parser.read(DISCORD_CONFIG_FILE, encoding="utf-8") or not parser.has_section("discord"):
        return ""
    return parser.get("discord", "webhook_url", fallback="").strip()

DISCORD_WEBHOOK_URL = _load_discord_webhook_url()'''


def secure_config(webhook_url):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    parser["discord"] = {"webhook_url": webhook_url}
    with CONFIG.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    os.chown(CONFIG, pwd.getpwnam("root").pw_uid, grp.getgrnam("admin").gr_gid)
    os.chmod(CONFIG, 0o640)


def main():
    source = SOURCE.read_text(encoding="utf-8")
    if MARKER in source:
        if not CONFIG.exists():
            raise SystemExit("source is patched but Discord config is missing")
        print("Discord webhook is already separated")
        return

    match = PATTERN.search(source)
    if not match:
        raise SystemExit("hard-coded Discord webhook assignment was not found")
    webhook_url = match.group(1).strip()
    if not webhook_url:
        raise SystemExit("Discord webhook is empty")

    secure_config(webhook_url)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    backup = BACKUP_DIR / f"p2p_eew_history_watcher.py.{datetime.now():%Y%m%d%H%M%S}"
    shutil.copy2(SOURCE, backup)
    os.chmod(backup, 0o600)
    SOURCE.write_text(PATTERN.sub(REPLACEMENT, source, count=1), encoding="utf-8")
    print(f"Discord webhook moved to {CONFIG}; protected backup={backup}")


if __name__ == "__main__":
    main()
