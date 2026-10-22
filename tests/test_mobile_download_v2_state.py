"""Run directly as well as under pytest; no production Flask imports needed."""

import sys
import ast
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.mobile_download_v2 import file_transition_error, resume_file_ids


class MobileDownloadV2StateTests(unittest.TestCase):
    def test_saved_is_only_reported_after_saving_and_cannot_be_downgraded(self):
        self.assertEqual(file_transition_error("pending", "saved"), "saving_report_required")
        self.assertIsNone(file_transition_error("pending", "saving"))
        self.assertIsNone(file_transition_error("saving", "saved"))
        self.assertIsNone(file_transition_error("saved", "saved"))
        self.assertEqual(file_transition_error("saved", "failed"), "already_saved")

    def test_uncertain_requires_explicit_resume(self):
        self.assertEqual(file_transition_error("uncertain", "saving"), "uncertain_requires_resume")
        files = [
            {"file_id": "1", "state": "saved"},
            {"file_id": "2", "state": "pending"},
            {"file_id": "3", "state": "uncertain"},
            {"file_id": "4", "state": "failed"},
        ]
        self.assertEqual(resume_file_ids(files, set()), ({"2", "4"}, None))
        self.assertEqual(resume_file_ids(files, {"3"}), ({"2", "3", "4"}, None))
        self.assertEqual(resume_file_ids(files, {"1"}), (set(), "invalid_retry_uncertain"))
        self.assertEqual(
            resume_file_ids([{"file_id": "3", "state": "uncertain"}], set()),
            (set(), "uncertain_files_require_confirmation"),
        )
        hundred = [
            {"file_id": str(index), "state": "saved" if index <= 97 else "pending"}
            for index in range(1, 101)
        ]
        self.assertEqual(resume_file_ids(hundred, set()), ({"98", "99", "100"}, None))

    def test_manifest_preserves_original_numbers_and_omits_confirmed_files(self):
        source = (Path(__file__).resolve().parents[1] / "utils" / "mobile_download.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        selected = [node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name in {"_manifest", "_progress_snapshot"}]
        functions = ast.Module(body=selected, type_ignores=[])

        class Cursor:
            def execute(self, sql, params):
                self.assert_params = params

            def fetchall(self):
                return [
                    {"file_id": "1", "state": "saved", "attempt_count": 1, "last_error_code": None},
                    {"file_id": "2", "state": "pending", "attempt_count": 0, "last_error_code": None},
                    {"file_id": "3", "state": "uncertain", "attempt_count": 1, "last_error_code": None},
                ]

        class DB:
            def cursor(self, **kwargs):
                return Cursor()

            def close(self):
                pass

        namespace = {
            "json": json,
            "V2_PROTOCOL_VERSION": 2,
            "ALBUM_NAME": "MFU",
            "get_db": DB,
            "url_for": lambda *args, **kwargs: f"/files/{kwargs['image_id']}",
        }
        exec(compile(functions, "mobile_download.py", "exec"), namespace)
        job = {
            "id": 9, "protocol_version": 2, "source_type": "upload",
            "upload_title": "Example", "session_expires_at": __import__("datetime").datetime(2026, 10, 22),
            "files_json": json.dumps([
                {"id": "1", "name": "a.jpg", "output_name": "00001.jpg"},
                {"id": "2", "name": "b.jpg", "output_name": "00002.jpg"},
                {"id": "3", "name": "c.jpg", "output_name": "00003.jpg"},
            ]),
        }
        progress = namespace["_progress_snapshot"](job)
        manifest = namespace["_manifest"](job)
        self.assertEqual(progress["counts"]["saved"], 1)
        self.assertEqual(progress["counts"]["uncertain"], 1)
        self.assertEqual([item["output_name"] for item in manifest["files"]], ["00002.jpg"])

    def test_v2_complete_requires_every_file_to_be_saved(self):
        source = (Path(__file__).resolve().parents[1] / "utils" / "mobile_download.py").read_text(
            encoding="utf-8"
        )
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_complete_v2_job")
        namespace = {"_bearer_token": lambda: "mfu_dl_test", "_hash_token": lambda value: value}

        class Cursor:
            def __init__(self):
                self.updated = False

            def execute(self, sql, params):
                if "SET completed_at" in sql:
                    self.updated = True

            def fetchone(self):
                return {"id": 9, "launch_token_hash": "hash", "completed_at": None}

        class DB:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.committed = False
                self.rolled_back = False

            def cursor(self, **kwargs):
                return self.cursor_instance

            def start_transaction(self):
                pass

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                pass

        db = DB()
        saved = 1
        namespace.update({
            "get_db": lambda: db,
            "jsonify": lambda payload: payload,
            "_progress_snapshot": lambda job, cur: {
                "completed": False,
                "counts": {"saved": saved, "total": 2},
            },
            "_record_shortcut_download": lambda *args, **kwargs: None,
            "mark_upload_download_status": lambda *args: None,
            "_shortcut_audit": lambda *args, **kwargs: None,
            "emit_download_event": lambda *args, **kwargs: None,
        })
        exec(compile(ast.Module(body=[function], type_ignores=[]), "mobile_download.py", "exec"), namespace)
        result, status = namespace["_complete_v2_job"](9)
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "files_not_all_saved")
        self.assertTrue(db.rolled_back)
        self.assertFalse(db.cursor_instance.updated)

        db = DB()
        saved = 2
        result = namespace["_complete_v2_job"](9)
        self.assertTrue(result["ok"])
        self.assertTrue(result["progress"]["completed"])
        self.assertTrue(db.committed)
        self.assertTrue(db.cursor_instance.updated)

    def test_result_progress_is_aggregated_by_state(self):
        source = (Path(__file__).resolve().parents[1] / "utils" / "mobile_download.py").read_text(
            encoding="utf-8"
        )
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_progress_counts")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "mobile_download.py", "exec"), namespace)

        class Cursor:
            def execute(self, sql, params):
                assert "GROUP BY state" in sql
                assert params == (42,)

            def fetchall(self):
                return [{"state": "saved", "count": 97}, {"state": "pending", "count": 3}]

        result = namespace["_progress_counts"](42, Cursor())
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["saved"], 97)
        self.assertEqual(result["remaining"], 3)

    def test_expired_upload_cannot_start_or_resume_v2(self):
        source = (Path(__file__).resolve().parents[1] / "utils" / "mobile_download.py").read_text(
            encoding="utf-8"
        )
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_v2_upload_active")
        namespace = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "mobile_download.py", "exec"), namespace)
        today_jst = datetime.now(timezone(timedelta(hours=9))).date()
        self.assertTrue(namespace["_v2_upload_active"]({"expire_at": today_jst}))
        self.assertFalse(namespace["_v2_upload_active"]({"expire_at": today_jst - timedelta(days=1)}))


if __name__ == "__main__":
    unittest.main()
