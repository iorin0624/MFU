from __future__ import annotations

import json
import os
from http.cookiejar import MozillaCookieJar
from typing import Any

import keyring

from config import CONFIG


class SessionStore:
    service_name = "MFU Chat Desktop"

    def __init__(self) -> None:
        os.makedirs(CONFIG.config_dir, exist_ok=True)
        self.settings_path = os.path.join(CONFIG.config_dir, "settings.json")
        self.cookie_path = os.path.join(CONFIG.config_dir, "cookies.txt")

    def load_settings(self) -> dict[str, Any]:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def save_settings(self, data: dict[str, Any]) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def cookie_jar(self) -> MozillaCookieJar:
        jar = MozillaCookieJar(self.cookie_path)
        if os.path.exists(self.cookie_path):
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        return jar

    def save_cookies(self, jar: MozillaCookieJar) -> None:
        jar.save(ignore_discard=True, ignore_expires=True)

    def save_password(self, username: str, password: str) -> None:
        keyring.set_password(self.service_name, username, password)

    def load_password(self, username: str) -> str | None:
        return keyring.get_password(self.service_name, username)
