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
