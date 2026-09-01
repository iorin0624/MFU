from datetime import datetime

from image_viewer.catalog import _group_rows


def _epoch(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp())


def test_group_rows_orders_newest_groups_and_keeps_natural_name_order():
    rows = [
        {"id": 1, "display_name": "10.jpg", "mtime_epoch": _epoch("2026-08-01 10:00:00")},
        {"id": 2, "display_name": "2.jpg", "mtime_epoch": _epoch("2026-08-01 11:00:00")},
        {"id": 3, "display_name": "1.jpg", "mtime_epoch": _epoch("2026-08-02 09:00:00")},
        {"id": 4, "display_name": "unknown.jpg", "mtime_epoch": 0},
    ]

    ordered, groups = _group_rows(rows, "ASC", "updated", "day")

    assert [group["key"] for group in groups] == ["2026-08-02", "2026-08-01", "unknown"]
    assert [row["display_name"] for row in ordered] == ["1.jpg", "2.jpg", "10.jpg", "unknown.jpg"]
    assert [(group["start"], group["count"]) for group in groups] == [(0, 1), (1, 2), (3, 1)]


def test_group_rows_uses_registration_and_capture_fields_independently():
    row = {
        "id": 1, "display_name": "photo.jpg",
        "mtime_epoch": _epoch("2026-08-03 10:00:00"),
        "registered_epoch": _epoch("2026-08-02 10:00:00"),
        "captured_epoch": _epoch("2025-12-06 14:00:00"),
    }

    assert _group_rows([row.copy()], "ASC", "updated", "day")[1][0]["key"] == "2026-08-03"
    assert _group_rows([row.copy()], "ASC", "registered", "day")[1][0]["key"] == "2026-08-02"
    assert _group_rows([row.copy()], "ASC", "captured", "day")[1][0]["key"] == "2025-12-06"
