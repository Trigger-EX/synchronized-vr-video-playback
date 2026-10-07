"""Tools tab (Power, Network and Testing cards) and the preview-then-confirm flow every fleet adb action goes through."""

import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..automation import SHOW_MODE_WARNING
from . import format as fmt
from . import terminal
from .theme import mark, set_property

POWER_ACTIONS = (("sleep", "Sleep"), ("wake", "Wake"), ("screen_refresh", "Screen refresh"))
POWEROFF_FEATURE = "power.poweroff"
SNAPSHOT_FEATURE = "debug.snapshot"
PURGE_FEATURE = "adb.purge"
BANDWIDTH_FEATURE = "debug.bandwidth"
TERMINAL_FEATURE = terminal.FEATURE
SNAPSHOT_DIR = "snapshots"  # under the data folder; see diagnostics.SNAPSHOT_DIR
STATUS_MS = 5000


class ConfirmDialog(QDialog):
    """Shows the server's own description of what a command will hit; nothing runs until it is accepted."""

    def __init__(self, preview: dict, parent=None):
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle("Confirm")
        self.scope_label = QLabel(preview.get("scope_text", ""))
        self.scope_label.setObjectName("target")
        self.scope_label.setWordWrap(True)
        self.show_label = QLabel(escape(SHOW_MODE_WARNING))
        self.show_label.setObjectName("error")
        self.show_label.setWordWrap(True)
        self.show_label.setVisible(bool(preview.get("show_mode")))
        self.dry_run_check = QCheckBox("Dry run (log what would happen, send nothing)")
        self.dry_run_check.setChecked(bool(preview.get("dry_run", True)))
        self.dry_run_check.setVisible(bool(preview.get("dry_run_choice")))
        others = [w for w in preview.get("warnings") or [] if w != SHOW_MODE_WARNING  # shown above instead
                  and not (preview.get("dry_run_choice") and w.startswith("Dry run:"))]  # the tick says it
        self.warning_label = QLabel("<br>".join(escape(w) for w in others))
        self.warning_label.setTextFormat(Qt.RichText)
        self.warning_label.setWordWrap(True)
        self.warning_label.setObjectName("error")
        self.warning_label.setVisible(bool(others))
        self.every_check = QCheckBox("I understand this affects EVERY headset")
        self.every_check.setVisible(bool(preview.get("every")) and bool(preview.get("destructive")))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText(preview.get("verb") or "Confirm")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.every_check.toggled.connect(lambda _c: self._sync())
        lay = QVBoxLayout(self)
        for w in (self.scope_label, self.show_label, self.warning_label, self.dry_run_check, self.every_check,
                  self.buttons):
            lay.addWidget(w)
        self._sync()

    def _sync(self) -> None:
        self.ok_button.setEnabled(not self.every_check.isVisibleTo(self) or self.every_check.isChecked())

    def dry_run(self) -> bool:
        return self.dry_run_check.isChecked()


