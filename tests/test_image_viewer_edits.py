from __future__ import annotations

import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image


MODULE = Path(__file__).resolve().parents[1] / "image_viewer" / "image_edits.py"
spec = importlib.util.spec_from_file_location("image_viewer_edits_test", MODULE)
edits = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(edits)
transfer_spec = importlib.util.spec_from_file_location(
    "image_viewer_smb_transfer_test", MODULE.with_name("smb_transfer.py"),
)
transfer = importlib.util.module_from_spec(transfer_spec)
assert transfer_spec.loader is not None
transfer_spec.loader.exec_module(transfer)


class ImageEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_crop_reuses_path_and_restores_first_original(self):
        active = self.root / "images" / "photo.png"
        active.parent.mkdir()
        Image.new("RGB", (200, 100), "red").save(active)
        original_bytes = active.read_bytes()
        original = edits.backup_path(self.root / "private", "images/photo.png", ".png")
        edits.edit_image(active, original, rectangle={"x": .25, "y": .1, "width": .5, "height": .8})
        with Image.open(active) as cropped:
            self.assertEqual(cropped.size, (100, 80))
        self.assertEqual(original.read_bytes(), original_bytes)
        edits.edit_image(active, original, rectangle={"x": 0, "y": 0, "width": .5, "height": .5})
        self.assertEqual(original.read_bytes(), original_bytes)
        edits.edit_image(active, original, restore=True)
        self.assertEqual(active.read_bytes(), original_bytes)
        self.assertTrue(original.is_file())

    def test_invalid_crop_does_not_change_image(self):
        active = self.root / "photo.jpg"
        Image.new("RGB", (20, 20), "blue").save(active)
        before = active.read_bytes()
        original = edits.backup_path(self.root / "private", "photo.jpg", ".jpg")
        with self.assertRaises(edits.ImageEditError):
            edits.edit_image(active, original, rectangle={"x": .9, "y": 0, "width": .5, "height": 1})
        self.assertEqual(active.read_bytes(), before)
        self.assertFalse(original.exists())

    def test_metadata_failure_rolls_back_bytes(self):
        active = self.root / "photo.png"
        Image.new("RGB", (30, 30), "green").save(active)
        before = active.read_bytes()
        original = edits.backup_path(self.root / "private", "photo.png", ".png")
        def fail(*_):
            raise RuntimeError("metadata failed")
        with self.assertRaises(RuntimeError):
            edits.edit_image(active, original, rectangle={"x": 0, "y": 0, "width": .5, "height": .5}, after_replace=fail)
        self.assertEqual(active.read_bytes(), before)
        self.assertFalse(original.exists())

    def test_backup_moves_with_filename(self):
        root = self.root / "private"
        source = edits.backup_path(root, "old/a.png", ".png")
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original")
        edits.move_backup(root, "old/a.png", "new/b.png", ".png")
        self.assertFalse(source.exists())
        self.assertEqual(edits.backup_path(root, "new/b.png", ".png").read_bytes(), b"original")

    def test_smb_transfer_never_overwrites_existing_name(self):
        source = self.root / "photo.png"
        source.write_bytes(b"new image")
        share = self.root / "share"
        share.mkdir()
        (share / "photo.png").write_bytes(b"existing image")
        with patch.object(transfer.os.path, "ismount", return_value=True):
            saved = transfer.copy_image(source, "photo.png", share)
        self.assertEqual(saved, "photo (1).png")
        self.assertEqual((share / "photo.png").read_bytes(), b"existing image")
        self.assertEqual((share / saved).read_bytes(), b"new image")

    def test_smb_transfer_rejects_unmounted_directory(self):
        source = self.root / "photo.png"
        source.write_bytes(b"image")
        share = self.root / "share"
        share.mkdir()
        with patch.object(transfer.os.path, "ismount", return_value=False):
            with self.assertRaises(transfer.TransferError):
                transfer.copy_image(source, "photo.png", share)


if __name__ == "__main__":
    unittest.main()
