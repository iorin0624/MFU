from x_ff_notification import build_notification


def test_all_four_change_types_have_clickable_profile_links():
    preview, fields = build_notification(
        "success",
        {"followers": 906, "following": 3847},
        {
            "followers": {
                "addedCount": 1, "removedCount": 1,
                "added": [{"handle": "New_Follower"}],
                "removed": [{"handle": "OldFollower"}],
            },
            "following": {
                "addedCount": 1, "removedCount": 1,
                "added": [{"handle": "NewFollowed"}],
                "removed": [{"handle": "OldFollowed"}],
            },
        },
    )
    assert "@New_Follower" in preview
    assert "@OldFollowed" in preview
    assert [field["name"] for field in fields] == [
        "フォロワー増加（1件）", "フォロワー減少（1件）",
        "フォロー中の追加（1件）", "フォロー中の解除（1件）",
    ]
    assert [field["value"] for field in fields] == [
        "[@New_Follower](https://x.com/New_Follower)",
        "[@OldFollower](https://x.com/OldFollower)",
        "[@NewFollowed](https://x.com/NewFollowed)",
        "[@OldFollowed](https://x.com/OldFollowed)",
    ]


def test_invalid_handles_are_not_saved_as_links_or_html():
    preview, fields = build_notification(
        "success", {},
        {"followers": {"addedCount": 1, "added": [
            {"handle": "Safe_name"}, {"handle": "safe_NAME"},
            {"handle": "bad/<script>"}, {"handle": "too_long_username_123"},
        ]}},
    )
    assert "@Safe_name" in preview
    assert len(fields) == 1
    assert fields[0]["value"] == "[@Safe_name](https://x.com/Safe_name)"


def test_preview_is_short_but_notification_fields_keep_every_link():
    handles = [{"handle": f"account{i:03d}"} for i in range(120)]
    preview, fields = build_notification(
        "success", {"followers": 120},
        {"followers": {"addedCount": 120, "added": handles}},
    )
    assert len(preview) <= 1000
    assert "ほか115件" in preview
    assert fields[0]["value"].count("https://x.com/") == 120
    assert "account119" in fields[0]["value"]


def test_incomplete_results_do_not_report_unfollows():
    preview, fields = build_notification(
        "incomplete", {"followers": 700},
        {"followers": {"removedCount": 200, "removed": [{"handle": "someone"}]}},
    )
    assert "減少判定は行っていません" in preview
    assert "@someone" not in preview
    assert fields == []
