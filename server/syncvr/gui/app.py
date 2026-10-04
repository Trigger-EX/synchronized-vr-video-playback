"""Qt application entry: error dialog and the main event loop. Needs PySide6."""

import signal
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .bridge import Bridge
from .main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def show_error(title: str, message: str) -> None:
    _app()
    QMessageBox.critical(None, title, message)


def acquire_instance_lock(data_dir: Path):
    """QLockFile held for the life of the process, or None if another SyncVR window owns it."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / "syncvr.lock"))
    lock.setStaleLockTime(0)  # a lock is stale only when its owner process is gone
    return lock if lock.tryLock(100) else None


def self_test(thread, config, log_path: Path, timeout_ms: int = 15000) -> bool:
    """Build the real window, wait for one snapshot through the bridge, close it."""
    app = _app()
    bridge = Bridge(thread)
    win = MainWindow(bridge, config, log_path)
    got = []
    bridge.state.connect(lambda snap: (got.append(snap), app.quit()))
    QTimer.singleShot(timeout_ms, app.quit)
    bridge.start()
    app.exec()
    bridge.close()
    win.close()
    return bool(got) and "devices" in got[0]


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
