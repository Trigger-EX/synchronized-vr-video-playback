import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_snapshot  # noqa: E402
from syncvr.automation import SHOW_MODE_WARNING  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402
from syncvr.gui.tools import ConfirmDialog  # noqa: E402


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def brake(active=False, reason=None, show_mode=False):
    return {"active": active, "reason": reason, "show_mode": show_mode}


def test_toggle_calls_bridge_and_snapshot_does_not_echo(win):
    win.bridge.state.emit(make_snapshot(brake=brake()))
    assert not win.show_mode_check.isChecked()
    win.show_mode_check.click()
    assert win.bridge.calls[-1] == ("set_show_mode", True)
    n = len(win.bridge.calls)
    win.bridge.state.emit(make_snapshot(brake=brake(True, "show_mode", True)))
    assert win.show_mode_check.isChecked() and len(win.bridge.calls) == n  # no feedback loop
    win.show_mode_check.click()
    assert win.bridge.calls[-1] == ("set_show_mode", False)


def test_brake_pill_text_and_property(win):
    win.bridge.state.emit(make_snapshot(brake=brake()))
    assert win.brake_label.text() == "Automation free" and win.brake_label.property("active") == "false"
    win.bridge.state.emit(make_snapshot(brake=brake(True, "sync in progress")))
    assert win.brake_label.text() == "Automation paused: sync in progress"
    assert win.brake_label.property("active") == "true"
    win.bridge.state.emit(make_snapshot(brake=brake(True, "show_mode", True)))
    assert win.brake_label.text() == "Automation paused: Show Mode"
    win.bridge.state.emit(make_snapshot())  # older server without the field
    assert win.brake_label.text() == "Automation free" and not win.show_mode_check.isChecked()


def test_confirm_dialog_show_mode_warning(qapp):
    base = {"scope_text": "Sleep 1 headset: A", "warnings": [], "every": False}
    plain = ConfirmDialog(base, None)
    assert plain.show_label.isHidden()
    on = ConfirmDialog(dict(base, show_mode=True, warnings=[SHOW_MODE_WARNING, "other"]), None)
    assert not on.show_label.isHidden() and "Show Mode is on" in on.show_label.text()
    assert "Show Mode" not in on.warning_label.text() and "other" in on.warning_label.text()
