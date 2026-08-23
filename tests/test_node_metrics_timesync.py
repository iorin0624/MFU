import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "node_metrics_timesync.py"
SPEC = importlib.util.spec_from_file_location("node_metrics_timesync_under_test", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NodeMetricsTimesyncTests(unittest.TestCase):
    def test_parse_microsecond_offset(self):
        parsed = module.parse_timesync_status(
            "Server: 192.168.103.15 (192.168.103.15)\n"
            "Stratum: 2\nOffset: -318us\nDelay: 395us\nJitter: 707us\n"
        )
        self.assertAlmostEqual(parsed["offset_seconds"], -0.000318)
        self.assertEqual(parsed["stratum"], 2)

    def test_parse_millisecond_offset(self):
        parsed = module.parse_timesync_status("Offset: +1.045ms\n")
        self.assertAlmostEqual(parsed["offset_seconds"], 0.001045)


if __name__ == "__main__":
    unittest.main()
