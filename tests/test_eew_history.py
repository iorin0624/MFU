from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import types
import unittest


if "mysql.connector" not in sys.modules:
    mysql_module = types.ModuleType("mysql")
    connector_module = types.ModuleType("mysql.connector")
    connector_module.connect = None
    mysql_module.connector = connector_module
    sys.modules["mysql"] = mysql_module
    sys.modules["mysql.connector"] = connector_module

MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "eew_history.py"
SPEC = importlib.util.spec_from_file_location("eew_history_under_test", MODULE_PATH)
eew_history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eew_history)


class EewHistoryTests(unittest.TestCase):
    def test_enrich_report_preserves_milliseconds_and_calculates_delays(self):
        row = {
            "jma_issue_time": datetime(2026, 8, 23, 2, 0, 53, 0),
            "p2p_time": datetime(2026, 8, 23, 2, 0, 53, 811000),
            "pi_ws_received_at": datetime(2026, 8, 23, 2, 1, 1, 0),
            "pi_history_received_at": None,
            "scale_from": 40,
            "scale_to": 50,
            "raw_json": "{}",
        }
        result = eew_history.enrich_report(row)
        self.assertEqual(result["formatted"]["p2p_time"], "2026-08-23 02:00:53.811")
        self.assertEqual(result["delays"]["jma_to_p2p"], 0.811)
        self.assertEqual(result["delays"]["p2p_to_ws"], 7.189)
        self.assertEqual(result["delays"]["jma_to_ws"], 8.0)
        self.assertEqual(result["scale_from_label"], "4")
        self.assertEqual(result["scale_to_label"], "5強")

    def test_raw_json_is_pretty_only_for_detail(self):
        row = {"raw_json": '{"a":1}', "scale_from": None, "scale_to": None}
        listed = eew_history.enrich_report(row)
        detailed = eew_history.enrich_report(row, include_raw=True)
        self.assertNotIn("raw_json", listed)
        self.assertIn('"a": 1', detailed["raw_json_pretty"])


if __name__ == "__main__":
    unittest.main()
