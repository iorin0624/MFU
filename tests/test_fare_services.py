import unittest
from datetime import date
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "fare" / "services.py"
spec = importlib.util.spec_from_file_location("fare_services", MODULE_PATH)
fare_services = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(fare_services)


class FareServiceTest(unittest.TestCase):
    def test_build_yahoo_url_contains_fixed_params(self):
        url = fare_services.build_yahoo_transit_url("五井", "新宿", date(2026, 3, 18))
        self.assertIn("from=%E4%BA%94%E4%BA%95", url)
        self.assertIn("to=%E6%96%B0%E5%AE%BF", url)
        self.assertIn("y=2026", url)
        self.assertIn("m=03", url)
        self.assertIn("d=18", url)
        self.assertIn("hh=11", url)
        self.assertIn("ex=0", url)

    def test_parse_route1_fare_prefers_ic_label(self):
        html = '<section id="route01"><p>IC優先：1,234円</p></section><section id="route02"><p>IC優先：999円</p></section>'
        self.assertEqual(fare_services.parse_route1_fare(html), 1234)

    def test_parse_route1_fare_fallback_any_fare(self):
        html = '<html><body><div>運賃：560円</div></body></html>'
        self.assertEqual(fare_services.parse_route1_fare(html), 560)

    def test_calculate_total_fare(self):
        result = fare_services.calculate_total_fare(560, 150)
        self.assertEqual(result["round_trip_fare"], 1120)
        self.assertEqual(result["total_fare"], 1270)

    def test_validate_input_errors(self):
        with self.assertRaises(fare_services.FareEstimateError):
            fare_services.validate_destination("   ")
        with self.assertRaises(fare_services.FareEstimateError):
            fare_services.validate_target_date("2026-99-99")
        with self.assertRaises(fare_services.FareEstimateError):
            fare_services.validate_parking_fee("-1")


if __name__ == "__main__":
    unittest.main()
