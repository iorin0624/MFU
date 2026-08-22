import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_invoice_pdf_module():
    repo_root = Path(__file__).resolve().parents[1]
    package_name = "invoice_pdf_recipient_test"

    package = types.ModuleType(package_name)
    package.__path__ = [str(repo_root / "invoice")]  # type: ignore[attr-defined]

    flask_module = types.ModuleType("flask")
    flask_module.current_app = None
    flask_module.render_template = lambda *args, **kwargs: ""

    services_module = types.ModuleType(f"{package_name}.services")
    services_module.get_invoice_effective_bank_info = lambda invoice: ""
    services_module.mark_invoice_pdf_generated = lambda *args, **kwargs: None
    services_module.normalize_multiline_text = lambda value: value

    utils_module = types.ModuleType(f"{package_name}.utils")
    for name in (
        "ensure_dir",
        "format_currency_yen",
        "format_ymd",
        "format_jp_date",
        "format_quantity",
        "internal_pdf_filename",
        "sanitize_filename_component",
        "visible_pdf_filename",
    ):
        setattr(utils_module, name, lambda *args, **kwargs: "")

    module_name = f"{package_name}.pdf"
    spec = importlib.util.spec_from_file_location(module_name, repo_root / "invoice" / "pdf.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            package_name: package,
            "flask": flask_module,
            f"{package_name}.services": services_module,
            f"{package_name}.utils": utils_module,
            module_name: module,
        },
    ):
        spec.loader.exec_module(module)
    return module


invoice_pdf = load_invoice_pdf_module()


class InvoicePdfRecipientTest(unittest.TestCase):
    def test_honorific_stays_on_same_line_when_contact_has_no_person(self):
        lines = invoice_pdf._build_contact_lines(
            {
                "contact_name_snapshot": "株式会社テスト",
                "contact_honorific_snapshot": "様",
                "contact_email_snapshot": "contact@example.jp",
            }
        )

        self.assertEqual(lines, ["株式会社テスト 様", "Email: contact@example.jp"])

    def test_person_and_honorific_stay_on_separate_line_from_organization(self):
        lines = invoice_pdf._build_contact_lines(
            {
                "contact_name_snapshot": "株式会社テスト",
                "contact_department_snapshot": "営業部",
                "contact_person_snapshot": "山田 太郎",
                "contact_honorific_snapshot": "様",
            }
        )

        self.assertEqual(lines, ["株式会社テスト 営業部", "山田 太郎 様"])


if __name__ == "__main__":
    unittest.main()
