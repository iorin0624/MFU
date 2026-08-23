import unittest
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "chrony_monitor.py"
SPEC = importlib.util.spec_from_file_location("chrony_monitor_under_test", MODULE_PATH)
chrony_monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chrony_monitor)
classify_client = chrony_monitor.classify_client
parse_clients = chrony_monitor.parse_clients
parse_sources = chrony_monitor.parse_sources
parse_tracking = chrony_monitor.parse_tracking


TRACKING = """Reference ID    : 0A545792 (ntp-a3.nict.go.jp)
Stratum         : 2
System time     : 0.000010054 seconds slow of NTP time
Update interval : 65.2 seconds
Leap status     : Normal
"""

SOURCES = """MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* ntp-a3.nict.go.jp             1   6   377    93   -506us[ -458us] +/- 2696us
^- 133.40.41.134                 2   6   377    27    -92us[  -92us] +/-   37ms
"""

CLIENTS = """Hostname                      NTP   Drop Int IntL Last     Cmd   Drop Int  Last
===============================================================================
localhost                       0      0   -   -     -      14      0   6    15
sip.iori0624.jp                 1      0   -   -   260       0      0   -     -
"""

CLIENTS_NUMERIC = """Hostname                      NTP   Drop Int IntL Last     Cmd   Drop Int  Last
===============================================================================
127.0.0.1                       0      0   -   -     -      14      0   6    15
192.168.103.21                  1      0   -   -   260       0      0   -     -
"""


class ChronyMonitorParserTests(unittest.TestCase):
    def test_tracking_slow_time_is_negative(self):
        parsed = parse_tracking(TRACKING)
        self.assertEqual(parsed["stratum"], 2)
        self.assertAlmostEqual(parsed["system_time_seconds"], -0.000010054)
        self.assertEqual(parsed["leap_status"], "Normal")

    def test_sources_parses_state(self):
        parsed = parse_sources(SOURCES)
        self.assertEqual(parsed[0]["state"], "*")
        self.assertEqual(parsed[1]["name"], "133.40.41.134")

    def test_clients_merges_hostname_and_numeric_address(self):
        parsed = parse_clients(CLIENTS, CLIENTS_NUMERIC)
        self.assertEqual(parsed[1]["address"], "192.168.103.21")
        self.assertEqual(parsed[1]["hostname"], "sip.iori0624.jp")
        self.assertEqual(parsed[1]["status"], "active")

    def test_client_status_thresholds(self):
        self.assertEqual(classify_client(10, 6, 1, 0), "active")
        self.assertEqual(classify_client(600, None, 1, 0), "warning")
        self.assertEqual(classify_client(1000, None, 1, 0), "stale")
        self.assertEqual(classify_client(None, None, 0, 0), "unknown")
        self.assertEqual(classify_client(10, 6, 1, 1), "warning")


if __name__ == "__main__":
    unittest.main()
