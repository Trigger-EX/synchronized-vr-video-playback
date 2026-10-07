"""Tools tab (Power and Testing cards) and the preview-then-confirm flow every fleet adb action goes through."""

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..automation import SHOW_MODE_WARNING
from . import format as fmt
from .theme import mark

POWER_ACTIONS = (("sleep", "Sleep"), ("wake", "Wake"), ("screen_refresh", "Screen refresh"))
POWEROFF_FEATURE = "power.poweroff"
SNAPSHOT_FEATURE = "debug.snapshot"
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
        others = [w for w in preview.get("warnings") or [] if w != SHOW_MODE_WARNING]  # shown above instead
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
        for w in (self.scope_label, self.show_label, self.warning_label, self.every_check, self.buttons):
            lay.addWidget(w)
        self._sync()

    def _sync(self) -> None:
        self.ok_button.setEnabled(not self.every_check.isVisibleTo(self) or self.every_check.isChecked())


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
                     verb=dict(POWER_ACTIONS, poweroff="Power off").get(action, "Confirm"))
        if not self._exec(ConfirmDialog(shown, self._window)):
            self._window.statusBar().showMessage("Cancelled: %s" % preview.get("scope_text", action), STATUS_MS)
            return
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
                button = QPushButton("Mark tested")
                button.clicked.connect(lambda _c=False, k=f["key"]: self.mark_tested(k))
                self.testing_grid.addWidget(button, i, column)
                self.testing_buttons[f["key"]] = button
            self.testing_grid.setColumnStretch(0, 1)
        self.testing_empty.setVisible(not untested)
