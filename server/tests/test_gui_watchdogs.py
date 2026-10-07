import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402
from syncvr.gui.tools import ArmDialog, PatternDialog  # noqa: E402


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def wd(name, title, mode="off", tested=False, blocker=None, **over):
    d = {"name": name, "title": title, "rule": "RULE of " + name, "enabled": mode != "off", "armed": mode == "armed",
         "mode": mode, "tested": tested, "arm_blocker": blocker, "cfg": {}, "last_cycle": None, "last_summary": "",
         "counters": {}, "decisions": []}
    d.update(over)
    return d


def feed(win, wds):
    win.bridge.state.emit(make_snapshot(devices=[make_device("a")], watchdogs=wds))
    return win.tab_widgets["Tools"]


def answer(tab, accept):
    shown = []

    def fake(dialog):
        shown.append(dialog)
        return accept
    tab._exec = fake
    return shown


def test_rows_and_pills(win):
    tab = feed(win, [wd("stay_awake", "Stay-awake", "armed"), wd("popup", "Popup", "observe"), wd("keepalive", "Keepalive")])
    assert list(tab.wd_rows) == ["stay_awake", "popup", "keepalive"]
    assert [tab.wd_rows[n]["pill"].text() for n in tab.wd_rows] == ["Armed", "Observe", "Off"]
    assert tab.wd_rows["stay_awake"]["pill"].property("state") == "syncing"
    assert tab.wd_rows["popup"]["pill"].property("state") == "paused"
    assert tab.wd_rows["keepalive"]["pill"].property("state") == "offline"
    assert tab.wd_rows["stay_awake"]["arm"].text() == "Disarm" and tab.wd_rows["popup"]["arm"].text() == "Arm..."
    assert tab.wd_rows["popup"]["enable"].isChecked() and not tab.wd_rows["keepalive"]["enable"].isChecked()


def test_enable_toggle_sends_set_watchdog(win):
    tab = feed(win, [wd("popup", "Popup")])
    tab.wd_rows["popup"]["enable"].click()
    assert win.bridge.calls[-1] == ("set_watchdog", "popup", {"enabled": True})
    feed(win, [wd("popup", "Popup", "observe")])
    tab.wd_rows["popup"]["enable"].click()
    assert win.bridge.calls[-1] == ("set_watchdog", "popup", {"enabled": False})


def test_arm_dialog_quotes_rule_and_arms_with_testing_when_untested(win):
    tab = feed(win, [wd("popup", "Popup", "observe")])
    shown = answer(tab, True)
    tab.wd_rows["popup"]["arm"].click()
    assert isinstance(shown[0], ArmDialog) and shown[0].rule_label.text() == "RULE of popup"
    assert not shown[0].untested_label.isHidden() or shown[0].untested_label.isVisibleTo(shown[0])
    assert shown[0].ok_button.text() == "Arm"
    assert win.bridge.calls[-1] == ("set_watchdog", "popup", {"armed": True, "testing": True})


def test_arm_tested_watchdog_omits_testing(win):
    tab = feed(win, [wd("popup", "Popup", "observe", tested=True)])
    shown = answer(tab, True)
    tab.wd_rows["popup"]["arm"].click()
    assert not shown[0].untested_label.isVisibleTo(shown[0])
    assert win.bridge.calls[-1] == ("set_watchdog", "popup", {"armed": True})


def test_cancelling_the_arm_dialog_sends_nothing(win):
    tab = feed(win, [wd("popup", "Popup", "observe")])
    answer(tab, False)
    tab.wd_rows["popup"]["arm"].click()
    assert not [c for c in win.bridge.calls if c[0] == "set_watchdog"]


def test_disarm_sends_armed_false_without_a_dialog(win):
    tab = feed(win, [wd("popup", "Popup", "armed", tested=True)])
    shown = answer(tab, True)
    tab.wd_rows["popup"]["arm"].click()
    assert shown == [] and win.bridge.calls[-1] == ("set_watchdog", "popup", {"armed": False})


