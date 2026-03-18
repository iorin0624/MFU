import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_invoice_services_module():
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_module = types.ModuleType("app.utils")
    app_utils_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_db_module = types.ModuleType("app.utils.db")
    app_utils_db_module.get_db = lambda: None
    sys.modules["app"] = app_module
    sys.modules["app.utils"] = app_utils_module
    sys.modules["app.utils.db"] = app_utils_db_module

    invoice_package = types.ModuleType("invoice")
    invoice_package.__path__ = [str(repo_root / "invoice")]  # type: ignore[attr-defined]
    sys.modules["invoice"] = invoice_package

    utils_spec = importlib.util.spec_from_file_location("invoice.utils", repo_root / "invoice" / "utils.py")
    utils_module = importlib.util.module_from_spec(utils_spec)
    assert utils_spec and utils_spec.loader
    sys.modules["invoice.utils"] = utils_module
    utils_spec.loader.exec_module(utils_module)

    services_spec = importlib.util.spec_from_file_location("invoice.services", repo_root / "invoice" / "services.py")
    services_module = importlib.util.module_from_spec(services_spec)
    assert services_spec and services_spec.loader
    sys.modules["invoice.services"] = services_module
    services_spec.loader.exec_module(services_module)
    return services_module


invoice_services = load_invoice_services_module()


class InvoiceIssuerTemplateTest(unittest.TestCase):
    def test_apply_issuer_template_to_form_data_overwrites_target_fields_only(self):
        form_data = {
            "issuer_name": "old name",
            "issuer_postal_code": "111-1111",
            "issuer_address1": "old address1",
            "issuer_address2": "old address2",
            "issuer_phone": "0000",
            "issuer_email": "old@example.com",
            "bank_info": "旧振込先",
            "note": "旧備考",
            "subject": "keep",
        }
        template = {
            "id": 5,
            "issuer_name": "new name",
            "issuer_postal_code": "222-2222",
            "issuer_address1": "new address1",
            "issuer_address2": "new address2",
            "issuer_phone": "9999",
            "issuer_email": "new@example.com",
            "bank_info": "銀行A\n支店B",
            "note": "1行目\n2行目",
        }

        result = invoice_services.apply_issuer_template_to_form_data(form_data, template)

        self.assertIs(result, form_data)
        self.assertEqual(result["issuer_template_id"], "5")
        self.assertEqual(result["issuer_name"], "new name")
        self.assertEqual(result["issuer_postal_code"], "222-2222")
        self.assertEqual(result["issuer_address1"], "new address1")
        self.assertEqual(result["issuer_address2"], "new address2")
        self.assertEqual(result["issuer_phone"], "9999")
        self.assertEqual(result["issuer_email"], "new@example.com")
        self.assertEqual(result["bank_info"], "銀行A\n支店B")
        self.assertEqual(result["note"], "1行目\n2行目")
        self.assertEqual(result["subject"], "keep")

    def test_build_invoice_form_data_for_new_invoice_has_template_selector_state(self):
        form_data = invoice_services.build_invoice_form_data()

        self.assertEqual(form_data["issuer_template_id"], "")
        self.assertEqual(form_data["items"][0]["row_type"], "normal")

    def test_build_issuer_template_form_data_has_defaults(self):
        form_data = invoice_services.build_issuer_template_form_data()

        self.assertEqual(form_data["template_name"], "")
        self.assertEqual(form_data["issuer_name"], "")
        self.assertEqual(form_data["issuer_email"], "")
        self.assertEqual(form_data["bank_info"], "")
        self.assertEqual(form_data["note"], "")
        self.assertEqual(form_data["sort_order"], "0")
        self.assertEqual(form_data["is_default"], "")

    def test_normalize_multiline_text_preserves_internal_newlines(self):
        result = invoice_services.normalize_multiline_text("\n  1行目\r\n2行目\r3行目  \n")

        self.assertEqual(result, "1行目\n2行目\n3行目")

    def test_parse_issuer_template_form_validates_required_fields_and_sort_order(self):
        with self.assertRaises(invoice_services.InvoiceValidationError):
            invoice_services._parse_issuer_template_form({"template_name": "", "issuer_name": "発行者", "sort_order": "0"})
        with self.assertRaises(invoice_services.InvoiceValidationError):
            invoice_services._parse_issuer_template_form({"template_name": "名称", "issuer_name": "", "sort_order": "0"})
        with self.assertRaises(invoice_services.InvoiceValidationError):
            invoice_services._parse_issuer_template_form({"template_name": "名称", "issuer_name": "発行者", "sort_order": "abc"})

        payload = invoice_services._parse_issuer_template_form(
            {
                "template_name": "事業用住所",
                "issuer_name": "テスト発行者",
                "issuer_postal_code": "123-4567",
                "issuer_address1": "東京都",
                "issuer_address2": "テストビル",
                "issuer_phone": "03-0000-0000",
                "issuer_email": "issuer@example.com",
                "bank_info": "\n三菱UFJ銀行\n渋谷支店\n",
                "note": "備考1\r\n備考2",
                "sort_order": "10",
                "is_default": "1",
            }
        )

        self.assertEqual(payload["template_name"], "事業用住所")
        self.assertEqual(payload["issuer_name"], "テスト発行者")
        self.assertEqual(payload["issuer_email"], "issuer@example.com")
        self.assertEqual(payload["bank_info"], "三菱UFJ銀行\n渋谷支店")
        self.assertEqual(payload["note"], "備考1\n備考2")
        self.assertEqual(payload["sort_order"], 10)
        self.assertEqual(payload["is_default"], 1)

    def test_build_invoice_mail_recipient_label_uses_full_width_space_and_suffix_once(self):
        result = invoice_services.build_invoice_mail_recipient_label("株式会社サンプル", "山田太郎", "様")
        self.assertEqual(result, "株式会社サンプル　山田太郎様")

    def test_build_invoice_mail_recipient_label_handles_missing_parts(self):
        self.assertEqual(invoice_services.build_invoice_mail_recipient_label("株式会社サンプル", "", "御中"), "株式会社サンプル御中")
        self.assertEqual(invoice_services.build_invoice_mail_recipient_label("", "山田太郎", "様"), "山田太郎様")
        self.assertEqual(invoice_services.build_invoice_mail_recipient_label("", "", ""), "お客様")

    def test_build_default_invoice_mail_body_prefers_snapshots(self):
        body = invoice_services.build_default_invoice_mail_body(
            {
                "contact_name_snapshot": "テスト株式会社",
                "contact_person_snapshot": "テスト太郎",
                "contact_honorific_snapshot": "様",
                "contact_name": "fallback company",
                "contact_person": "fallback person",
                "honorific": "御中",
                "issuer_name": "いおりん写真室（小松　伊織）",
            }
        )
        self.assertEqual(
            body,
            "いつもお世話になっております、テスト株式会社　テスト太郎様\n"
            "いおりん写真室（小松　伊織）です。\n\n"
            "請求書をお送りいたします。ご確認のほどよろしくお願いいたします。",
        )


if __name__ == "__main__":
    unittest.main()
