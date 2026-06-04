from __future__ import annotations

from typing import Any, Callable

import socketio

from config import CONFIG


class SocketClient:
    def __init__(self, api_client: Any) -> None:
        self.api = api_client
        self.sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._bind()

    def _bind(self) -> None:
        for event_name in [
            "connect",
            "disconnect",
            "chat_message",
            "chat_error",
            "chat_read_snapshot",
            "chat_read_update",
            "chat_reaction_update",
            "chat_delete_update",
            "chat_edit_update",
            "chat_typing_update",
        ]:
            self.sio.on(event_name, lambda data=None, name=event_name: self._dispatch(name, data))

    def on(self, event_name: str, callback: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event_name, []).append(callback)

    def _dispatch(self, event_name: str, data: Any = None) -> None:
        for callback in self._handlers.get(event_name, []):
            callback(data)

    def connect(self) -> None:
        if self.sio.connected:
            return
        headers = {}
        cookies = {cookie.name: cookie.value for cookie in self.api.session.cookies}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        self.sio.connect(
            self.api.base_url,
            headers=headers,
            socketio_path=CONFIG.socket_path.strip("/"),
            transports=["websocket", "polling"],
            wait_timeout=10,
        )

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if not self.sio.connected:
            self.connect()
        self.sio.emit(event_name, payload)

    def disconnect(self) -> None:
        if self.sio.connected:
            self.sio.disconnect()

    def join_event(self, event_id: int, room_id: str | None) -> None:
        self._emit("chat_join", {"event_id": event_id, "room_id": room_id or ""})

    def join_dm(self, dm_uuid: str) -> None:
        self._emit("chat_join", {"dm_uuid": dm_uuid, "room_id": f"dm:{dm_uuid}"})

    def send_event(self, event_id: int, room_id: str, body: str, reply_to: int | None = None, thread_root_id: int | None = None) -> None:
        payload = {"event_id": event_id, "room_id": room_id, "body": body}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if thread_root_id:
            payload["thread_root_id"] = thread_root_id
        self._emit("chat_send", payload)

    def send_dm(self, dm_uuid: str, body: str) -> None:
        self._emit("chat_send", {"dm_uuid": dm_uuid, "body": body})

    def seen_event(self, event_id: int, room_id: str, last_seen_message_id: int) -> None:
        self._emit("chat_seen", {"event_id": event_id, "room_id": room_id, "last_seen_message_id": last_seen_message_id})

    def seen_dm(self, dm_uuid: str, last_seen_message_id: int) -> None:
        self._emit("chat_seen", {"dm_uuid": dm_uuid, "room_id": f"dm:{dm_uuid}", "last_seen_message_id": last_seen_message_id})

    def react_event(self, event_id: int, room_id: str, message_id: int, emoji: str) -> None:
        self._emit("chat_react", {"event_id": event_id, "room_id": room_id, "message_id": message_id, "emoji": emoji})

    def react_dm(self, dm_uuid: str, message_id: int, emoji: str) -> None:
        self._emit("dm_react", {"dm_uuid": dm_uuid, "message_id": message_id, "emoji": emoji})

    def typing_event(self, event_id: int, room_id: str, is_typing: bool) -> None:
        self._emit("chat_typing", {"event_id": event_id, "room_id": room_id, "is_typing": is_typing})
