import ast
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def _load_module_without_app_dependencies(relative_path: str, module_name: str, **dependencies):
    path = ROOT / relative_path
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    tree.body = [
        node for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.")
        )
    ]
    module = types.ModuleType(module_name)
    module.__dict__["__file__"] = str(path)
    module.__dict__.update(dependencies)
    sys.modules[module_name] = module
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def _unused(*args, **kwargs):
    raise AssertionError("unexpected dependency call")


deletion = _load_module_without_app_dependencies(
    "utils/upload_deletion.py",
    "testable_upload_deletion",
    get_db=_unused,
    purge_upload_download_history=lambda *args, **kwargs: 0,
)
expiry = _load_module_without_app_dependencies(
    "utils/upload_expiry.py",
    "testable_upload_expiry",
    get_db=_unused,
    send_mail=_unused,
    delete_normal_upload=deletion.delete_normal_upload,
    send_discord_upload_notification=_unused,
)


class UploadExpiryScheduleTest(unittest.TestCase):
    def test_notice_is_previous_day_at_nine_and_delete_is_following_day(self):
        schedule = expiry.schedule_for(date(2026, 7, 20))
        self.assertEqual(schedule.notice_at.isoformat(), "2026-07-19T09:00:00+09:00")
        self.assertEqual(schedule.delete_at.isoformat(), "2026-07-21T00:15:00+09:00")

    def test_late_first_notice_adds_twenty_four_hour_grace(self):
        late = datetime(2026, 7, 22, 12, 0, tzinfo=expiry.JST)
        self.assertEqual(expiry.deletion_not_before(date(2026, 7, 20), late), late + timedelta(hours=24))

    def test_normal_notice_does_not_extend_nominal_deletion(self):
        normal = datetime(2026, 7, 19, 9, 0, tzinfo=expiry.JST)
        self.assertEqual(
            expiry.deletion_not_before(date(2026, 7, 20), normal),
            datetime(2026, 7, 21, 0, 15, tzinfo=expiry.JST),
        )

    def test_notification_method_mapping(self):
        self.assertEqual(expiry.notification_channels("discord"), (expiry.ACTION_NOTICE_DISCORD,))
        self.assertEqual(expiry.notification_channels("email"), (expiry.ACTION_NOTICE_EMAIL,))
        self.assertEqual(
            expiry.notification_channels("both"),
            (expiry.ACTION_NOTICE_DISCORD, expiry.ACTION_NOTICE_EMAIL),
        )
        self.assertEqual(expiry.notification_channels("none"), (expiry.ACTION_NOTICE_NONE,))

    def test_notice_explains_layer_preservation(self):
        _, body = expiry.build_expiry_notice(
            {"title": "テスト", "expire_at": date(2026, 7, 20)},
            public_base_url="https://mfu.example",
        )
        self.assertIn("レイヤーアップロードのデータは削除されません", body)
        self.assertIn("https://mfu.example/upload_list", body)

    def test_late_notice_explains_grace_instead_of_saying_tomorrow(self):
        _, body = expiry.build_expiry_notice(
            {"title": "テスト", "expire_at": date(2026, 7, 20)},
            public_base_url="https://mfu.example",
            notice_started_at=datetime(2026, 7, 22, 12, 0, tzinfo=expiry.JST),
        )
        self.assertIn("有効期限を過ぎています", body)
        self.assertIn("24時間以上の猶予", body)
        self.assertNotIn("明日まで", body)
        self.assertIn("2026年07月23日 12:00", body)

    def test_same_day_notice_says_today_and_explains_grace(self):
        _, body = expiry.build_expiry_notice(
            {"title": "テスト", "expire_at": date(2026, 7, 20)},
            public_base_url="https://mfu.example",
            notice_started_at=datetime(2026, 7, 20, 10, 0, tzinfo=expiry.JST),
        )
        self.assertIn("有効期限は本日まで", body)
        self.assertIn("前日通知が遅れた", body)
        self.assertNotIn("明日まで", body)


class FakeCursor:
    def __init__(self):
        self.rowcount = 1
        self.statements = []

    def execute(self, sql, params):
        self.statements.append((" ".join(sql.split()), params))


class FakeDb:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class UploadDeletionSafetyTest(unittest.TestCase):
    def test_rejects_layer_directory_and_traversal(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(deletion.UnsafeUploadPath):
                deletion.resolve_normal_upload_directory(tmp, "layer_uploads")
            with self.assertRaises(deletion.UnsafeUploadPath):
                deletion.resolve_normal_upload_directory(tmp, "../outside")

    def test_rejects_symbolic_link_upload_directory_when_supported(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real-upload").mkdir()
            try:
                (root / "linked-upload").symlink_to(root / "real-upload", target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are not available in this Windows environment")
            with self.assertRaises(deletion.UnsafeUploadPath):
                deletion.resolve_normal_upload_directory(root, "linked-upload")

    def test_deletes_normal_directory_but_not_layer_directory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = root / "abc123"
            layer = root / "layer_uploads" / "abc123"
            normal.mkdir()
            layer.mkdir(parents=True)
            (normal / "image.jpg").write_bytes(b"normal")
            (layer / "image.jpg").write_bytes(b"layer")
            fake_db = FakeDb()

            result = deletion.delete_normal_upload(
                upload_id=17,
                uuid="abc123",
                storage_root=root,
                db_factory=lambda: fake_db,
            )

            self.assertTrue(result["removed_directory"])
            self.assertFalse(normal.exists())
            self.assertTrue((layer / "image.jpg").exists())
            sql = " ".join(statement for statement, _ in fake_db.cursor_instance.statements)
            self.assertIn("UPDATE uploads", sql)
            self.assertNotIn("DELETE FROM uploads", sql)
            self.assertTrue(fake_db.committed)


if __name__ == "__main__":
    unittest.main()
