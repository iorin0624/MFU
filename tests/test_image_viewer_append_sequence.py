from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_append_sequence_plan_preserves_extensions_and_orders_target_first():
    source = (ROOT / "image_viewer" / "catalog.py").read_text(encoding="utf-8")
    routes = (ROOT / "image_viewer" / "routes.py").read_text(encoding="utf-8")
    explorer = (
        ROOT / "image_viewer" / "frontend" / "src" / "components" / "ExplorerWindow.vue"
    ).read_text(encoding="utf-8")

    assert "def build_append_sequence_plan" in source
    assert 'sequence_match = re.fullmatch(r"(.+)_([0-9]+)", target_stem)' in source
    assert "normalized_sources.sort(" in source
    assert "_natural_sort_key(PurePosixPath(value).name)" in source
    assert 'new_name = f"{sequence_stem}_{next_number}{extension}"' in source
    assert "def append_sequence_files" in source
    assert '@image_viewer_bp.post("/api/entries/append-sequence")' in routes
    assert "後付け連番の確認" in explorer
    assert "model.value.selectedPaths.slice().sort" in explorer
    assert "collator.compare(leftName, rightName)" in explorer
    assert "appendPreview(dialog.sources, dialog.target)" in explorer
    assert "imageViewerApi.appendSequence(sources, target.path)" in explorer
    assert "event.key === 'Escape' && model.value.appendSources.length" in explorer
