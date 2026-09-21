from datetime import time, timedelta

import pytest

from inpa_app.share import _share_artifacts, _time_text, visibility_allows


@pytest.mark.parametrize(
    ("visibility", "expected"),
    [("link", True), ("logged_in", False), ("following", False), ("mutual", False), ("private", False)],
)
def test_anonymous_link_viewer(visibility, expected):
    assert visibility_allows(
        visibility,
        owner=False,
        has_link=True,
        logged_in=False,
        owner_follows_viewer=False,
        viewer_follows_owner=False,
        blocked=False,
    ) is expected


def test_logged_in_and_relationship_levels():
    common = {"owner": False, "has_link": True, "logged_in": True, "blocked": False}
    assert visibility_allows("logged_in", owner_follows_viewer=False, viewer_follows_owner=False, **common)
    assert visibility_allows("following", owner_follows_viewer=True, viewer_follows_owner=False, **common)
    assert not visibility_allows("mutual", owner_follows_viewer=True, viewer_follows_owner=False, **common)
    assert visibility_allows("mutual", owner_follows_viewer=True, viewer_follows_owner=True, **common)


@pytest.mark.parametrize("visibility", ["link", "logged_in", "following", "mutual", "private"])
def test_block_hides_every_level_from_non_owner(visibility):
    assert not visibility_allows(
        visibility,
        owner=False,
        has_link=True,
        logged_in=True,
        owner_follows_viewer=True,
        viewer_follows_owner=True,
        blocked=True,
    )


@pytest.mark.parametrize("visibility", ["link", "logged_in", "following", "mutual", "private"])
def test_owner_always_sees_own_visit(visibility):
    assert visibility_allows(
        visibility,
        owner=True,
        has_link=False,
        logged_in=True,
        owner_follows_viewer=False,
        viewer_follows_owner=False,
        blocked=True,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(time(9, 30), "09:30:00"), (timedelta(hours=9, minutes=30), "09:30:00"), (None, None)],
)
def test_database_time_serialization(value, expected):
    assert _time_text(value) == expected


def test_share_artifacts_use_short_url_and_embedded_svg():
    from inpa_app import create_app

    app = create_app("public", {"TESTING": True, "PUBLIC_ORIGIN": "https://inpa.example"})
    with app.app_context():
        result = _share_artifacts("0123456789ABCDEFGHJKM")
    assert result["url"] == "https://inpa.example/0123456789ABCDEFGHJKM"
    assert result["qr_svg"].startswith("<svg")