class PoweroffDialog(QDialog):
    """Every headset listed with its own checkbox, all unticked; dry run is on until the operator turns it off."""

    def __init__(self, devices: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Power off headsets")
        self.setMinimumSize(360, 400)
        intro = QLabel("Tick the headsets to power off. A powered-off headset has to be turned on by hand.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        for dev in fmt.sort_devices(devices):
            item = QListWidgetItem((dev.get("label") or dev["id"]) + ("" if dev.get("online") else " (offline)"))
            item.setData(Qt.UserRole, dev["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)
        self.dry_run_check = QCheckBox("Dry run (log what would happen, send nothing)")
        self.dry_run_check.setChecked(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Preview...")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.list.itemChanged.connect(lambda _i: self._sync())
        lay = QVBoxLayout(self)
        for w in (intro, self.list, self.dry_run_check, self.buttons):
            lay.addWidget(w)
        self._sync()

    def _sync(self) -> None:
        self.ok_button.setEnabled(bool(self.selected_ids()))

    def selected_ids(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def dry_run(self) -> bool:
        return self.dry_run_check.isChecked()


class ArmDialog(QDialog):
    """Quotes the exact rule a watchdog will follow once armed. Nothing is armed until it is accepted."""

    def __init__(self, wd: dict, parent=None):
        super().__init__(parent)
        self.wd = wd
        self.setWindowTitle("Arm %s" % wd.get("title", wd.get("name", "")))
        self.setMinimumWidth(420)
        intro = QLabel("Once armed, this watchdog sends commands to headsets on its own, by this rule:")
        intro.setWordWrap(True)
        self.rule_label = QLabel(wd.get("rule", ""))
        self.rule_label.setObjectName("target")
        self.rule_label.setWordWrap(True)
        self.rule_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.untested_label = QLabel("This watchdog is not marked tested on real headsets. Arming it now is a test run.")
        self.untested_label.setObjectName("error")
        self.untested_label.setWordWrap(True)
        self.untested_label.setVisible(not wd.get("tested"))
        self.note = QLabel("Show Mode and active downloads still stop it from sending. Arming is forgotten when "
                           "the server restarts.")
        self.note.setObjectName("fieldHelp")
        self.note.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Arm")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        for w in (intro, self.rule_label, self.untested_label, self.note, self.buttons):
            lay.addWidget(w)


class PatternDialog(QDialog):
    """Matches of a pattern in a snapshot folder. Confirming is what lets the overheat watchdog be armed."""

    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle("Pattern test")
        self.setMinimumSize(520, 360)
        if result.get("error"):
            head = "Cannot test: %s" % result["error"]
        else:
            head = "%d line%s matched in %s" % (result.get("total", 0), "" if result.get("total") == 1 else "s",
                                                 ", ".join(result.get("files") or []))
        self.head_label = QLabel(head)
        self.head_label.setObjectName("target")
        self.head_label.setWordWrap(True)
        self.list = QListWidget()
        for m in result.get("matches") or []:
            self.list.addItem("%s:%d  %s" % (m["file"], m["line_no"], m["line"]))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.confirm_button = self.buttons.button(QDialogButtonBox.Ok)
        self.confirm_button.setText("Confirm pattern")
        self.confirm_button.setEnabled(not result.get("error") and bool(result.get("total")))
        self.buttons.button(QDialogButtonBox.Cancel).setText("Close")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        for w in (self.head_label, self.list, self.buttons):
            lay.addWidget(w)


class CommandConfirmer:
    """Preview -> confirm dialog -> command with the server's token. One per window; owned by the main window."""

    def __init__(self, window):
        self._window = window
        window.bridge.previewed.connect(self.on_previewed)

    def request(self, action: str, targets, **params) -> None:
        self._window.bridge.preview(action, targets, **params)

    def _exec(self, dialog) -> bool:
        return dialog.exec() == QDialog.Accepted

    def on_previewed(self, action: str, preview, request) -> None:
        if not preview.get("needs_confirm"):
            self._send(action, preview, request)
            return
        shown = dict(preview, destructive=action == "poweroff" and request.get("dry_run", True) is False,
                     verb=dict(POWER_ACTIONS, poweroff="Power off", purge="Purge").get(action, "Confirm"),
                     dry_run=request.get("dry_run", True))
        dialog = ConfirmDialog(shown, self._window)
        if not self._exec(dialog):
            self._window.statusBar().showMessage("Cancelled: %s" % preview.get("scope_text", action), STATUS_MS)
            return
        if preview.get("dry_run_choice"):  # the dialog's tick decides; the server priced both tokens
            dry = dialog.dry_run()
            preview = dict(preview, token=(preview.get("tokens") or {}).get("dry_run" if dry else "live"))
            request = dict(request, dry_run=dry)
        self._send(action, preview, request)

    def _send(self, action: str, preview: dict, request: dict) -> None:
        params = {k: v for k, v in request.items() if k != "targets"}
        params["confirm"] = preview.get("token")
        if preview.get("every"):
            params["confirm_every"] = True
        self._window.bridge.confirmed_command(action, request.get("targets"), **params)


class ToolsTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._features = []

        self.target_label = QLabel()
        self.target_label.setObjectName("target")
        self.asleep_label = QLabel()
        self.asleep_label.setObjectName("muted")
        self.power_buttons = {}
        row = QHBoxLayout()
        for action, text in POWER_ACTIONS:
            button = QPushButton(text)
            button.clicked.connect(lambda _c=False, a=action: self.run_power(a))
            self.power_buttons[action] = button
            row.addWidget(button)
        self.poweroff_button = QPushButton("Power off...")
        mark(self.poweroff_button, danger=True)
        self.poweroff_button.clicked.connect(lambda _c=False: self.open_poweroff())
        row.addWidget(self.poweroff_button)
        row.addStretch(1)
        self.power_note = QLabel("Sleep and Wake act on the selected headsets (all when none are selected). "
                                 "Screen refresh sleeps then wakes each one and interrupts playback. "
                                 "Power off asks which headsets, every time.")
        self.power_note.setWordWrap(True)
        self.power_note.setObjectName("fieldHelp")
        power = QGroupBox("POWER")
        power.setObjectName("toolCard")
        lay = QVBoxLayout(power)
        for item in (self.target_label, row, self.power_note, self.asleep_label):
            lay.addLayout(item) if hasattr(item, "addWidget") else lay.addWidget(item)

        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.clicked.connect(lambda _c=False: self.run_snapshot())
        self.screenshot_check = QCheckBox("Include screenshot")
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(lambda _c=False: self.open_snapshots_folder())
        drow = QHBoxLayout()
        for w in (self.snapshot_button, self.screenshot_check, self.open_folder_button):
            drow.addWidget(w)
        drow.addStretch(1)
        self.debug_note = QLabel("Snapshot saves dumpsys, logcat and properties of the selected headsets (all when "
                                 "none are selected) to the snapshots folder. It only reads from the headsets.")
        self.debug_note.setWordWrap(True)
        self.debug_note.setObjectName("fieldHelp")
        debug = QGroupBox("DEBUG")
        debug.setObjectName("toolCard")
        dlay = QVBoxLayout(debug)
        dlay.addLayout(drow)
        dlay.addWidget(self.debug_note)

        self.connect_button = QPushButton("Connect all")
        self.connect_button.clicked.connect(lambda _c=False: self.run_connect())
        self.purge_button = QPushButton("Purge...")
        mark(self.purge_button, danger=True)
        self.purge_button.clicked.connect(lambda _c=False: self.run_purge())
        self.scan_input = QLineEdit()
        self.scan_input.setPlaceholderText("Subnet to scan, e.g. 192.168.1.0/27 (private, at most 32 hosts)")
        self.scan_button = QPushButton("Scan subnet...")
        self.scan_button.clicked.connect(lambda _c=False: self.run_scan())
        self.bandwidth_button = QPushButton("Bandwidth test")
        self.bandwidth_button.clicked.connect(lambda _c=False: self.run_bandwidth())
        nrow = QHBoxLayout()
        for w in (self.connect_button, self.purge_button, self.bandwidth_button):
            nrow.addWidget(w)
        nrow.addStretch(1)
        srow = QHBoxLayout()
        srow.addWidget(self.scan_input, 1)
        srow.addWidget(self.scan_button)
        self.network_note = QLabel("Connect all runs adb connect to every saved headset address. Purge drops those "
                                   "connections and reconnects them (dry run unless you untick it). Scan probes a "
                                   "small private subnet and connects known headsets. Bandwidth test makes the "
                                   "selected idle headsets download part of a video and reports Mbps.")
        self.network_note.setWordWrap(True)
        self.network_note.setObjectName("fieldHelp")
        network = QGroupBox("NETWORK")
        network.setObjectName("toolCard")
        nlay = QVBoxLayout(network)
        nlay.addLayout(nrow)
        nlay.addLayout(srow)
        nlay.addWidget(self.network_note)

        self.wd_rows = {}
        self._watchdogs = {}
        self.wd_grid = QGridLayout()
        self.wd_empty = QLabel("Waiting for the server...")
        self.wd_empty.setObjectName("muted")
        wd_note = QLabel("Watchdogs start observe-only: they log what they would do and send nothing. Arming "
                         "lets one send, by the rule shown when you arm it. Arming is forgotten on restart.")
        wd_note.setWordWrap(True)
        wd_note.setObjectName("fieldHelp")
        wd_card = QGroupBox("WATCHDOGS")
        wd_card.setObjectName("toolCard")
        wlay = QVBoxLayout(wd_card)
        wlay.addWidget(wd_note)
        wlay.addLayout(self.wd_grid)
        wlay.addWidget(self.wd_empty)
        self._last_test = None
        self._window.bridge.result.connect(self.on_bridge_result)

        self.testing_grid = QGridLayout()
        self.testing_empty = QLabel("Every feature has been marked as tested.")
        self.testing_empty.setObjectName("muted")
        testing_note = QLabel("These features have not been proven on real headsets yet. Gated ones run only "
                              "from here, with a warning, until you mark them tested.")
        testing_note.setWordWrap(True)
        testing_note.setObjectName("fieldHelp")
        testing = QGroupBox("TESTING")
        testing.setObjectName("toolCard")
        tlay = QVBoxLayout(testing)
        tlay.addWidget(testing_note)
        tlay.addLayout(self.testing_grid)
        tlay.addWidget(self.testing_empty)
        self.testing_buttons = {}

        page = QWidget()
        page.setObjectName("tabPage")
        play = QVBoxLayout(page)
        play.setContentsMargins(20, 18, 20, 18)
        play.setSpacing(18)
        play.addWidget(power)
        play.addWidget(debug)
        play.addWidget(network)
        play.addWidget(wd_card)
        play.addWidget(testing)
        play.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.update_state()

    # ------------------------------------------------------------ power

    def run_power(self, action: str) -> None:
        self._window.confirmer.request(action, self._window.target_spec())

    def poweroff_tested(self) -> bool:
        return any(f.get("key") == POWEROFF_FEATURE and f.get("tested") for f in self._features)

    def open_poweroff(self, testing: bool = False) -> None:
        devices = (self._window.snapshot or {}).get("devices") or []
        if not devices:
            self._window.statusBar().showMessage("No headsets to power off.", STATUS_MS)
            return
        dialog = PoweroffDialog(devices, self)
        if not self._exec(dialog):
            return
        params = {"dry_run": dialog.dry_run()}
        if testing or not self.poweroff_tested():
            params["testing"] = True
        self._window.confirmer.request("poweroff", dialog.selected_ids(), **params)

    def _exec(self, dialog) -> bool:
        return dialog.exec() == QDialog.Accepted

    # ----------------------------------------------------------- network

    def _tested(self, key: str) -> bool:
        return any(f.get("key") == key and f.get("tested") for f in self._features)

    def run_connect(self) -> None:
        self._window.bridge.command("connect", "all")

    def run_purge(self, testing: bool = False) -> None:
        params = {"testing": True} if testing or not self._tested(PURGE_FEATURE) else {}
        self._window.confirmer.request("purge", self._window.target_spec(), **params)

    def run_scan(self) -> None:
        cidr = self.scan_input.text().strip()
        if not cidr:
            self._window.statusBar().showMessage("Enter a subnet to scan, e.g. 192.168.1.0/27.", STATUS_MS)
            return
        self._window.bridge.scan(cidr)

    def run_bandwidth(self, testing: bool = False) -> None:
        params = {"testing": True} if testing or not self._tested(BANDWIDTH_FEATURE) else {}
        self._window.confirmer.request("bandwidth_test", self._window.target_spec(), **params)

    # ------------------------------------------------------------ debug

    def snapshot_tested(self) -> bool:
        return any(f.get("key") == SNAPSHOT_FEATURE and f.get("tested") for f in self._features)

    def run_snapshot(self, testing: bool = False) -> None:
        params = {"screenshot": self.screenshot_check.isChecked()}
        if testing or not self.snapshot_tested():
            params["testing"] = True
        self._window.confirmer.request("snapshot", self._window.target_spec(), **params)

    def snapshots_dir(self):
        data_dir = getattr(self._window._config, "data_dir", None)
        return Path(data_dir) / SNAPSHOT_DIR if data_dir else None

    def open_snapshots_folder(self) -> None:
        folder = self.snapshots_dir()
        if folder is None:
            self._window.statusBar().showMessage("No data folder is configured.", STATUS_MS)
            return
        folder.mkdir(parents=True, exist_ok=True)
        self._open_url(QUrl.fromLocalFile(str(folder)))

    def _open_url(self, url) -> bool:
        return QDesktopServices.openUrl(url)

    # ----------------------------------------------------------- testing

    def open_terminal(self, testing: bool = False) -> None:
        self._window.open_terminal(self._window.selected_online_id(), testing)

    def mark_tested(self, key: str) -> None:
        self._window.bridge.set_feature_tested(key, True)

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        self._features = snap.get("features") or []
        n = len(self._window.targets)
        self.target_label.setText("%d selected headset%s" % (n, "" if n == 1 else "s") if n else "All headsets")
        asleep = snap.get("asleep") or []
        self.asleep_label.setText("%d headset%s put to sleep from here" % (len(asleep), "" if len(asleep) == 1 else "s")
                                  if asleep else "")
        self.poweroff_button.setEnabled(self.poweroff_tested())
        self.poweroff_button.setToolTip("" if self.poweroff_tested() else
                                        "Not marked tested yet: use Open in the Testing card")
        self.snapshot_button.setEnabled(self.snapshot_tested())
        self.snapshot_button.setToolTip("" if self.snapshot_tested() else
                                        "Not marked tested yet: use Open in the Testing card")
        for key, button in ((PURGE_FEATURE, self.purge_button), (BANDWIDTH_FEATURE, self.bandwidth_button)):
            button.setEnabled(self._tested(key))
            button.setToolTip("" if self._tested(key) else "Not marked tested yet: use Open in the Testing card")
        untested = [f for f in self._features if not f.get("tested")]
        keys = [f["key"] for f in untested]
        if keys != list(self.testing_buttons):
            while self.testing_grid.count():
                w = self.testing_grid.takeAt(0).widget()
                if w is not None:
                    w.deleteLater()
            self.testing_buttons = {}
            for i, f in enumerate(untested):
                self.testing_grid.addWidget(QLabel(f["label"]), i, 0)
                cat = QLabel(f.get("category", ""))
                cat.setObjectName("muted")
                self.testing_grid.addWidget(cat, i, 1)
                column = 2
                if f["key"] == SNAPSHOT_FEATURE:
                    opener = QPushButton("Open")
                    opener.clicked.connect(lambda _c=False: self.run_snapshot(testing=True))
                    self.testing_grid.addWidget(opener, i, column)
                    column += 1
                if f["key"] == POWEROFF_FEATURE:
                    opener = QPushButton("Open")
                    opener.clicked.connect(lambda _c=False: self.open_poweroff(testing=True))
                    self.testing_grid.addWidget(opener, i, column)
                    column += 1
                for key, opener_fn in ((PURGE_FEATURE, self.run_purge), (BANDWIDTH_FEATURE, self.run_bandwidth)):
                    if f["key"] == key:
                        opener = QPushButton("Open")
                        opener.clicked.connect(lambda _c=False, fn=opener_fn: fn(testing=True))
                        self.testing_grid.addWidget(opener, i, column)
                        column += 1
                if f["key"] == TERMINAL_FEATURE:
                    opener = QPushButton("Open")
                    opener.setToolTip("Open a terminal for testing: on the selected headset, or local when none is selected")
                    opener.setEnabled(terminal.AVAILABLE)
                    opener.clicked.connect(lambda _c=False: self.open_terminal(testing=True))
                    self.testing_grid.addWidget(opener, i, column)
                    column += 1
                button = QPushButton("Mark tested")
                button.clicked.connect(lambda _c=False, k=f["key"]: self.mark_tested(k))
                self.testing_grid.addWidget(button, i, column)
                self.testing_buttons[f["key"]] = button
            self.testing_grid.setColumnStretch(0, 1)
        self.testing_empty.setVisible(not untested)
        self.update_watchdogs(snap.get("watchdogs") or [])

    # --------------------------------------------------------- watchdogs

    def _build_watchdog_row(self, wd: dict, row: int) -> dict:
        name = wd["name"]
        r = {"title": QLabel(wd.get("title") or name), "pill": QLabel(), "enable": QCheckBox("Enabled"),
             "arm": QPushButton("Arm..."), "summary": QLabel()}
        r["pill"].setObjectName("badge")
        r["summary"].setObjectName("muted")
        r["summary"].setWordWrap(True)
        r["enable"].clicked.connect(lambda checked=False, n=name: self.set_enabled(n, bool(checked)))
        r["arm"].clicked.connect(lambda _c=False, n=name: self.arm_or_disarm(n))
        top = QHBoxLayout()
        for w in (r["title"], r["pill"], r["enable"], r["arm"]):
            top.addWidget(w)
        top.addStretch(1)
        box = QVBoxLayout()
        box.addLayout(top)
        if name == "overheat":
            r["pattern"] = QLineEdit()
            r["pattern"].setPlaceholderText("Regular expression for the overheat prompt (no built-in default)")
            r["set_pattern"] = QPushButton("Set")
            r["test_pattern"] = QPushButton("Test pattern against snapshot...")
            r["pattern_state"] = QLabel()
            r["pattern_state"].setObjectName("fieldHelp")
            r["pattern_state"].setWordWrap(True)
            r["set_pattern"].clicked.connect(lambda _c=False: self.set_pattern())
            r["test_pattern"].clicked.connect(lambda _c=False: self.test_pattern())
            prow = QHBoxLayout()
            for w in (r["pattern"], r["set_pattern"], r["test_pattern"]):
                prow.addWidget(w)
            box.addLayout(prow)
            box.addWidget(r["pattern_state"])
        box.addWidget(r["summary"])
        holder = QWidget()
        holder.setLayout(box)
        box.setContentsMargins(0, 4, 0, 8)
        self.wd_grid.addWidget(holder, row, 0)
        r["holder"] = holder
        return r

    def update_watchdogs(self, wds: list) -> None:
        names = [w["name"] for w in wds]
        if names != list(self.wd_rows):
            for r in self.wd_rows.values():
                r["holder"].deleteLater()
            self.wd_rows = {}
            for i, wd in enumerate(wds):
                self.wd_rows[wd["name"]] = self._build_watchdog_row(wd, i)
        self.wd_empty.setVisible(not wds)
        self._watchdogs = {w["name"]: w for w in wds}
        for wd in wds:
            r = self.wd_rows[wd["name"]]
            mode = wd.get("mode")
            r["pill"].setText({"armed": "Armed", "observe": "Observe"}.get(mode, "Off"))
            set_property(r["pill"], "state", {"armed": "syncing", "observe": "paused"}.get(mode, "offline"))
            r["enable"].setChecked(bool(wd.get("enabled")))
            blocker = wd.get("arm_blocker")
            r["arm"].setText("Disarm" if wd.get("armed") else "Arm...")
            r["arm"].setEnabled(bool(wd.get("armed")) or not blocker)
            r["arm"].setToolTip("" if wd.get("armed") or not blocker else "Cannot arm: %s" % blocker)
            r["summary"].setText(self.watchdog_summary(wd))
            if "pattern" in r:
                if not r["pattern"].hasFocus():
                    r["pattern"].setText((wd.get("cfg") or {}).get("pattern", ""))
                r["pattern_state"].setText("Pattern confirmed against a snapshot." if not blocker else blocker.capitalize() + ".")

    @staticmethod
    def watchdog_summary(wd: dict) -> str:
        cycle = wd.get("last_cycle")
        when = "last cycle %s" % time.strftime("%H:%M:%S", time.localtime(cycle)) if cycle else "no cycle yet"
        text = "%s: %s" % (when, wd.get("last_summary") or "-") if cycle else when
        decisions = wd.get("decisions") or []
        if decisions:
            text += "\nLast decision: %s%s" % ((decisions[-1].get("label") + ": ") if decisions[-1].get("label") else "",
                                                 decisions[-1].get("text", ""))
        return text

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._window.bridge.set_watchdog(name, enabled=enabled)

    def arm_or_disarm(self, name: str) -> None:
        wd = (self._watchdogs or {}).get(name)
        if wd is None:
            return
        if wd.get("armed"):
            self._window.bridge.set_watchdog(name, armed=False)
            return
        if wd.get("arm_blocker"):
            self._window.statusBar().showMessage("Cannot arm: %s" % wd["arm_blocker"], STATUS_MS)
            return
        if not self._exec(ArmDialog(wd, self)):
            return
        params = {"armed": True}
        if not wd.get("tested"):
            params["testing"] = True
        self._window.bridge.set_watchdog(name, **params)

    def set_pattern(self) -> None:
        r = self.wd_rows.get("overheat")
        if r is not None:
            self._window.bridge.set_watchdog("overheat", cfg={"pattern": r["pattern"].text().strip()})

    def _pick_folder(self):
        base = self.snapshots_dir()
        return QFileDialog.getExistingDirectory(self, "Snapshot folder", str(base) if base else "") or None

    def test_pattern(self) -> None:
        r = self.wd_rows.get("overheat")
        folder = self._pick_folder()
        if r is None or not folder:
            return
        self._last_test = {"folder": folder, "pattern": r["pattern"].text().strip()}
        self._window.bridge.watchdog_test_pattern("overheat", **self._last_test)

    def on_bridge_result(self, label: str, result) -> None:
        if label != "watchdog_pattern" or not self._last_test or result.get("confirmed"):
            if label == "watchdog_pattern" and result.get("confirmed"):
                self._window.statusBar().showMessage("Overheat pattern confirmed.", STATUS_MS)
            return
        if self._exec(PatternDialog(result, self)):
            self._window.bridge.watchdog_test_pattern("overheat", confirm=True, **self._last_test)