def test_arm_blocker_disables_the_button_with_a_tooltip(win):
    tab = feed(win, [wd("overheat", "Overheat", "observe", blocker="no pattern is set", cfg={"pattern": ""})])
    arm = tab.wd_rows["overheat"]["arm"]
    assert not arm.isEnabled() and "no pattern is set" in arm.toolTip()
    assert "No pattern is set" in tab.wd_rows["overheat"]["pattern_state"].text()
    feed(win, [wd("overheat", "Overheat", "observe", cfg={"pattern": "x"})])
    assert arm.isEnabled() and arm.toolTip() == ""


def test_set_pattern_sends_cfg(win):
    tab = feed(win, [wd("overheat", "Overheat", "observe", blocker="no pattern is set")])
    r = tab.wd_rows["overheat"]
    r["pattern"].setText("  Hot|Warm  ")
    r["set_pattern"].click()
    assert win.bridge.calls[-1] == ("set_watchdog", "overheat", {"cfg": {"pattern": "Hot|Warm"}})


def test_test_pattern_shows_matches_and_confirm_sends_confirm(win, monkeypatch):
    tab = feed(win, [wd("overheat", "Overheat", "observe", blocker="no pattern is set")])
    monkeypatch.setattr(tab, "_pick_folder", lambda: "/data/snapshots/s1")
    r = tab.wd_rows["overheat"]
    r["pattern"].setText("Hot")
    r["test_pattern"].click()
    expected = {"folder": "/data/snapshots/s1", "pattern": "Hot"}
    assert win.bridge.calls[-1] == ("watchdog_test_pattern", "overheat", expected)
    shown = answer(tab, True)
    result = {"error": None, "total": 2, "files": ["logcat.txt"], "confirmed": False,
              "matches": [{"file": "logcat.txt", "line_no": 3, "line": "Hot now"}]}
    win.bridge.result.emit("watchdog_pattern", result)
    dlg = shown[0]
    assert isinstance(dlg, PatternDialog) and dlg.confirm_button.isEnabled() and dlg.list.count() == 1
    assert "2 lines matched" in dlg.head_label.text()
    assert win.bridge.calls[-1] == ("watchdog_test_pattern", "overheat", dict(expected, confirm=True))


def test_pattern_dialog_cannot_confirm_without_matches(qapp):
    assert not PatternDialog({"error": None, "total": 0, "files": [], "matches": []}).confirm_button.isEnabled()
    assert not PatternDialog({"error": "bad", "total": 0}).confirm_button.isEnabled()


def test_closing_the_pattern_dialog_sends_nothing_more(win, monkeypatch):
    tab = feed(win, [wd("overheat", "Overheat", "observe")])
    monkeypatch.setattr(tab, "_pick_folder", lambda: "/f")
    tab.wd_rows["overheat"]["test_pattern"].click()
    answer(tab, False)
    win.bridge.result.emit("watchdog_pattern", {"error": None, "total": 1, "files": [], "matches": [], "confirmed": False})
    assert [c[0] for c in win.bridge.calls].count("watchdog_test_pattern") == 1


def test_cancelled_folder_pick_sends_nothing(win, monkeypatch):
    tab = feed(win, [wd("overheat", "Overheat", "observe")])
    monkeypatch.setattr(tab, "_pick_folder", lambda: None)
    tab.wd_rows["overheat"]["test_pattern"].click()
    assert not [c for c in win.bridge.calls if c[0] == "watchdog_test_pattern"]


def test_summary_text(win):
    tab = feed(win, [wd("popup", "Popup", "observe", last_cycle=1000.0, last_summary="2 headsets checked",
                        decisions=[{"label": "Go A", "text": "would back (observe-only, not sent)"}]),
                     wd("keepalive", "Keepalive")])
    assert tab.wd_rows["keepalive"]["summary"].text() == "no cycle yet"
    text = tab.wd_rows["popup"]["summary"].text()
    assert "2 headsets checked" in text and "Last decision: Go A: would back (observe-only, not sent)" in text
