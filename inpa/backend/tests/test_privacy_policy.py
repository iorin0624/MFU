from datetime import date

import pytest

from inpa_app.privacy import effective_fields, empty_matrix, validate_matrix, viewer_audience
from inpa_app.share import _render_visits


def test_effective_fields_are_cumulative_by_audience():
    matrix = empty_matrix()
    matrix["link"]["date"] = True
    matrix["logged_in"]["park"] = True
    matrix["mutual"]["costume"] = True
    matrix["private"]["memo"] = True

    assert effective_fields(matrix, "link") == {"date"}
    assert effective_fields(matrix, "logged_in") == {"date", "park"}
    assert effective_fields(matrix, "mutual") == {"date", "park", "costume"}
    assert effective_fields(matrix, "private") == {"date", "park", "costume", "memo"}


def test_blocked_viewer_has_no_audience_but_owner_still_has_private_audience():
    assert viewer_audience(
        owner=False, logged_in=True, owner_follows_viewer=True,
        viewer_follows_owner=True, blocked=True,
    ) is None
    assert viewer_audience(
        owner=True, logged_in=True, owner_follows_viewer=False,
        viewer_follows_owner=False, blocked=True,
    ) == "private"


def test_mutual_and_logged_in_audiences_are_distinguished():
    assert viewer_audience(
        owner=False, logged_in=True, owner_follows_viewer=True,
        viewer_follows_owner=True, blocked=False,
    ) == "mutual"
    assert viewer_audience(
        owner=False, logged_in=True, owner_follows_viewer=True,
        viewer_follows_owner=False, blocked=False,
    ) == "logged_in"


def test_matrix_validation_requires_every_boolean_field():
    matrix = empty_matrix()
    assert validate_matrix(matrix) == matrix
    del matrix["link"]["memo"]
    with pytest.raises(ValueError):
        validate_matrix(matrix)


def test_shared_visit_uses_profile_matrix_instead_of_visit_level_settings():
    matrix = empty_matrix()
    matrix["link"]["date"] = True
    matrix["logged_in"]["park"] = True
    matrix["mutual"]["costume"] = True
    matrix["private"]["memo"] = True
    rows = [{
        "visit_date": date(2030, 1, 2), "park": "land",
        "costume": "red", "memo": "owner note",
    }]

    anonymous = _render_visits(1, rows, None, (False, False, False), matrix)
    logged_in = _render_visits(1, rows, 2, (False, False, False), matrix)
    mutual = _render_visits(1, rows, 2, (False, True, True), matrix)
    owner = _render_visits(1, rows, 1, (False, False, False), matrix)

    assert anonymous == [{"visit_date": "2030-01-02"}]
    assert logged_in == [{"visit_date": "2030-01-02", "park": "land"}]
    assert mutual == [{"visit_date": "2030-01-02", "park": "land", "costume": "red"}]
    assert owner == [{
        "visit_date": "2030-01-02", "park": "land", "costume": "red", "memo": "owner note",
    }]
    assert _render_visits(1, rows, 2, (True, True, True), matrix) == []
