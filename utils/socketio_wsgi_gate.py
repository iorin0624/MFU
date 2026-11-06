"""Restrict the dedicated listener to Engine.IO and an internal health probe."""

from __future__ import annotations

from typing import Callable


class SocketIOOnlyWSGI:
    def __init__(self, downstream: Callable, health: Callable[[], bool]):
        self.downstream = downstream
        self.health = health

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/socketio-health":
            healthy = self.health()
            body = b"ok\n" if healthy else b"redis_unavailable\n"
            start_response(
                "200 OK" if healthy else "503 Service Unavailable",
                [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
            )
            return [body]
        if path != "/socket.io" and not path.startswith("/socket.io/"):
            body = b"Not Found\n"
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]
        return self.downstream(environ, start_response)
