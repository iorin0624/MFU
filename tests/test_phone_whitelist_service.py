import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "phone_whitelist" / "service.py"
spec = importlib.util.spec_from_file_location("phone_whitelist_service", MODULE_PATH)
service = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(service)


class PhoneWhitelistServiceTest(unittest.TestCase):
    def test_normalize_domestic_hyphen_and_e164(self):
        self.assertEqual(service.normalize_phone_number("080-9324-2655"), "08093242655")
        self.assertEqual(service.normalize_phone_number("+81 80 9324 2655"), "08093242655")
        self.assertEqual(service.normalize_phone_number("０８０－９３２４－２６５５"), "08093242655")

    def test_reject_invalid_number(self):
        with self.assertRaises(service.WhitelistValidationError):
            service.normalize_phone_number("anonymous")

    def test_parse_utf8_csv(self):
        rows = service.parse_csv_bytes(
            "phone_number,name,note\n080-9324-2655,テスト,確認用\n0436252137,会社,代表\n".encode("utf-8")
        )
        self.assertEqual(rows[0]["phone_number"], "08093242655")
        self.assertEqual(rows[1]["name"], "会社")

    def test_parse_japanese_headers(self):
        rows = service.parse_csv_bytes("電話番号,名称,備考\n+818093242655,担当者,携帯\n".encode("utf-8"))
        self.assertEqual(rows[0]["phone_number"], "08093242655")

    def test_reject_duplicate_after_normalization(self):
        data = "phone_number,name,note\n08093242655,A,\n+818093242655,B,\n".encode("utf-8")
        with self.assertRaises(service.WhitelistValidationError):
            service.parse_csv_bytes(data)

    def test_build_payload_is_unique_and_sorted(self):
        payload = service.build_pbx_payload(
            [
                {"phone_number": "09000000000", "name": "テスト会社"},
                {"phone_number": "08093242655", "name": "自分の電話番号"},
                {"phone_number": "09000000000", "name": "テスト会社"},
            ],
            blacklist_numbers=[
                {"phone_number": "+81 43 625 2137", "name": "千葉南警察署　鎌取駅前交番"},
                {"phone_number": "0436252137", "name": "千葉南警察署　鎌取駅前交番"},
                "09011112222",
            ],
            whitelist_disabled_until=123,
            anonymous_allowed_until=456,
        )
        self.assertEqual(
            payload,
            "# Managed by MFU.2 phone whitelist\n"
            "# MFU_WHITELIST_DISABLED_UNTIL=123\n"
            "# MFU_ANONYMOUS_ALLOWED_UNTIL=456\n"
            "08093242655|6Ieq5YiG44Gu6Zu76Kmx55Wq5Y+3\n"
            "09000000000|44OG44K544OI5Lya56S+\n"
            "B|0436252137|5Y2D6JGJ5Y2X6K2m5a+f572y44CA6Y6M5Y+W6aeF5YmN5Lqk55Wq\n"
            "B|09011112222|\n",
        )

    def test_sanitize_sip_caller_name(self):
        self.assertEqual(service.sanitize_sip_caller_name("会社\r\nInjected"), "会社Injected")
        self.assertEqual(len(service.sanitize_sip_caller_name("あ" * 40)), 32)


if __name__ == "__main__":
    unittest.main()
