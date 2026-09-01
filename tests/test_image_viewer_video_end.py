import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO_VIEWER = ROOT / "image_viewer" / "frontend" / "src" / "components" / "VideoViewerWindow.vue"


class ImageViewerVideoEndTests(unittest.TestCase):
    def test_video_end_stays_on_current_file(self):
        source = VIDEO_VIEWER.read_text(encoding="utf-8")
        self.assertIn('@ended="sync"', source)
        self.assertNotIn('@ended="move(1)"', source)


if __name__ == "__main__":
    unittest.main()
