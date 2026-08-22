import unittest
from datetime import datetime, timedelta

from utils.fw_auto_ban import (
    choose_ban_escalation,
    classify_request_path,
    enforcement_enabled,
    evaluate_events,
)


SETTINGS = {
    "sensitive_window_sec": 60,
    "sensitive_threshold": 2,
    "short_window_sec": 10,
    "short_threshold": 8,
    "ip_window_sec": 300,
    "ip_threshold": 20,
    "ban_duration_sec": 3600,
    "repeat_ban_duration_sec": 86400,
    "generic_third_ban_duration_sec": 604800,
    "sensitive_permanent_threshold": 3,
    "generic_permanent_threshold": 4,
}


class FwAutoBanTests(unittest.TestCase):
    def test_secret_paths_are_critical(self):
        self.assertEqual(classify_request_path("/laravel/.env"), "critical")
        self.assertEqual(classify_request_path("/.gcloud/credentials"), "critical")

    def test_application_404_is_not_a_generic_scan(self):
        self.assertEqual(
            classify_request_path(
                "/image_viewer/api/instagram/jobs/abc/preview/12",
                endpoint="image_viewer.instagram_preview",
            ),
            "application",
        )
        self.assertEqual(
            classify_request_path(
                "/uploads/example/original/missing.jpg",
                endpoint="uploaded_file",
            ),
            "application",
        )

    def test_two_sensitive_requests_trigger(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        rows = [
            {"log_date": now - timedelta(seconds=2), "path": "/.env", "status": 404, "endpoint": ""},
            {"log_date": now - timedelta(seconds=1), "path": "/wp-config.php", "status": 404, "endpoint": ""},
        ]
        evidence = evaluate_events(rows, SETTINGS, now=now)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.reason, "sensitive")

    def test_generic_scan_counts_distinct_paths(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        repeated = [
            {"log_date": now - timedelta(seconds=i), "path": "/missing", "status": 404, "endpoint": ""}
            for i in range(8)
        ]
        self.assertIsNone(evaluate_events(repeated, SETTINGS, now=now))

        distinct = [
            {"log_date": now - timedelta(seconds=i), "path": f"/missing-{i}", "status": 404, "endpoint": ""}
            for i in range(8)
        ]
        evidence = evaluate_events(distinct, SETTINGS, now=now)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.reason, "short")
        self.assertEqual(evidence.distinct_paths, 8)

    def test_observe_mode_switches_after_deadline(self):
        before = datetime(2026, 8, 2, 12, 0, 0)
        after = datetime(2026, 8, 4, 12, 0, 1)
        settings = {"mode": "observe", "observe_until": "2026-08-04T12:00:00"}
        self.assertFalse(enforcement_enabled(settings, now=before))
        self.assertTrue(enforcement_enabled(settings, now=after))

    def test_sensitive_third_detection_is_permanent(self):
        first = choose_ban_escalation(prior_count=0, escalation_class="sensitive", settings=SETTINGS)
        second = choose_ban_escalation(prior_count=1, escalation_class="sensitive", settings=SETTINGS)
        third = choose_ban_escalation(prior_count=2, escalation_class="sensitive", settings=SETTINGS)
        self.assertEqual(first.duration_sec, 3600)
        self.assertEqual(second.duration_sec, 86400)
        self.assertEqual(third.action_kind, "permanent")

    def test_generic_fourth_detection_is_permanent(self):
        third = choose_ban_escalation(prior_count=2, escalation_class="generic", settings=SETTINGS)
        fourth = choose_ban_escalation(prior_count=3, escalation_class="generic", settings=SETTINGS)
        self.assertEqual(third.duration_sec, 604800)
        self.assertEqual(fourth.action_kind, "permanent")


if __name__ == "__main__":
    unittest.main()
