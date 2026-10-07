"""Terminal dock: a local shell or an `adb shell` on a headset, on a pty. GUI only, POSIX only, gated by `terminal.shell`.

There is deliberately no web or CLI route to this: it is a shell on the operator's machine.
"""

import errno
import os
import signal
import struct
import subprocess
import sys
import time
from typing import List, Optional

from PySide6.QtCore import QObject, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QDockWidget, QPlainTextEdit

from ..adbtool import Adb
from ..fleetops import AdbFleet, adb_serial
from ..termstream import TermStream

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows
    pty = None

FEATURE = "terminal.shell"
AVAILABLE = pty is not None and os.name == "posix"
UNAVAILABLE_TIP = "Terminal needs a POSIX pty (Linux or macOS); it is not available on Windows."
UNTESTED = "Terminal is not marked tested yet: use Open in the Tools tab Testing card."
STATUS_MS = 5000
GRACE_S = 1.0
RENDER_MS = 30
ROWS, COLS = 24, 100
# Becoming session leader is not enough for job control: the pty must also be made the controlling terminal.
# Doing it in a tiny exec wrapper avoids preexec_fn, which is unsafe in a process with other threads.
_CTTY = "import fcntl,termios,os,sys;fcntl.ioctl(0,termios.TIOCSCTTY,0);os.execvp(sys.argv[1],sys.argv[1:])"

