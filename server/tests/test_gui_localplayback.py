import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from gui_fakes import Cfg, FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.localplayer import LocalPlayer  # noqa: E402
from syncvr.gui.main_window import MainWindow  # noqa: E402
from test_localplayer import FakeBackend, FakeView  # noqa: E402


def desired(video="a.mp4"):
    return {"mode": "playing", "video": video, "pos": 5.0, "at": 1000.0, "duration": 60}


@pytest.fixture
def win(qapp, tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    bridge, made = FakeBridge(), []

    def bf(video):
        made.append(FakeBackend(video))
        return made[-1]

    class C(Cfg):
        content_dir = str(tmp_path)

    holder = {}
    lp = LocalPlayer(bridge, tmp_path, lambda: holder["w"].server_now(), bf, FakeView)
    w = MainWindow(bridge, C(), tmp_path / "log", local_player=lp)
    holder["w"] = w
    w.made = made
    yield w
    w.close()


def feed(win, devices):
    win.bridge.state.emit(make_snapshot(devices=devices, library=[{"name": "a.mp4", "title": "A", "size": 1}]))


def test_defaults_unchecked_and_follow_disabled(win):
    p = win.playback
    assert not p.local_video.isChecked() and not p.local_audio.isChecked() and not p.local_follow.isChecked()
    assert not p.local_follow.isEnabled()
    assert p.local_mode() is None


def test_exclusive_and_follow_enable(win):
    p = win.playback
    p.local_video.setChecked(True)
    assert p.local_follow.isEnabled() and win.local_player.mode == "video"
    p.local_audio.setChecked(True)
    assert not p.local_video.isChecked() and not p.local_follow.isEnabled()
    assert win.local_player.mode == "audio"
    p.local_audio.setChecked(False)
    assert win.local_player.mode is None


def test_state_feeds_player_and_follow_target(win):
    feed(win, [make_device("a", desired=desired()), make_device("b")])
    p = win.playback
    p.local_video.setChecked(True)
    assert ("play",) in win.made[0].calls
    p.local_follow.setChecked(True)
    assert ("follow", "a") in win.bridge.calls  # focus device
    win.set_targets(["b"])
    assert ("follow", "b") in win.bridge.calls  # single selected target wins
    p.local_follow.setChecked(False)
    assert win.bridge.calls[-1] == ("unfollow",)


def test_window_close_unchecks_and_close_event_stops(win):
    p = win.playback
    p.local_video.setChecked(True)
    win.local_player.window.close()
    assert not p.local_video.isChecked() and win.local_player.mode is None
    p.local_audio.setChecked(True)
    win.close()
    assert win.local_player.mode is None


def test_unavailable_greys_checkboxes(qapp, tmp_path, monkeypatch):
    from syncvr.gui import localplayer
    monkeypatch.setattr(localplayer.QtMediaBackend, "available", staticmethod(lambda: (False, "no media extra")))
    w = MainWindow(FakeBridge(), Cfg(), tmp_path / "log")
    try:
        p = w.playback
        assert not p.local_video.isEnabled() and not p.local_audio.isEnabled()
        assert "no media extra" in p.local_video.toolTip()
    finally:
        w.close()
