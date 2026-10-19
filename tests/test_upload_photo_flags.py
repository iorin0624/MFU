from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_flag_is_persisted_and_only_upload_owner_can_change_image_files():
    schema = (ROOT / "utils" / "upload_security.py").read_text(encoding="utf-8")
    app = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'ADD COLUMN is_flagged TINYINT(1) NOT NULL DEFAULT 0' in schema
    assert '@app.post("/view/<uuid>/flag")' in app
    assert 'if not is_upload_owner(upload):' in app.split('@app.post("/view/<uuid>/flag")', 1)[1]
    assert 'Path(str(row["filename"])).suffix.lower() not in image_extensions' in app
    assert '"flagged": bool(row.get("is_flagged"))' in app
    assert 'AND (%s = 1 OR is_hidden = 0)' in app


def test_public_viewer_shows_flag_to_viewers_and_owner_can_toggle_with_e():
    viewer = (
        ROOT / "external_login_user" / "frontend" / "src" / "views" / "PublicUploadViewer.vue"
    ).read_text(encoding="utf-8")
    styles = (
        ROOT / "external_login_user" / "frontend" / "src" / "public-upload.css"
    ).read_text(encoding="utf-8")

    assert "file.flagged && file.kind === 'image'" in viewer
    assert 'class="flag-badge"' in viewer
    assert 'class="flag-legend"' in viewer
    assert '🚩　${lightboxFile.name}' in viewer
    assert "event.key.toLowerCase() === 'e'" in viewer
    assert 'aria-keyshortcuts="E"' in viewer
    assert "event.key.toLowerCase() === 'x'" in viewer
    assert '.media-card.is-flagged { border:4px solid #dc2626;' in styles
