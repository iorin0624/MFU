"""Opt-in live CRUD smoke test; uses a hidden row and cleans it up."""

import io
import os
import unittest
import uuid


class Upload:
    filename = "validation.png"

    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)


@unittest.skipUnless(os.environ.get("MFU_PROFILE_SOCIAL_INTEGRATION") == "1", "live DB test disabled")
class ProfileSocialIntegrationTest(unittest.TestCase):
    def test_hidden_link_and_custom_icon_lifecycle(self):
        from PIL import Image
        from app import app
        from app.profile.social_icons import SOCIAL_ICON_ROOT, remove_social_icon, save_social_icon
        from app.profile.services import (
            delete_social_link,
            get_main_profile,
            list_social_links,
            save_social_link,
            social_icon_is_visible,
        )

        profile_id = get_main_profile()["id"]
        label = f"内部検証-{uuid.uuid4().hex[:8]}"
        icon_file = None
        link_id = None
        try:
            save_social_link(profile_id, {
                "label": label, "url": "https://example.invalid/", "icon_mode": "bootstrap",
                "icon_name": "bi-link-45deg", "icon_file": None, "is_visible": 0,
            })
            matches = [row for row in list_social_links(profile_id) if row["label"] == label]
            self.assertEqual(len(matches), 1)
            link_id = matches[0]["id"]

            data = io.BytesIO()
            Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(data, format="PNG")
            icon_file = save_social_icon(Upload(data.getvalue()))
            previous = save_social_link(profile_id, {
                "label": label, "url": "https://example.invalid/", "icon_mode": "custom",
                "icon_name": None, "icon_file": icon_file, "is_visible": 0,
            }, link_id=link_id)
            self.assertIsNone(previous)
            self.assertTrue((SOCIAL_ICON_ROOT / icon_file).exists())
            self.assertFalse([row for row in list_social_links(profile_id, visible_only=True) if row["id"] == link_id])
            client = app.test_client()
            self.assertEqual(client.get(f"/profile/social-icon/{icon_file}").status_code, 404)
            self.assertTrue(social_icon_is_visible(icon_file, include_hidden=True))

            previous = save_social_link(profile_id, {
                "label": label, "url": "https://example.invalid/", "icon_mode": "bootstrap",
                "icon_name": "bi-camera", "icon_file": None, "is_visible": 0,
            }, link_id=link_id)
            self.assertEqual(previous, icon_file)
            remove_social_icon(previous)
            icon_file = None
            self.assertEqual(delete_social_link(profile_id, link_id), None)
            link_id = None
        finally:
            if link_id is not None:
                try:
                    remove_social_icon(delete_social_link(profile_id, link_id))
                except LookupError:
                    pass
            if icon_file is not None:
                remove_social_icon(icon_file)
