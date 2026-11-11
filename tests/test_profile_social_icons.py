import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image

    MODULE_PATH = Path(__file__).resolve().parents[1] / "profile" / "social_icons.py"
    spec = importlib.util.spec_from_file_location("profile_social_icons", MODULE_PATH)
    social_icons = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(social_icons)


class Upload:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)
        self.filename = "icon.png"


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is not installed")
class ProfileSocialIconTest(unittest.TestCase):
    def test_transparent_icon_is_kept_and_resized(self):
        original = io.BytesIO()
        Image.new("RGBA", (400, 200), (0, 0, 0, 0)).save(original, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = social_icons.save_social_icon(Upload(original.getvalue()), root=root)
            self.assertTrue(social_icons.valid_icon_file(name))
            with Image.open(root / name) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (256, 256))
                self.assertEqual(image.convert("RGBA").getpixel((10, 10))[3], 0)
            social_icons.remove_social_icon(name, root=root)
            self.assertFalse((root / name).exists())

    def test_rejects_bad_image_and_invalid_icon_name(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                social_icons.save_social_icon(Upload(b"not an image"), root=Path(folder))
        self.assertTrue(social_icons.valid_bootstrap_icon("bi-instagram"))
        self.assertFalse(social_icons.valid_bootstrap_icon("bi-instagram onclick=alert(1)"))

    def test_link_field_validation(self):
        self.assertEqual(
            social_icons.validate_social_link_fields(
                label=" X ", url=" https://example.org/ ", icon_mode="bootstrap",
                icon_name="bi-twitter-x", has_custom_icon=False,
            ),
            ("X", "https://example.org/", "bootstrap", "bi-twitter-x"),
        )
        with self.assertRaises(ValueError):
            social_icons.validate_social_link_fields(
                label="X", url="javascript:alert(1)", icon_mode="bootstrap",
                icon_name="bi-twitter-x", has_custom_icon=False,
            )
        with self.assertRaises(ValueError):
            social_icons.validate_social_link_fields(
                label="X", url="https://example.org", icon_mode="custom",
                icon_name="", has_custom_icon=False,
            )
