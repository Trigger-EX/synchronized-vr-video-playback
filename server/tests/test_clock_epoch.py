import time

import pytest

from syncvr import protocol


@pytest.fixture(autouse=True)
def _reset():
    protocol.set_clock_offset(0.0)
    yield
    protocol.set_clock_offset(0.0)


def test_same_boot_keeps_offset():
    protocol.set_clock_offset(1234.5)
    saved = protocol.clock_epoch()
    protocol.set_clock_offset(0.0)
    protocol.rebase_clock(saved)
    assert protocol.server_clock() - time.monotonic() == pytest.approx(1234.5)


def test_reboot_continues_within_10ms(monkeypatch):
    real_mono, real_wall = time.monotonic(), time.time()
    protocol.set_clock_offset(500.0)
    # first boot: monotonic = real_mono
    monkeypatch.setattr(time, "monotonic", lambda: real_mono)
    monkeypatch.setattr(time, "time", lambda: real_wall)
    saved = protocol.clock_epoch()
    # reboot 60 s later: monotonic restarts near zero, wall keeps going
    monkeypatch.setattr(time, "monotonic", lambda: 3.0)
    monkeypatch.setattr(time, "time", lambda: real_wall + 60.0)
    protocol.set_clock_offset(0.0)
    protocol.rebase_clock(saved)
    assert protocol.server_clock() == pytest.approx(saved["server_now"] + 60.0, abs=0.01)


@pytest.mark.parametrize("bad", [None, {}, [], "x", 5, {"offset": "a", "wall_minus_mono": None},
                                 {"offset": float("nan"), "wall_minus_mono": 1, "server_now": "q"},
                                 {"offset": True, "saved_wall": [], "server_now": {}}])
def test_garbage_input_is_ignored(bad):
    protocol.set_clock_offset(7.0)
    protocol.rebase_clock(bad)
    assert protocol.server_clock() - time.monotonic() == pytest.approx(7.0, abs=0.01)
