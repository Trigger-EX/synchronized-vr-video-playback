import subprocess
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui import mirror as m  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402


class FakeProc:
    stdout = None

    def __init__(self, cmd, **kw):
        self.cmd, self.returncode, self.terminated = cmd, None, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, t=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def env(monkeypatch):
    popen, run, errors = [], [], []
    monkeypatch.setattr(m, "find_tool", lambda name, extra=(), **k: "/bin/" + name)
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: popen.append(FakeProc(cmd)) or popen[-1])

    def fake_run(cmd, **kw):
        run.append(cmd)
        return SimpleNamespace(returncode=0, stdout="connected to 10.0.0.5:5555\n", stderr="")
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    now = [0.0]
    sleeps = []
    mgr = m.MirrorManager(on_error=lambda t, msg: errors.append(msg), clock=lambda: now[0], sleep=sleeps.append)
    return SimpleNamespace(mgr=mgr, popen=popen, run=run, errors=errors, now=now, sleeps=sleeps)


def test_find_tool_order(tmp_path):
    extra, exe, bundle = tmp_path / "x", tmp_path / "exe", tmp_path / "b"
    for d in (extra, exe / "tools", bundle):
        d.mkdir(parents=True)
    name = m._exe_name("adb")
    (exe / "tools" / name).write_text("")
    (bundle / name).write_text("")
    which = lambda n: "/usr/bin/" + n  # noqa: E731
    kw = dict(frozen=True, exe_dir=exe, meipass=bundle, which=which)
    assert m.find_tool("adb", [extra], **kw) == str(exe / "tools" / name)
    (extra / name).write_text("")
    assert m.find_tool("adb", [extra], **kw) == str(extra / name)
    (extra / name).unlink()
    (exe / "tools" / name).unlink()
    assert m.find_tool("adb", [], **kw) == str(bundle / name)
    assert m.find_tool("scrcpy", [], **kw) == "/usr/bin/scrcpy"
    assert m.find_tool("adb", [], frozen=False, which=which) == "/usr/bin/adb"


def test_start_and_duplicate_guard(env):
    assert env.mgr.start("a", "10.0.0.5", "Seat 1")
    assert env.run == [["/bin/adb", "connect", "10.0.0.5:5555"]]
    cmd = env.popen[0].cmd
    assert cmd[:3] == ["/bin/scrcpy", "-s", "10.0.0.5:5555"] and "SyncVR - Seat 1" in cmd
    assert "27183:27282" in cmd and "--window-borderless" not in cmd
    assert cmd[cmd.index("--max-size") + 1] == str(m.MAX_SIZE) == "480" and "--max-fps" in cmd
    assert "--video-bit-rate" in cmd and "--no-mipmaps" in cmd and "--video-buffer=0" in cmd and "--no-audio" in cmd
    assert not env.mgr.start("a", "10.0.0.5", "Seat 1")
    assert len(env.popen) == 1 and len(env.run) == 1
    env.popen[0].returncode = 0  # window closed: can open again
    env.mgr.poll()
    assert env.mgr.start("a", "10.0.0.5", "Seat 1") and len(env.popen) == 2


def test_missing_tool_message(env, monkeypatch):
    monkeypatch.setattr(m, "find_tool", lambda name, extra=(), **k: None)
    assert not env.mgr.start("a", "10.0.0.5")
    msg = env.errors[0]
    assert "adb and scrcpy not found" in msg and "sudo apt install adb scrcpy" in msg
    assert "brew install scrcpy android-platform-tools" in msg and "adb tcpip 5555" in msg
    assert not env.popen


def test_connect_failure_shows_output(env, monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="failed to connect to 10.0.0.5:5555\n", stderr=""))
    assert not env.mgr.start("a", "10.0.0.5")
    assert "failed to connect to 10.0.0.5:5555" in env.errors[0] and not env.popen

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("adb", 8)
    monkeypatch.setattr(m.subprocess, "run", timeout)
    assert not env.mgr.start("b", "10.0.0.6")
    assert "timed out" in env.errors[1]


def test_scrcpy_failure_reports_stderr_and_cleanup(env):
    env.mgr.start("a", "10.0.0.5")
    env.mgr.start("b", "10.0.0.6")
    env.popen[0].returncode = 1
    env.mgr._procs["a"].lines.append("ERROR: no device")
    env.mgr.poll()
    assert "ERROR: no device" in env.errors[0]
    env.mgr.stop_all()
    assert env.popen[1].terminated and not env.mgr._procs


