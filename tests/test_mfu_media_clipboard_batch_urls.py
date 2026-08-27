from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_module():
    source = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "mfu_media_clipboard"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("mfu_media_clipboard_batch_test", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_media_urls_keeps_order_removes_duplicates_and_trailing_punctuation():
    module = _load_module()
    instagram = "https://www.instagram.com/reel/DWFtwowE03H/?g=5"
    threads = "https://www.threads.com/@akemiuka1024/post/Db8IAnxIH5j"
    x_url = "https://x.com/example/status/1234567890"

    urls = module.extract_media_urls(
        f"最初 {instagram}\nThreads {threads}\n次 {x_url}。\n重複 {instagram}"
    )

    assert urls == [instagram, threads, x_url]


def test_extract_media_urls_accepts_instagram_story_urls():
    module = _load_module()
    story_list = "https://www.instagram.com/stories/zz_4869/"
    story_item = "https://www.instagram.com/stories/zz_4869/12345678901234567/?utm_source=ig_story_item_share"

    assert module.extract_media_urls(f"{story_list}\n{story_item}") == [
        story_list,
        story_item,
    ]


def test_extract_media_urls_accepts_any_path_on_supported_domains():
    module = _load_module()
    urls = [
        "https://x.com/home?from=test",
        "https://m.instagram.com/new-kind/abc123?share=1",
        "https://www.threads.com/@example/unknown-format/xyz",
    ]

    assert module.extract_media_urls("\n".join(urls)) == urls


def test_extract_media_urls_rejects_lookalike_and_unrelated_domains():
    module = _load_module()

    assert module.extract_media_urls(
        "https://instagram.com.example.jp/p/abc\n"
        "https://example.jp/?next=https://x.com/example/status/1"
    ) == []


def test_manual_url_dialog_accepts_multiple_urls_and_enforces_batch_limit():
    module = _load_module()
    app = module.QApplication.instance() or module.QApplication([])
    dialog = module.ManualUrlDialog()
    first = "https://www.instagram.com/p/Abc_def-123/"
    second = "https://twitter.com/example/status/1234567890"

    dialog.url_edit.setPlainText(f"{first}\n{second}")
    app.processEvents()

    assert dialog.urls() == [first, second]
    assert dialog.ok_button.isEnabled()
    assert "2件" in dialog.url_count.text()

    too_many = "\n".join(
        f"https://x.com/example/status/{1000000000 + index}"
        for index in range(module.MAX_BATCH_URLS + 1)
    )
    dialog.url_edit.setPlainText(too_many)
    app.processEvents()

    assert len(dialog.urls()) == module.MAX_BATCH_URLS + 1
    assert not dialog.ok_button.isEnabled()
    dialog.close()


def test_manual_url_dialog_appends_supported_clipboard_urls_without_duplicates():
    module = _load_module()
    app = module.QApplication.instance() or module.QApplication([])
    clipboard = app.clipboard()
    clipboard.clear()
    dialog = module.ManualUrlDialog()
    first = "https://www.instagram.com/p/first/"
    second = "https://www.threads.com/@example/post/second"

    clipboard.setText(first)
    app.processEvents()
    dialog._check_clipboard()
    clipboard.setText(f"{first}\n{second}")
    app.processEvents()
    dialog._check_clipboard()

    assert dialog.urls() == [first, second]
    dialog.close()


def test_batch_queue_is_sequential_and_supports_retry_and_cancellation():
    module = _load_module()
    app = module.QApplication.instance() or module.QApplication([])
    started: list[str] = []

    class FakeSignal:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

        def emit(self, value):
            assert self.callback
            self.callback(value)

    class FakeSignals:
        def __init__(self):
            self.progress = FakeSignal()
            self.finished = FakeSignal()
            self.failed = FakeSignal()

    class FakeWorker:
        def __init__(self, _api, url, _kinds):
            self.url = url
            self.signals = FakeSignals()
            started.append(url)

        def is_alive(self):
            return False

        def start(self):
            if self.url.endswith("/2"):
                self.signals.failed.emit("取得テスト失敗")
            else:
                self.signals.finished.emit(
                    {"images": None, "videos": None, "errors": []}
                )

    module.FetchWorker = FakeWorker
    controller = module.MediaClipboardApp.__new__(module.MediaClipboardApp)
    module.QObject.__init__(controller)
    controller.api = object()
    controller.pending_urls = []
    controller.last_seen_urls = ()
    controller.worker = None
    controller.progress = None
    controller.batch_urls = []
    controller.batch_kinds = []
    controller.batch_results = []
    controller.batch_index = 0
    controller.batch_cancel_requested = False
    controller.current_url = ""
    summaries = []
    controller._show_batch_summary = lambda results, cancelled, kinds: summaries.append(
        (results, cancelled, kinds)
    )
    urls = [
        "https://x.com/example/status/1",
        "https://x.com/example/status/2",
        "https://x.com/example/status/3",
    ]

    controller._start_batch(urls, ["images"])
    for _ in range(10):
        app.processEvents()

    assert started == urls
    assert len(summaries) == 1
    assert len(summaries[0][0]) == 3
    assert summaries[0][0][1]["errors"] == ["取得テスト失敗"]
    assert summaries[0][1] == 0

    source = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "mfu_media_clipboard"
        / "main.py"
    ).read_text(encoding="utf-8")

    assert "def _cancel_batch(self)" in source
    assert 'box.addButton("失敗したURLだけ再試行"' in source
