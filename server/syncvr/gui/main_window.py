"""Main operator window: tabs, status bar and the playback panel."""

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QTabWidget,
                               QVBoxLayout, QWidget)

from ..launcher import load_overrides, open_path, save_override
from . import format as fmt
from .headsets import HeadsetsTab
from .library import LibraryTab
from .log import LogTab
from .mirror import MirrorManager, embed_supported, find_native_window
from .viewpane import ViewPane
from .playback import PlaybackPanel
from .settings import SettingsTab
from .theme import set_property

TABS = ("Headsets", "Library", "Settings", "Log")
STATUS_MS = 5000


class MainWindow(QMainWindow):
    def __init__(self, bridge, config, log_path: Path, mirror=None, finder=find_native_window, embedder=None):
        super().__init__()
        self.bridge = bridge
        self._config = config
        self._log_path = log_path
        self.mirror = mirror or MirrorManager(tools_dir=load_overrides(
            getattr(config, "data_dir", None) or ".").get("tools_dir", ""))
        self.mirror.on_error = self.show_mirror_error
        self.mirror.on_ready = self.on_mirror_ready
        self._mirror_timer = QTimer(self)
        self._mirror_timer.setInterval(1000)
        self._mirror_timer.timeout.connect(self.mirror.poll)
        self._mirror_timer.start()
        self.snapshot = None
        self.received_at = 0.0
        self.targets = []  # selected headset ids; empty means every headset
        self.setWindowTitle("SyncVR")
        pane_args = {"embedder": embedder} if embedder else {}
        self.view_pane = ViewPane(self, finder, can_embed=embed_supported(), **pane_args)
        self.addDockWidget(Qt.RightDockWidgetArea, self.view_pane)
        self.view_pane.visibilityChanged.connect(lambda vis: vis or self.close_view())
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
        view_act = QAction("&View selected headset", self)
        view_act.triggered.connect(lambda _c=False: self.view_selected())
        self.view_action = view_act
        for text, handler in (("Open &content folder", lambda: open_path(Path(self._config.content_dir).resolve())),
                              ("&Choose content folder...", self.choose_content_folder),
                              ("Set &scrcpy/adb folder...", self.choose_tools_folder),
                              ("Open &log file", lambda: open_path(self._log_path)),
                              ("&Quit", self.close)):
            act = QAction(text, self)
            act.triggered.connect(lambda _checked=False, h=handler: h())
            file_menu.addAction(act)

        file_menu.insertAction(file_menu.actions()[-1], view_act)
        toolbar = self.addToolBar("Headset")
        toolbar.setObjectName("toolbar")
        toolbar.addAction(view_act)

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

    def choose_tools_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder containing scrcpy and adb", self.mirror.tools_dir)
        if not folder:
            return
        self.mirror.tools_dir = folder
        data_dir = getattr(self._config, "data_dir", None)
        if data_dir:
            save_override(data_dir, "tools_dir", folder)
        self.statusBar().showMessage("scrcpy/adb folder set to %s" % folder, STATUS_MS)

    def show_mirror_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def view_headset(self, device_id: str) -> None:
        """Dock the headset's screen; the same headset again closes the view, another one switches to it."""
        if self.view_pane.device_id == device_id:
            self.close_view()
            return
        dev = next((d for d in (self.snapshot or {}).get("devices") or [] if d["id"] == device_id), None)
        if dev is None:
            return
        self.close_view()
        name = dev.get("label") or device_id
        embed = self.view_pane.can_embed
        if self.mirror.start(device_id, dev.get("ip") or "", name, embed):
            self.view_pane.begin(device_id, name, self.mirror.title(name))

    def close_view(self) -> None:
        device_id = self.view_pane.device_id
        if device_id:
            self.mirror.stop(device_id)
        self.view_pane.close_view()

    def on_mirror_ready(self, device_id: str) -> None:
        if device_id == self.view_pane.device_id:
            self.view_pane.attach()

    def view_selected(self) -> None:
        devs = [d for d in self.target_devices() if d.get("online")] if self.targets else []
        if len(devs) != 1:
            self.statusBar().showMessage("Select exactly one online headset to view.", STATUS_MS)
            return
        self.view_headset(devs[0]["id"])

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
        cur = self.view_pane.device_id
        if cur and len(self.targets) == 1 and self.targets[0] != cur:
            self.view_headset(self.targets[0])  # docked view follows the selection
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
        self._mirror_timer.stop()
        self.close_view()
        self.mirror.stop_all()
        self.bridge.close()
        event.accept()
