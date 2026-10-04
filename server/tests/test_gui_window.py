import concurrent.futures

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.bridge import Bridge  # noqa: E402
from syncvr.gui.main_window import TABS, MainWindow  # noqa: E402


@pytest.fixture
def window(qapp, tmp_path):
    bridge = FakeBridge()
    win = MainWindow(bridge, Cfg(), tmp_path / "log")
    yield win
    win.close()


def test_tabs_and_empty_status(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == list(TABS)
    assert window.target_spec() == "all"
    assert window.server_now() == 0.0


def test_state_updates_status_bar(window):
    devs = [make_device("a", status={"state": "playing", "drift_ms": 12.0}), make_device("b"),
            make_device("c", online=False)]
    window.bridge.state.emit(make_snapshot(
        devices=devs, downloads={"active": ["a"], "queued": ["b", "c"], "max_concurrent": 4}))
    assert window.stats_label.text() == "2 / 3 online · 1 playing · worst drift 12 ms"
    assert window.downloads_label.text() == "Downloading to 1, 2 waiting"
    assert window.address_label.text() == "Operator app address: 192.168.1.5:8080"
    assert window.server_now() >= 1000.0


def test_targets_follow_known_devices(window):
    window.bridge.state.emit(make_snapshot(devices=[make_device("a"), make_device("b")]))
    window.set_targets(["a", "b"])
    assert window.target_spec() == ["a", "b"]
    assert [d["id"] for d in window.target_devices()] == ["a", "b"]
    window.bridge.state.emit(make_snapshot(devices=[make_device("b")]))
    assert window.targets == ["b"]
    window.set_targets([])
    assert window.target_spec() == "all"


def test_result_and_failure_messages(window):
    window.bridge.result.emit("play", {"warning": "No targeted headset is online"})
    assert window.statusBar().currentMessage() == "No targeted headset is online"
    window.bridge.result.emit("rescan", {"videos": 3})
    assert window.statusBar().currentMessage() == "3 video(s) in library"
    window.bridge.failed.emit("unknown headset: x")
    assert window.statusBar().currentMessage() == "unknown headset: x"
    window.bridge.stopped.emit()
    assert "server stopped" in window.stats_label.text()


def test_close_closes_bridge(qapp, tmp_path):
    bridge = FakeBridge()
    win = MainWindow(bridge, Cfg(), tmp_path / "log")
    win.show()
    win.close()
    assert bridge.closed


class FakeThread:
    running = True

    def __init__(self):
        self.listener = None

    def call(self, fn, *args):
        fut = concurrent.futures.Future()
        controller = type("C", (), {"add_listener": lambda s, f: setattr(self, "listener", f),
                                    "execute": lambda s, a, p: {"action": a, "params": p}})()
        try:
            fut.set_result(fn(controller, *args))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    def snapshot_async(self):
        fut = concurrent.futures.Future()
        fut.set_result(make_snapshot())
        return fut


def test_bridge_state_and_command(qapp):
    thread = FakeThread()
    bridge = Bridge(thread)
    states, results, errors = [], [], []
    bridge.state.connect(states.append)
    bridge.result.connect(lambda a, r: results.append((a, r)))
    bridge.failed.connect(errors.append)
    bridge.start()
    for _ in range(5):
        qapp.processEvents()
    assert len(states) == 1 and thread.listener is not None
    thread.listener()  # a controller change marks the bridge dirty
    bridge._tick()
    for _ in range(5):
        qapp.processEvents()
    assert len(states) == 2
    bridge.command("seek", ["a"], delta=10)
    for _ in range(5):
        qapp.processEvents()
    assert results == [("seek", {"action": "seek", "params": {"delta": 10, "targets": ["a"]}})]
    bridge.close()
    bridge.refresh()
    assert len(states) == 2 and not errors
