import sys
import time

import pytest

from syncvr import launcher
from syncvr.app import ServerConfig


def make_config(tmp_path):
    (tmp_path / "content").mkdir()
    return ServerConfig(content_dir=tmp_path / "content", data_dir=tmp_path / "data", host="127.0.0.1",
                        http_port=0, tcp_port=0, discovery=False)


def test_server_thread_start_snapshot_stop(tmp_path):
    t = launcher.ServerThread(make_config(tmp_path))
    t.start()
    try:
        assert t.http_port > 0
        snap = t.snapshot()
        assert snap["devices"] == [] and "server" in snap
        assert launcher.port_in_use(t.http_port)
    finally:
        began = time.monotonic()
        assert t.stop(timeout=5)
        assert time.monotonic() - began < 5
    assert not t.running
    assert t.snapshot() is None


def test_server_thread_start_failure_reports(tmp_path):
    first = launcher.ServerThread(make_config(tmp_path))
    first.start()
    try:
        cfg = ServerConfig(content_dir=tmp_path / "content", data_dir=tmp_path / "data2", host="127.0.0.1",
                           http_port=first.http_port, tcp_port=0, discovery=False)
        with pytest.raises(OSError):
            launcher.ServerThread(cfg).start()
    finally:
        first.stop()


def test_status_lines():
    url = "10.0.0.2:8080"
    assert launcher.status_lines(None, url)[0] == "Starting..."
    snap = {"devices": [{"online": True}, {"online": False}, {"online": True}],
            "library": [{}, {}], "downloads": {"active": ["a"], "queued": ["b", "c"]}}
    lines = launcher.status_lines(snap, url)
    assert lines[0] == "Headsets: 2 online of 3 known"
    assert "Videos in content folder: 2" in lines
    assert "Downloads: 3 active or queued" in lines
    assert lines[-1] == "Operator app address: " + url


def test_operator_address():
    snap = {"server": {"addresses": ["192.168.1.5"]}}
    assert launcher.operator_address(snap, 8080) == "192.168.1.5:8080"
    assert launcher.operator_address(None, 9) == "localhost:9"


def test_port_in_use():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    assert launcher.port_in_use(port)
    s.close()
    assert not launcher.port_in_use(port)


def test_load_overrides(tmp_path):
    assert launcher.load_overrides(tmp_path) == {}
    (tmp_path / "launcher.json").write_text('{"http_port": 9000}')
    assert launcher.load_overrides(tmp_path) == {"http_port": 9000}
    (tmp_path / "launcher.json").write_text("not json")
    assert launcher.load_overrides(tmp_path) == {}


def test_setup_logging_writes_file(tmp_path):
    import logging
    path = launcher.setup_logging(tmp_path)
    try:
        logging.getLogger("t").info("hello launcher")
        for h in logging.getLogger().handlers:
            h.flush()
        assert "hello launcher" in path.read_text()
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_syncvr", False):
                root.removeHandler(h)
                h.close()


def test_import_without_pyside6(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6", None)
    assert launcher.qt_available(False) is False
    assert launcher.qt_available(True) is False


def test_snapshot_async(tmp_path):
    t = launcher.ServerThread(make_config(tmp_path))
    assert t.snapshot_async() is None
    t.start()
    try:
        fut = t.snapshot_async()
        assert fut.result(5)["devices"] == []
    finally:
        t.stop()
    assert t.snapshot_async() is None


def test_server_thread_call(tmp_path):
    t = launcher.ServerThread(make_config(tmp_path))
    t.start()
    try:
        assert t.call(lambda c, a, b: (len(c.devices), a + b), 1, 2).result(5) == (0, 3)
        with pytest.raises(ZeroDivisionError):
            t.call(lambda c: 1 / 0).result(5)
    finally:
        t.stop()
    with pytest.raises(RuntimeError):
        t.call(lambda c: 1).result(1)


# ---------------------------------------------------------------- supervisor

class FakeProc:
    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


def run_supervisor(codes, **kw):
    cmds, sleeps, msgs = [], [], []
    it = iter(codes)

    def spawn(cmd):
        cmds.append(cmd)
        return FakeProc(next(it))
    now = [0.0]
    sleep = lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s))
    rc = launcher.supervise(["gui", "--http-port", "9"], spawn=spawn, sleep=sleep, clock=lambda: now[0],
                            say=msgs.append, **kw)
    return rc, cmds, sleeps, msgs


def test_no_restart_after_clean_exit():
    rc, cmds, sleeps, _ = run_supervisor([0])
    assert rc == 0 and len(cmds) == 1 and not sleeps
    assert "--supervised" in cmds[0] and "--recovered" not in cmds[0]


def test_restart_after_exit_1_and_signal_with_recovered_flag():
    rc, cmds, sleeps, _ = run_supervisor([1, -11, 0])
    assert rc == 0 and len(cmds) == 3
    assert "--recovered" not in cmds[0] and "--recovered" in cmds[1] and "--recovered" in cmds[2]
    assert cmds[1][-3:] == ["--http-port", "9", "--supervised"][-3:] or "--http-port" in cmds[1]
    assert sleeps == [1.0, 2.0]


def test_backoff_and_restart_cap():
    rc, cmds, sleeps, msgs = run_supervisor([1] * 10)
    assert rc == 1 and len(cmds) == 6  # first run + 5 restarts
    assert sleeps == [1.0, 2.0, 5.0, 5.0, 5.0]
    assert "giving up" in msgs[-1]


def test_restarts_age_out_of_window(monkeypatch):
    monkeypatch.setattr(launcher, "RESTART_WINDOW_S", 8.0)  # old crashes expire while backing off
    rc, cmds, _, _ = run_supervisor([1] * 8 + [0])
    assert rc == 0 and len(cmds) == 9


def test_waits_for_port_before_respawn():
    busy = iter([True, True, False])
    sleeps = []
    procs = iter([FakeProc(1), FakeProc(0)])
    rc = launcher.supervise(["gui"], port=8080, spawn=lambda c: next(procs), sleep=sleeps.append,
                            clock=lambda: 0.0, port_busy=lambda p: next(busy), say=lambda m: None)
    assert rc == 0 and sleeps == [1.0, 0.5, 0.5]


def test_env_disable_and_child_flags(monkeypatch):
    import argparse
    ns = argparse.Namespace(supervised=False, self_test=False)
    monkeypatch.delenv("SYNCVR_NO_SUPERVISOR", raising=False)
    assert launcher.supervisor_enabled(ns)
    monkeypatch.setenv("SYNCVR_NO_SUPERVISOR", "1")
    assert not launcher.supervisor_enabled(ns)
    monkeypatch.delenv("SYNCVR_NO_SUPERVISOR")
    assert not launcher.supervisor_enabled(argparse.Namespace(supervised=True, self_test=False))
    assert not launcher.supervisor_enabled(argparse.Namespace(supervised=False, self_test=True))


def test_child_command_frozen_and_source(monkeypatch):
    src = launcher.child_command(["gui", "--console"], recovered=True)
    assert src[1:4] == ["-m", "syncvr", "gui"] and src[-3:] == ["--console", "--supervised", "--recovered"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launcher.child_command(["gui", "--console"]) == [sys.executable, "--console", "--supervised"]
