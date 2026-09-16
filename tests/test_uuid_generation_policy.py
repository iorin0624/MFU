import re
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_UUID_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)


class UuidGenerationPolicyTests(unittest.TestCase):
    def test_upload_uuid_validator_keeps_legacy_and_accepts_canonical(self):
        legacy = uuid.uuid4().hex
        canonical = str(uuid.uuid4())
        self.assertEqual(len(legacy), 32)
        self.assertEqual(len(canonical), 36)
        self.assertIsNotNone(UPLOAD_UUID_RE.fullmatch(legacy))
        self.assertIsNotNone(UPLOAD_UUID_RE.fullmatch(canonical))

    def test_new_external_users_and_events_do_not_use_database_uuid_v1(self):
        for relative in (
            "external_login_user/admin.py",
            "external_login_user/users.py",
            "external_login_user/utils.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("UNHEX(REPLACE(UUID(),'-',''))", source, relative)

    def test_new_upload_ids_use_hyphenated_uuid4(self):
        main_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        api_source = (ROOT / "utils/ext_api_uploads.py").read_text(encoding="utf-8")
        self.assertIn("uid = str(uuid4())", main_source)
        self.assertIn("uid = str(uuid.uuid4())", main_source)
        self.assertIn("uuid32 = str(_uuid.uuid4())", api_source)


if __name__ == "__main__":
    unittest.main()
