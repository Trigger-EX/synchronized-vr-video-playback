"""Qt application entry: error dialog and the main event loop. Needs PySide6."""

import signal
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .bridge import Bridge
from .main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def show_error(title: str, message: str) -> None:
    _app()
    QMessageBox.critical(None, title, message)


def run_window(thread, config, log_path: Path) -> None:
    app = _app()
    bridge = Bridge(thread)
    win = MainWindow(bridge, config, log_path)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()  # lets the interpreter run so Ctrl+C is noticed
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)
    bridge.start()
    win.show()
    app.exec()
    bridge.close()
    thread.stop(5)
