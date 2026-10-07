import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.features import REGISTRY  # noqa: E402
from syncvr.gui import terminal  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402

FAKEADB = Path(__file__).parent / "fakeadb"
needs_pty = pytest.mark.skipif(not terminal.AVAILABLE, reason="no pty on this platform")


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    w.mirror.tools_dir = str(FAKEADB)
    yield w
    w.close()


def feed(win, tested=True, devices=None):
    feats = [{"key": k, "category": c, "label": label, "tested": tested or k != terminal.FEATURE}
             for k, (c, label) in REGISTRY.items()]
    devs = devices if devices is not None else [make_device("a", label="Go A", ip="192.168.1.20")]
    win.bridge.state.emit(make_snapshot(devices=devs, features=feats))


def pump(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:  # no /proc: fall back to signal 0
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


def run(argv, **kw):
    """A PtySession collecting its output; returns (session, output list, exit codes)."""
    s = terminal.PtySession(argv, **kw)
    out, codes = [], []
    s.data.connect(out.append)
    s.finished.connect(codes.append)
    return s, out, codes


def test_gate_error_rules():
    tested = [{"key": terminal.FEATURE, "tested": True}]
    untested = [{"key": terminal.FEATURE, "tested": False}]
    assert terminal.gate_error(untested, False) == (terminal.UNTESTED if terminal.AVAILABLE else terminal.UNAVAILABLE_TIP)
    assert terminal.gate_error([], False) is not None
    if terminal.AVAILABLE:
        assert terminal.gate_error(tested, False) is None
        assert terminal.gate_error(untested, True) is None
        assert terminal.gate_error([], True) is None


def test_key_bytes():
    assert terminal.key_bytes(Qt.Key_C, Qt.ControlModifier, "\x03") == b"\x03"
    assert terminal.key_bytes(Qt.Key_D, Qt.ControlModifier, "\x04") == b"\x04"
    assert terminal.key_bytes(Qt.Key_Return, Qt.NoModifier, "\r") == b"\r"
    assert terminal.key_bytes(Qt.Key_Up, Qt.NoModifier, "") == b"\x1b[A"
    assert terminal.key_bytes(Qt.Key_A, Qt.NoModifier, "é") == "é".encode()
    assert terminal.key_bytes(Qt.Key_Shift, Qt.NoModifier, "") == b""


def test_menu_entries_exist_and_follow_availability(win):
    acts = win.terminal_actions
    assert set(acts) == {"local", "headset"}
    assert all(a.isEnabled() == terminal.AVAILABLE for a in acts.values())
    if not terminal.AVAILABLE:
        assert terminal.UNAVAILABLE_TIP in acts["local"].toolTip()


@needs_pty
def test_refused_until_tested_or_testing_chosen(win):
    feed(win, tested=False)
    made = []
    win.terminals.session_factory = lambda argv: made.append(argv) or pytest.fail("session must not start")
    win.open_terminal()
    assert made == [] and win.terminals.docks == []
    assert "not marked tested" in win.statusBar().currentMessage()
    win.open_terminal("a")
    assert win.terminals.docks == []


@needs_pty
def test_testing_card_opener_opens_even_when_untested(win):
    feed(win, tested=False)
    tab = win.tab_widgets["Tools"]
    assert terminal.FEATURE in tab.testing_buttons
    tab.open_terminal(testing=True)
    assert len(win.terminals.docks) == 1
    win.terminals.close_all()
    assert win.terminals.docks == []


@needs_pty
def test_local_session_has_controlling_tty_and_window_size():
    s, out, codes = run(["sh", "-c", "tty; stty size"], rows=17, cols=61)
    assert pump(lambda: codes)
    text = b"".join(out).decode()
    assert "/dev/pts/" in text or "/dev/tty" in text
    assert "17 61" in text and codes == [0]


@needs_pty
def test_headset_terminal_runs_adb_shell_on_resolved_serial(win):
    feed(win)
    win.open_terminal("a")
    dock, = win.terminals.docks
    assert pump(lambda: "fake ok 192.168.1.20:5555: shell" in dock.stream.text())
    assert pump(lambda: "[process exited with status 0]" in dock.stream.text())
    assert dock.windowTitle() == "Terminal: Go A"


@needs_pty
def test_headset_terminal_refused_when_not_adb_reachable(win):
    feed(win, devices=[make_device("z", label="Go Z", ip="10.9.9.9")])
    win.open_terminal("z")
    assert win.terminals.docks == []
    assert "not reachable by adb" in win.statusBar().currentMessage()


@needs_pty
def test_headset_terminal_needs_adb_binary(win, monkeypatch):
    feed(win)
    monkeypatch.setattr(win.mirror, "tool", lambda name: None)
    win.open_terminal("a")
    assert win.terminals.docks == [] and "needs adb" in win.statusBar().currentMessage()


@needs_pty
def test_typing_reaches_the_shell_and_output_is_rendered(win):
    feed(win)
    dock = win.open_terminal() or win.terminals.docks[0]
    dock.view.typed.emit(b"echo syncvr-$((20+22))\r")
    assert pump(lambda: "syncvr-42" in dock.view.toPlainText())
    assert "\x1b" not in dock.view.toPlainText()


@needs_pty
def test_close_hangs_up_then_kills_the_whole_group(monkeypatch):
    monkeypatch.setattr(terminal, "GRACE_S", 0.2)
    # HUP is ignored by the shell and (being inherited) by its child: only SIGKILL stops them
    s, out, codes = run(["sh", "-c", "trap '' HUP; sleep 60 & echo child=$!; wait"])
    assert pump(lambda: b"child=" in b"".join(out))
    child = int(re.search(rb"child=(\d+)", b"".join(out)).group(1))
    leader = s.proc.pid
    assert alive(leader) and alive(child)
    s.close()
    assert codes == [-signal.SIGKILL] and not alive(leader)
    assert pump(lambda: not alive(child), 3)
    s.close()  # idempotent
    assert codes == [-signal.SIGKILL]


@needs_pty
def test_closing_the_dock_and_the_window_kills_sessions(win):
    feed(win)
    win.open_terminal()
    win.open_terminal()
    pids = [d.session.proc.pid for d in win.terminals.docks]
    assert len(pids) == 2 and all(alive(p) for p in pids)
    win.terminals.docks[0].close()
    assert len(win.terminals.docks) == 1 and not alive(pids[0]) and alive(pids[1])
    win.close()
    assert not alive(pids[1]) and win.terminals.docks == []


def test_no_web_or_cli_route_exists():
    pytest.importorskip("aiohttp")
    from syncvr.web import WebApp
    paths = [getattr(r.resource, "canonical", "") for r in WebApp(None).app.router.routes()]
    assert paths and not [p for p in paths if re.search(r"term|shell|pty", p, re.I)]
    for name in ("web.py", "__main__.py", "controller.py", "app.py"):
        src = (Path(__file__).parent.parent / "syncvr" / name).read_text()
        assert "termstream" not in src and "gui.terminal" not in src and "import pty" not in src, name
    proc = subprocess.run([sys.executable, "-m", "syncvr", "terminal"], capture_output=True, text=True,
                          cwd=Path(__file__).parent.parent)
    assert proc.returncode != 0
