from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.image_viewer import routes
from app.utils import media_hub


def test_media_download_rejects_internal_and_unapproved_urls_before_request():
    blocked = (
        "http://cdninstagram.com/photo.jpg",
        "https://127.0.0.1/photo.jpg",
        "https://cdninstagram.com.evil.example/photo.jpg",
        "https://user:pass@cdninstagram.com/photo.jpg",
        "https://cdninstagram.com:8080/photo.jpg",
    )
    with patch.object(routes.requests, "get") as get:
        for url in blocked:
            with pytest.raises(ValueError):
                routes._open_media_download(url, {}, (1, 1))
    get.assert_not_called()


def test_media_download_rejects_private_dns_result_and_redirect():
    public = [(2, 1, 6, "", ("93.184.216.34", 443))]
    private = [(2, 1, 6, "", ("10.0.0.5", 443))]
    with patch.object(routes.socket, "getaddrinfo", return_value=private), patch.object(routes.requests, "get") as get:
        with pytest.raises(ValueError):
            routes._open_media_download("https://cdninstagram.com/photo.jpg", {}, (1, 1))
        get.assert_not_called()

    redirect = Mock(status_code=302, headers={"Location": "http://127.0.0.1/admin"})
    with patch.object(routes.socket, "getaddrinfo", return_value=public), patch.object(routes.requests, "get", return_value=redirect) as get:
        with pytest.raises(ValueError):
            routes._open_media_download("https://cdninstagram.com/photo.jpg", {}, (1, 1))
        get.assert_called_once()
        redirect.close.assert_called_once()


def test_media_download_enforces_stream_size_without_content_length():
    response = Mock(headers={}, iter_content=Mock(return_value=[b"1234", b"5678"]))
    with TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError):
            routes._write_limited_media(response, Path(temp_dir) / "media", 7)


def test_media_job_requires_same_owner_and_completed_status():
    app = Flask("media-security-test")
    current = {"owner_username": "alice", "created_at": routes.time.time(), "status": "done"}
    with app.test_request_context("/"):
        with patch.object(routes, "_request_username", return_value="bob"):
            _, error = routes._owned_media_job("job-1", lambda _: current, require_done=True)
            assert error[1] == 404

        with patch.object(routes, "_request_username", return_value="alice"):
            job, error = routes._owned_media_job("job-1", lambda _: current, require_done=True)
            assert error is None and job is current
            current["status"] = "pending"
            _, error = routes._owned_media_job("job-1", lambda _: current, require_done=True)
            assert error[1] == 409
            current.pop("owner_username")
            _, error = routes._owned_media_job("job-1", lambda _: current)
            assert error[1] == 410


def test_selection_cannot_name_an_index_missing_from_server_job():
    with Flask("media-security-test").test_request_context("/"):
        job = {"images": [{"index": 1, "url": "https://cdninstagram.com/photo.jpg"}]}
        assert routes._validate_job_selection({"selected": [1]}, job, "images") is None
        assert routes._validate_job_selection({"selected": [2]}, job, "images")[1] == 400


def test_websocket_fetch_status_returns_job_id_only_to_owner():
    job = {"owner_username": "alice", "status": "done", "images": [{"index": 1}]}
    with patch.object(routes, "_read_instagram_job", return_value=job):
        own = media_hub._direct_job_status("fetch_images", {"jobId": "job-1"}, "alice")
        other = media_hub._direct_job_status("fetch_images", {"jobId": "job-1"}, "bob")
    assert own["ok"] is True and own["jobId"] == "job-1"
    assert other["ok"] is False and "images" not in other


def test_image_save_ignores_client_supplied_image_url():
    job = {
        "source": "instagram",
        "shortcode": "abc123",
        "source_url": "https://www.instagram.com/p/abc123/",
        "images": [{"index": 1, "url": "https://cdninstagram.com/photo.jpg"}],
    }
    with Flask("media-security-test").test_request_context("/"):
        with (
            patch.object(routes, "_download_instagram_image", side_effect=lambda _url, target: target.write_bytes(b"image")) as download,
            patch.object(routes.catalog, "store_file", return_value={"uuid": "saved"}),
            patch.object(routes.catalog, "generate_thumbnail"),
        ):
            response = routes._catalog_instagram_save(
                {
                    "selected": [1],
                    "images": [{"index": 1, "url": "https://127.0.0.1/private"}],
                    "sourceUrl": "https://evil.example/",
                },
                job,
            )
        assert response.get_json()["ok"] is True
        assert download.call_args.args[0] == "https://cdninstagram.com/photo.jpg"
