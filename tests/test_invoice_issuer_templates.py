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
            "subject": "keep",
        }
        template = {
            "id": 5,
            "issuer_name": "new name",
            "issuer_postal_code": "222-2222",
            "issuer_address1": "new address1",
            "issuer_address2": "new address2",
            "issuer_phone": "9999",
        }

        result = invoice_services.apply_issuer_template_to_form_data(form_data, template)

        self.assertIs(result, form_data)
        self.assertEqual(result["issuer_template_id"], "5")
        self.assertEqual(result["issuer_name"], "new name")
        self.assertEqual(result["issuer_postal_code"], "222-2222")
        self.assertEqual(result["issuer_address1"], "new address1")
        self.assertEqual(result["issuer_address2"], "new address2")
        self.assertEqual(result["issuer_phone"], "9999")
        self.assertEqual(result["subject"], "keep")

    def test_build_invoice_form_data_for_new_invoice_has_template_selector_state(self):
        form_data = invoice_services.build_invoice_form_data()

        self.assertEqual(form_data["issuer_template_id"], "")
        self.assertEqual(form_data["items"][0]["row_type"], "normal")


if __name__ == "__main__":
    unittest.main()
