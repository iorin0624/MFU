from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CLIPBOARD_PATH = ROOT / "tools" / "mfu_media_clipboard" / "main.py"
HUB_SERVER_PATH = ROOT / "utils" / "media_hub.py"
HUB_CLIENT_PATH = ROOT / "tools" / "mfu_media_hub" / "main.py"


def _load_clipboard():
    spec = importlib.util.spec_from_file_location("mfu_media_hub_clipboard_test", CLIPBOARD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_progress_text_shows_live_item_count_before_and_after_total_is_known():
    module = _load_clipboard()
    client = module.ApiClient()

    assert client._progress_text({"processed": 12}) == "取得中... 12枚取得"
    assert client._progress_text({"processed": 12, "total": 38}) == "取得中... 12/38枚"
    assert client._progress_text({"processed": 12, "total": 38, "downloaded": 11, "failed": 1}) == (
        "取得中... 12/38枚  成功:11  失敗:1"
    )


def test_server_limits_websocket_to_explicit_media_operations():
    source = HUB_SERVER_PATH.read_text(encoding="utf-8")
    assert 'namespace="/media-clipboard"' in source
    assert '"fetch_images": ("POST", "/image_viewer/api/instagram/fetch")' in source
    assert '"save_videos": ("POST", "/image_viewer/api/video/save-async")' in source
    assert '"unsupported_operation"' in source
    assert 'emit("media_clipboard_progress", current)' in source


def test_integrated_client_uses_one_receiver_and_context_aware_notification_click():
    source = HUB_CLIENT_PATH.read_text(encoding="utf-8")
    clipboard_source = CLIPBOARD_PATH.read_text(encoding="utf-8")
    assert "ReceiverThread(self.photo_settings, self.photo_token, self.media_token)" in source
    assert "self.photo_receiver.media_progress.connect(self.api.handle_websocket_progress)" in source
    assert "self.api.websocket_rpc = self.photo_receiver.media_call" in source
    assert "self._open_saved_folder(value)" in source
    assert "self.notification_action" in clipboard_source
    assert "URL {self.batch_index + 1}/{len(self.batch_urls)}" in clipboard_source


def test_unified_login_issues_both_scoped_tokens_and_dpapi_store_is_used():
    server = HUB_SERVER_PATH.read_text(encoding="utf-8")
    client = HUB_CLIENT_PATH.read_text(encoding="utf-8")
    assert "_issue_device_token" in server
    assert "issue_media_clipboard_token" in server
    assert '"photo_token": photo_token' in server
    assert '"media_token": media_token' in server
    assert "CryptProtectData" in client
    assert "CryptUnprotectData" in client
