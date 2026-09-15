from pathlib import Path

from app.utils.upload_sort import natural_filename_sort_key, sort_upload_file_rows


ROOT = Path(__file__).resolve().parents[1]


def test_upload_file_rows_use_natural_case_insensitive_name_order():
    rows = [
        {"id": 1, "filename": "IMG_10.jpg"},
        {"id": 2, "filename": "img_2.jpg"},
        {"id": 3, "filename": "IMG_01.jpg"},
    ]

    assert [row["filename"] for row in sort_upload_file_rows(rows)] == [
        "IMG_01.jpg",
        "img_2.jpg",
        "IMG_10.jpg",
    ]
    assert natural_filename_sort_key("photo2.jpg") < natural_filename_sort_key("photo10.jpg")


def test_upload_file_rows_prefer_embedded_capture_time_then_filename():
    rows = [
        {"id": 1, "filename": "IMG_10.jpg"},
        {"id": 2, "filename": "IMG_2.jpg"},
        {"id": 3, "filename": "no-date.jpg"},
    ]
    capture_times = {
        "IMG_10.jpg": "2026-09-15T10:00:02.000000",
        "IMG_2.jpg": "2026-09-15T10:00:01.000000",
    }

    sorted_rows = sort_upload_file_rows(rows, capture_times=capture_times)

    assert [row["filename"] for row in sorted_rows] == ["IMG_2.jpg", "IMG_10.jpg", "no-date.jpg"]
    assert sorted_rows[0]["captured_at"] == "2026-09-15T10:00:01.000000"


def test_upload_page_previews_and_sends_files_in_name_order():
    template = (ROOT / "templates" / "upload.html").read_text(encoding="utf-8")
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'new Intl.Collator("ja", { numeric: true, sensitivity: "base" })' in template
    assert "for (const file of filesByName(files))" in template
    assert "const files = filesByName(fileInput.files);" in template
    assert app_source.count("sort_upload_file_rows(cursor.fetchall())") >= 2