def test_launch_gap_ready_and_timeout(env):
    ready = []
    env.mgr.on_ready = ready.append
    env.mgr.start("a", "10.0.0.5", embed=True)
    assert "--window-borderless" in env.popen[0].cmd and not env.sleeps
    env.mgr.start("b", "10.0.0.6")
    assert env.sleeps == [m.LAUNCH_GAP]
    env.mgr._procs["a"].ready.set()
    env.mgr.poll()
    env.mgr.poll()
    assert ready == ["a"]
    env.now[0] += m.READY_TIMEOUT + 1
    env.mgr.poll()
    assert env.popen[1].terminated and "did not connect" in env.errors[0] and "b" not in env.mgr._procs


def test_embed_supported():
    assert m.embed_supported({}, "win32") and m.embed_supported({"DISPLAY": ":0"}, "linux")
    assert not m.embed_supported({"XDG_SESSION_TYPE": "wayland"}, "linux") and not m.embed_supported({}, "darwin")
    assert not m.embed_supported({"DISPLAY": ":0"}, "linux", "wayland")
    assert not m.embed_supported({"DISPLAY": ":0"}, "linux", "offscreen")
    xwl = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    assert m.embed_supported(xwl, "linux", "xcb")  # Qt on XWayland


def make_window(qapp, tmp_path, env, can_embed=True):
    from PySide6.QtWidgets import QLabel
    found, embedded = [], []
    win = MainWindow(FakeBridge(), Cfg(), tmp_path / "log", env.mgr, finder=lambda t: found.append(t) or 42,
                     embedder=lambda i: embedded.append(i) or QLabel("view"))
    win.view_pane.can_embed = can_embed
    win.bridge.state.emit(make_snapshot(devices=[
        make_device("a", ip="10.0.0.5"), make_device("b", ip="10.0.0.6"), make_device("c", online=False, ip="10.0.0.7")]))
    return win, found, embedded


def test_dock_embeds_toggles_and_switches(qapp, tmp_path, env):
    win, found, embedded = make_window(qapp, tmp_path, env)
    assert win.headsets.cards["a"].view_button.isEnabled() and not win.headsets.cards["c"].view_button.isEnabled()
    win.headsets.cards["a"].view_button.click()
    assert "--window-borderless" in env.popen[0].cmd and win.view_pane.device_id == "a"
    env.mgr._procs["a"].ready.set()
    env.mgr.poll()
    assert embedded == [42] and found == ["SyncVR - a"]
    win.set_targets(["b"])  # selection change switches the view
    assert env.popen[0].terminated and win.view_pane.device_id == "b" and len(env.popen) == 2
    win.headsets.cards["b"].view_button.click()  # same headset again closes the pane
    assert env.popen[1].terminated and win.view_pane.device_id is None
    win.set_targets(["a"])
    win.view_action.trigger()
    assert len(env.popen) == 3
    win.close()
    assert env.popen[2].terminated


def test_wayland_fallback_note(qapp, tmp_path, env):
    win, found, embedded = make_window(qapp, tmp_path, env, can_embed=False)
    win.view_headset("a")
    assert "--window-borderless" not in env.popen[0].cmd
    env.mgr._procs["a"].ready.set()
    env.mgr.poll()
    assert not embedded
    assert not win.view_pane.isVisible() and win.view_pane.device_id is None  # no empty strip
    assert "own window" in win.statusBar().currentMessage()
    assert not env.popen[0].terminated  # scrcpy keeps running as its own window
    win.close()


def test_embed_failure_falls_back(qapp, tmp_path, env):
    win, found, embedded = make_window(qapp, tmp_path, env)
    def boom(i):
        raise RuntimeError("no")
    win.view_pane.embedder = boom
    win.view_headset("a")
    env.mgr._procs["a"].ready.set()
    env.mgr.poll()
    assert not win.view_pane.isVisible() and "open separately" in win.statusBar().currentMessage()
    assert not env.popen[0].terminated
    win.close()


def test_single_eye_crop_by_default_and_full_feed_toggle(monkeypatch):
    from syncvr.gui import mirror as m
    seen = []

    class P:
        stdout = iter(())
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    mgr = m.MirrorManager(sleep=lambda s: None)
    mgr.tool = lambda n: "/bin/" + n
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "connected to x", "stderr": ""})())
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **k: seen.append(cmd) or P())
    mgr.start("a", "1.2.3.4")
    mgr.stop("a")
    mgr.full_feed = True
    mgr.start("a", "1.2.3.4")
    assert "--crop" in seen[0] and m.EYE_CROP in seen[0]
    assert "--crop" not in seen[1]
