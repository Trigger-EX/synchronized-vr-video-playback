"""Main operator window: tabs, status bar and the playback panel."""

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QSpinBox, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QScrollArea, QSizePolicy, QTabWidget,
                               QVBoxLayout, QWidget)

from ..launcher import load_overrides, open_path, save_override
from . import format as fmt
from .headsets import HeadsetsTab
from .library import LibraryTab
from .log import LogTab
from .mirror import BatchPreview, MirrorManager, embed_supported, find_native_window
from .viewpane import ViewPane
from .localplayer import LocalPlayer
from .playback import PlaybackPanel
from .settings import SettingsTab
from . import terminal
from .terminal import TerminalManager
from .theme import set_property
from .tools import CommandConfirmer, ToolsTab

TABS = ("Headsets", "Library", "Tools", "Settings", "Log")
STATUS_MS = 5000


class MainWindow(QMainWindow):
    def __init__(self, bridge, config, log_path: Path, mirror=None, finder=find_native_window, embedder=None,
                 local_player=None, recovered: bool = False):
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
        self._recovery_shown = False
        self.recovered = recovered
        self.received_at = 0.0
        self.targets = []  # selected headset ids; empty means every headset
        self.setWindowTitle("SyncVR")
        pane_args = {"embedder": embedder} if embedder else {}
        self.view_pane = ViewPane(self, finder, can_embed=embed_supported(qt_platform=QGuiApplication.platformName()), **pane_args)
        self.view_pane.fell_back.connect(lambda text: self.statusBar().showMessage(text, STATUS_MS * 2))
        self.addDockWidget(Qt.RightDockWidgetArea, self.view_pane)
        self.view_pane.visibilityChanged.connect(lambda vis: vis or self.close_view())
        self._grown_by = 0
        self.view_pane.visibilityChanged.connect(self._make_room)
        self.view_pane.topLevelChanged.connect(lambda _f: self._make_room(self.view_pane.isVisible()))
        self.resize(1100, 760)
        self.setMinimumSize(560, 360)

        self.tabs = QTabWidget()
        self.confirmer = CommandConfirmer(self)
        self.headsets = HeadsetsTab(self)
        self.library = LibraryTab(self)
        self.tab_widgets = {"Headsets": self.headsets, "Library": self.library,
                            "Tools": ToolsTab(self), "Settings": SettingsTab(self), "Log": LogTab(self)}
        for name in TABS:
            self.tabs.addTab(self.tab_widgets[name], name)
        self.local_player = local_player or LocalPlayer(bridge, getattr(config, "content_dir", "."), self.server_now)
        self.local_player.error.connect(self.on_failed)
        self.local_player.windowClosed.connect(lambda: self.playback.uncheck_local())
        self.playback = PlaybackPanel(self)
        self.playback.localChanged.connect(self.sync_local)
        self.stats_label = QLabel("Starting...")
        self.stats_label.setObjectName("stats")
        self.server_label = QLabel("")
        self.server_label.setObjectName("serverName")
        self.conn_label = QLabel("connecting…")
        self.conn_label.setObjectName("conn")
        set_property(self.conn_label, "ok", False)
        self.show_mode_check = QCheckBox("Show Mode")
        self.show_mode_check.setToolTip("Pause automation (watchdogs) during a show. Manual commands still run.")
        self.show_mode_check.clicked.connect(lambda on: self.bridge.set_show_mode(bool(on)))
        self.brake_label = QLabel("Automation free")
        self.brake_label.setObjectName("brake")
        set_property(self.brake_label, "active", False)
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
        head.addWidget(self.show_mode_check)
        head.addWidget(self.brake_label)
        head.addWidget(self.conn_label)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 8, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(self.tabs, 1)
        # Playback sits in a scroll area so a short window can still reach every control.
        play_scroll = QScrollArea()
        play_scroll.setWidgetResizable(True)
        play_scroll.setFrameShape(QFrame.NoFrame)
        play_scroll.setWidget(self.playback)
        play_scroll.setMinimumHeight(110)
        play_scroll.setMaximumHeight(max(self.playback.sizeHint().height() + 4, 110))
        play_scroll.setSizePolicy(play_scroll.sizePolicy().horizontalPolicy(), QSizePolicy.Preferred)
        self.tabs.setMinimumHeight(120)
        lay.addWidget(play_scroll)
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

        pop_act = QAction("&Pop out headset view", self, checkable=True)
        pop_act.toggled.connect(self.view_pane.setFloating)
        self.view_pane.topLevelChanged.connect(pop_act.setChecked)
        self.pop_action = pop_act
        feed_act = QAction("Show &both eyes (full feed)", self, checkable=True)
        feed_act.setChecked(self.mirror.full_feed)
        feed_act.toggled.connect(self.set_full_feed)
        self.feed_action = feed_act
        for act in (view_act, pop_act, feed_act):
            file_menu.insertAction(file_menu.actions()[-1], act)
        view_menu = self.menuBar().addMenu("&View")
        self.capture_actions = {}
        for key, text, handler in (("selected", "Capture &selected", lambda: self.capture(False)),
                                   ("all", "Capture &all", lambda: self.capture(True)),
                                   ("batch", "&Batch preview...", self.batch_preview_dialog),
                                   ("close", "&Close all captures", self.close_captures)):
            act = QAction(text, self)
            act.triggered.connect(lambda _c=False, h=handler: h())
            view_menu.addAction(act)
            self.capture_actions[key] = act
        view_menu.addSeparator()
        view_menu.setToolTipsVisible(True)
        self.terminals = TerminalManager(self)
        self.terminal_actions = {}
        for key, text, handler in (("local", "Open &local terminal", lambda: self.open_terminal()),
                                   ("headset", "Open terminal on selected &headset", self.open_headset_terminal)):
            act = QAction(text, self)
            act.triggered.connect(lambda _c=False, h=handler: h())
            act.setEnabled(terminal.AVAILABLE)
            act.setToolTip("" if terminal.AVAILABLE else terminal.UNAVAILABLE_TIP)
            view_menu.addAction(act)
            self.terminal_actions[key] = act
        self.batch = None
        self._batch_timer = QTimer(self)
        self._batch_timer.setInterval(1000)
        self._batch_timer.timeout.connect(lambda: self.batch and self.batch.tick())
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
        if recovered:
            self.show_recovered_banner()

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

    def _make_room(self, visible: bool) -> None:
        """Docking the view widens the window by its width (and gives it back), so nothing is covered."""
        want = self.view_pane.width() + 8 if visible and not self.view_pane.isFloating() else 0
        if want == self._grown_by or self.isMaximized() or self.isFullScreen():
            self._grown_by = want if (self.isMaximized() or self.isFullScreen()) else self._grown_by
            return
        self.resize(self.width() + want - self._grown_by, self.height())
        self._grown_by = want

    def set_full_feed(self, full: bool) -> None:
        self.mirror.full_feed = full
        device_id = self.view_pane.device_id
        if device_id:  # restart the open view with the new crop
            self.close_view()
            self.view_headset(device_id)

    def close_view(self) -> None:
        device_id = self.view_pane.device_id
        if device_id:
            self.mirror.stop(device_id)
        self.view_pane.close_view()

    def screen_size(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        return (geo.width(), geo.height()) if geo else (1920, 1080)

    def _capture_devices(self, everything: bool) -> list:
        devs = (self.snapshot or {}).get("devices") or []
        if not everything and self.targets:
            devs = [d for d in devs if d["id"] in self.targets]
        return [{"id": d["id"], "ip": d.get("ip") or "", "label": d.get("label") or d["id"]}
                for d in devs if d.get("online")]

    def capture(self, everything: bool) -> None:
        devs = self._capture_devices(everything)
        if not devs:
            self.statusBar().showMessage("No online headsets to capture.", STATUS_MS)
            return
        started = self.mirror.start_many(devs, screen=self.screen_size())
        self.statusBar().showMessage("Capturing %d of %d headsets." % (len(started), len(devs)), STATUS_MS)

    def batch_preview_dialog(self) -> None:
        if self.batch and self.batch.running:
            self.stop_batch()
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Batch preview")
        form = QFormLayout(dlg)
        size, dwell = QSpinBox(), QSpinBox()
        size.setRange(1, 6)
        size.setValue(4)
        dwell.setRange(1, 600)
        dwell.setValue(10)
        dwell.setSuffix(" s")
        form.addRow("Headsets per group", size)
        form.addRow("Seconds per group", dwell)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() == QDialog.Accepted:
            self.start_batch(size.value(), dwell.value())

    def start_batch(self, group_size: int, dwell: float) -> bool:
        devs = self._capture_devices(False)
        if not devs:
            self.statusBar().showMessage("No online headsets to preview.", STATUS_MS)
            return False
        size = self.screen_size()
        self.batch = BatchPreview(
            devs, group_size, dwell, lambda group: self.mirror.start_many(group, screen=size),
            lambda group: [self.mirror.stop(d["id"]) for d in group])
        self._batch_timer.start()
        return self.batch.start()

    def stop_batch(self) -> None:
        self._batch_timer.stop()
        if self.batch:
            self.batch.stop()
            self.batch = None

    def close_captures(self) -> None:
        self.stop_batch()
        n = self.mirror.close_all()
        self.statusBar().showMessage("Closed %d captures." % n, STATUS_MS)

    def on_mirror_ready(self, device_id: str) -> None:
        if device_id == self.view_pane.device_id:
            self.view_pane.attach()

    def view_selected(self) -> None:
        devs = [d for d in self.target_devices() if d.get("online")] if self.targets else []
        if len(devs) != 1:
            self.statusBar().showMessage("Select exactly one online headset to view.", STATUS_MS)
            return
        self.view_headset(devs[0]["id"])

    def selected_online_id(self):
        devs = [d for d in self.target_devices() if d.get("online")] if self.targets else []
        return devs[0]["id"] if len(devs) == 1 else None

    def open_terminal(self, device_id=None, testing: bool = False) -> None:
        """A local shell, or one on the headset `device_id`; refused unless `terminal.shell` is tested or `testing`."""
        if device_id:
            self.terminals.open_headset(device_id, testing)
        else:
            self.terminals.open_local(testing)

    def open_headset_terminal(self) -> None:
        device_id = self.selected_online_id()
        if device_id is None:
            self.statusBar().showMessage("Select exactly one online headset to open a terminal on.", STATUS_MS)
            return
        self.open_terminal(device_id)

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
        self.sync_local()

    def show_recovered_banner(self) -> None:
        self.statusBar().showMessage("SyncVR restarted after a crash; rejoining headsets...", STATUS_MS * 6)

    def _check_recovery(self, snap) -> None:
        rec = (snap.get("server") or {}).get("recovery") or {}
        if self._recovery_shown or not rec or rec.get("active") or not self.recovered:
            return
        self._recovery_shown = True
        pos = int(rec.get("position") or 0)
        self.statusBar().showMessage("Recovered show: %d headsets rejoined at %02d:%02d"
                                     % (int(rec.get("rejoined") or 0), pos // 60, pos % 60), STATUS_MS * 6)

    def on_state(self, snap) -> None:
        self.snapshot = snap
        self._check_recovery(snap)
        self.received_at = time.monotonic()
        known = {d["id"] for d in snap.get("devices") or []}
        self.targets = [i for i in self.targets if i in known]
        self.update_status(snap)
        for widget in self.tab_widgets.values():
            widget.update_state()
        self.playback.update_state()
        self.sync_local()

    def sync_local(self) -> None:
        lp, pb = self.local_player, self.playback
        lp.set_mode(pb.local_mode())
        focus = pb.focus_device()
        lp.update(self.snapshot, focus["id"] if focus else None)
        lp.set_follow(pb.follow_device_id())

    def update_status(self, snap) -> None:
        st = fmt.fleet_stats(snap.get("devices") or [])
        text = "%d / %d online · %d playing" % (st["online"], st["total"], st["playing"])
        if st["worst_drift"] is not None:
            text += " · worst drift %.0f ms" % st["worst_drift"]
        self.stats_label.setText(text)
        self.server_label.setText((snap.get("server") or {}).get("name") or "")
        self.conn_label.setText("live")
        set_property(self.conn_label, "ok", True)
        self.update_brake(snap.get("brake") or {})
        dl = snap.get("downloads") or {}
        active, queued = len(dl.get("active") or []), len(dl.get("queued") or [])
        self.downloads_label.setText("Downloading to %d, %d waiting" % (active, queued) if active or queued else "")
        server = snap.get("server") or {}
        addrs = server.get("addresses") or []
        self.address_label.setText("Operator app address: %s:%s" % (addrs[0], server.get("http_port"))
                                   if addrs else "")

    def update_brake(self, brake: dict) -> None:
        active = bool(brake.get("active"))
        reason = brake.get("reason") or ""
        self.show_mode_check.setChecked(bool(brake.get("show_mode")))  # setChecked does not emit clicked
        self.brake_label.setText("Automation paused: %s" % ("Show Mode" if reason == "show_mode" else reason)
                                 if active else "Automation free")
        set_property(self.brake_label, "active", active)

    def on_result(self, action: str, result) -> None:
        if result.get("warning"):
            self.statusBar().showMessage(result["warning"], STATUS_MS)
        elif result.get("job"):
            self.statusBar().showMessage("%s started; results are in the Log tab" % action.replace("_", " ").capitalize(),
                                         STATUS_MS)
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
        self.local_player.stop()
        self.close_view()
        self.stop_batch()
        self.terminals.close_all()
        self.mirror.stop_all()
        self.bridge.close()
        event.accept()
