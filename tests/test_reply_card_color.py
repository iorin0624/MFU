from pathlib import Path

from utils.reply_card_color import DEFAULT_REPLY_CARD_COLOR, normalize_reply_card_color


ROOT = Path(__file__).resolve().parents[1]


def test_color_accepts_hex_and_normalizes_case():
    assert DEFAULT_REPLY_CARD_COLOR == "#f4b3c2"
    assert normalize_reply_card_color(" #A1B2C3 ") == "#a1b2c3"


def test_color_rejects_css_injection_and_invalid_values():
    for value in (None, "", "red", "#fff", "#123456; background:black", "url(x)"):
        assert normalize_reply_card_color(value) == DEFAULT_REPLY_CARD_COLOR


def test_mode_setting_flows_to_public_reply_card():
    server = (ROOT / "__init__.py").read_text(encoding="utf-8")
    schema = (ROOT / "utils" / "upload_security.py").read_text(encoding="utf-8")
    editor = (ROOT / "templates" / "mode_edit_combined.html").read_text(encoding="utf-8")
    viewer = (ROOT / "external_login_user" / "frontend" / "src" / "views" / "PublicUploadViewer.vue").read_text(encoding="utf-8")
    css = (ROOT / "external_login_user" / "frontend" / "src" / "public-upload.css").read_text(encoding="utf-8")

    assert "ADD COLUMN reply_card_color CHAR(7)" in schema
    assert 'name="reply_card_color"' in editor
    assert '"cardColor": normalize_reply_card_color(mode_row.get("reply_card_color"))' in server
    assert "'--reply-card-color':data.reply.cardColor" in viewer
    assert "background:var(--reply-card-color,#f4b3c2)" in css
