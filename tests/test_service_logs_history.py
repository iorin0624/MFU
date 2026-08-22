import importlib.util
import json
import subprocess
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask_stub = types.ModuleType("flask")

    class FakeBlueprint:
        def __init__(self, *args, **kwargs):
            pass

        def before_request(self, func):
            return func

        def get(self, *args, **kwargs):
            return lambda func: func

    flask_stub.Blueprint = FakeBlueprint
    flask_stub.Response = object
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(args={})
    flask_stub.abort = lambda *args, **kwargs: None
    flask_stub.stream_with_context = lambda value: value
    flask_stub.session = {}
    flask_stub.g = types.SimpleNamespace()
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.url_for = lambda *args, **kwargs: "/"
    flask_stub.jsonify = lambda value: value
    sys.modules["flask"] = flask_stub

MODULE_PATH = ROOT / "utils" / "service_logs.py"
SPEC = importlib.util.spec_from_file_location("mfu_service_logs_history", MODULE_PATH)
service_logs = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(service_logs)


class ServiceLogHistoryTest(unittest.TestCase):
    def test_decode_journal_entry(self):
        row = service_logs._decode_journal_entry(
            {
                "__REALTIME_TIMESTAMP": "1784986200123456",
                "PRIORITY": "4",
                "_SYSTEMD_UNIT": "mfu.service",
                "SYSLOG_IDENTIFIER": "gunicorn",
                "_PID": "123",
                "_HOSTNAME": "SE02",
                "_TRANSPORT": "stdout",
                "__CURSOR": "cursor-1",
                "MESSAGE": "warning message",
            }
        )

        self.assertEqual(row["level"], "warning")
        self.assertEqual(row["unit"], "mfu.service")
        self.assertEqual(row["identifier"], "gunicorn")
        self.assertEqual(row["pid"], "123")
        self.assertEqual(row["message"], "warning message")
        self.assertTrue(row["timestamp"].endswith("+09:00"))

    def test_history_query_defaults_to_current_day_and_rejects_long_range(self):
        now = datetime(2026, 7, 25, 22, 0, tzinfo=service_logs.JST)
        query = service_logs._history_query_from_args({}, now=now)
        self.assertEqual(query["service"], "mfu")
        self.assertEqual(query["date_from"].hour, 0)
        self.assertEqual(query["date_to"], now)

        with self.assertRaisesRegex(service_logs.HistoryQueryError, "最大31日"):
            service_logs._history_query_from_args(
                {
                    "date_from": "2026-06-01T00:00",
                    "date_to": "2026-07-25T22:00",
                },
                now=now,
            )

    def test_file_source_is_rejected_for_history(self):
        with patch.object(
            service_logs,
            "_get_config",
            return_value=(
                {"mail_log": {"file": "/var/log/mail.log"}},
                {"mail_log": "mail.log"},
                "mail_log",
                200,
            ),
        ):
            with self.assertRaisesRegex(service_logs.HistoryQueryError, "ファイル追尾専用"):
                service_logs._history_query_from_args({"service": "mail_log"})

    def test_history_search_counts_and_pages_after_literal_keyword_filter(self):
        entries = []
        for index, message in enumerate(("other", "Needle one", "needle two", "last")):
            entries.append(
                json.dumps(
                    {
                        "__REALTIME_TIMESTAMP": str(1784986200000000 + index),
                        "PRIORITY": "6",
                        "_SYSTEMD_UNIT": "mfu.service",
                        "SYSLOG_IDENTIFIER": "gunicorn",
                        "MESSAGE": message,
                    }
                )
            )
        completed = subprocess.CompletedProcess(
            args=["journalctl"],
            returncode=0,
            stdout="\n".join(entries),
            stderr="",
        )
        query = {
            "units": ["mfu.service"],
            "date_from": datetime(2026, 7, 25, 0, 0, tzinfo=service_logs.JST),
            "date_to": datetime(2026, 7, 25, 23, 59, tzinfo=service_logs.JST),
            "level": "all",
            "keyword": "needle",
            "page": 1,
            "page_size": 25,
        }

        with patch.object(service_logs.subprocess, "run", return_value=completed) as run:
            result = service_logs._read_journal_history(query)

        self.assertEqual(result["total"], 2)
        self.assertEqual([row["message"] for row in result["rows"]], ["Needle one", "needle two"])
        command = run.call_args.args[0]
        self.assertIn("--output=json", command)
        self.assertIn("mfu.service", command)
        self.assertNotIn("needle", command)

    def test_csv_formula_prefix_is_escaped(self):
        self.assertEqual(service_logs._csv_safe("=cmd"), "'=cmd")
        self.assertEqual(service_logs._csv_safe("normal"), "normal")

    def test_template_contains_requested_controls(self):
        source = (ROOT / "templates" / "service_logs.html").read_text(encoding="utf-8-sig")
        for marker in (
            "history-mode-btn",
            "live-mode-btn",
            "history-keyword",
            "history-level",
            "history-pagination",
            "history-auto-refresh",
            "history.csv",
            "詳細を表示",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
