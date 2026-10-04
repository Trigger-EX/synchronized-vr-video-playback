"""Docked pane that embeds the scrcpy window of the selected headset (or notes that it opened separately)."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QDockWidget, QLabel, QVBoxLayout, QWidget

FIND_MS = 300
FIND_TRIES = 20


def qt_embedder(native_id: int) -> QWidget:
    return QWidget.createWindowContainer(QWindow.fromWinId(native_id))


class ViewPane(QDockWidget):
    """`finder(title) -> native id or None` and `embedder(id) -> QWidget` are injectable for tests."""

    def __init__(self, window, finder, embedder=qt_embedder, can_embed: bool = True):
        super().__init__("Headset view", window)
        self.setObjectName("viewPane")
        self.setAllowedAreas(Qt.RightDockWidgetArea)
        self.finder, self.embedder, self.can_embed = finder, embedder, can_embed
        self.device_id = None
        self.title = ""
        self.container = None
        self._tries = 0
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        self.body = QWidget()
        self.body.setMinimumWidth(360)
        self.lay = QVBoxLayout(self.body)
        self.lay.addWidget(self.note)
        self.lay.addStretch(1)
        self.setWidget(self.body)
        self._timer = QTimer(self)
        self._timer.setInterval(FIND_MS)
        self._timer.timeout.connect(self._look)
        self.hide()

    def begin(self, device_id: str, name: str, title: str) -> None:
        self.clear()
        self.device_id, self.title = device_id, title
        self.setWindowTitle("Headset view - %s" % name)
        self.note.setText("Connecting to %s..." % name)
        self.show()

    def attach(self) -> None:
        """scrcpy is ready: embed its window, or leave it separate when that is not possible."""
        if not self.can_embed:
            self.note.setText("Embedding is not available on this display (Wayland or macOS): "
                              "the view opened in its own window.")
            return
        self._tries = 0
        self._timer.start()
        self._look()

    def _look(self) -> None:
        native = self.finder(self.title)
        if native:
            self._timer.stop()
            self.container = self.embedder(native)
            self.lay.insertWidget(1, self.container, 1)
            self.note.hide()
        else:
            self._tries += 1
            if self._tries >= FIND_TRIES:
                self._timer.stop()
                self.note.setText("Could not dock the view window: it is open separately.")

    def clear(self) -> None:
        self._timer.stop()
        if self.container is not None:
            self.lay.removeWidget(self.container)
            self.container.deleteLater()
            self.container = None
        self.device_id = None
        self.note.show()
        self.note.setText("")

    def close_view(self) -> None:
        self.clear()
        self.hide()
