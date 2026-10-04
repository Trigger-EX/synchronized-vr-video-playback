import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402

LIBRARY = [{"name": "a.mp4", "title": "Alpha", "size": 10}, {"name": "b.mp4", "title": "", "size": 20}]


def playing(video="a.mp4", pos=30.0, at=1000.0, duration=120.0, mode="playing"):
    return {"mode": mode, "video": video, "pos": pos, "at": at, "duration": duration}


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def feed(win, devices, library=LIBRARY, time=1010.0):
    win.bridge.state.emit(make_snapshot(devices=devices, library=library, server={
        "name": "SyncVR", "time": time, "http_port": 8080, "addresses": []}))


def last(win):
    return win.bridge.calls[-1]


def test_video_combo_and_buttons(win):
    feed(win, [make_device("a"), make_device("b")])
    p = win.playback
    assert [p.video_combo.itemText(i) for i in range(2)] == ["Alpha", "b.mp4"]
    assert p.target_label.text() == "Controlling all 2 headsets"
    p.buttons["play"].click()
    assert last(win) == ("play", "all", {"video": "a.mp4"})
    p.buttons["load"].click()
    assert last(win) == ("load", "all", {"video": "a.mp4"})
    p.buttons["restart"].click()
    assert last(win) == ("play", "all", {"video": "a.mp4", "pos": 0})
    for key, action in (("pause", "pause"), ("stop", "stop"), ("resync", "resync"),
                        ("recenter", "recenter"), ("identify", "identify"), ("cancel_downloads", "cancel_downloads")):
        p.buttons[key].click()
        assert last(win) == (action, "all", {})
    p.buttons["seek-30"].click()
    assert last(win) == ("seek", "all", {"delta": -30})
    p.buttons["seek+10"].click()
    assert last(win) == ("seek", "all", {"delta": 10})
    p.buttons["push"].click()
    assert last(win) == ("sync_content", "all", {"videos": ["a.mp4"]})
    p.buttons["push_all"].click()
    assert last(win) == ("sync_content", "all", {"videos": "all"})


def test_commands_use_selected_targets(win):
    feed(win, [make_device("a"), make_device("b")])
    win.set_targets(["b"])
    assert win.playback.target_label.text() == "Controlling 1 selected headset"
    win.playback.buttons["pause"].click()
    assert last(win) == ("pause", ["b"], {})


def test_no_video_reports_error(win):
    feed(win, [make_device("a")], library=[])
    win.playback.buttons["play"].click()
    assert win.bridge.calls == []
    assert win.statusBar().currentMessage() == "No video in the library"


def test_message_send(win):
    feed(win, [make_device("a")])
    p = win.playback
    p.message_edit.setText("  hello  ")
    p.buttons["message"].click()
    assert last(win) == ("message", "all", {"text": "hello", "seconds": 10})
    assert p.message_edit.text() == ""
    p.buttons["message"].click()
    assert last(win) == ("message", "all", {"text": "", "seconds": 0})


def test_delete_needs_confirmation(win):
    feed(win, [make_device("a"), make_device("b")])
    p = win.playback
    asked = []
    p.confirm = lambda text: asked.append(text) or False
    p.buttons["delete"].click()
    assert asked == ['Remove "a.mp4" from 2 headset(s)?'] and win.bridge.calls == []
    p.confirm = lambda text: True
    p.buttons["delete"].click()
    assert last(win) == ("delete_content", "all", {"videos": ["a.mp4"]})


def test_now_playing_and_seek_position(win):
    feed(win, [make_device("a", desired=playing())])
    p = win.playback
    assert p.now_playing.text() == "Playing: Alpha"
    assert p.pos_label.text() == "0:40" and p.dur_label.text() == "2:00"
    assert p.seek.value() == round(40 / 120 * 1000)
    assert p.video_combo.currentData() == "a.mp4"


def test_follows_loaded_video_until_touched(win):
    feed(win, [make_device("a", desired=playing(video="b.mp4", mode="paused"))])
    p = win.playback
    assert p.video_combo.currentData() == "b.mp4"
    assert p.now_playing.text() == "Paused: b.mp4"
    p.video_combo.activated.emit(0)
    p.video_combo.setCurrentIndex(0)
    feed(win, [make_device("a", desired=playing(video="b.mp4", mode="paused"))])
    assert p.video_combo.currentData() == "a.mp4"


def test_nothing_loaded(win):
    feed(win, [make_device("a")])
    assert win.playback.now_playing.text() == "Nothing loaded"


def test_seek_slider_not_updated_while_dragging(win):
    feed(win, [make_device("a", desired=playing())])
    p = win.playback
    before = p.seek.value()
    p.seek.sliderPressed.emit()
    feed(win, [make_device("a", desired=playing())], time=1050.0)
    assert p.seek.value() == before and p.pos_label.text() == "0:40"
    p.seek.sliderMoved.emit(500)
    assert p.pos_label.text() == "1:00"
    assert win.bridge.calls == []
    p.seek.setValue(500)
    assert win.bridge.calls == []
    p.seek.sliderReleased.emit()
    assert last(win) == ("seek", "all", {"pos": 60.0})
    feed(win, [make_device("a", desired=playing())], time=1050.0)
    assert p.seek.value() == round(80 / 120 * 1000)


def test_programmatic_updates_send_nothing(win):
    feed(win, [make_device("a", desired=playing(), volume=0.4)])
    assert win.bridge.calls == []
    assert win.playback.volume.value() == 40 and win.playback.volume_label.text() == "40%"


def test_volume_sends_on_release(win):
    feed(win, [make_device("a", volume=1.0)])
    p = win.playback
    p.volume.sliderPressed.emit()
    p.volume.setValue(25)
    assert win.bridge.calls == []
    p.volume.sliderReleased.emit()
    assert last(win) == ("volume", "all", {"value": 0.25})
    p.volume.setValue(60)
    assert last(win) == ("volume", "all", {"value": 0.6})


def test_view_combo_sends_update_video(win):
    lib = [dict(LIBRARY[0], projection="360", stereo="mono", format_source="filename"), LIBRARY[1]]
    feed(win, [make_device("a")], library=lib)
    p = win.playback
    assert p.view_combo.currentData() == "auto"
    assert win.bridge.calls == []  # syncing the combo sends nothing
    p.view_combo.setCurrentIndex(p.view_combo.findData("180/sbs"))
    p._on_view_chosen(p.view_combo.currentIndex())
    assert last(win) == ("update_video", "a.mp4", {"projection": "180", "stereo": "sbs"})
    p.view_combo.setCurrentIndex(0)
    p._on_view_chosen(0)
    assert last(win) == ("update_video", "a.mp4", {"projection": "auto", "stereo": "auto"})
    lib[0]["format_source"] = "operator"
    lib[0]["projection"], lib[0]["stereo"] = "flat", "mono"
    feed(win, [make_device("a")], library=lib)
    assert p.view_combo.currentData() == "flat/mono"
