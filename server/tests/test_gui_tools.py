import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import Qt  # noqa: E402

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.features import REGISTRY  # noqa: E402
from syncvr.gui.main_window import TABS, MainWindow  # noqa: E402
from syncvr.gui.tools import ConfirmDialog, PoweroffDialog  # noqa: E402


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def features(tested=()):
    return [{"key": k, "category": c, "label": label, "tested": k in tested} for k, (c, label) in REGISTRY.items()]


def feed(win, tested=(), **over):
    devices = [make_device("a", label="Go A"), make_device("b", label="Go B", online=False)]
    win.bridge.state.emit(make_snapshot(devices=devices, features=features(tested), **over))
    return win.tab_widgets["Tools"]


def preview(scope="Sleep 1 headset: Go A", token="tok1", **over):
    return dict({"scope_text": scope, "labels": ["Go A"], "warnings": [], "every": False, "token": token,
                 "needs_confirm": True}, **over)


def answer(win, accept):
    """Replace the modal dialog by a recorder that accepts or rejects it."""
    shown = []

    def fake(dialog):
        shown.append(dialog)
        return accept
    win.confirmer._exec = fake
    return shown


def test_tools_tab_registered(win):
    assert "Tools" in TABS and win.tab_widgets["Tools"] is win.tabs.widget(TABS.index("Tools"))


def test_power_button_previews_selected_targets_then_confirms_with_token(win):
    tab = feed(win)
    win.set_targets(["a"])
    tab.power_buttons["sleep"].click()
    assert win.bridge.calls[-1] == ("preview", "sleep", ["a"], {})
    shown = answer(win, True)
    win.bridge.previewed.emit("sleep", preview(), {"targets": ["a"]})
    assert shown[0].scope_label.text() == "Sleep 1 headset: Go A"
    assert win.bridge.calls[-1] == ("confirmed", "sleep", ["a"], {"confirm": "tok1"})


def test_no_selection_means_all_and_every_wording_is_shown(win):
    tab = feed(win)
    tab.power_buttons["wake"].click()
    assert win.bridge.calls[-1] == ("preview", "wake", "all", {})
    shown = answer(win, True)
    win.bridge.previewed.emit("wake", preview("Wake EVERY headset (2)", every=True), {"targets": "all"})
    assert shown[0].scope_label.text() == "Wake EVERY headset (2)"
    assert win.bridge.calls[-1][3] == {"confirm": "tok1", "confirm_every": True}


def test_cancel_sends_nothing(win):
    feed(win)
    answer(win, False)
    before = list(win.bridge.calls)
    win.bridge.previewed.emit("sleep", preview(), {"targets": ["a"]})
    assert win.bridge.calls == before
    assert win.statusBar().currentMessage().startswith("Cancelled: Sleep 1 headset")


def test_refresh_warning_text_is_shown(win):
    feed(win)
    shown = answer(win, False)
    text = "Screen refresh interrupts playback (visible hitch) on 1 playing headset: Go A"
    win.bridge.previewed.emit("screen_refresh", preview("Screen refresh 1 headset: Go A", warnings=[text]),
                              {"targets": ["a"]})
    dialog = shown[0]
    assert text in dialog.warning_label.text() and not dialog.warning_label.isHidden()


def test_token_mismatch_error_is_surfaced(win):
    feed(win)
    answer(win, True)
    win.bridge.previewed.emit("sleep", preview(token="stale"), {"targets": ["a"]})
    assert win.bridge.calls[-1][3]["confirm"] == "stale"  # the server, not the GUI, judges the token
    win.bridge.failed.emit("confirmation missing or out of date (headsets changed since the preview)")
    assert "confirmation missing" in win.statusBar().currentMessage()


