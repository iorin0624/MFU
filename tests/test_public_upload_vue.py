from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_upload_viewer_keeps_legacy_rollback_and_access_control():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'request.args.get("legacy") != "1"' in source
    assert '@app.get("/view/<uuid>/api")' in source
    assert 'full_access = not bool(upload.get("upload_deleted_at"))' in source
    assert "has_layer_reply_upload_auth(uuid)" in source
    assert '"notice": "" if reply_only_access else str(message_row.get("message") or "").strip()' in source
    assert 'upload.get("mode") == "layer"' in source
    assert 'mode_row.get("enable_layer_upload_url")' in source
    assert "fetch_layer_reply_access_record" in source
    assert 'bool(upload.get("upload_deleted_at"))' in source


def test_public_upload_viewer_uses_requested_labels_without_notice_copy_button():
    component = (
        ROOT
        / "external_login_user"
        / "frontend"
        / "src"
        / "views"
        / "PublicUploadViewer.vue"
    ).read_text(encoding="utf-8")

    assert '<h2 class="notice-alert-title">⚠️お知らせ⚠️</h2>' in component
    assert '<small>必ずお読みください</small>' in component
    assert '<details v-if="data.notice" class="notice-card" :open="noticeOpen"' in component
    assert "mfu-public-notice-collapsed:" in component
    assert "localStorage.getItem(noticeStorageKey()) !== fingerprint" in component
    assert "localStorage.setItem(noticeStorageKey(), activeNoticeFingerprint)" in component
    assert "const noticeParts = computed<NoticePart[]>" in component
    assert "const urlPattern = /https?:\\/\\/" in component
    assert 'target="_blank" rel="noopener noreferrer"' in component
    assert "v-html" not in component
    assert ">閉じる</button>" in component
    assert ">以後折りたたむ</button>" in component
    assert 'ref="albumPanel"' in component
    assert "async function scrollToAlbumStart()" in component
    assert "target.scrollIntoView({ behavior: 'smooth', block: 'start' })" in component
    assert "<h2>折り返し</h2>" in component
    assert 'id="reply"' in component
    assert 'id="replies"' in component
    assert '<details v-if="data.reply.enabled" id="reply"' in component
    assert 'ref="replyDetails"' in component
    assert ':open="focusReplyOnLoad"' in component
    assert "requestedSection === 'reply'" in component
    assert "window.setTimeout(scroll, 350)" in component
    assert '<details v-if="data.reply.canList" id="replies"' in component
    assert 'class="reply-list-embedded reply-collapsible"' in component
    assert component.index('id="reply"') < component.index('id="replies"') < component.index('<section v-if="!data.replyOnly"')
    assert "target instanceof HTMLDetailsElement" in component
    assert "アップロード日時ごとに表示します。" in component
    assert "{{ group.count }}枚" in component
    assert "submitReply" in component
    assert "この回をZIPでDL" not in component
    assert "replyZipDownload" not in component
    assert "zipPrepareUrl" not in component
    assert "お知らせをコピー" not in component
    assert "notice-card" in component
    assert "FILE UPLOAD" in component
    assert "SHARED ALBUM" not in component
    assert "<dt>撮影日</dt>" in component
    assert "<dt>枚数</dt>" in component
    assert "<dt>保存期間</dt>" in component
    assert " 23:59" in component
    assert "data.upload.modeLabel" not in component
    assert 'class="lightbox-filename"' in component
    assert "{{ lightboxFile.name }}" in component
    assert "changeLightboxVisibility(lightboxFile)" in component
    assert "公開に戻す" in component
    assert "非公開にする" in component
    assert '@touchstart.passive="startLightboxSwipe"' in component
    assert '@touchend.passive="finishLightboxSwipe"' in component
    assert "Math.abs(deltaX) < 48" in component
    assert "event.key.toLowerCase() === 'x'" in component
    assert "changeLightboxVisibility(lightboxFile.value)" in component
    assert "new Intl.Collator('ja', { numeric: true, sensitivity: 'base' })" in component
    assert "payload.files = sortFilesByCaptureTime(payload.files)" in component
    assert "cache: 'no-store'" in component
    assert "controller.abort(), 20_000" in component
    assert "通信が時間内に完了しませんでした。再読み込みしてください。" in component
    assert "続きを表示" not in component
    assert "visibleLimit" not in component
    assert "&& !lightboxFile.value.hidden" not in component
    assert "@dblclick.prevent" in component
    styles = (
        ROOT / "external_login_user" / "frontend" / "src" / "public-upload.css"
    ).read_text(encoding="utf-8")
    assert "notice-alert-blink 1.5s" in styles
    assert "0%,66.666%" in styles
    assert "66.667%,100%" in styles
    assert "align-items:stretch" in styles

    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "public_upload_vue.html").read_text(encoding="utf-8")
    assert 'response.headers["Cache-Control"] = "private, no-store, max-age=0"' in app_source
    assert "public-upload-viewer.js') }}?v=20260916-notice-album-focus1" in template


def test_public_upload_viewer_build_artifacts_exist():
    output = ROOT / "static" / "external_login_vue"

    assert (output / "public-upload-viewer.js").is_file()
    assert (output / "public-upload-viewer.css").is_file()
