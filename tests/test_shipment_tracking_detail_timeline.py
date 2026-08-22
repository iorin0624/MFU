from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shipment_tracking_detail_uses_readable_carrier_timeline():
    routes = (ROOT / "shipment_tracking" / "routes.py").read_text(encoding="utf-8")
    services = (ROOT / "shipment_tracking" / "services.py").read_text(encoding="utf-8")
    template = (
        ROOT
        / "shipment_tracking"
        / "template"
        / "admin"
        / "shipment_tracking"
        / "detail.html"
    ).read_text(encoding="utf-8")

    assert "get_tracking_timeline" in routes
    assert "ORDER BY checked_at ASC, id ASC" in services
    assert "追跡側の日時" in template
    assert "MFU確認日時" in template
    assert "配送会社の追跡履歴" in template
    assert "技術情報・元データを表示" in template
    assert "tracking-timeline-mobile" in template
    assert "AND changed=1" in services
    assert '"is_archived": key not in current_keys' in services
    assert "配送会社側では現在非表示" in template
