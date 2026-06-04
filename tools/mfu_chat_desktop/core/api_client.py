from __future__ import annotations

import os
import uuid
from typing import Any

import requests

from config import CONFIG
from core.session_store import SessionStore


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.base_url = store.load_settings().get("base_url") or CONFIG.base_url
        self.session = requests.Session()
        self.session.cookies = store.cookie_jar()
        self.csrf_token = ""
        self.actor: dict[str, Any] | None = None
        settings = store.load_settings()
        self.client_id = settings.get("client_id") or f"desktop-{uuid.uuid4()}"
        settings["client_id"] = self.client_id
        store.save_settings(settings)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def save_cookies(self) -> None:
        self.store.save_cookies(self.session.cookies)

    def _json(self, response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(f"HTTP {response.status_code}: invalid JSON") from exc
        if response.status_code >= 400 or data.get("ok") is False:
            raise ApiError(str(data.get("error") or f"HTTP {response.status_code}"))
        return data

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        res = self.session.get(self.url(path), params={k: v for k, v in params.items() if v is not None}, timeout=30)
        return self._json(res)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        res = self.session.post(self.url(path), json=payload, timeout=30)
        data = self._json(res)
        self.save_cookies()
        return data

    def check_session(self) -> dict[str, Any]:
        data = self.get("/chat/api/gui/session")
        if data.get("authenticated"):
            self.csrf_token = data.get("csrf_token") or ""
            self.actor = data.get("actor") or {}
            self.save_cookies()
        return data

    def login(self, username: str, password: str) -> dict[str, Any]:
        data = self.post_json("/chat/api/gui/login", {"username": username, "password": password})
        self.csrf_token = data.get("csrf_token") or ""
        self.actor = data.get("actor") or {}
        self.save_cookies()
        return data

    def bootstrap(self) -> dict[str, Any]:
        data = self.get("/chat/api/gui/bootstrap")
        self.csrf_token = data.get("csrf_token") or self.csrf_token
        return data

    def event_snapshot(self, event_id: int, room_id: str | None = None) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/events/{event_id}/snapshot", room_id=room_id)

    def dm_snapshot(self, dm_uuid: str) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/dm/{dm_uuid}/snapshot")

    def older_event_messages(self, event_id: int, room_id: str, before_id: int, limit: int = 50) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/events/{event_id}/messages", room_id=room_id, before_id=before_id, limit=limit)

    def older_dm_messages(self, dm_uuid: str, before_id: int, limit: int = 50) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/dm/{dm_uuid}/messages", before_id=before_id, limit=limit)

    def search_event(self, event_id: int, room_id: str, q: str) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/events/{event_id}/search", room_id=room_id, q=q)

    def search_dm(self, dm_uuid: str, q: str) -> dict[str, Any]:
        return self.get(f"/chat/api/gui/dm/{dm_uuid}/search", q=q)

    def thread_messages(self, event_id: int, room_id: str, root_message_id: int) -> dict[str, Any]:
        return self.get(f"/chat/api/events/{event_id}/threads/{root_message_id}", room_id=room_id)

    def reaction_details(self, target: Any, message_id: int) -> dict[str, Any]:
        if target.kind == "dm":
            return self.get(f"/chat/api/gui/dm/{target.dm_uuid}/messages/{message_id}/reactions")
        return self.get(f"/chat/api/events/{target.event_id}/messages/{message_id}/reactions", room_id=target.room_id)

    def upload_images(
        self,
        files: list[str],
        body: str,
        *,
        event_id: int | None = None,
        room_id: str | None = None,
        dm_uuid: str | None = None,
        reply_to_message_id: int | None = None,
        thread_root_id: int | None = None,
    ) -> dict[str, Any]:
        form = {"csrf_token": self.csrf_token, "body": body}
        if room_id:
            form["room_id"] = room_id
        if dm_uuid:
            form["dm_uuid"] = dm_uuid
        if reply_to_message_id:
            form["reply_to_message_id"] = str(reply_to_message_id)
        if thread_root_id:
            form["thread_root_id"] = str(thread_root_id)
        handles = []
        try:
            upload_files = []
            for path in files:
                handle = open(path, "rb")
                handles.append(handle)
                upload_files.append(("file", (os.path.basename(path), handle, "application/octet-stream")))
            target = f"/chat/api/events/{event_id}/upload-image" if event_id else "/chat/dm/api/upload-image"
            res = self.session.post(self.url(target), data=form, files=upload_files, timeout=120)
            data = self._json(res)
            self.save_cookies()
            return data
        finally:
            for handle in handles:
                handle.close()

    def edit_message(self, target: Any, message_id: int, body: str) -> dict[str, Any]:
        if target.kind == "dm":
            return self.post_json(f"/chat/dm/api/messages/{message_id}/edit", {"csrf_token": self.csrf_token, "dm_uuid": target.dm_uuid, "body_text": body})
        return self.post_json(f"/chat/api/events/{target.event_id}/messages/{message_id}/edit", {"csrf_token": self.csrf_token, "room_id": target.room_id, "body": body})

    def delete_message(self, target: Any, message_id: int) -> dict[str, Any]:
        if target.kind == "dm":
            return self.post_json(f"/chat/dm/api/messages/{message_id}/delete", {"csrf_token": self.csrf_token, "dm_uuid": target.dm_uuid})
        return self.post_json(f"/chat/api/events/{target.event_id}/messages/{message_id}/delete", {"csrf_token": self.csrf_token, "room_id": target.room_id})

    def presence(self, action: str, *, event_id: int = 0, room_id: str = "", is_visible: bool = True) -> dict[str, Any]:
        if action not in {"enter", "ping", "leave"}:
            raise ApiError("invalid_presence_action")
        return self.post_json(
            f"/chat/api/room-presence/{action}",
            {
                "csrf_token": self.csrf_token,
                "event_id": event_id,
                "room_id": room_id,
                "client_id": self.client_id,
                "is_visible": bool(is_visible),
            },
        )
