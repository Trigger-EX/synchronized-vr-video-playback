import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.headsets import EditDialog  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def feed(win, devices, library=()):
    win.bridge.state.emit(make_snapshot(devices=devices, library=list(library)))
    return win.headsets


def shown(tab):
    return [i for i, c in tab.cards.items() if not c.isHidden()]


def test_cards_updated_in_place(win):
    tab = feed(win, [make_device("a1", status={"state": "playing", "position": 30, "duration": 60,
                                              "drift_ms": 90.0, "battery": 0.15, "wifi_rssi": -80, "rtt_ms": 60,
                                              "temp_c": 45, "worn": True, "storage_free": 1024 ** 3,
                                              "video": "x.mp4"},
                                  group="A", player="exo")],
                    library=[{"name": "x.mp4", "size": 5}])
    card = tab.cards["a1"]
    assert card.state_label.text() == "playing"
    assert "drift +90 ms" in card.time_label.text() and "0:30 / 1:00" in card.time_label.text()
    assert "color" in card.time_label.text()  # poor drift is coloured
    for frag in ("battery 15%", "45°C", "on head", "rtt 60 ms", "wifi 40%", "free", "0/1 videos"):
        assert frag in card.info_label.text()
    assert card.group_label.text() == "A" and card.player_label.text() == "exo"
    assert card.position_bar.value() == 500
    feed(win, [make_device("a1", status={"state": "paused", "position": 31, "duration": 60,
                                         "download": {"name": "x.mp4", "received": 50, "total": 100},
                                         "error": "boom"})])
    assert tab.cards["a1"] is card
    assert card.state_label.text() == "paused"
    assert not card.download_bar.isHidden() and "50%" in card.download_label.text()
    assert card.download_bar.value() == 500
    assert card.error_label.text() == "boom" and not card.error_label.isHidden()
    feed(win, [make_device("a1", online=False, last_seen=0)])
    assert card.state_label.text() == "offline" and card.seen_label.text() == "last seen never"
    assert card.info_label.isHidden() and card.error_label.isHidden()
    feed(win, [])
    assert tab.cards == {}


def test_order_and_filters(win):
    tab = feed(win, [make_device("b", group="B"), make_device("a2", group="A"), make_device("a10", group="A"),
                     make_device("off", online=False, group="A")])
    assert [w.device_id for w in tab.flow.widgets()] == ["a2", "a10", "b", "off"]
    assert [tab.group_filter.itemText(i) for i in range(3)] == ["All groups", "A", "B"]
    tab.show_offline.setChecked(False)
    assert set(shown(tab)) == {"a2", "a10", "b"}
    tab.group_filter.setCurrentIndex(tab.group_filter.findData("A"))
    assert set(shown(tab)) == {"a2", "a10"}
    tab.show_offline.setChecked(True)
    assert set(shown(tab)) == {"a2", "a10", "off"}
    tab.select_all()
    assert win.target_spec() == ["a2", "a10", "off"]
    feed(win, [make_device("b", group="B")])  # group A gone: filter falls back
    assert tab.group_filter.currentData() == ""
    assert win.targets == []


def test_click_selects_and_clears(win):
    tab = feed(win, [make_device("a"), make_device("b")])
    tab.show()
    QTest.mouseClick(tab.cards["a"], Qt.LeftButton)
    QTest.mouseClick(tab.cards["b"], Qt.LeftButton)
    assert win.target_spec() == ["a", "b"]
    assert win.playback.target_label.text() == "Controlling 2 selected headsets"
    QTest.mouseClick(tab.cards["a"], Qt.LeftButton)
    assert win.target_spec() == ["b"]
    tab.select_none_button.click()
    assert win.target_spec() == "all"


def test_edit_and_forget(win, monkeypatch):
    tab = feed(win, [make_device("a", name="Old", group="G"), make_device("z", online=False)])
    dev = win.snapshot["devices"][0]
    dialog = EditDialog(dev, ["G"])
    assert dialog.name_edit.text() == "Old" and dialog.group_edit.currentText() == "G"
    assert dialog.forget_button.isHidden()
    assert not EditDialog(win.snapshot["devices"][1], []).forget_button.isHidden()

    def run(d):
        d.name_edit.setText("New")
        d.group_edit.setCurrentText("H")
        d.save_button.click()
        return True
    monkeypatch.setattr(tab, "_exec", run)
    tab.open_edit("a")
    assert win.bridge.calls[-1] == ("update_device", "a", {"name": "New", "group": "H"})

    win.set_targets(["z"])
    monkeypatch.setattr(tab, "confirm", lambda text: False)
    tab.apply_edit(win.snapshot["devices"][1], "forget", "", "")
    assert win.bridge.calls[-1][0] == "update_device"
    monkeypatch.setattr(tab, "confirm", lambda text: True)
    tab.apply_edit(win.snapshot["devices"][1], "forget", "", "")
    assert win.bridge.calls[-1] == ("forget_device", "z")
    assert win.targets == []
