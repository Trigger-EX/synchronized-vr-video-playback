"""Log tab: controller events, newest first."""

import re
import time

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout,
                               QWidget)

from .theme import LEVEL_COLORS

LEVELS = (("error", "ERROR"), ("warn", "WARN"), ("info", "INFO"), ("debug", "DEBUG"))
DEFAULT_ON = ("error", "warn", "info")
# "12:00:00  [warn]  ..." (event_text) or the logger's "12:00:00 WARNING msg" (see __main__ basicConfig)
LINE_LEVEL = re.compile(r"\[(error|warn|info|debug)\]|^\S+\s+(ERROR|CRITICAL|WARNING|INFO|DEBUG)\b", re.I)
LEVEL_ALIASES = {"critical": "error", "warning": "warn"}


def line_level(line: str) -> str:
    m = LINE_LEVEL.search(line)
    level = (m.group(1) or m.group(2)).lower() if m else "info"
    return LEVEL_ALIASES.get(level, level)


def event_text(event: dict, names: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(event["t"]))
    who = event.get("device")
    who = "%s: " % names.get(who, who) if who else ""
    return "%s  [%s]  %s%s" % (stamp, event.get("level", "info"), who, event.get("message", ""))


class LogTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._key = None
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.level_boxes = {}
        bar = QHBoxLayout()
        for level, label in LEVELS:
            box = QCheckBox(label)
            box.setChecked(level in DEFAULT_ON)
            box.toggled.connect(lambda _c: self._refresh())
            self.level_boxes[level] = box
            bar.addWidget(box)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.list)

    def shown_levels(self) -> set:
        return {level for level, box in self.level_boxes.items() if box.isChecked()}

    def _refresh(self) -> None:
        self._key = None
        self.update_state()

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        events = snap.get("events") or []
        names = {d["id"]: d["label"] for d in snap.get("devices") or []}
        shown = self.shown_levels()
        key = (len(events), events[-1] if events else None, tuple(sorted(names.items())), tuple(sorted(shown)))
        if key == self._key:
            return
        self._key = key
        bar = self.list.verticalScrollBar()
        pos = bar.value()
        self.list.clear()
        for ev in reversed(events):
            if line_level(event_text(ev, names)) not in shown:
                continue
            item = QListWidgetItem(event_text(ev, names))
            color = LEVEL_COLORS.get(ev.get("level")) if ev.get("level") in ("error", "warn") else None
            if color:
                item.setForeground(QBrush(QColor(color)))
            self.list.addItem(item)
        bar.setValue(pos)
