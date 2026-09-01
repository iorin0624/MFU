import unittest

from utils.traffic_source import (
    PUBLIC_TRAFFIC_SOURCE_MAX_LENGTH,
    normalize_public_traffic_source,
    public_traffic_source_host,
)


class PublicTrafficSourceTests(unittest.TestCase):
    def test_only_public_alias_hosts_are_tracked(self):
        self.assertTrue(public_traffic_source_host("pro.iori0624.jp"))
        self.assertTrue(public_traffic_source_host("suc.iori0624.jp:443"))
        self.assertFalse(public_traffic_source_host("mfu.iori0624.jp"))

    def test_free_form_source_keeps_japanese_and_normalizes_controls(self):
        self.assertEqual(
            normalize_public_traffic_source("  Instagram\n2026 夏企画  "),
            "Instagram 2026 夏企画",
        )

    def test_source_is_bounded_and_blank_clears(self):
        self.assertIsNone(normalize_public_traffic_source("\r\n\t"))
        self.assertEqual(
            len(normalize_public_traffic_source("a" * 200) or ""),
            PUBLIC_TRAFFIC_SOURCE_MAX_LENGTH,
        )
