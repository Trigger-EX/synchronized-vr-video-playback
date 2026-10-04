"""Library tab: table of videos with editable projection/stereo/rotation/loop, sent through the bridge."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QSpinBox, QStyledItemDelegate, QTableView, QVBoxLayout, QWidget)

from . import format as fmt

COLUMNS = ("Title", "File", "Checks", "Length", "Size", "Projection", "Stereo", "Yaw°", "Loop", "On headsets")
TITLE, FILE, CHECKS, LENGTH, SIZE, PROJECTION, STEREO, ROTATION, LOOP, ON_HEADSETS = range(len(COLUMNS))
FIELDS = {TITLE: "title", PROJECTION: "projection", STEREO: "stereo", ROTATION: "rotation", LOOP: "loop"}
PROJECTIONS = (("360", "360°"), ("180", "180°"), ("flat", "Flat screen"))
STEREO_MODES = (("mono", "Mono"), ("tb", "3D top/bottom"), ("sbs", "3D side-by-side"))
CHOICES = {PROJECTION: PROJECTIONS, STEREO: STEREO_MODES}
CHECK_COLORS = {"ok": "#2e9d4a", "info": "#3b6fb6", "warn": "#c27c0e", "error": "#d0342c", "pending": "#7a7f87"}


class LibraryModel(QAbstractTableModel):
    def __init__(self, on_edit, parent=None):
        super().__init__(parent)
        self._on_edit = on_edit  # (video name, {field: value})
        self.videos = []
        self.counts = {}  # name -> headsets that hold the full file
        self.total = 0

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.videos)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == LOOP:
            return flags | Qt.ItemIsUserCheckable
        return flags | Qt.ItemIsEditable if index.column() in FIELDS else flags

    def set_data(self, videos, devices) -> None:
        counts = {v["name"]: sum(1 for d in devices if (d.get("inventory") or {}).get(v["name"]) == v["size"])
                  for v in videos}
        same_rows = [v["name"] for v in videos] == [v["name"] for v in self.videos]
        if same_rows:
            self.videos, self.counts, self.total = list(videos), counts, len(devices)
            if videos:
                self.dataChanged.emit(self.index(0, 0), self.index(len(videos) - 1, len(COLUMNS) - 1))
            return
        self.beginResetModel()
        self.videos, self.counts, self.total = list(videos), counts, len(devices)
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        v, col = self.videos[index.row()], index.column()
        if role == Qt.DisplayRole and col == ON_HEADSETS:
            return self.display_on_headsets(index.row())
        if role in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.EditRole and col in FIELDS and col != LOOP:
                return v.get(FIELDS[col])
            return self._display(v, col)
        if role == Qt.CheckStateRole and col == LOOP:
            return Qt.Checked if v.get("loop") else Qt.Unchecked
        if role == Qt.ToolTipRole:
            return self._tooltip(v, col)
        if role == Qt.ForegroundRole and col == CHECKS:
            return QBrush(QColor(CHECK_COLORS.get(fmt.issues_summary(v)[1], "#000000")))
        return None

    @staticmethod
    def _display(v, col):
        if col == TITLE:
            return v.get("title") or ""
        if col == FILE:
            probe = fmt.probe_summary(v)
            return v["name"] + ("\n" + probe if probe else "")
        if col == CHECKS:
            return fmt.issues_summary(v)[0]
        if col == LENGTH:
            return fmt.fmt_time(v.get("duration"))
        if col == SIZE:
            return fmt.fmt_bytes(v.get("size"))
        if col in CHOICES:
            return dict(CHOICES[col]).get(v.get(FIELDS[col]), v.get(FIELDS[col]))
        if col == ROTATION:
            return v.get("rotation")
        return None

    def _tooltip(self, v, col):
        if col == FILE:
            return v["name"]
        if col != CHECKS or not v.get("analysis"):
            return None
        lines = ["%s: %s" % (i.get("level"), i.get("message")) for i in v.get("issues") or []]
        if v["analysis"] == "pending":
            lines = lines or ["Analysing…"]
        elif not lines:
            lines = ["No problems found."]
        if v.get("sha256"):
            lines.append("SHA-256 " + v["sha256"])
        return "\n".join(lines)

    def display_on_headsets(self, row) -> str:
        return "%d / %d" % (self.counts.get(self.videos[row]["name"], 0), self.total)

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        col = index.column()
        if not index.isValid() or col not in FIELDS:
            return False
        if col == LOOP:
            if role != Qt.CheckStateRole:
                return False
            value = value in (Qt.Checked, Qt.Checked.value)
        elif role != Qt.EditRole:
            return False
        elif col == ROTATION:
            value = int(value)
        else:
            value = str(value) if col == TITLE else value
        video = self.videos[index.row()]
        field = FIELDS[col]
        if video.get(field) == value:
            return False
        video[field] = value  # optimistic; the next snapshot (or a failure reload) is authoritative
        self.dataChanged.emit(index, index)
        self._on_edit(video["name"], {field: value})
        return True


class ChoiceDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for value, label in CHOICES[index.column()]:
            combo.addItem(label, value)
        combo.activated.connect(lambda _i, c=combo: (self.commitData.emit(c), self.closeEditor.emit(c)))
        return combo

    def setEditorData(self, editor, index) -> None:
        editor.setCurrentIndex(max(0, editor.findData(index.data(Qt.EditRole))))

    def setModelData(self, editor, model, index) -> None:
        model.setData(index, editor.currentData(), Qt.EditRole)


class RotationDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        spin = QSpinBox(parent)
        spin.setRange(0, 359)
        return spin

    def setEditorData(self, editor, index) -> None:
        editor.setValue(int(index.data(Qt.EditRole) or 0))

    def setModelData(self, editor, model, index) -> None:
        model.setData(index, editor.value(), Qt.EditRole)


class LibraryTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._snap = None
        self._deferred = False

        self.model = LibraryModel(window.bridge.update_video, self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.SelectedClicked | QAbstractItemView.AnyKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col in (PROJECTION, STEREO):
            self.table.setItemDelegateForColumn(col, ChoiceDelegate(self.table))
        self.table.setItemDelegateForColumn(ROTATION, RotationDelegate(self.table))
        self.table.setColumnWidth(TITLE, 200)
        self.table.setColumnWidth(FILE, 260)
        for col in (PROJECTION, STEREO):
            self.table.setColumnWidth(col, 130)
        for delegate in (self.table.itemDelegate(), *(self.table.itemDelegateForColumn(c)
                                                      for c in (PROJECTION, STEREO, ROTATION))):
            delegate.closeEditor.connect(self._editor_closed)

        self.rescan_button = QPushButton("Rescan content folder")
        self.rescan_button.clicked.connect(lambda _c=False: window.bridge.rescan())
        self.empty_label = QLabel("The content folder is empty.")
        hint = QLabel("Put video files in the server's content folder. Changes here apply the next time a video is loaded.")
        bar = QHBoxLayout()
        bar.addWidget(self.rescan_button)
        bar.addWidget(hint, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.empty_label)
        lay.addWidget(self.table, 1)
        window.bridge.failed.connect(lambda _msg: self.reload())
        self.update_state()

    def editing(self) -> bool:
        return self.table.state() == QAbstractItemView.EditingState

    def _editor_closed(self, *_args) -> None:
        if self._deferred:
            self._deferred = False
            self.reload()

    def update_state(self) -> None:
        self._snap = self._window.snapshot or {}
        if self.editing():  # never replace what the operator is typing
            self._deferred = True
            return
        self.reload()

    def reload(self) -> None:
        snap = self._snap or {}
        if self.editing():
            self._deferred = True
            return
        videos = [dict(v) for v in snap.get("library") or []]
        self.model.set_data(videos, snap.get("devices") or [])
        self.empty_label.setVisible(not videos)
