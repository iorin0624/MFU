import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import app
from app.image_viewer import catalog, routes, style_tags


class ImageViewerStyleTagsApiTest(unittest.TestCase):
    def test_save_read_clear_image_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (2, 2), "blue").save(root / "photo.png")
            with (
                patch.object(catalog, "CATALOG_ENABLED", False),
                patch.object(routes, "UPLOAD_ROOT", root),
                patch.object(routes, "THUMB_ROOT", root / ".thumbs"),
                patch.object(style_tags, "DEFAULT_DB_PATH", root / "tags.sqlite3"),
            ):
                # The default argument is bound when style_tags functions are
                # defined, so patch the callable boundary for test isolation.
                original_get = style_tags.get_assignment
                original_put = style_tags.put_assignment
                original_delete = style_tags.delete_assignment
                db_path = root / "tags.sqlite3"
                with (
                    patch.object(style_tags, "get_assignment", side_effect=lambda sha: original_get(sha, db_path)),
                    patch.object(style_tags, "put_assignment", side_effect=lambda sha, value: original_put(sha, value, db_path)),
                    patch.object(style_tags, "delete_assignment", side_effect=lambda sha: original_delete(sha, db_path)),
                ):
                    client = app.test_client()
                    with client.session_transaction() as session:
                        session["user"] = "style-tag-test-user"
                        session["csrf_token"] = "style-tag-test"
                    url = "/image_viewer/api/entries/style-tags"
                    self.assertIsNone(client.get(url, query_string={"path": "photo.png"}).json["assignment"])
                    saved = client.post(url, json={
                        "path": "photo.png", "action": "save", "assignment": {
                            "major": "casual", "middleTags": [
                                {"name": "ディズニー", "smallTags": ["アナ風", "青"]},
                            ],
                        },
                    }, headers={"X-CSRF-Token": "style-tag-test"})
                    self.assertEqual(saved.status_code, 200, saved.json)
                    loaded = client.get(url, query_string={"path": "photo.png"})
                    self.assertEqual(loaded.json["assignment"]["middleTags"][0]["smallTags"], ["アナ風", "青"])
                    cleared = client.post(url, json={"path": "photo.png", "action": "clear"},
                                          headers={"X-CSRF-Token": "style-tag-test"})
                    self.assertEqual(cleared.status_code, 200, cleared.json)
                    self.assertIsNone(client.get(url, query_string={"path": "photo.png"}).json["assignment"])


if __name__ == "__main__":
    unittest.main()
