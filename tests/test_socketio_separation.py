"""Dependency-light checks for split Socket.IO startup and URL isolation."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


runtime = load_file("socketio_runtime_test", ROOT / "utils/socketio_runtime.py")
gate = load_file("socketio_wsgi_gate_test", ROOT / "utils/socketio_wsgi_gate.py")


class Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, *args):
        self.warnings.append(args)


class SocketIOSeparationTests(unittest.TestCase):
    def test_import_time_schema_initializers_are_gated(self):
        root_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("if dedicated_mode():\n    app.register_blueprint(bank_account_bp)", root_source)
        for name in ("albums/routes.py", "external_login_user/schema.py", "external_login_user/notifications.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('if os.environ.get("MFU_SOCKETIO_DEDICATED", "").strip().lower() in', source)

    def test_split_mode_requires_redis_queue(self):
        with patch.dict(os.environ, {"MFU_SOCKETIO_DEDICATED": "1", "SOCKETIO_MESSAGE_QUEUE": ""}):
            with self.assertRaisesRegex(RuntimeError, "required"):
                runtime.resolve_message_queue(Logger())

    def test_split_mode_refuses_failed_redis(self):
        fake = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a, **k: (_ for _ in ()).throw(OSError("down"))))
        with patch.dict(os.environ, {"MFU_SOCKETIO_DEDICATED": "1", "SOCKETIO_MESSAGE_QUEUE": "redis://127.0.0.1/0"}):
            with patch.dict(sys.modules, {"redis": fake}):
                with self.assertRaisesRegex(RuntimeError, "refusing local-only"):
                    runtime.resolve_message_queue(Logger())

    def test_existing_http_mode_keeps_legacy_fallback(self):
        with patch.dict(os.environ, {"MFU_SOCKETIO_DEDICATED": "0", "SOCKETIO_QUEUE_REQUIRED": "0", "SOCKETIO_MESSAGE_QUEUE": "redis://127.0.0.1/0"}):
            with patch.dict(sys.modules, {"redis": None}):
                logger = Logger()
                self.assertIsNone(runtime.resolve_message_queue(logger))
                self.assertTrue(logger.warnings)

    def test_split_mode_requires_explicit_origin(self):
        with patch.dict(os.environ, {"MFU_SOCKETIO_DEDICATED": "1", "SOCKETIO_CORS_ALLOWED_ORIGINS": "*"}):
            with self.assertRaisesRegex(RuntimeError, "explicit"):
                runtime.allowed_origins()
        with patch.dict(os.environ, {"MFU_SOCKETIO_DEDICATED": "1", "SOCKETIO_CORS_ALLOWED_ORIGINS": "https://mfu.iori0624.jp/"}):
            self.assertEqual(runtime.allowed_origins(), ["https://mfu.iori0624.jp"])

    def test_dedicated_wsgi_blocks_normal_http_and_exposes_health(self):
        calls = []

        def downstream(environ, respond):
            calls.append(environ["PATH_INFO"])
            respond("200 OK", [])
            return [b"engine"]

        app = gate.SocketIOOnlyWSGI(downstream, lambda: True)
        responses = []
        respond = lambda status, headers: responses.append(status)
        self.assertEqual(app({"PATH_INFO": "/admin"}, respond), [b"Not Found\n"])
        self.assertEqual(responses[-1], "404 Not Found")
        self.assertEqual(app({"PATH_INFO": "/socketio-health"}, respond), [b"ok\n"])
        self.assertEqual(app({"PATH_INFO": "/socket.io/"}, respond), [b"engine"])
        self.assertEqual(app({"PATH_INFO": "/socket.io"}, respond), [b"engine"])
        self.assertEqual(calls, ["/socket.io/", "/socket.io"])


if __name__ == "__main__":
    unittest.main()