def test_context_menu_target_rule(win):
    feed(win)
    win.set_targets(["a", "b"])
    tab = win.headsets
    from syncvr.gui.headsets import POWER_MENU
    assert [a for a, _t in POWER_MENU] == ["sleep", "wake", "screen_refresh"]
    # same rule as contextMenuEvent: a selected card acts on the whole selection, an unselected one alone
    win.confirmer.request("sleep", list(win.targets) if "a" in win.targets else ["a"])
    assert win.bridge.calls[-1] == ("preview", "sleep", ["a", "b"], {})
    assert tab.cards["a"].device_id == "a"


def test_poweroff_dialog_unticked_dry_run_on(win):
    dialog = PoweroffDialog([make_device("a", label="Go A"), make_device("b", label="Go B", online=False)])
    assert dialog.list.count() == 2 and dialog.selected_ids() == []
    assert dialog.dry_run() and not dialog.ok_button.isEnabled()
    assert "(offline)" in dialog.list.item(1).text()
    dialog.list.item(0).setCheckState(Qt.Checked)
    assert dialog.selected_ids() == ["a"] and dialog.ok_button.isEnabled()


def test_poweroff_flow_dry_run_and_testing_flag(win):
    tab = feed(win)  # power.poweroff not tested yet
    assert not tab.poweroff_button.isEnabled()
    picked = []

    def pick(dialog):
        dialog.list.item(0).setCheckState(Qt.Checked)
        picked.append(dialog.dry_run())
        return True
    tab._exec = pick
    tab.open_poweroff(testing=True)
    assert picked == [True]
    assert win.bridge.calls[-1] == ("preview", "poweroff", ["a"], {"dry_run": True, "testing": True})
    shown = answer(win, True)
    win.bridge.previewed.emit("poweroff", preview("Power off 1 headset: Go A", token="p1"),
                              {"targets": ["a"], "dry_run": True, "testing": True})
    assert not shown[0].every_check.isVisibleTo(shown[0])
    assert win.bridge.calls[-1] == ("confirmed", "poweroff", ["a"],
                                    {"dry_run": True, "testing": True, "confirm": "p1"})


def test_poweroff_enabled_once_tested_and_dry_run_can_be_turned_off(win):
    tab = feed(win, tested={"power.poweroff"})
    assert tab.poweroff_button.isEnabled()

    def pick(dialog):
        for i in range(dialog.list.count()):
            dialog.list.item(i).setCheckState(Qt.Checked)
        dialog.dry_run_check.setChecked(False)
        return True
    tab._exec = pick
    tab.poweroff_button.click()
    assert win.bridge.calls[-1] == ("preview", "poweroff", ["a", "b"], {"dry_run": False})


def test_real_poweroff_of_every_headset_needs_extra_tick(win):
    feed(win, tested={"power.poweroff"})
    shown = answer(win, True)
    win.bridge.previewed.emit("poweroff", preview("Power off EVERY headset (2)", every=True),
                              {"targets": ["a", "b"], "dry_run": False})
    dialog = shown[0]
    assert dialog.every_check.isVisibleTo(dialog) and not dialog.ok_button.isEnabled()
    dialog.every_check.setChecked(True)
    assert dialog.ok_button.isEnabled()


def test_confirm_dialog_plain(qapp):
    d = ConfirmDialog(preview(), None)
    assert d.warning_label.isHidden() and not d.every_check.isVisibleTo(d) and d.ok_button.isEnabled()


def test_testing_card_lists_untested_and_marks(win):
    tab = feed(win, tested={"power.sleep"})
    assert "power.sleep" not in tab.testing_buttons and "power.wake" in tab.testing_buttons
    assert tab.testing_empty.isHidden()
    tab.testing_buttons["power.wake"].click()
    assert win.bridge.calls[-1] == ("set_feature_tested", "power.wake", True)
    feed(win, tested=set(REGISTRY))
    assert tab.testing_buttons == {} and not tab.testing_empty.isHidden()


def test_asleep_label(win):
    tab = feed(win, asleep=["a"])
    assert tab.asleep_label.text() == "1 headset put to sleep from here"
