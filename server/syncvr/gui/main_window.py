"""Main operator window: tabs, status bar and the playback panel."""

import time
from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..launcher import open_path, save_override
from . import format as fmt
from .headsets import HeadsetsTab
from .library import LibraryTab
from .log import LogTab
from .playback import PlaybackPanel
from .settings import SettingsTab
from .theme import set_property

TABS = ("Headsets", "Library", "Settings", "Log")
STATUS_MS = 5000


class MainWindow(QMainWindow):
    def __init__(self, bridge, config, log_path: Path):
        super().__init__()
        self.bridge = bridge
        self._config = config
        self._log_path = log_path
        self.snapshot = None
        self.received_at = 0.0
        self.targets = []  # selected headset ids; empty means every headset
        self.setWindowTitle("SyncVR")
        self.resize(1100, 760)

        self.tabs = QTabWidget()
        self.headsets = HeadsetsTab(self)
        self.library = LibraryTab(self)
        self.tab_widgets = {"Headsets": self.headsets, "Library": self.library,
                            "Settings": SettingsTab(self), "Log": LogTab(self)}
        for name in TABS:
            self.tabs.addTab(self.tab_widgets[name], name)
        self.playback = PlaybackPanel(self)
        self.stats_label = QLabel("Starting...")
        self.stats_label.setObjectName("stats")
        self.server_label = QLabel("")
        self.server_label.setObjectName("serverName")
        self.conn_label = QLabel("connecting…")
        self.conn_label.setObjectName("conn")
        set_property(self.conn_label, "ok", False)
        header = QFrame()
        header.setObjectName("topbar")
        head = QHBoxLayout(header)
        head.setContentsMargins(16, 10, 16, 10)
        head.setSpacing(14)
        brand = QLabel("SyncVR")
        brand.setObjectName("brand")
        for w in (brand, self.server_label):
            head.addWidget(w)
        head.addWidget(self.stats_label, 1)
        head.addWidget(self.conn_label)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 8, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.playback)
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addWidget(body, 1)
        self.setCentralWidget(central)

        file_menu = self.menuBar().addMenu("&File")
        for text, handler in (("Open &content folder", lambda: open_path(Path(self._config.content_dir).resolve())),
                              ("&Choose content folder...", self.choose_content_folder),
                              ("Open &log file", lambda: open_path(self._log_path)),
                              ("&Quit", self.close)):
            act = QAction(text, self)
            act.triggered.connect(lambda _checked=False, h=handler: h())
            file_menu.addAction(act)

        bar = self.statusBar()
        self.downloads_label = QLabel("")
        self.address_label = QLabel("")
        bar.addWidget(QLabel(""), 1)
        bar.addPermanentWidget(self.downloads_label)
        bar.addPermanentWidget(self.address_label)

        bridge.state.connect(self.on_state)
        bridge.result.connect(self.on_result)
        bridge.failed.connect(self.on_failed)
        bridge.stopped.connect(self.on_stopped)

    def choose_content_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose content folder", str(self._config.content_dir))
        if not folder:
            return
        save_override(self._config.data_dir, "content", folder)
        QMessageBox.information(self, "SyncVR", "Content folder set to:\n%s\n\nRestart SyncVR to use it." % folder)

    def server_now(self) -> float:
        if not self.snapshot:
            return 0.0
        return self.snapshot["server"]["time"] + (time.monotonic() - self.received_at)

    def target_spec(self):
        return list(self.targets) if self.targets else "all"

    def target_devices(self) -> list:
        devices = (self.snapshot or {}).get("devices") or []
        return [d for d in devices if d["id"] in self.targets] if self.targets else devices

    def set_targets(self, ids) -> None:
        self.targets = list(ids)
        self.headsets.update_selection()
        self.playback.update_state()

    def on_state(self, snap) -> None:
        self.snapshot = snap
        self.received_at = time.monotonic()
        known = {d["id"] for d in snap.get("devices") or []}
        self.targets = [i for i in self.targets if i in known]
        self.update_status(snap)
        for widget in self.tab_widgets.values():
            widget.update_state()
        self.playback.update_state()

    def update_status(self, snap) -> None:
        st = fmt.fleet_stats(snap.get("devices") or [])
        text = "%d / %d online · %d playing" % (st["online"], st["total"], st["playing"])
        if st["worst_drift"] is not None:
            text += " · worst drift %.0f ms" % st["worst_drift"]
        self.stats_label.setText(text)
        self.server_label.setText((snap.get("server") or {}).get("name") or "")
        self.conn_label.setText("live")
        set_property(self.conn_label, "ok", True)
        dl = snap.get("downloads") or {}
        active, queued = len(dl.get("active") or []), len(dl.get("queued") or [])
        self.downloads_label.setText("Downloading to %d, %d waiting" % (active, queued) if active or queued else "")
        server = snap.get("server") or {}
        addrs = server.get("addresses") or []
        self.address_label.setText("Operator app address: %s:%s" % (addrs[0], server.get("http_port"))
                                   if addrs else "")

    def on_result(self, action: str, result) -> None:
        if result.get("warning"):
            self.statusBar().showMessage(result["warning"], STATUS_MS)
        elif action == "rescan":
            self.statusBar().showMessage("%d video(s) in library" % result.get("videos", 0), STATUS_MS)

    def on_failed(self, message: str) -> None:
        self.statusBar().showMessage(message, STATUS_MS)

    def on_stopped(self) -> None:
        self.stats_label.setText("The server stopped. See the log for details.")
        self.conn_label.setText("stopped")
        set_property(self.conn_label, "ok", False)

    def closeEvent(self, event) -> None:
        self.bridge.close()
        event.accept()
