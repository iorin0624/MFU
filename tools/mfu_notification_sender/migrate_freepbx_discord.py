#!/usr/bin/env python3
"""Move active FreePBX Discord producers to the localhost MFU bridge."""
from __future__ import annotations

import os
import re
from pathlib import Path


ENV_PATH = Path("/etc/asterisk/vm-watch-10610.env")
TRANSCRIBER = Path("/var/lib/asterisk/bin/vm_transcribe_notify.py")
CALL_URL = "http://127.0.0.1:8765/discord/freepbx_calls"
VOICEMAIL_URL = "http://127.0.0.1:8765/discord/voicemail_transcription"


def atomic_write(path: Path, text: str) -> None:
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.mfu-migrate")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, stat.st_mode)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)


def set_env(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$", re.MULTILINE)
    replacement = f'{key}="{value}"'
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return text.rstrip() + "\n" + replacement + "\n"


def main() -> int:
    env_text = ENV_PATH.read_text(encoding="utf-8")
    env_text = set_env(env_text, "DISCORD_WEBHOOK_URL", CALL_URL)
    env_text = set_env(env_text, "MFU_VOICEMAIL_NOTIFICATION_WEBHOOK_URL", VOICEMAIL_URL)
    atomic_write(ENV_PATH, env_text)

    source = TRANSCRIBER.read_text(encoding="utf-8")
    old = 'DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()'
    new = 'DISCORD_WEBHOOK_URL = (os.environ.get("MFU_VOICEMAIL_NOTIFICATION_WEBHOOK_URL", "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())'
    if old not in source and new not in source:
        raise RuntimeError("voicemail webhook assignment was not found")
    atomic_write(TRANSCRIBER, source.replace(old, new))
    print("migrated=2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
