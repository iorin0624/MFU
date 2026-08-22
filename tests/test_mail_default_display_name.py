import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unspecified_sender_display_name_is_mfu_system():
    source = (ROOT / "utils" / "mail.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "DEFAULT_FROM_DISPLAY_NAME"
    }
    assert assignments["DEFAULT_FROM_DISPLAY_NAME"] == "MFU_System"
    assert 'event_name or DEFAULT_FROM_DISPLAY_NAME' in source
    assert 'Header(DEFAULT_FROM_DISPLAY_NAME, "utf-8")' in source
