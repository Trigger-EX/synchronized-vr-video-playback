from pathlib import Path

import pytest

from syncvr import parsers

FX = Path(__file__).parent / "fixtures" / "dumpsys"


def fx(name):
    return (FX / name).read_text(errors="replace")


def test_wakefulness():
    assert parsers.wakefulness(fx("power_awake.txt")) == "Awake"
    assert parsers.wakefulness(fx("power_asleep.txt")) == "Asleep"


def test_display_state_variants():
    assert parsers.display_state(fx("display.txt")) == "ON"
    assert parsers.display_state("mScreenState=OFF") == "OFF"
    assert parsers.display_state('DisplayInfo{"x", state DOZE, 1 x 1}') == "DOZE"


def test_focus_and_app():
    text = fx("window.txt")
    assert parsers.focused_window(text) == "com.syncvr.player/com.syncvr.player.MainActivity"
    assert parsers.focused_app(text) == "com.syncvr.player/.MainActivity"
    assert parsers.package_of(parsers.focused_app(text)) == "com.syncvr.player"
    nofocus = fx("window_nofocus.txt")
    assert parsers.focused_window(nofocus) is None and parsers.focused_app(nofocus) is None


def test_thermal_and_battery_temp():
    assert parsers.thermal_status(fx("thermal.txt")) == 1
    assert parsers.thermal_status("Thermal Status: 9") is None  # out of range
    assert parsers.battery_temp_c(fx("battery.txt")) == pytest.approx(31.2)
    assert parsers.battery_temp_c("temperature: 99999") is None


ALL = (parsers.wakefulness, parsers.display_state, parsers.focused_window, parsers.focused_app,
       parsers.thermal_status, parsers.battery_temp_c)


@pytest.mark.parametrize("fn", ALL)
@pytest.mark.parametrize("text", ["", None, 42, b"bytes", "\x00\x01\xff", "x" * 100000,
                                  (FX / "garbage.txt").read_text()])
def test_garbled_input_returns_none_and_never_raises(fn, text):
    assert fn(text) is None
