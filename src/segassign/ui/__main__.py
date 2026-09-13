"""Run the development UI with ``python -m segassign.ui``."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("SegAssign")
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
