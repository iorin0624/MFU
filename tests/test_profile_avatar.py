import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
if PIL_AVAILABLE:
    from PIL import Image

    MODULE_PATH = Path(__file__).resolve().parents[1] / "profile" / "avatar.py"
    spec = importlib.util.spec_from_file_location("profile_avatar", MODULE_PATH)
    profile_avatar = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(profile_avatar)


class Upload:
    def __init__(self, data: bytes, filename: str = "portrait.jpg"):
        self.stream = io.BytesIO(data)
        self.filename = filename


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is not installed")
class ProfileAvatarTest(unittest.TestCase):
    def test_valid_image_becomes_square_webp(self):
        original = io.BytesIO()
        Image.new("RGB", (1200, 800), "#c02060").save(original, format="JPEG")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = profile_avatar.save_profile_avatar(Upload(original.getvalue()), root=root)
            self.assertTrue(profile_avatar.valid_avatar_name(name))
            with Image.open(root / name) as saved:
                self.assertEqual(saved.format, "WEBP")
                self.assertEqual(saved.size, (1024, 1024))
            profile_avatar.remove_profile_avatar(name, root=root)
            self.assertFalse((root / name).exists())

    def test_invalid_image_and_unsafe_name_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                profile_avatar.save_profile_avatar(Upload(b"not an image"), root=root)
            with self.assertRaises(ValueError):
                profile_avatar.save_profile_avatar(Upload(b"x" * (8 * 1024 * 1024 + 1)), root=root)
            self.assertFalse(profile_avatar.valid_avatar_name("../other.webp"))
            profile_avatar.remove_profile_avatar("../other.webp", root=root)
            self.assertEqual(list(root.iterdir()), [])
