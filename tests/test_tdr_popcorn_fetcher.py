from unittest.mock import patch

from app.tdr.popcorn import fetcher as popcorn_fetcher
from app.tdr.popcorn.fetcher import PopcornFetchError, parse_design_page, parse_taste_page
from app.tdr.popcorn.routes import _flavor_options


def test_parse_taste_page_extracts_area_location_and_time_condition():
    html = """
    <main>
      <div class="section">
        <h2 class="heading2">【17:00～】カレー味</h2>
        <a href="/tds/restaurant/food/478/">
          <div class="listTextArea">
            <span>アラビアンコースト</span>
            <h3 class="heading3">ポップコーンワゴン（アラビアンコースト前）</h3>
          </div>
        </a>
      </div>
    </main>
    """

    rows = parse_taste_page(html, "tds")

    assert rows == [
        {
            "park": "tds",
            "name": "カレー味",
            "popcorn_type": "regular",
            "offers": [
                {
                    "area": "アラビアンコースト",
                    "location": "ポップコーンワゴン（アラビアンコースト前）",
                    "official_url": "https://www.tokyodisneyresort.jp/tds/restaurant/food/478/",
                    "time_note": "17:00～",
                    "available_from": "17:00",
                    "available_until": "",
                }
            ],
        }
    ]


def test_parse_taste_page_marks_big_pop_as_bb():
    html = """
    <div class="section">
      <h2 class="heading2">ソルティキャラメル味</h2>
      <a href="/tdl/restaurant/food/293/">
        <div class="listTextArea"><span>トゥモローランド</span><h3 class="heading3">ビッグポップ</h3></div>
      </a>
    </div>
    """
    rows = parse_taste_page(html, "tdl")
    assert rows[0]["popcorn_type"] == "bb"


def test_parse_taste_page_accepts_lenticular_time_brackets():
    html = """
    <div class="section">
      <h2 class="heading2">〖～16:30〗ソルティキャラメル味</h2>
      <a href="/tds/restaurant/food/478/">
        <div class="listTextArea"><span>アラビアンコースト</span><h3 class="heading3">テストワゴン</h3></div>
      </a>
    </div>
    """
    rows = parse_taste_page(html, "tds")
    assert rows[0]["name"] == "ソルティキャラメル味"
    assert rows[0]["offers"][0]["available_until"] == "16:30"


def test_parse_design_page_keeps_factual_data_without_image_url():
    html = """
    <main>
      <a href="/food/3991/">
        <img alt="商品画像" src="https://media1.tokyodisneyresort.jp/food_menu/image/3991_test.jpg">
        <div class="listTextArea">
          <h3 class="heading3">ポップコーン、バケット付き</h3>
          <div class="tagArea"><span class="new">NEW</span></div>
          <p class="text">1月13日より新価格になりました ¥2,400</p>
          <div class="description">
            <p class="bold">#ハニー味</p>
            <div class="listType-disc"><ul>
              <li>ポップコーンワゴン（イッツ・ア・スモールワールド前）</li>
              <li class="no-disc">2026年7月1日 〜</li>
            </ul></div>
          </div>
        </div>
      </a>
      <a href="/food/1720/">
        <div class="listTextArea">
          <h3 class="heading3">ポップコーン、レギュラーボックス</h3>
          <p class="text">¥400</p><div class="description"></div>
        </div>
      </a>
    </main>
    """

    rows = parse_design_page(html, "tdl")

    assert len(rows) == 1
    assert rows[0]["id"] == "3991"
    assert rows[0]["price"] == 2400
    assert rows[0]["is_new"] is True
    assert rows[0]["notice"] == "1月13日より新価格になりました"
    assert rows[0]["official_url"] == "https://www.tokyodisneyresort.jp/food/3991/"
    assert rows[0]["image_url"] == "https://media1.tokyodisneyresort.jp/food_menu/image/3991_test.jpg"
    assert rows[0]["offers"] == [
        {
            "flavor": "ハニー味",
            "location": "ポップコーンワゴン（イッツ・ア・スモールワールド前）",
            "period": "2026年7月1日 〜",
        }
    ]


def test_flavor_options_deduplicates_and_removes_only_the_trailing_aji_label():
    data = {
        "flavors": [
            {"name": "キャラメル味"},
            {"name": "ソルト味"},
            {"name": "キャラメル味"},
        ],
        "products": [
            {"offers": [{"flavor": "麻辣味"}, {"flavor": "味わいミックス"}]},
        ],
    }

    assert _flavor_options(data) == [
        {"value": "キャラメル味", "label": "キャラメル"},
        {"value": "ソルト味", "label": "ソルト"},
        {"value": "麻辣味", "label": "麻辣"},
        {"value": "味わいミックス", "label": "味わいミックス"},
    ]


def test_parse_design_page_keeps_same_title_products_separate_by_official_id():
    html = """
    <main>
      <a href="/food/1584/">
        <img src="https://media1.tokyodisneyresort.jp/food_menu/image/1584_test.jpg">
        <div class="listTextArea">
          <h3 class="heading3">BBポップコーン、バケット付き</h3>
          <p class="text">¥3,600</p>
          <div class="description">
            <p class="bold">#ソルティキャラメル味</p>
            <div class="listType-disc"><ul><li>ビッグポップ</li></ul></div>
          </div>
        </div>
      </a>
      <a href="/food/4169/">
        <img src="https://media1.tokyodisneyresort.jp/food_menu/image/4169_test.jpg">
        <div class="listTextArea">
          <h3 class="heading3">BBポップコーン、バケット付き</h3>
          <p class="text">¥3,800</p>
          <div class="description">
            <p class="bold">#ソルティキャラメル味</p>
            <div class="listType-disc"><ul><li>ビッグポップ</li></ul></div>
          </div>
        </div>
      </a>
    </main>
    """

    rows = parse_design_page(html, "tdl")

    assert [row["id"] for row in rows] == ["1584", "4169"]
    assert [row["price"] for row in rows] == [3600, 3800]
    assert rows[0]["image_url"].endswith("/1584_test.jpg")
    assert rows[1]["image_url"].endswith("/4169_test.jpg")


def test_fetch_all_pages_falls_back_to_chromium():
    calls = []
    expected = ({"flavors": [], "products": []}, {"counts": {}})

    class FakeChromiumGetter:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __call__(self, _url):
            raise AssertionError("The collector is stubbed in this test")

    def fake_collect(getter, *, transport):
        calls.append(transport)
        if transport == "http":
            raise PopcornFetchError("blocked")
        assert isinstance(getter, FakeChromiumGetter)
        return expected

    with (
        patch.object(popcorn_fetcher, "_fetch_all_pages_with_getter", fake_collect),
        patch.object(popcorn_fetcher, "ChromiumGetter", FakeChromiumGetter),
    ):
        assert popcorn_fetcher.fetch_all_pages() == expected
        assert calls == ["http", "chromium"]


def test_custom_getter_does_not_start_chromium():
    calls = []
    expected = ({"flavors": []}, {"counts": {}})

    def custom_getter(_url):
        return object()

    def fake_collect(getter, *, transport):
        calls.append((getter, transport))
        return expected

    with patch.object(popcorn_fetcher, "_fetch_all_pages_with_getter", fake_collect):
        assert popcorn_fetcher.fetch_all_pages(get=custom_getter) == expected
        assert calls == [(custom_getter, "custom")]
