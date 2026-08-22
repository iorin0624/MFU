from pathlib import Path
from unittest.mock import Mock, patch

from jinja2 import DictLoader, Environment

from app.etc_accounting import repository as etc_repository
from app.phone_diagnostics import routes as phone_diagnostics_routes
from app.phone_whitelist import routes as phone_whitelist_routes
from app.tdr.popcorn import repository as popcorn_repository


ROOT = Path(__file__).resolve().parents[1]


def _existing_nav_db(*rows):
    cursor = Mock()
    cursor.fetchone.side_effect = list(rows)
    db = Mock()
    db.cursor.return_value = cursor
    return db, cursor


def _nav_updates(cursor):
    return [
        call.args[0]
        for call in cursor.execute.call_args_list
        if call.args and str(call.args[0]).lstrip().upper().startswith("UPDATE MFU_NAV_ITEMS")
    ]


def test_feature_initializers_do_not_overwrite_admin_nav_labels():
    etc_db, etc_cursor = _existing_nav_db({"id": 42})
    with patch.object(etc_repository, "get_db", return_value=etc_db):
        etc_repository.ensure_nav_item()
    assert _nav_updates(etc_cursor) == []

    diagnostics_db, diagnostics_cursor = _existing_nav_db({"acquired": 1}, {"id": 43})
    with patch.object(phone_diagnostics_routes, "get_db", return_value=diagnostics_db):
        phone_diagnostics_routes.ensure_phone_diagnostics_nav_item()
    assert _nav_updates(diagnostics_cursor) == []

    whitelist_db, whitelist_cursor = _existing_nav_db({"id": 44})
    with patch.object(phone_whitelist_routes, "get_db", return_value=whitelist_db):
        phone_whitelist_routes.ensure_phone_whitelist_nav_item()
    assert _nav_updates(whitelist_cursor) == []

    popcorn_db, popcorn_cursor = _existing_nav_db({"id": 45}, {"id": 46})
    with patch.object(popcorn_repository, "get_db", return_value=popcorn_db):
        popcorn_repository.ensure_nav_item()
    assert _nav_updates(popcorn_cursor) == []


def test_nav_list_renders_one_valid_parent_block_per_parent():
    template_source = (ROOT / "templates" / "admin_nav_list.html").read_text(encoding="utf-8")
    env = Environment(
        loader=DictLoader(
            {
                "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}",
                "admin_nav_list.html": template_source,
            }
        ),
        autoescape=True,
    )
    rendered = env.get_template("admin_nav_list.html").render(
        csrf_token_value="test-token",
        nav_items=[
            {
                "id": 1,
                "label": "親1",
                "url": "/one",
                "feature_key": None,
                "is_enabled": 1,
                "children": [
                    {
                        "id": 11,
                        "label": "子1",
                        "url": "/one/child",
                        "feature_key": None,
                        "is_enabled": 1,
                    }
                ],
            },
            {
                "id": 2,
                "label": "親2",
                "url": "/two",
                "feature_key": None,
                "is_enabled": 1,
                "children": [],
            },
        ],
    )

    assert rendered.count('<tbody class="nav-parent-block"') == 2
    assert "nav-sort-group" not in rendered
    first_block = rendered.index('<tbody class="nav-parent-block" data-item-id="1">')
    first_child = rendered.index('data-item-id="11"', first_block)
    first_close = rendered.index("</tbody>", first_block)
    second_block = rendered.index('<tbody class="nav-parent-block" data-item-id="2">')
    assert first_block < first_child < first_close < second_block
    assert "const blocks = getParentBlocks();" in rendered
    assert "item_ids: blocks.map" in rendered


if __name__ == "__main__":
    test_feature_initializers_do_not_overwrite_admin_nav_labels()
    test_nav_list_renders_one_valid_parent_block_per_parent()
    print("2 tests passed")
