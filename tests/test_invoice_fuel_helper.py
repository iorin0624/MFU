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


class InvoiceFuelHelperTest(unittest.TestCase):
    def test_calculate_yen_per_km_uses_decimal_ceiling(self):
        self.assertEqual(invoice_services.calculate_yen_per_km_from_fuel_log("100", "3", "170"), 6)
        self.assertEqual(invoice_services.calculate_yen_per_km_from_fuel_log("34", "1", "170"), 5)

    def test_calculate_yen_per_km_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            invoice_services.calculate_yen_per_km_from_fuel_log("0", "3", "170")
        with self.assertRaises(ValueError):
            invoice_services.calculate_yen_per_km_from_fuel_log("100", "-1", "170")
        with self.assertRaises(ValueError):
            invoice_services.calculate_yen_per_km_from_fuel_log("100", "3", "")

    def test_build_fuel_cost_helper_returns_unavailable_for_invalid_latest_record(self):
        original = invoice_services.get_latest_bike_fuel_log
        try:
            invoice_services.get_latest_bike_fuel_log = lambda: {
                "trip_km": "0",
                "liters": "3.2",
                "yen_per_liter": "180",
            }
            helper = invoice_services.build_fuel_cost_helper()
        finally:
            invoice_services.get_latest_bike_fuel_log = original

        self.assertFalse(helper["available"])
        self.assertEqual(helper["message"], "最新の燃費記録から単価を計算できません")


if __name__ == "__main__":
    unittest.main()
