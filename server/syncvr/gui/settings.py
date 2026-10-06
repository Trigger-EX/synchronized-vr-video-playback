"""Settings tab: sync tuning parameters and the download limit, driven by SETTINGS_SPEC."""

from collections import namedtuple

from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ..protocol import CORRECTION_MODES, DEFAULT_SYNC_SETTINGS

Spec = namedtuple("Spec", "key label help kind lo hi step decimals")
MAX_DOWNLOADS = "max_downloads"
GRID_COLUMNS = 3  # label+field cells per row


def _spec(key, label, help_text, kind="int", lo=0, hi=10000, step=1, decimals=0):
    return Spec(key, label, help_text, kind, lo, hi, step, decimals)


SETTINGS_SPEC = [
    _spec("play_lead_ms", "Start delay (ms)",
          "How far ahead synchronized starts are scheduled. Raise on busy networks.", lo=200),
    _spec("seek_lead_ms", "Seek delay (ms)", "How far ahead playback restarts after a seek.", lo=200),
    _spec("pause_lead_ms", "Pause delay (ms)", "How far ahead synchronized pauses are scheduled.", hi=5000),
    _spec("correction_mode", "Correction mode",
          "rate: nudge speed, seek for big jumps. seek: only jump. external: player's own clock (experimental).",
          kind="choice"),
    _spec("deadband_ms", "Deadband (ms)", "Drift smaller than this is left alone.", lo=1, hi=1000),
    _spec("rate_gain", "Rate gain", "How strongly speed reacts to drift (per second of drift).",
          kind="float", lo=0.05, hi=5.0, step=0.1, decimals=2),
    _spec("max_rate_adjust", "Max speed change", "0.05 = play at most 5% faster or slower while catching up.",
          kind="float", lo=0.0, hi=0.25, step=0.01, decimals=3),
    _spec("hard_seek_ms", "Hard re-sync above (ms)", "Drift beyond this pauses briefly and restarts in sync.", lo=20),
    _spec("seek_mode_threshold_ms", "Seek-mode threshold (ms)",
          "In seek mode, drift beyond this triggers a re-sync.", lo=10),
    _spec("seek_cooldown_ms", "Re-sync cooldown (ms)", "Minimum time between two re-syncs on a headset.", hi=60000),
    _spec("settle_ms", "Settle time (ms)", "Ignore drift for this long after starting or seeking."),
    _spec(MAX_DOWNLOADS, "Simultaneous downloads",
          "Headsets downloading at once (0 = no limit). Keep low on busy Wi-Fi.", hi=1000),
]


def make_editor(spec: Spec):
    if spec.kind == "choice":
        w = QComboBox()
        w.addItems(CORRECTION_MODES)
        return w
    w = QDoubleSpinBox() if spec.kind == "float" else QSpinBox()
    if spec.kind == "float":
        w.setDecimals(spec.decimals)
    w.setRange(spec.lo, spec.hi)
    w.setSingleStep(spec.step)
    return w


class SettingsTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._loading = False
        self._dirty = set()
        self.editors = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        for i, spec in enumerate(SETTINGS_SPEC):
            w = make_editor(spec)
            w.setToolTip(spec.help)
            signal = w.currentTextChanged if spec.kind == "choice" else w.valueChanged
            signal.connect(lambda _v, k=spec.key: self._edited(k))
            self.editors[spec.key] = w
            help_label = QLabel(spec.help)
            help_label.setWordWrap(True)
            help_label.setObjectName("fieldHelp")
            cell = QVBoxLayout()
            cell.addWidget(QLabel(spec.label))
            cell.addWidget(w)
            cell.addWidget(help_label)
            cell.addStretch(1)
            grid.addLayout(cell, *divmod(i, GRID_COLUMNS))
        for col in range(GRID_COLUMNS):
            grid.setColumnStretch(col, 1)
        self.save_button = QPushButton("Save settings")
        self.save_button.setProperty("primary", "true")
        self.defaults_button = QPushButton("Restore defaults")
        self.save_button.clicked.connect(lambda _c=False: self.save())
        self.defaults_button.clicked.connect(lambda _c=False: self.restore_defaults())
        row = QHBoxLayout()
        row.addWidget(self.save_button)
        row.addWidget(self.defaults_button)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addLayout(grid)
        lay.addLayout(row)
        lay.addStretch(1)

    def _edited(self, key: str) -> None:
        if not self._loading:
            self._dirty.add(key)

    @staticmethod
    def _get(spec, w):
        return w.currentText() if spec.kind == "choice" else (w.value() if spec.kind == "float" else int(w.value()))

    @staticmethod
    def _set(spec, w, value) -> None:
        if spec.kind == "choice":
            w.setCurrentText(str(value))
        else:
            w.setValue(float(value) if spec.kind == "float" else int(value))

    def values(self) -> dict:
        return {s.key: self._get(s, self.editors[s.key]) for s in SETTINGS_SPEC}

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        current = dict(snap.get("settings") or {})
        if "max_concurrent" in (snap.get("downloads") or {}):
            current[MAX_DOWNLOADS] = snap["downloads"]["max_concurrent"]
        self._loading = True
        try:
            for spec in SETTINGS_SPEC:
                w = self.editors[spec.key]
                if spec.key in current and spec.key not in self._dirty and not w.hasFocus() \
                        and not (spec.kind != "choice" and w.lineEdit().hasFocus()):
                    self._set(spec, w, current[spec.key])
        finally:
            self._loading = False

    def save(self) -> None:
        values = self.values()
        bridge = self._window.bridge
        bridge.update_settings({k: v for k, v in values.items() if k != MAX_DOWNLOADS})
        bridge.set_max_downloads(values[MAX_DOWNLOADS])
        self._dirty.clear()
        self._window.statusBar().showMessage("Settings saved and sent to headsets", 5000)

    def restore_defaults(self) -> None:
        self._loading = True
        try:
            for spec in SETTINGS_SPEC:
                if spec.key in DEFAULT_SYNC_SETTINGS:
                    self._set(spec, self.editors[spec.key], DEFAULT_SYNC_SETTINGS[spec.key])
                    self._dirty.discard(spec.key)
        finally:
            self._loading = False
        self._window.bridge.update_settings(dict(DEFAULT_SYNC_SETTINGS))
        self._window.statusBar().showMessage("Defaults restored", 5000)
