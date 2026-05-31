from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatTarget:
    kind: str
    title: str
    event_id: int | None = None
    room_id: str | None = None
    dm_uuid: str | None = None
    unread_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if self.kind == "dm":
            return f"dm:{self.dm_uuid}"
        return f"event:{self.event_id}:{self.room_id or ''}"


@dataclass
class PendingUpload:
    path: str
    size: int
