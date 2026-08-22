import os

from app.ticket_price_research.services import (
    _extract_area_from_shop_page,
    _normalize_item,
    fetch_disney_ticket_items,
    format_date,
)


def test_format_date_converts_month_end_to_actual_last_day():
    assert format_date("2027年8月末") == ("2027年8月31日", "2027-08-31")


def test_format_date_month_end_is_leap_year_aware():
    assert format_date("2028年2月末") == ("2028年2月29日", "2028-02-29")
    assert format_date("2027年2月末") == ("2027年2月28日", "2027-02-28")


def test_extract_area_prefers_official_shop_breadcrumb():
    html = """
    <div class="widget">
      <div class="widget-body">
        検索トップ &gt; 神奈川県 &gt; 横浜市青葉区 &gt; 大黒屋 青葉台店
      </div>
    </div>
    """
    assert _extract_area_from_shop_page(html) == ("神奈川県横浜市青葉区", "")


def test_normalized_item_contains_shop_area():
    item = _normalize_item(
        {
            "商品名": "1DAYパスポート",
            "価格": 7900,
            "店舗名": "草加買取センター",
            "店舗所在地": "埼玉県草加市",
        }
    )
    assert item["shop_area"] == "埼玉県草加市"


if __name__ == "__main__":
    test_format_date_converts_month_end_to_actual_last_day()
    test_format_date_month_end_is_leap_year_aware()
    test_extract_area_prefers_official_shop_breadcrumb()
    test_normalized_item_contains_shop_area()
    print("ticket price service tests: ok")
    if os.environ.get("TICKET_PRICE_LIVE_TEST") == "1":
        payload = fetch_disney_ticket_items(force_refresh=True)
        assert payload["ok"] and payload["items"]
        located = [item for item in payload["items"] if item.get("shop_area")]
        assert located
        print(
            "live fetch:",
            payload["count"],
            "items /",
            len(located),
            "located / samples:",
            [
                (item["expiry_display"], item["shop_name"], item["shop_area"])
                for item in located[:5]
            ],
        )
        from app import app as flask_app
        from app.ticket_price_research.mail_service import render_html_body
        from app.ticket_price_research.pdf import render_disney_ticket_pdf

        with flask_app.app_context():
            html = render_html_body(payload)
            assert located[0]["shop_area"] in html
            pdf = render_disney_ticket_pdf(payload)
            assert pdf.startswith(b"%PDF")
        print("email/html and PDF rendering: ok")
        with flask_app.test_client() as client:
            with client.session_transaction() as session:
                session["user"] = "admin"
            response = client.get("/admin/ticket-price/disney")
            assert response.status_code == 200
            assert "店舗・所在地で検索" in response.get_data(as_text=True)
        print("admin page rendering: ok")
