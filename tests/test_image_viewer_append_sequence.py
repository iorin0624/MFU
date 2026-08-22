from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_append_sequence_plan_preserves_extensions_and_orders_target_first():
    source = (ROOT / "image_viewer" / "catalog.py").read_text(encoding="utf-8")
    routes = (ROOT / "image_viewer" / "routes.py").read_text(encoding="utf-8")
    template = (
        ROOT / "image_viewer" / "template" / "image_viewer.html"
    ).read_text(encoding="utf-8")

    assert "def build_append_sequence_plan" in source
    assert 'new_name = f"{target_stem}_{index}{extension}"' in source
    assert "def append_sequence_files" in source
    assert '@image_viewer_bp.post("/api/entries/append-sequence")' in routes
    assert "後付け連番" in template
    assert "function orderAppendSequenceSources" in template
    assert "compareNaturalText(aName, bName)" in template
    assert "const sources = orderAppendSequenceSources(entries);" in template
    assert "chooseAppendSequenceTarget" in template
    assert "Escキーでキャンセル" in template