KEYS = {Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f", Qt.Key_Tab: b"\t",
        Qt.Key_Escape: b"\x1b", Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B", Qt.Key_Right: b"\x1b[C",
        Qt.Key_Left: b"\x1b[D", Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F", Qt.Key_Delete: b"\x1b[3~"}


def key_bytes(key: int, modifiers, text: str) -> bytes:
    """What a key press sends to the shell; empty for keys that mean nothing to it."""
    if modifiers & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
        return bytes([key - Qt.Key_A + 1])
    if key in KEYS:
        return KEYS[key]
    return text.encode("utf-8") if text and text >= " " else b""


def gate_error(features: list, testing: bool) -> Optional[str]:
    """Why the terminal may not open, from the snapshot's feature list (same rule as the other gated actions)."""
    if not AVAILABLE:
        return UNAVAILABLE_TIP
    if not testing and not any(f.get("key") == FEATURE and f.get("tested") for f in features):
        return UNTESTED
    return None


def local_argv() -> List[str]:
    return ["bash", "-i"]


class PtySession(QObject):
    data = Signal(bytes)
    finished = Signal(int)

    def __init__(self, argv: List[str], parent=None, rows: int = ROWS, cols: int = COLS):
        super().__init__(parent)
        self.argv = argv
        self.proc: Optional[subprocess.Popen] = None
        self.fd = -1
        self._out = b""
        self._reader = self._writer = None
        master, slave = pty.openpty()
        try:
            self.fd = master
            self.resize(rows, cols)
            fcntl.fcntl(master, fcntl.F_SETFL, fcntl.fcntl(master, fcntl.F_GETFL) | os.O_NONBLOCK)
            env = dict(os.environ, TERM="dumb", LINES=str(rows), COLUMNS=str(cols))
            self.proc = subprocess.Popen([sys.executable, "-c", _CTTY] + argv, stdin=slave, stdout=slave, stderr=slave,
                                         env=env, close_fds=True, start_new_session=True)
        except BaseException:
            os.close(master)
            self.fd = -1
            raise
        finally:
            os.close(slave)
        self._reader = QSocketNotifier(master, QSocketNotifier.Read, self)
        self._reader.activated.connect(self._readable)
        self._writer = QSocketNotifier(master, QSocketNotifier.Write, self)
        self._writer.setEnabled(False)
        self._writer.activated.connect(self._flush)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.fd >= 0

    def resize(self, rows: int, cols: int) -> None:
        if self.fd >= 0:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, _winsize(rows, cols))  # the kernel sends SIGWINCH

    def send(self, data: bytes) -> None:
        if self.running and data:
            self._out += data
            self._flush()

    def _flush(self) -> None:
        while self._out and self.fd >= 0:
            try:
                n = os.write(self.fd, self._out)
            except BlockingIOError:
                break
            except OSError:
                self._out = b""
                return
            self._out = self._out[n:]
        if self._writer:
            self._writer.setEnabled(bool(self._out) and self.fd >= 0)

    def _readable(self) -> None:
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:  # EIO is how Linux reports that the slave side is gone
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            chunk = b""
        if chunk:
            self.data.emit(chunk)
        else:
            self.close()

    def close(self) -> None:
        """Hang up the whole process group, escalating to SIGKILL after a short grace. Safe to call twice."""
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        for n in (self._reader, self._writer):
            if n:
                n.setEnabled(False)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        _kill_group(proc.pid, signal.SIGHUP)
        deadline = time.monotonic() + GRACE_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        _kill_group(proc.pid, signal.SIGKILL)  # also reaches descendants that ignored the hangup
        code = proc.wait()
        self.finished.emit(code)


def _winsize(rows: int, cols: int) -> bytes:
    return struct.pack("HHHH", rows, cols, 0, 0)


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class TermView(QPlainTextEdit):
    typed = Signal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setContextMenuPolicy(Qt.NoContextMenu)

    def keyPressEvent(self, event) -> None:
        if event.matches(event.StandardKey.Paste) or (event.modifiers() & Qt.ControlModifier
                                                      and event.modifiers() & Qt.ShiftModifier and event.key() == Qt.Key_V):
            self.typed.emit(QGuiApplication.clipboard().text().replace("\n", "\r").encode("utf-8"))
        else:
            self.typed.emit(key_bytes(event.key(), event.modifiers(), event.text()))
        event.accept()

    def char_size(self):
        m = self.fontMetrics()
        return max(1, m.horizontalAdvance("M")), max(1, m.lineSpacing())


class TerminalDock(QDockWidget):
    closed = Signal(object)

    def __init__(self, title: str, session: PtySession, parent=None):
        super().__init__(title, parent)
        self.setObjectName("terminal")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.session = session
        self.stream = TermStream()
        self.view = TermView(self)
        self.setWidget(self.view)
        self._dirty = False
        self._timer = QTimer(self)  # coalesces bursts of output into one repaint
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self.render)
        self.view.typed.connect(session.send)
        session.data.connect(self.on_data)
        session.finished.connect(self.on_finished)

    def on_data(self, chunk: bytes) -> None:
        self.stream.feed(chunk)
        if not self._timer.isActive():
            self._timer.start()

    def render(self) -> None:
        self.view.setPlainText(self.stream.text())
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        cw, ch = self.view.char_size()
        size = self.view.viewport().size()
        self.session.resize(max(2, size.height() // ch), max(2, size.width() // cw))

    def on_finished(self, code: int) -> None:
        self.stream.feed(("\r\n[process exited with status %d]\r\n" % code).encode())
        self.render()
        self.view.appendPlainText("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cw, ch = self.view.char_size()
        size = self.view.viewport().size()
        self.session.resize(max(2, size.height() // ch), max(2, size.width() // cw))

    def closeEvent(self, event) -> None:
        self.session.close()
        self.closed.emit(self)
        super().closeEvent(event)


class TerminalManager(QObject):
    """Opens and owns the terminal docks of one main window."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.docks: List[TerminalDock] = []
        self.session_factory = PtySession  # tests substitute their own

    def _say(self, text: str) -> None:
        self._window.statusBar().showMessage(text, STATUS_MS)

    def _features(self) -> list:
        return (self._window.snapshot or {}).get("features") or []

    def open_local(self, testing: bool = False) -> Optional[TerminalDock]:
        err = gate_error(self._features(), testing)
        if err:
            self._say(err)
            return None
        return self._open("Terminal", local_argv())

    def open_headset(self, device_id: str, testing: bool = False) -> Optional[TerminalDock]:
        err = gate_error(self._features(), testing)
        if err:
            self._say(err)
            return None
        dev = next((d for d in (self._window.snapshot or {}).get("devices") or [] if d["id"] == device_id), None)
        adb = self._window.mirror.tool("adb")
        if dev is None or not adb:
            self._say("Terminal on a headset needs adb (File > Set scrcpy/adb folder...)." if dev else
                      "That headset is not known.")
            return None
        listed = AdbFleet(Adb(adb)).listed()
        serial = adb_serial(_Dev(dev), listed)
        name = dev.get("label") or device_id
        if serial is None:
            self._say("%s is not reachable by adb (not in `adb devices`)." % name)
            return None
        return self._open("Terminal: %s" % name, [adb, "-s", serial, "shell"])

    def _open(self, title: str, argv: List[str]) -> Optional[TerminalDock]:
        try:
            session = self.session_factory(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            self._say("Could not start the terminal: %s" % exc)
            return None
        dock = TerminalDock(title, session, self._window)
        dock.closed.connect(lambda d: d in self.docks and self.docks.remove(d))
        self.docks.append(dock)
        self._window.addDockWidget(Qt.BottomDockWidgetArea, dock)
        if len(self.docks) > 1:
            self._window.tabifyDockWidget(self.docks[-2], dock)
        dock.show()
        dock.view.setFocus()
        return dock

    def close_all(self) -> None:
        for dock in list(self.docks):
            dock.close()


class _Dev:
    """The snapshot's device dict in the shape fleetops.adb_serial expects."""

    def __init__(self, d: dict):
        self.ip, self.serial = d.get("ip") or "", d.get("serial") or ""
