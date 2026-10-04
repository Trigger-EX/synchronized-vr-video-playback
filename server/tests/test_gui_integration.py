"""Real server thread + simulated headsets driven through the real Bridge and MainWindow (offscreen)."""

import asyncio
import threading
import time

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from syncvr import launcher  # noqa: E402
from syncvr.app import ServerConfig  # noqa: E402
from syncvr.gui.bridge import Bridge  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402
from syncvr.sim import SimHeadset  # noqa: E402


class Fleet:
    """Simulated headsets on their own event loop thread."""

    def __init__(self, port, tmp_path, count):
        self.loop = asyncio.new_event_loop()
        self.headsets = []
        self.tasks = []
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        for i in range(count):
            self.headsets.append(self._make(i + 1, port, tmp_path / ("hs%d" % (i + 1))))

    def _make(self, index, port, path):
        async def start():
            h = SimHeadset(index, "127.0.0.1", port, download_dir=path, realistic=False)
            self.tasks.append(asyncio.ensure_future(h.run()))
            return h
        return asyncio.run_coroutine_threadsafe(start(), self.loop).result(5)

    def stop(self):
        async def cancel():
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(cancel(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def pump(qapp, predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def stack(qapp, content_dir, tmp_path):
    cfg = ServerConfig(content_dir=content_dir, data_dir=tmp_path / "data", host="127.0.0.1",
                       http_port=0, tcp_port=0, discovery=False)
    thread = launcher.ServerThread(cfg)
    thread.start()
    fleet = Fleet(thread.server.headsets.port, tmp_path, 2)
    bridge = Bridge(thread)
    win = MainWindow(bridge, cfg, tmp_path / "log")
    bridge.start()
    yield thread, fleet, bridge, win
    bridge.close()
    win.close()
    fleet.stop()
    thread.stop(5)


def test_load_and_play_through_panel(qapp, stack):
    thread, fleet, bridge, win = stack
    pump(qapp, lambda: win.snapshot and sum(1 for d in win.snapshot["devices"] if d["online"]) == 2
         and len(win.snapshot["library"]) == 2)
    assert win.stats_label.text().startswith("2 / 2 online")
    panel = win.playback
    idx = panel.video_combo.findData("trailer_flat.mp4")
    assert idx >= 0
    panel.video_combo.setCurrentIndex(idx)

    panel.buttons["load"].click()
    pump(qapp, lambda: all((d.get("desired") or {}).get("mode") == "paused" and
                           d["desired"]["video"] == "trailer_flat.mp4" for d in win.snapshot["devices"]))
    panel.buttons["play"].click()
    pump(qapp, lambda: all((d.get("desired") or {}).get("mode") == "playing" for d in win.snapshot["devices"]))
    pump(qapp, lambda: all(h.player.is_playing for h in fleet.headsets))
    assert "trailer_flat" in panel.now_playing.text()

    panel.buttons["stop"].click()
    pump(qapp, lambda: all((d.get("desired") or {}).get("mode") not in ("playing", "paused")
                           for d in win.snapshot["devices"]))
