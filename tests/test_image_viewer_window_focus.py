import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ImageViewerWindowFocusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            ROOT / "image_viewer" / "frontend" / "src" / "stores" / "desktop.ts"
        ).read_text(encoding="utf-8-sig")

    def test_next_window_is_selected_by_current_z_order(self):
        self.assertIn(".sort((a, b) => b.z - a.z)[0]", self.source)

    def test_close_activates_top_window_before_explorer_fallback(self):
        self.assertIn("function close(id: string)", self.source)
        self.assertIn("activeId.value = next?.id || ''", self.source)

    def test_minimize_uses_the_same_z_order_selection(self):
        self.assertIn("function minimize(id: string)", self.source)
        self.assertIn("entry.id !== id).sort((a, b) => b.z - a.z)[0]", self.source)


if __name__ == "__main__":
    unittest.main()
