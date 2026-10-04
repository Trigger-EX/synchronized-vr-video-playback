"""Log tab: controller events, newest first."""

import time

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

LEVEL_COLORS = {"error": "#d0342c", "warn": "#c27c0e"}


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
        lay = QVBoxLayout(self)
        lay.addWidget(self.list)

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        events = snap.get("events") or []
        names = {d["id"]: d["label"] for d in snap.get("devices") or []}
        key = (len(events), events[-1] if events else None, tuple(sorted(names.items())))
        if key == self._key:
            return
        self._key = key
        bar = self.list.verticalScrollBar()
        pos = bar.value()
        self.list.clear()
        for ev in reversed(events):
            item = QListWidgetItem(event_text(ev, names))
            color = LEVEL_COLORS.get(ev.get("level"))
            if color:
                item.setForeground(QBrush(QColor(color)))
            self.list.addItem(item)
        bar.setValue(pos)
