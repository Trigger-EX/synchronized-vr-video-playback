import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.log import event_text  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402
from syncvr.gui.settings import MAX_DOWNLOADS, SETTINGS_SPEC  # noqa: E402
from syncvr.protocol import DEFAULT_SYNC_SETTINGS  # noqa: E402


@pytest.fixture
def win(qapp, tmp_path):
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    yield w
    w.close()


def feed(win, settings=None, max_concurrent=4, **over):
    win.bridge.state.emit(make_snapshot(settings=dict(settings or DEFAULT_SYNC_SETTINGS),
                                        downloads={"active": [], "queued": [], "max_concurrent": max_concurrent},
                                        **over))


def test_spec_covers_all_settings():
    keys = [s.key for s in SETTINGS_SPEC]
    assert set(DEFAULT_SYNC_SETTINGS) | {MAX_DOWNLOADS} == set(keys) and len(keys) == len(set(keys))


def test_load_and_save(win):
    tab = win.tab_widgets["Settings"]
    feed(win, dict(DEFAULT_SYNC_SETTINGS, play_lead_ms=2500, correction_mode="seek", rate_gain=1.5), 7)
    assert tab.values()["play_lead_ms"] == 2500 and tab.values()["correction_mode"] == "seek"
    assert tab.values()[MAX_DOWNLOADS] == 7 and tab.values()["rate_gain"] == 1.5
    tab.editors["deadband_ms"].setValue(30)
    tab.editors[MAX_DOWNLOADS].setValue(2)
    tab.save_button.click()
    sent = win.bridge.calls[-2]
    assert sent[0] == "update_settings" and sent[1]["deadband_ms"] == 30 and MAX_DOWNLOADS not in sent[1]
    assert set(sent[1]) == set(DEFAULT_SYNC_SETTINGS)
    assert win.bridge.calls[-1] == ("set_max_downloads", 2)


def test_edited_fields_survive_refresh(win):
    tab = win.tab_widgets["Settings"]
    feed(win)
    tab.editors["settle_ms"].setValue(999)
    feed(win, dict(DEFAULT_SYNC_SETTINGS, settle_ms=100, deadband_ms=50))
    assert tab.values()["settle_ms"] == 999 and tab.values()["deadband_ms"] == 50
    tab.save_button.click()
    feed(win, dict(DEFAULT_SYNC_SETTINGS, settle_ms=100))
    assert tab.values()["settle_ms"] == 100


def test_defaults(win):
    tab = win.tab_widgets["Settings"]
    feed(win, dict(DEFAULT_SYNC_SETTINGS, hard_seek_ms=900), 3)
    tab.defaults_button.click()
    assert win.bridge.calls[-1] == ("update_settings", DEFAULT_SYNC_SETTINGS)
    assert tab.values()["hard_seek_ms"] == DEFAULT_SYNC_SETTINGS["hard_seek_ms"] and tab.values()[MAX_DOWNLOADS] == 3


def test_log_newest_first_with_device_names(win):
    tab = win.tab_widgets["Log"]
    events = [{"t": 1.0, "level": "info", "message": "first"},
              {"t": 2.0, "level": "error", "message": "boom", "device": "a"},
              {"t": 3.0, "level": "warn", "message": "gone", "device": "zz"}]
    feed(win, events=events, devices=[make_device("a", label="Seat 1")])
    texts = [tab.list.item(i).text() for i in range(tab.list.count())]
    assert len(texts) == 3
    assert texts[0].endswith("[warn]  zz: gone") and texts[1].endswith("[error]  Seat 1: boom")
    assert texts[2].endswith("[info]  first")
    feed(win, events=events + [{"t": 4.0, "level": "info", "message": "new"}])
    assert tab.list.item(0).text().endswith("new") and tab.list.count() == 4
    assert event_text({"t": 0, "message": "m"}, {}).endswith("[info]  m")
