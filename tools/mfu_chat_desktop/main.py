from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app import DesktopApp
from config import CONFIG


def main() -> int:
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(CONFIG.app_name)
    controller = DesktopApp(qt_app)
    controller.start()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
