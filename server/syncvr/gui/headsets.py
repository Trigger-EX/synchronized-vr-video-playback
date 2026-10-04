"""Headsets tab: one card per headset, updated in place, with group filter, multi-select and an edit dialog."""

import time
from html import escape

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QMenu, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLayout, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget, QFormLayout, QLineEdit)

from . import format as fmt
from .theme import SEVERITY_COLORS, mark, set_property, state_style

CARD_WIDTH = 270
SPACING = 8


def colored(text: str, severity: str = "") -> str:
    color = SEVERITY_COLORS.get(severity)
    text = escape(text)
    return '<span style="color:%s">%s</span>' % (color, text) if color else text


class FlowLayout(QLayout):
    """Wraps items left to right. Hidden widgets take no space; `set_order` reorders without recreating widgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def set_order(self, widgets) -> None:
        rank = {id(w): i for i, w in enumerate(widgets)}
        self._items.sort(key=lambda it: rank.get(id(it.widget()), len(rank)))
        self.invalidate()

    def widgets(self) -> list:
        return [it.widget() for it in self._items]

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_h = area.x(), area.y(), 0
        for it in self._items:
            if it.isEmpty():
                continue
            hint = it.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x, y, row_h = area.x(), y + row_h + SPACING, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + SPACING
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y() + m.bottom()


class DeviceCard(QFrame):
    clicked = Signal(str)
    edit_requested = Signal(str)
    view_requested = Signal(str)

    def __init__(self, device_id: str):
        super().__init__()
        self.device_id = device_id
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(CARD_WIDTH)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("card")

        self.name_label = QLabel()
        self.name_label.setObjectName("cardName")
        self.group_label = QLabel()
        self.group_label.setObjectName("chip")
        self.player_label = QLabel()
        self.player_label.setObjectName("chip")
        self.edit_button = QPushButton("Edit")
        mark(self.edit_button, small=True)
        self.edit_button.setCursor(Qt.ArrowCursor)
        self.edit_button.clicked.connect(lambda _c=False: self.edit_requested.emit(self.device_id))
        self.view_button = QPushButton("View")
        mark(self.view_button, small=True)
        self.view_button.setCursor(Qt.ArrowCursor)
        self.view_button.setToolTip("Mirror this headset's screen (adb + scrcpy)")
        self.view_button.clicked.connect(lambda _c=False: self.view_requested.emit(self.device_id))
        head = QHBoxLayout()
        for w in (self.name_label, self.group_label, self.player_label):
            head.addWidget(w)
        head.addStretch(1)
        head.addWidget(self.view_button)
        head.addWidget(self.edit_button)

        self.state_label = QLabel()
        self.state_label.setObjectName("badge")
        self.video_label = QLabel()
        self.video_label.setObjectName("muted")
        self.video_label.setTextFormat(Qt.PlainText)
        state_row = QHBoxLayout()
        state_row.addWidget(self.state_label)
        state_row.addWidget(self.video_label, 1)

        self.position_bar = self._bar()
        self.time_label = QLabel()
        self.time_label.setTextFormat(Qt.RichText)
        self.info_label = QLabel()
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setWordWrap(True)
        self.download_label = QLabel()
        self.download_bar = self._bar()
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("error")
        self.seen_label = QLabel()
        self.seen_label.setObjectName("muted")
        self.download_label.setObjectName("muted")
        self.download_bar.setProperty("download", "true")
        self.time_label.setObjectName("time")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addLayout(state_row)
        for w in (self.position_bar, self.time_label, self.info_label, self.download_label, self.download_bar,
                  self.error_label, self.seen_label):
            lay.addWidget(w)
        self._selected = None
        mark(self, online=True)
        self.set_selected(False)

    @staticmethod
    def _bar() -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        return bar

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.device_id)
        super().mouseReleaseEvent(event)

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        set_property(self, "selected", selected)

    def update_device(self, dev: dict, library: list) -> None:
        st = dev.get("status") or {}
        online = bool(dev.get("online"))
        self.name_label.setText(dev["label"])
        self.name_label.setToolTip(dev["id"])
        self.group_label.setText(dev.get("group") or "")
        self.group_label.setVisible(bool(dev.get("group")))
        self.player_label.setText(dev.get("player") or "")
        self.player_label.setVisible(bool(dev.get("player")))
        self.player_label.setToolTip("Player app")
        name = fmt.state_name(dev)
        self.state_label.setText(name)
        set_property(self.state_label, "state", state_style(name))
        set_property(self, "online", online)
        self.view_button.setEnabled(online)
        self.video_label.setText(st.get("video") or "")

        pos, dur = st.get("position"), st.get("duration")
        show_pos = online and pos is not None and bool(dur)
        self.position_bar.setVisible(show_pos)
        self.time_label.setVisible(show_pos)
        if show_pos:
            self.position_bar.setValue(int(min(1.0, pos / dur) * 1000))
            bits = ["%s / %s" % (fmt.fmt_time(pos), fmt.fmt_time(dur))]
            drift = st.get("drift_ms")
            if drift is not None and st.get("state") == "playing":
                bits.append(colored("drift %s%.0f ms" % ("+" if drift > 0 else "", drift),
                                    fmt.drift_class(drift)))
            if st.get("rate") and st["rate"] != 1:
                bits.append("%.1f%%" % (st["rate"] * 100))
            self.time_label.setText("  ".join(bits))

        self.info_label.setVisible(online)
        dl = st.get("download") or {}
        show_dl = online and bool(dl.get("total"))
        self.download_label.setVisible(show_dl)
        self.download_bar.setVisible(show_dl)
        self.error_label.setVisible(online and bool(st.get("error")))
        self.seen_label.setVisible(not online)
        if online:
            self.info_label.setText("  ".join(colored(t, s) for t, s in fmt.device_info(dev, library)))
            if show_dl:
                pct = dl["received"] / dl["total"] * 100
                self.download_label.setText("↓ %s %.0f%%" % (dl.get("name", ""), pct))
                self.download_bar.setValue(int(min(100.0, pct) * 10))
            self.error_label.setText(st.get("error") or "")
        else:
            seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dev["last_seen"])) if dev.get("last_seen") \
                else "never"
            self.seen_label.setText("last seen " + seen)


class EditDialog(QDialog):
    """Name/group editor. `action` is "save", "forget" or "cancel" after it closes."""

    def __init__(self, dev: dict, groups: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Headset")
        self.action = "cancel"
        bits = [dev["id"], dev.get("model"), dev.get("ip"), dev.get("player") and dev["player"] + " player",
                dev.get("app_version") and "app " + dev["app_version"]]
        self.id_label = QLabel(" · ".join(b for b in bits if b))
        self.id_label.setObjectName("muted")
        self.name_edit = QLineEdit(dev.get("name") or "")
        self.name_edit.setMaxLength(64)
        self.name_edit.setPlaceholderText("e.g. Seat 12")
        self.group_edit = QComboBox()
        self.group_edit.setEditable(True)
        self.group_edit.addItems(groups)
        self.group_edit.setCurrentText(dev.get("group") or "")
        self.group_edit.lineEdit().setMaxLength(64)
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.setProperty("primary", "true")
        self.cancel_button = QPushButton("Cancel")
        self.forget_button = QPushButton("Forget")
        self.forget_button.setProperty("danger", "true")
        self.forget_button.setVisible(not dev.get("online"))
        self.save_button.clicked.connect(lambda _c=False: self._finish("save"))
        self.cancel_button.clicked.connect(lambda _c=False: self._finish("cancel"))
        self.forget_button.clicked.connect(lambda _c=False: self._finish("forget"))
        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Group", self.group_edit)
        row = QHBoxLayout()
        row.addWidget(self.save_button)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        row.addWidget(self.forget_button)
        lay = QVBoxLayout(self)
        lay.addWidget(self.id_label)
        lay.addLayout(form)
        lay.addLayout(row)

    def _finish(self, action: str) -> None:
        self.action = action
        self.accept() if action != "cancel" else self.reject()


class HeadsetsTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.cards = {}
        self._groups = None

        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Clear selection")
        self.group_filter = QComboBox()
        self.group_filter.addItem("All groups", "")
        self.show_offline = QCheckBox("Show offline")
        self.show_offline.setChecked(True)
        self.downloads_label = QLabel("")
        self.select_all_button.clicked.connect(lambda _c=False: self.select_all())
        self.select_none_button.clicked.connect(lambda _c=False: self._window.set_targets([]))
        self.group_filter.currentIndexChanged.connect(lambda _i: self.update_state())
        self.show_offline.toggled.connect(lambda _c: self.update_state())

        bar = QHBoxLayout()
        for w in (self.select_all_button, self.select_none_button, self.group_filter, self.show_offline):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.downloads_label)

        self.flow = FlowLayout()
        self.flow.setContentsMargins(0, 8, 0, 8)
        self.container = QWidget()
        self.container.setLayout(self.flow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.container)
        self.empty_label = QLabel("No headsets yet. Start the SyncVR player app on a headset connected to the "
                                  "same network, or run `python -m syncvr sim` to try it with simulated headsets.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("muted")
        self.downloads_label.setObjectName("muted")
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.empty_label)
        lay.addWidget(scroll, 1)

    # ---------------------------------------------------------------- view

    def visible_devices(self) -> list:
        devices = (self._window.snapshot or {}).get("devices") or []
        group = self.group_filter.currentData()
        show = self.show_offline.isChecked()
        return [d for d in fmt.sort_devices(devices) if (not group or d.get("group") == group)
                and (show or d.get("online"))]

    def select_all(self) -> None:
        ids = list(self._window.targets)
        ids += [d["id"] for d in self.visible_devices() if d["id"] not in ids]
        self._window.set_targets(ids)

    def toggle(self, device_id: str) -> None:
        ids = list(self._window.targets)
        if device_id in ids:
            ids.remove(device_id)
        else:
            ids.append(device_id)
        self._window.set_targets(ids)

    def update_selection(self) -> None:
        for dev_id, card in self.cards.items():
            card.set_selected(dev_id in self._window.targets)

    def update_state(self) -> None:
        snap = self._window.snapshot or {}
        devices = snap.get("devices") or []
        library = snap.get("library") or []
        self._fill_groups(fmt.groups_of(devices))
        by_id = {d["id"]: d for d in devices}
        for dev_id in [i for i in self.cards if i not in by_id]:
            card = self.cards.pop(dev_id)
            self.flow.removeWidget(card)
            card.deleteLater()
        for dev in devices:
            card = self.cards.get(dev["id"])
            if card is None:
                card = self.cards[dev["id"]] = DeviceCard(dev["id"])
                card.clicked.connect(self.toggle)
                card.edit_requested.connect(self.open_edit)
                card.view_requested.connect(self._window.view_headset)
                self.flow.addWidget(card)
            card.update_device(dev, library)
        shown = self.visible_devices()
        shown_ids = {d["id"] for d in shown}
        for dev_id, card in self.cards.items():
            card.setVisible(dev_id in shown_ids)
        self.flow.set_order([self.cards[d["id"]] for d in fmt.sort_devices(devices)])
        self.update_selection()
        self.empty_label.setVisible(not devices)
        dl = snap.get("downloads") or {}
        active, queued = len(dl.get("active") or []), len(dl.get("queued") or [])
        self.downloads_label.setText("Downloading to %d, %d waiting" % (active, queued) if active or queued else "")

    def _fill_groups(self, groups: list) -> None:
        if groups == self._groups:
            return
        self._groups = groups
        current = self.group_filter.currentData()
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("All groups", "")
        for g in groups:
            self.group_filter.addItem(g, g)
        idx = self.group_filter.findData(current)
        self.group_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.group_filter.blockSignals(False)

    def contextMenuEvent(self, event) -> None:
        card = next((c for c in self.cards.values() if c.isVisible()
                     and c.rect().contains(c.mapFromGlobal(event.globalPos()))), None)
        if card is None:
            return
        menu = QMenu(self)
        act = menu.addAction("View headset (scrcpy)...")
        act.setEnabled(card.view_button.isEnabled())
        act.triggered.connect(lambda _c=False, i=card.device_id: self._window.view_headset(i))
        menu.exec(event.globalPos())

    # ---------------------------------------------------------------- edit

    def _exec(self, dialog) -> bool:
        return dialog.exec() == QDialog.Accepted

    def confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "SyncVR", text) == QMessageBox.Yes

    def open_edit(self, device_id: str) -> None:
        dev = next((d for d in (self._window.snapshot or {}).get("devices") or [] if d["id"] == device_id), None)
        if dev is None:
            return
        dialog = EditDialog(dev, fmt.groups_of(self._window.snapshot["devices"]), self)
        if self._exec(dialog):
            self.apply_edit(dev, dialog.action, dialog.name_edit.text(), dialog.group_edit.currentText())

    def apply_edit(self, dev: dict, action: str, name: str, group: str) -> None:
        bridge = self._window.bridge
        if action == "save":
            bridge.update_device(dev["id"], {"name": name, "group": group})
        elif action == "forget":
            if self.confirm('Forget "%s"? It will be removed until it connects again.' % dev["label"]):
                bridge.forget_device(dev["id"])
                self._window.set_targets([i for i in self._window.targets if i != dev["id"]])
