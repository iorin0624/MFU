import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _literal_assignment(name: str):
    source = (ROOT / "utils" / "logs.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in item.targets)
    )
    return ast.literal_eval(node.value)


def test_periodic_monitoring_paths_are_excluded_from_access_log_storage():
    assert "/api/speedtest/" in _literal_assignment("SKIP_PREFIXES")
    assert "/admin/nodes/chrony/data" in _literal_assignment("SKIP_PATHS")


def test_visible_pages_remain_loggable():
    prefixes = _literal_assignment("SKIP_PREFIXES")
    paths = _literal_assignment("SKIP_PATHS")
    assert not any("/speedtest".startswith(prefix) for prefix in prefixes)
    assert "/admin/nodes/chrony" not in paths
