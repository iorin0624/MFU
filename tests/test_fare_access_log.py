import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "fare" / "audit.py"
SPEC = importlib.util.spec_from_file_location("fare_audit", AUDIT_PATH)
fare_audit = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(fare_audit)


class FareAccessLogTest(unittest.TestCase):
    def test_success_marker_contains_unmasked_destination(self):
        marker = fare_audit.build_fare_search_marker("東京ディズニーランド", succeeded=True)

        self.assertEqual(
            marker,
            "[FARE_ESTIMATE_SEARCH] 結果：成功 到着地点：東京ディズニーランド",
        )

    def test_failure_marker_is_single_line_and_fits_access_log_marker(self):
        marker = fare_audit.build_fare_search_marker("新宿\r\n駅" + "A" * 200, succeeded=False)

        self.assertIn("結果：失敗", marker)
        self.assertNotIn("\r", marker)
        self.assertNotIn("\n", marker)
        self.assertLessEqual(len(marker), 160)

    def test_admin_log_template_has_dedicated_pretty_view(self):
        template = (ROOT / "templates" / "admin_logs.html").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn(r"/\[FARE_ESTIMATE_SEARCH\]/", template)
        self.assertIn("料金検索", template)
        self.assertIn("到着地点：", template)


if __name__ == "__main__":
    unittest.main()
