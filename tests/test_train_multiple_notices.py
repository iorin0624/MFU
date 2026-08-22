import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERT_SPEC = importlib.util.spec_from_file_location(
    "train_alert", ROOT / "signage" / "train_alert.py"
)
TRAIN_ALERT = importlib.util.module_from_spec(ALERT_SPEC)
assert ALERT_SPEC.loader is not None
ALERT_SPEC.loader.exec_module(TRAIN_ALERT)
SPEC = importlib.util.spec_from_file_location(
    "jreast_kanto_status", ROOT / "scripts" / "jreast_kanto_status.py"
)
JREAST = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(JREAST)


def _detail_html(notices):
    payload = {
        "props": {
            "pageProps": {
                "updateTimeText": "8月14日 16時49分",
                "diainfoTrainFeature": {
                    "routeInfo": {
                        "property": {
                            "railCode": "149",
                            "displayName": "小湊鉄道線",
                            "diainfo": notices,
                        }
                    }
                },
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></html>"
    ).encode()


def _notices():
    return [
        {
            "infoId": "plan",
            "status": "運転計画",
            "message": "明日始発から一部区間を運休します。",
            "publishTime": "2026-08-14 16:30:00",
        },
        {
            "infoId": "suspended",
            "status": "運転見合わせ",
            "message": "本日は終日、一部区間で運転を見合わせています。",
            "publishTime": "2026-08-14 16:25:00",
        },
    ]


def test_parse_detail_keeps_all_notices_and_uses_most_severe_as_primary():
    result = JREAST.parse_detail(_detail_html(_notices()))

    assert [item["notice_id"] for item in result["notices"]] == ["suspended", "plan"]
    assert result["status"] == "運転見合わせ"
    assert result["message"].startswith("本日は終日")


def test_summary_emits_one_row_per_notice():
    detail = JREAST.parse_detail(_detail_html(_notices()))
    raw = {
        "updated": "8月14日 16時49分",
        "source": "https://transit.yahoo.co.jp/diainfo/area/4",
        "lines": [
            {
                "line_name": "小湊鉄道線",
                "url": "https://transit.yahoo.co.jp/diainfo/149/0",
                **detail,
            }
        ],
    }

    summary = JREAST.build_summary(raw)

    assert len(summary["lines"]) == 2
    assert {item["notice_id"] for item in summary["lines"]} == {"suspended", "plan"}


def test_alert_state_keeps_route_active_and_builds_two_discord_cards():
    url = "https://transit.yahoo.co.jp/diainfo/149/0"
    detail = JREAST.parse_detail(_detail_html(_notices()))
    catalog = {url: {"url": url, "line_name": "小湊鉄道線", "group": "kanto"}}
    raw = {"area_parse_ok": True, "lines": [{"line_name": "小湊鉄道線", "url": url, **detail}]}

    current, unavailable = TRAIN_ALERT._current_route_states(catalog, raw)
    route = current[url]
    embeds = TRAIN_ALERT.build_embeds(
        {"event": "updated", "current": route, "previous": {}},
        "https://mfu.iori0624.jp/train-status",
        "2026-08-14T16:49:00+09:00",
    )

    assert unavailable == []
    assert route["normal"] is False
    assert len(route["notices"]) == 2
    assert [embed["fields"][1]["value"] for embed in embeds] == ["運転見合わせ", "運転計画"]


def test_removing_one_of_two_notices_is_an_update_not_a_recovery():
    url = "https://transit.yahoo.co.jp/diainfo/149/0"
    catalog = {url: {"url": url, "line_name": "小湊鉄道線", "group": "kanto"}}
    previous_detail = JREAST.parse_detail(_detail_html(_notices()))
    current_detail = JREAST.parse_detail(_detail_html([_notices()[1]]))
    previous, _ = TRAIN_ALERT._current_route_states(
        catalog,
        {"area_parse_ok": True, "lines": [{"line_name": "小湊鉄道線", "url": url, **previous_detail}]},
    )
    current, _ = TRAIN_ALERT._current_route_states(
        catalog,
        {"area_parse_ok": True, "lines": [{"line_name": "小湊鉄道線", "url": url, **current_detail}]},
    )

    assert current[url]["normal"] is False
    assert TRAIN_ALERT._event_for(previous[url], current[url]) == "updated"
