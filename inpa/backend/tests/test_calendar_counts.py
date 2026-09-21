from inpa_app.calendar import _park_counts


def test_park_counts_keep_both_land_sea_and_undecided_separate():
    entries = [
        {"park": "both"},
        {"park": "land"},
        {"park": "sea"},
        {"park": "sea"},
        {"park": "undecided"},
        {},
    ]

    assert _park_counts(entries) == {
        "both": 1,
        "land": 1,
        "sea": 2,
        "undecided": 1,
    }
