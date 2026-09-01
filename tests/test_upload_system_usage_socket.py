from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UploadSystemUsageSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        cls.template_source = (ROOT / "templates" / "upload.html").read_text(encoding="utf-8")

    def test_admin_namespace_requires_live_admin_session(self):
        self.assertIn('@socketio.on("connect", namespace="/admin-system")', self.app_source)
        self.assertIn(
            'session.get("user") != ADMIN_USERNAME or not validate_admin_session()',
            self.app_source,
        )

    def test_collector_uses_redis_lock_and_admin_room(self):
        self.assertIn('"mfu:system-usage:collector"', self.app_source)
        self.assertIn('room=_SYSTEM_USAGE_ROOM', self.app_source)
        self.assertIn('namespace="/admin-system"', self.app_source)

    def test_frontend_prefers_socket_and_keeps_http_fallback(self):
        self.assertIn("window.io('/admin-system'", self.template_source)
        self.assertIn("usageSocket.on('system_usage_update'", self.template_source)
        self.assertIn("function startUsageFallback()", self.template_source)
        self.assertIn("fetch('/api/storage_usage'", self.template_source)

    def test_status_sections_are_independent_cards(self):
        for title in (
            "🌍 環境一覧",
            "🔧 サーバー状態",
            "💾 ストレージ使用状況",
            "🧠 CPU使用率",
            "🔌 リアルタイム接続",
        ):
            self.assertIn(title, self.template_source)
        self.assertNotIn("Raspberry Pi 状態", self.template_source)
        self.assertGreaterEqual(self.template_source.count("upload-system-card--"), 5)

    def test_realtime_connection_metrics_are_in_socket_payload(self):
        self.assertIn('payload["realtime"] = connection_snapshot(', self.app_source)
        self.assertIn("renderRealtimeConnections(payload.realtime)", self.template_source)
        self.assertIn('id="realtime-total"', self.template_source)
        self.assertIn('id="realtime-websocket"', self.template_source)
        self.assertIn('id="realtime-polling"', self.template_source)

    def test_storage_fallback_is_admin_only(self):
        marker = '@app.route("/api/storage_usage")\n@admin_required'
        self.assertIn(marker, self.app_source)

    def test_nodes_use_shared_parallel_socket_collector(self):
        nodes_template = (ROOT / "templates" / "admin_nodes.html").read_text(encoding="utf-8")
        self.assertIn('executor.submit(_fetch_node_metrics, target, headers)', self.app_source)
        self.assertIn('"mfu:admin-nodes:collector"', self.app_source)
        self.assertIn('@socketio.on("nodes_subscribe", namespace="/admin-system")', self.app_source)
        self.assertIn("socket.emit('nodes_subscribe')", nodes_template)
        self.assertIn("socket.on('nodes_status_update'", nodes_template)
        self.assertIn('const FALLBACK_INTERVAL_MS = 10000;', nodes_template)


if __name__ == "__main__":
    unittest.main()
