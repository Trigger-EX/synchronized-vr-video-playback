import concurrent.futures
import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from syncvr import qt_ui  # noqa: E402


class FakeThread:
    http_port = 8080
    running = True

    def __init__(self):
        self.stopped = None

    def snapshot_async(self):
        f = concurrent.futures.Future()
        f.set_result({"devices": [{"online": True}], "library": [{}, {}], "downloads": {}})
        return f

    def stop(self, timeout=5.0):
        self.stopped = timeout
        self.running = False
        return True


class Cfg:
    content_dir = "."


def test_window_status_and_stop(tmp_path):
    app = QApplication.instance() or QApplication([])
    fake = FakeThread()
    win = qt_ui.ControlWindow(fake, Cfg(), "http://localhost:8080/", tmp_path / "log")
    for _ in range(20):
        app.processEvents()
    text = win.status_label.text()
    assert "Headsets: 1 online of 1 known" in text
    assert "Videos in content folder: 2" in text
    win.buttons["stop"].click()
    assert fake.stopped == 5
    assert win.status_label.text() == "Stopping..."
