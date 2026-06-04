from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app import DesktopApp
from config import CONFIG


def main() -> int:
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(CONFIG.app_name)
    qt_app.setQuitOnLastWindowClosed(False)
    style_path = Path(__file__).resolve().parent / "resources" / "styles" / "app.qss"
    if style_path.exists():
        qt_app.setStyleSheet(style_path.read_text(encoding="utf-8"))
    controller = DesktopApp(qt_app)
    controller.start()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
