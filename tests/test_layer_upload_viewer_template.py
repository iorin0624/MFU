from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_layer_reply_images_open_in_one_parent_gallery():
    template = (ROOT / "templates" / "layer_upload_detail.html").read_text(encoding="utf-8")
    assert 'id="layer-image-gallery"' in template
    assert 'class="text-decoration-none layer-image-open"' in template
    assert 'data-reply-uuid="{{ group.reply_uuid }}"' in template
    assert 'data-filename="{{ image_name }}"' in template
    assert 'target="_blank"' not in template
    assert 'id="layer-image-viewer"' in template
    assert 'id="layer-image-viewer-copy"' in template
    assert 'layer-upload-viewer.js' in template
    assert 'layer-upload-viewer.css' in template
    assert 'このアップロードをZIP' in template
    assert 'group.comment' in template


def test_layer_image_endpoint_keeps_existing_permission_checks():
    source = (ROOT / "utils" / "upload_history.py").read_text(encoding="utf-8")
    route = source.split('def layer_upload_image(uuid, reply_uuid, filename):', 1)[1].split(
        '@upload_history_bp.route("/upload_delete', 1,
    )[0]
    assert '_fetch_upload_by_uuid(uuid, scope="layer")' in route
    assert '_ensure_upload_permission(upload, username)' in route
    assert 'layer_reply_file_exists(' in route
    assert 'return send_file(target, conditional=True)' in route


def test_viewer_image_is_fitted_inside_the_available_stage():
    css = (ROOT / "static" / "css" / "layer-upload-viewer.css").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "layer_upload_detail.html").read_text(encoding="utf-8")
    assert "grid-template-rows: minmax(0, 1fr)" in css
    assert ".layer-image-viewer-image-wrap img {\n  position: absolute;\n  inset: 0;\n  display: block;\n  width: 100%;\n  height: 100%;\n  object-fit: contain;" in css
    assert "20261104-fit1" in template
