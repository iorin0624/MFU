import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from app.etc_accounting import browser_session as etc_browser
from app.image_viewer import routes as image_viewer_routes


def test_etc_browser_has_dedicated_runtime_defaults():
    assert etc_browser.DEBUG_PORT != image_viewer_routes.INSTAGRAM_BROWSER_DEBUG_PORT
    assert etc_browser.ETC_BROWSER_PROFILE_DIR != image_viewer_routes.INSTAGRAM_BROWSER_PROFILE_DIR
    assert etc_browser.ETC_BROWSER_DISPLAY != image_viewer_routes.INSTAGRAM_BROWSER_DISPLAY
    assert etc_browser.ETC_BROWSER_NOVNC_PORT != image_viewer_routes.INSTAGRAM_BROWSER_NOVNC_PORT


def test_ensure_shared_browser_starts_etc_browser_only():
    expected = {"running": True, "url": "http://etc-browser/"}
    with patch.object(etc_browser, "_start_etc_browser", return_value=expected) as start:
        assert etc_browser.ensure_shared_browser() == expected
    start.assert_called_once_with()


def test_etc_browser_start_reuses_existing_debug_browser():
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        started_names = []

        def fake_start(name, command, env=None):
            started_names.append(name)
            return {"xvfb": 201, "x11vnc": 203, "novnc": 204}[name], False

        with (
            patch.object(etc_browser, "ETC_BROWSER_STATE_DIR", root / "state"),
            patch.object(etc_browser, "ETC_BROWSER_ROOT", root / "auth"),
            patch.object(etc_browser, "ETC_BROWSER_PROFILE_DIR", root / "profile"),
            patch.object(etc_browser, "ETC_BROWSER_HOME_DIR", root / "home"),
            patch.object(etc_browser, "_vnc_password", return_value="test-password"),
            patch.object(etc_browser, "_browser_debug_ready", return_value=True),
            patch.object(etc_browser, "_running_browser_pid", return_value=202),
            patch.object(etc_browser, "_start_process", side_effect=fake_start),
            patch.object(etc_browser, "_wait_browser_debug"),
            patch.object(etc_browser, "_remove_duplicate_browser_processes"),
            patch.object(etc_browser, "_write_pid"),
        ):
            result = etc_browser._start_etc_browser_locked()

    assert result["running"] is True
    assert started_names == ["xvfb", "x11vnc", "novnc"]


def test_etc_browser_repairs_stale_component_pid_file():
    with (
        patch.object(etc_browser, "_read_pid", return_value=999),
        patch.object(etc_browser, "_process_matches", return_value=False),
        patch.object(etc_browser, "_matching_processes", return_value=[2179645]),
        patch.object(etc_browser, "_write_pid") as write_pid,
        patch.object(etc_browser, "_pid_path", return_value=Mock(unlink=Mock())),
    ):
        assert etc_browser._resolve_component_pid("x11vnc") == 2179645

    write_pid.assert_called_once_with("x11vnc", 2179645)


def test_etc_browser_process_match_excludes_renderer_children():
    main_command = (
        "chromium "
        f"--user-data-dir={etc_browser.ETC_BROWSER_PROFILE_DIR} "
        f"--remote-debugging-port={etc_browser.DEBUG_PORT}"
    )
    tokens = etc_browser._component_tokens("chromium")
    forbidden = etc_browser._component_forbidden_tokens("chromium")
    with patch.object(etc_browser, "_process_cmdline", return_value=main_command):
        assert etc_browser._process_matches(200, tokens, forbidden)
    with patch.object(etc_browser, "_process_cmdline", return_value=f"{main_command} --type=renderer"):
        assert not etc_browser._process_matches(201, tokens, forbidden)


def test_instagram_page_target_is_activated_before_use():
    response = Mock()
    response.json.return_value = [
        {
            "type": "page",
            "id": "etc-target",
            "url": "https://www2.etc-meisai.jp/etc/",
            "webSocketDebuggerUrl": "ws://etc",
        },
        {
            "type": "page",
            "id": "instagram-target",
            "url": "https://www.instagram.com/p/example/",
            "webSocketDebuggerUrl": "ws://instagram",
        },
    ]
    with (
        patch.object(image_viewer_routes.requests, "get", return_value=response),
        patch.object(image_viewer_routes, "_browser_cdp_call") as cdp_call,
    ):
        assert image_viewer_routes._browser_page_ws_url() == "ws://instagram"
    cdp_call.assert_called_once_with("Target.activateTarget", {"targetId": "instagram-target"})


