import json
from pathlib import Path

from tools.mfu_notification_sender.repair_notification_card_links import repaired_cards, train_urls
from tools.mfu_media_hub.card_links import notification_card_url


def test_windows_card_links_accept_safe_sources_and_hide_self_links():
    assert notification_card_url("https://www.police.pref.chiba.jp/kohoka/orders_prefecture_12345.html")
    assert notification_card_url("/etc-accounting/") == "https://mfu.iori0624.jp/etc-accounting/"
    for raw in ("/mfu-notifications", "//example.com", "javascript:alert(1)", "https://user:pass@example.com/"):
        assert notification_card_url(raw) == ""


def test_saved_police_and_train_cards_restore_only_missing_source_links(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"lines": [{"line_name": "山手線", "url": "https://transit.yahoo.co.jp/diainfo/21/0"}]}), encoding="utf-8")
    trains = train_urls(catalog, tmp_path / "missing.json")
    chiba = {"2026-10-12": "https://www.police.pref.chiba.jp/kohoka/orders_prefecture_12345.html"}
    police = {"cards": [
        {"title": "2026年10月12日事件・事故ファイル", "description": "千葉県警察の掲載ページを開く", "url": "/mfu-notifications"},
        {"title": "事件本文", "description": "事案", "url": "/mfu-notifications"},
    ]}
    police, changed, unresolved = repaired_cards("chiba_police_incidents", police, chiba, trains)
    assert (changed, unresolved) == (1, 0)
    assert police["cards"][0]["url"] == chiba["2026-10-12"]
    assert police["cards"][1]["url"] == "/mfu-notifications"
    train = {"cards": [{"title": "山手線：遅延", "url": "/mfu-notifications", "fields": [{"name": "路線", "value": "山手線"}]}]}
    train, changed, unresolved = repaired_cards("train_status", train, chiba, trains)
    assert (changed, unresolved) == (1, 0)
    assert train["cards"][0]["url"] == "https://transit.yahoo.co.jp/diainfo/21/0"


def test_web_and_windows_render_card_source_links():
    root = Path(__file__).resolve().parents[1]
    web = (root / "external_login_user/template/notifications.html").read_text(encoding="utf-8")
    windows = (root / "tools/mfu_media_hub/main.py").read_text(encoding="utf-8")
    assert "(?:を開く|を確認する)" in web
    assert "data-card-link" in web
    assert "_notification_link_label(description.strip(), card_url)" in windows
    assert "_notification_link_label(card_title, card_url, bold=True)" in windows
    assert 'if _notification_card_url(item.get("target_url")):' in windows
    assert 'if _notification_card_url(row.get("target_url")):' in windows
