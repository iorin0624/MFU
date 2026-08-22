import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ImageViewerWindowFocusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            ROOT / "image_viewer" / "template" / "image_viewer.html"
        ).read_text(encoding="utf-8-sig")

    def test_next_window_is_selected_by_current_z_order(self):
        self.assertIn("function topVisibleWindow", self.source)
        self.assertIn("Number(candidate.el.style.zIndex) || 0", self.source)
        self.assertIn("candidateZ > topZ", self.source)

    def test_close_activates_top_window_before_explorer_fallback(self):
        self.assertIn(
            "state.activeId === id && !activateTopVisibleWindow()",
            self.source,
        )
        self.assertNotIn(
            "if (state.activeId === id) activate('explorer');",
            self.source,
        )

    def test_minimize_uses_the_same_z_order_selection(self):
        self.assertIn("activateTopVisibleWindow(id);", self.source)
        self.assertNotIn(
            ".filter(w => !w.minimized && w.id !== id).pop()",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