def test_instagram_idle_target_can_be_reused_for_next_fetch():
    response = Mock()
    response.json.return_value = [
        {
            "type": "page",
            "id": "instagram-idle-target",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://instagram-idle",
        }
    ]
    with (
        patch.object(image_viewer_routes.requests, "get", return_value=response),
        patch.object(image_viewer_routes, "_browser_cdp_call") as cdp_call,
    ):
        assert image_viewer_routes._browser_page_ws_url() == "ws://instagram-idle"
    cdp_call.assert_called_once_with("Target.activateTarget", {"targetId": "instagram-idle-target"})


def test_instagram_browser_start_reuses_existing_debug_browser():
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        started_names = []

        def fake_start(name, command, env=None):
            started_names.append(name)
            return {"xvfb": 101, "x11vnc": 103, "novnc": 104}[name], False

        with (
            patch.object(image_viewer_routes, "INSTAGRAM_AUTH_DIR", root / "auth"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_STATE_DIR", root / "state"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_PROFILE_DIR", root / "profile"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_HOME_DIR", root / "home"),
            patch.object(image_viewer_routes, "_instagram_vnc_password", return_value="test-password"),
            patch.object(image_viewer_routes, "_instagram_browser_debug_ready", return_value=True),
            patch.object(image_viewer_routes, "_running_instagram_chromium_pid", return_value=102),
            patch.object(image_viewer_routes, "_start_process", side_effect=fake_start),
            patch.object(image_viewer_routes, "_wait_instagram_browser_debug"),
            patch.object(image_viewer_routes, "_remove_duplicate_instagram_chromium"),
            patch.object(image_viewer_routes, "_write_pid_value"),
        ):
            result = image_viewer_routes._start_instagram_browser_locked()

    assert result["running"] is True
    assert started_names == ["xvfb", "x11vnc", "novnc"]


def test_instagram_browser_start_rejects_unrelated_debug_listener_before_spawning():
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        with (
            patch.object(image_viewer_routes, "INSTAGRAM_AUTH_DIR", root / "auth"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_STATE_DIR", root / "state"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_PROFILE_DIR", root / "profile"),
            patch.object(image_viewer_routes, "INSTAGRAM_BROWSER_HOME_DIR", root / "home"),
            patch.object(image_viewer_routes, "_instagram_vnc_password", return_value="test-password"),
            patch.object(image_viewer_routes, "_instagram_browser_debug_ready", return_value=True),
            patch.object(image_viewer_routes, "_running_instagram_chromium_pid", return_value=0),
            patch.object(image_viewer_routes, "_start_process") as start_process,
        ):
            try:
                image_viewer_routes._start_instagram_browser_locked()
            except RuntimeError as exc:
                assert "デバッグポート" in str(exc)
            else:
                raise AssertionError("Unrelated Chromium debug listener was accepted.")

    start_process.assert_not_called()


def test_instagram_browser_process_match_excludes_renderer_children():
    main_command = (
        "chromium "
        f"--user-data-dir={image_viewer_routes.INSTAGRAM_BROWSER_PROFILE_DIR} "
        f"--remote-debugging-port={image_viewer_routes.INSTAGRAM_BROWSER_DEBUG_PORT}"
    )
    renderer_command = f"{main_command} --type=renderer"
    tokens = image_viewer_routes._component_tokens("chromium")
    forbidden = image_viewer_routes._component_forbidden_tokens("chromium")

    with patch.object(image_viewer_routes, "_process_cmdline", return_value=main_command):
        assert image_viewer_routes._process_matches(100, tokens, forbidden)
    with patch.object(image_viewer_routes, "_process_cmdline", return_value=renderer_command):
        assert not image_viewer_routes._process_matches(101, tokens, forbidden)


def test_idle_instagram_browser_pauses_media_and_parks_on_blank_page():
    with (
        patch.object(image_viewer_routes, "_instagram_browser_running", return_value=True),
        patch.object(image_viewer_routes, "_browser_evaluate_value", return_value=1) as evaluate,
        patch.object(image_viewer_routes, "_browser_open_url") as open_url,
        patch.object(image_viewer_routes, "_instagram_log"),
    ):
        assert image_viewer_routes._idle_instagram_browser("job-1", "shortcode", context="test") is True

    assert "video.pause()" in evaluate.call_args.args[0]
    open_url.assert_called_once_with("about:blank")


def test_instagram_mixed_post_excludes_video_posters():
    photo_url = "https://scontent.example.cdninstagram.com/v/t51.82787-15/photo.jpg"
    marked_video_poster_url = "https://scontent.example.cdninstagram.com/v/t51.82787-15/poster.jpg"
    known_video_poster_url = "https://scontent.example.cdninstagram.com/v/t51.71878-15/cover.jpg"
    browser_value = {
        "urls": [
            {"url": photo_url, "previewUrl": photo_url, "mediaType": "image"},
            {
                "url": marked_video_poster_url,
                "previewUrl": marked_video_poster_url,
                "mediaType": "video_poster",
            },
            {
                "url": known_video_poster_url,
                "previewUrl": known_video_poster_url,
                "mediaType": "image",
            },
        ],
        "hasNext": False,
    }

    with (
        patch.object(image_viewer_routes, "_instagram_auth_configured", return_value=True),
        patch.object(image_viewer_routes, "_start_instagram_browser"),
        patch.object(image_viewer_routes, "_browser_open_url"),
        patch.object(image_viewer_routes, "_browser_evaluate_value_retry", return_value=browser_value),
        patch.object(image_viewer_routes.time, "sleep"),
        patch.object(image_viewer_routes, "_instagram_log"),
    ):
        items = image_viewer_routes._instagram_browser_image_items("mixed-post", job_id="test")

    assert [item["url"] for item in items] == [photo_url]


def test_instagram_browser_accepts_heic_photos_as_jpeg_payloads():
    photo_url = "https://scontent.example.cdninstagram.com/v/t51.82787-15/photo.heic?ig_cache_key=abc"
    browser_value = {
        "urls": [{"url": photo_url, "previewUrl": photo_url, "mediaType": "image"}],
        "hasNext": False,
    }

    with (
        patch.object(image_viewer_routes, "_instagram_auth_configured", return_value=True),
        patch.object(image_viewer_routes, "_start_instagram_browser"),
        patch.object(image_viewer_routes, "_browser_open_url"),
        patch.object(image_viewer_routes, "_browser_evaluate_value_retry", return_value=browser_value),
        patch.object(image_viewer_routes.time, "sleep"),
        patch.object(image_viewer_routes, "_instagram_log"),
    ):
        items = image_viewer_routes._instagram_browser_image_items("heic-post", job_id="test")

    assert [item["url"] for item in items] == [photo_url]
    payload = image_viewer_routes._instagram_image_payload("heic-post", items)
    assert payload[0]["suffix"] == ".jpg"
    assert payload[0]["filename"] == "instagram_heic-post_001.jpg"


def test_instagram_heic_download_converts_before_saving():
    with TemporaryDirectory() as temp_dir:
        target = Path(temp_dir) / "photo.jpg"
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [b"heic-source"]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        def fake_convert(source, destination):
            assert source.read_bytes() == b"heic-source"
            destination.write_bytes(b"jpeg-result")

        with (
            patch.object(image_viewer_routes.requests, "get", return_value=response),
            patch.object(image_viewer_routes, "_convert_heic_to_jpeg", side_effect=fake_convert) as convert,
        ):
            image_viewer_routes._download_instagram_image(
                "https://scontent.example.cdninstagram.com/photo.heic?ig_cache_key=abc",
                target,
            )

        convert.assert_called_once()
        assert target.read_bytes() == b"jpeg-result"
        assert not list(Path(temp_dir).glob("*.heic"))


def test_instagram_video_poster_url_markers_are_rejected():
    assert image_viewer_routes._instagram_image_url_is_video_poster(
        "https://cdninstagram.com/v/t51.71878-15/cover.jpg"
    )
    assert image_viewer_routes._instagram_image_url_is_video_poster(
        "https://cdninstagram.com/photo.jpg?efg=dmlkZW9fbmZyYW1lX2NvdmVyX2ZyYW1l"
    )
    assert not image_viewer_routes._instagram_image_url_is_video_poster(
        "https://cdninstagram.com/v/t51.82787-15/photo.jpg"
    )


def _instagram_resource_url(asset_id: str, bitrate: int, tag: str, **extra_query) -> str:
    metadata = {
        "vencode_tag": tag,
        "xpv_asset_id": int(asset_id),
        "bitrate": bitrate,
    }
    query = {
        "efg": base64.b64encode(json.dumps(metadata).encode("utf-8")).decode("ascii"),
        **extra_query,
    }
    return f"https://scontent.example.cdninstagram.com/o1/v/t2/video-{bitrate}.mp4?{urlencode(query)}"


def test_instagram_video_resources_select_highest_carousel_quality_and_remove_byte_range():
    low = _instagram_resource_url(
        "12345",
        200000,
        "ig-xpvds.carousel_item.c2-C3.dash_vp9-basic-gen2_360p",
        bytestart="818",
        byteend="885",
    )
    high = _instagram_resource_url(
        "12345",
        1200000,
        "ig-xpvds.carousel_item.c2-C3.dash_vp9-basic-gen2_1080p",
        bytestart="886",
        byteend="900000",
    )
    related_clip = _instagram_resource_url(
        "99999",
        2000000,
        "ig-xpvds.clips.c2-C3.dash_vp9-basic-gen2_1080p",
    )
    audio = _instagram_resource_url(
        "12345",
        70000,
        "ig-xpvds.carousel_item.c2-C3.dash_ln_heaac_audio",
    )

    selected = image_viewer_routes._select_instagram_browser_video_resources(
        [low, related_clip, audio, high]
    )

    assert len(selected) == 1
    assert "video-1200000.mp4" in selected[0]["url"]
    assert "bytestart=" not in selected[0]["url"]
    assert "byteend=" not in selected[0]["url"]
    assert "audioUrl" in selected[0]
    assert selected[0]["audioExpected"] is True


def test_instagram_reel_pairs_audio_and_excludes_ads_and_related_carousels():
    target_video = _instagram_resource_url(
        "77777",
        1800000,
        "ig-xpvds.clips.c2-C3.dash_baseline_1_v1",
    )
    target_audio = _instagram_resource_url(
        "77777",
        64000,
        "ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr3_audio",
    )
    related_reel_video = _instagram_resource_url(
        "66666",
        2400000,
        "ig-xpvds.clips.c2-C3.dash_baseline_1_v1",
    )
    related_reel_audio = _instagram_resource_url(
        "66666",
        96000,
        "ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr3_audio",
    )
    ad_video = _instagram_resource_url(
        "88888",
        3000000,
        "ig-xpvds.ad.igwww-C3.dash_baseline_1_v1",
    )
    ad_audio = _instagram_resource_url(
        "88888",
        96000,
        "ig-xpvds.ad.igwww-C3.dash_ln_heaac_vbr3_audio",
    )
    related_carousel = _instagram_resource_url(
        "99999",
        2200000,
        "ig-xpvds.carousel_item.c2-C3.dash_vp9-basic-gen2_1080p",
    )

    selected = image_viewer_routes._select_instagram_browser_video_resources(
        [
            ad_video,
            target_audio,
            related_carousel,
            target_video,
            related_reel_video,
            related_reel_audio,
            ad_audio,
        ],
        media_kind="reel",
    )

    assert len(selected) == 1
    assert selected[0]["assetId"] == "77777"
    assert "video-1800000.mp4" in selected[0]["url"]
    assert "video-64000.mp4" in selected[0]["audioUrl"]


def test_instagram_reel_source_url_is_preserved_as_reel():
    source_url = "https://www.instagram.com/cake_daisuki8/reel/DWFtwowE03H/"
    assert image_viewer_routes._instagram_post_kind(source_url) == "reel"
    assert image_viewer_routes._instagram_post_url("DWFtwowE03H", source_url) == (
        "https://www.instagram.com/reel/DWFtwowE03H/"
    )


def test_catalog_video_save_reports_progress_for_each_selected_item():
    source_videos = [
        {"index": 1, "url": "https://cdninstagram.com/one.mp4", "filename": "one.mp4"},
        {"index": 2, "url": "https://cdninstagram.com/two.mp4", "filename": "two.mp4"},
    ]
    progress = []

    def fake_download(_url, target):
        target.write_bytes(b"video-data")

    with (
        patch.object(image_viewer_routes, "_read_video_job", return_value={}),
        patch.object(image_viewer_routes, "_download_video_file", side_effect=fake_download),
        patch.object(
            image_viewer_routes.catalog,
            "store_file",
            side_effect=[
                {"uuid": "video-1", "display_name": "one.mp4"},
                {"uuid": "video-2", "display_name": "two.mp4"},
            ],
        ),
        patch.object(image_viewer_routes.catalog, "generate_thumbnail"),
    ):
        result = image_viewer_routes._catalog_video_save_result(
            {
                "videos": source_videos,
                "selected": [1, 2],
                "folder": "video",
            },
            progress_callback=lambda processed, total, *_args: progress.append((processed, total)),
        )

    assert result["ok"] is True
    assert len(result["saved"]) == 2
    assert progress == [(1, 2), (2, 2)]


if __name__ == "__main__":
    test_etc_browser_has_dedicated_runtime_defaults()
    test_ensure_shared_browser_starts_etc_browser_only()
    test_instagram_page_target_is_activated_before_use()
    test_instagram_idle_target_can_be_reused_for_next_fetch()
    test_idle_instagram_browser_pauses_media_and_parks_on_blank_page()
    test_instagram_mixed_post_excludes_video_posters()
    test_instagram_video_poster_url_markers_are_rejected()
    test_instagram_video_resources_select_highest_carousel_quality_and_remove_byte_range()
    test_instagram_reel_pairs_audio_and_excludes_ads_and_related_carousels()
    test_instagram_reel_source_url_is_preserved_as_reel()
    test_catalog_video_save_reports_progress_for_each_selected_item()
    print("11 tests passed")
