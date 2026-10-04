"""PySide6 control window for the launcher. Imported lazily; needs PySide6."""

import signal
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QVBoxLayout, QLabel, QWidget

from .launcher import REFRESH_MS, dashboard_url, open_path, status_lines


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def show_error(title: str, message: str) -> None:
    _app()
    QMessageBox.critical(None, title, message)


class ControlWindow(QWidget):
    snapshot_ready = Signal(object)

    def __init__(self, thread, config, url_local: str, log_path: Path):
        super().__init__()
        self._thread = thread
        self._config = config
        self._url_local = url_local
        self._log_path = log_path
        self._snap = None
        self._pending = None
        self._closing = False
        self.setWindowTitle("SyncVR")

        layout = QVBoxLayout(self)
        self.title_label = QLabel("SyncVR server is running")
        font = self.title_label.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 3)
        self.title_label.setFont(font)
        self.status_label = QLabel("Starting...")
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        self.buttons = {}
        for key, text, handler in (
                ("dashboard", "Open dashboard", lambda: open_path(self._url_local)),
                ("content", "Open content folder", lambda: open_path(Path(self._config.content_dir).resolve())),
                ("log", "Open log", lambda: open_path(self._log_path)),
                ("stop", "Stop && quit", self.stop_and_quit)):
            b = QPushButton(text)
            b.clicked.connect(lambda _checked=False, h=handler: h())
            layout.addWidget(b)
            self.buttons[key] = b

        self.snapshot_ready.connect(self._on_snapshot)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        if self._closing:
            return
        if not self._thread.running:
            self.status_label.setText("The server stopped. See the log for details.")
            return
        if self._pending is not None:
            return
        fut = self._thread.snapshot_async()
        if fut is None:
            return
        self._pending = fut
        fut.add_done_callback(self._done)

    def _done(self, fut) -> None:  # runs on the server loop thread; the queued signal hops to the GUI thread
        try:
            snap = fut.result()
        except Exception:
            snap = None
        self.snapshot_ready.emit(snap)

    def _on_snapshot(self, snap) -> None:
        self._pending = None
        if self._closing:
            return
        self._snap = snap or self._snap
        url = dashboard_url(self._snap, self._thread.http_port)
        self.status_label.setText("\n".join(status_lines(self._snap, url)))

    def stop_and_quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self.status_label.setText("Stopping...")
        QApplication.processEvents()
        self._thread.stop(5)
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:
        if not self._closing:
            self._closing = True
            self._timer.stop()
            self.status_label.setText("Stopping...")
            QApplication.processEvents()
            self._thread.stop(5)
        event.accept()


def run_window(thread, config, url_local: str, log_path: Path) -> None:
    app = _app()
    win = ControlWindow(thread, config, url_local, log_path)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()  # lets the interpreter run so Ctrl+C is noticed
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)
    win.show()
    app.exec()
    if not win._closing:
        thread.stop(5)
