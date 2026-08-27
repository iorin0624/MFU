from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RealtimeProgressSourceTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_admin_socket_has_timer_and_job_subscriptions(self):
        source = self.read("__init__.py")
        self.assertIn('timer_scan_subscribe", namespace="/admin-system"', source)
        self.assertIn('admin_job_subscribe", namespace="/admin-system"', source)
        self.assertIn('download-progress', source)

    def test_timer_scan_is_pushed_after_persistence(self):
        source = self.read("routes/timer_routes.py")
        self.assertLess(source.index("_save_last_scan(payload)"), source.index('"timer_scan_update"'))

    def test_external_notification_polling_is_not_two_seconds(self):
        source = self.read("external_login_user/template/notifications.html")
        self.assertNotIn("window.setInterval(pollUpdatesOnce, 2000)", source)
        self.assertIn("notifSocket.on('notif_unread'", source)

    def test_zip_and_shortcut_use_download_progress_namespace(self):
        zip_js = self.read("static/js/mfu_zip_download.js")
        shortcut_js = self.read("static/js/mfu_shortcut_download.js")
        self.assertIn("global.io('/download-progress'", zip_js)
        self.assertIn("window.io('/download-progress'", shortcut_js)

    def test_etc_and_logs_use_admin_job_events(self):
        manual = self.read("etc_accounting/templates/etc_accounting/index.html")
        batch = self.read("etc_accounting/templates/etc_accounting/batch_progress.html")
        logs = self.read("templates/admin/logs_loading.html")
        self.assertIn("etc_manual_job_update", manual)
        self.assertIn("etc_batch_job_update", batch)
        self.assertIn("admin_logs_job_update", logs)


if __name__ == "__main__":
    unittest.main()
