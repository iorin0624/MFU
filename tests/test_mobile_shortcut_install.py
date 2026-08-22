from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]


def test_shortcut_button_is_ios_only_and_shared_by_all_download_views():
    javascript = (ROOT / "static" / "js" / "mfu_shortcut_download.js").read_text(
        encoding="utf-8"
    )
    assert "/iPad|iPhone|iPod/i" in javascript
    assert "navigator.platform === 'MacIntel'" in javascript
    assert "navigator.maxTouchPoints > 1" in javascript
    assert "height:100dvh" in javascript
    assert "width:min(100%,300px)" in javascript
    assert "position:sticky" in javascript
    assert "/mobile-download/api/shortcut-config" in javascript
    assert "shortcut_status_url" in javascript

    for relative_path in (
        "templates/view.html",
        "albums/templates/view_child.html",
        "albums/templates/view_child_movie.html",
    ):
        template = (ROOT / relative_path).read_text(encoding="utf-8")
        assert 'data-mfu-shortcut-button hidden style="display:none"' in template
        assert "mfu_shortcut_download.js" in template
        assert "MFUShortcutDownload.launch(data)" in template
        assert "window.location.assign(data.shortcut_url)" not in template


def test_shortcut_detection_and_admin_settings_are_server_backed():
    source = (ROOT / "utils" / "mobile_download.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS mobile_download_shortcut_settings" in source
    assert '"/mobile-download/api/shortcut-status/<launch_token>"' in source
    assert '"/admin/mobile-download/shortcut", methods=["GET", "POST"]' in source
    assert 'return str(session.get("user") or "")' in source
    assert 'if username != "admin":' in source
    assert "@login_required" not in source
    assert '"shortcut_status_url"' in source
    assert "bool(row.get(\"exchanged_at\"))" in source


def test_shortcut_admin_template_parses():
    template = (
        ROOT / "templates" / "admin_mobile_download_shortcut.html"
    ).read_text(encoding="utf-8")
    Environment().parse(template)
    assert "ショートカット配布URL" in template
    assert "ポップアップのプレビュー" in template
    assert "判定待ち時間" in template
