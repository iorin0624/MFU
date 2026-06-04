from __future__ import annotations

from typing import Any

try:
    from winotify import Notification
except Exception:
    Notification = None

from config import CONFIG


class Notifier:
    def __init__(self) -> None:
        self.enabled = True

    def show_message(self, message: dict[str, Any]) -> None:
        if not self.enabled or Notification is None:
            return
        title = str(message.get("sender_display_name") or CONFIG.app_name)
        body = "画像が送信されました" if message.get("has_image") else str(message.get("body_plain") or message.get("body") or "")
        toast = Notification(app_id=CONFIG.app_name, title=title, msg=body[:160])
        toast.show()
