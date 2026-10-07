import os
import stat
import sys

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell script fakes")

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui import mirror as m  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    _script(d / "adb", 'case "$2" in 10.0.0.9:*) echo "failed to connect to $2";; *) echo "connected to $2";; esac\n')
    _script(d / "scrcpy", 'echo "$@" >> "%s"\n[ "$1" = "--help" ] && { echo "--window-x"; exit 0; }\n'
            'echo "[server] INFO: Device: fake"\nexec sleep 30\n' % (tmp_path / "args.log"))
    return d


@pytest.fixture
def mgr(tools):
    errors = []
    mg = m.MirrorManager(on_error=lambda t, msg: errors.append(msg), tools_dir=str(tools), sleep=lambda s: None)
    mg.errors = errors
    yield mg
    mg.stop_all()


def devs(n, bad=()):
    return [{"id": "d%d" % i, "ip": "10.0.0.%d" % (9 if i in bad else i + 1), "label": "H%d" % i} for i in range(n)]


@pytest.mark.parametrize("count,screen,cols,rows", [(1, (1920, 1080), 1, 1), (2, (1920, 1080), 2, 1),
                                                    (4, (1920, 1080), 2, 2), (6, (1920, 1080), 3, 2),
                                                    (6, (1080, 1920), 2, 3), (3, (1000, 1000), 2, 2)])
def test_tile_geometry(count, screen, cols, rows):
    tiles = m.tile_geometry(count, *screen)
    assert len(tiles) == count
    assert len({t[:2] for t in tiles}) == count
    assert len({t[2:] for t in tiles}) == 1
    assert len({t[0] for t in tiles}) == min(cols, count) and len({t[1] for t in tiles}) == rows
    for x, y, w, h in tiles:
        assert x + w <= screen[0] and y + h <= screen[1]
    assert m.tile_geometry(0, 100, 100) == []


def test_batch_stepping_with_fake_clock():
    now, log = [0.0], []
    b = m.BatchPreview(list("abcde"), 2, 10, lambda g: log.append(("open", g)), lambda g: log.append(("close", g)),
                       clock=lambda: now[0])
    assert b.start() and not b.start()
    assert log == [("open", ["a", "b"])]
    now[0] = 9.9
    b.tick()
    assert len(log) == 1
    now[0] = 10
    b.tick()
    assert log[1:] == [("close", ["a", "b"]), ("open", ["c", "d"])]
    b.next()
    assert log[-2:] == [("close", ["c", "d"]), ("open", ["e"])]
    b.next()  # wraps
    assert log[-1] == ("open", ["a", "b"])
    b.stop()
    assert log[-1] == ("close", ["a", "b"]) and not b.running and b.current() == []
    n = len(log)
    b.tick()
    b.next()
    assert len(log) == n


def test_start_many_flags_and_cap(mgr, tmp_path):
    started = mgr.start_many(devs(8), max_parallel=6, screen=(1920, 1080))
    assert started == ["d0", "d1", "d2", "d3", "d4", "d5"]
    assert len(mgr.batch_running()) == 6
    assert "At most 6 captures" in mgr.errors[0] and "H6" in mgr.errors[0] and "H7" in mgr.errors[0]
    assert mgr.start_many(devs(8)[6:], max_parallel=6) == []  # cap counts tiles already open
    assert mgr.close_all() == 6 and not mgr.batch_running()
    import time
    time.sleep(0.2)
    lines = [ln for ln in (tmp_path / "args.log").read_text().splitlines() if "--window-x" in ln and "-s" in ln]
    assert len(lines) == 6
    assert all("--max-fps 15" in ln and "--window-width" in ln and "--window-height" in ln and "--window-y" in ln
               for ln in lines)


def test_unreachable_skipped_and_logged(mgr, caplog):
    with caplog.at_level("INFO"):
        started = mgr.start_many(devs(3, bad=(1,)))
    assert started == ["d0", "d2"]
    assert "skipping H1" in caplog.text
    assert not mgr.errors


def test_close_all_only_tracked(mgr):
    assert mgr.start("single", "10.0.0.50", "Solo")
    assert mgr.start_many(devs(2)) == ["d0", "d1"]
    single, tile = mgr._procs["single"].proc, mgr._procs["d0"].proc
    assert mgr.close_all() == 2
    assert tile.poll() is not None and single.poll() is None
    assert mgr.is_running("single")


def test_unsupported_flags_omit_geometry(mgr, tools):
    _script(tools / "scrcpy", 'echo "old scrcpy"\n[ "$1" = "--help" ] && exit 0\nexec sleep 30\n')
    started = mgr.start_many(devs(1))
    assert started == ["d0"]
    assert "--window-x" not in mgr._procs["d0"].proc.args


def test_window_menu_actions(tools, tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    errors = []
    mg = m.MirrorManager(on_error=lambda t, msg: errors.append(msg), tools_dir=str(tools), sleep=lambda s: None)
    win = MainWindow(FakeBridge(), Cfg(), tmp_path / "log", mg)
    snap = make_snapshot(devices=[make_device("d0", ip="10.0.0.1"), make_device("d1", ip="10.0.0.2"),
                                  make_device("d2", ip="10.0.0.3", online=False)])
    win.snapshot = snap
    try:
        assert set(win.capture_actions) == {"selected", "all", "batch", "close"}
        win.targets = ["d1"]
        win.capture(False)
        assert mg.batch_running() == ["d1"]
        win.capture(True)
        assert sorted(mg.batch_running()) == ["d0", "d1"]
        assert win.start_batch(1, 5) and win.batch.running
        win.close_captures()
        assert not mg.batch_running() and win.batch is None
    finally:
        win.close()
