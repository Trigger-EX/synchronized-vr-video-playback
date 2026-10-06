import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtWidgets import QWidget  # noqa: E402

from gui_fakes import FakeBridge, make_device, make_snapshot  # noqa: E402
from syncvr.gui.localplayer import LocalPlayer  # noqa: E402


class FakeBackend:
    def __init__(self, video):
        self.video, self.calls, self.pos = video, [], 0.0
        self.on_error = self.on_frame = None

    def load(self, path): self.calls.append(("load", path))
    def play(self): self.calls.append(("play",))
    def pause(self): self.calls.append(("pause",))
    def stop(self): self.calls.append(("stop",))
    def seek(self, s): self.calls.append(("seek", s)); self.pos = s
    def position(self): return self.pos
    def close(self): self.calls.append(("close",))


class FakeView(QWidget):
    def __init__(self):
        super().__init__()
        self.log = []

    def set_frame(self, img): self.log.append(("frame", img))
    def set_view(self, p, s, r=0.0): self.log.append(("view", p, s, r))
    def set_pose(self, y, p, r=0.0): self.log.append(("pose", y, p, r))


def snap(video="a.mp4", mode="playing", pos=10.0, at=1000.0):
    d = make_device("a", desired={"mode": mode, "video": video, "pos": pos, "at": at, "duration": 60})
    lib = [{"name": "a.mp4", "projection": "180", "stereo": "sbs", "rotation": 5.0}]
    return make_snapshot(devices=[d], library=lib)


@pytest.fixture
def env(qapp, tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    bridge, backends, views, errors = FakeBridge(), [], [], []

    def bf(video):
        b = FakeBackend(video)
        backends.append(b)
        return b

    def vf():
        v = FakeView()
        views.append(v)
        return v

    lp = LocalPlayer(bridge, tmp_path, lambda: 1000.0, bf, vf)
    lp.error.connect(errors.append)
    yield lp, bridge, backends, views, errors
    lp.stop()


def test_video_mode_loads_seeks_plays_and_sets_view(env):
    lp, bridge, backends, views, errors = env
    lp.update(snap(), "a")
    lp.set_mode("video")
    b = backends[0]
    assert b.video and lp.window.isVisible() is False or True
    names = [c[0] for c in b.calls]
    assert names[:4] == ["stop", "load", "seek", "play"]
    assert ("seek", 10.0) in b.calls
    assert ("view", "180", "sbs", 5.0) in views[0].log
    b.on_frame("img")
    assert ("frame", "img") in views[0].log
    lp.update(snap(mode="paused"), "a")
    lp.tick()
    assert b.calls[-1] == ("pause",)
    lp.update(snap(mode="stopped"), "a")
    lp.tick()
    assert b.calls[-1] == ("stop",)


def test_audio_only_has_no_window(env):
    lp, _b, backends, views, _e = env
    lp.update(snap(), "a")
    lp.set_mode("audio")
    assert lp.window is None and not views and backends[0].video is False
    assert ("play",) in backends[0].calls


def test_missing_file_reports_once_until_video_changes(env):
    lp, _b, backends, _v, errors = env
    lp.set_mode("audio")
    lp.update(snap("nope.mp4"), "a")
    lp.tick()
    lp.tick()
    assert len(errors) == 1 and "not found" in errors[0]
    lp.update(snap("b.mp4"), "a")
    lp.tick()
    assert ("load", str(lp._content_dir) + "/b.mp4") in backends[0].calls


def test_codec_error_not_retried(env):
    lp, _b, backends, _v, errors = env
    lp.set_mode("audio")
    lp.update(snap(), "a")
    lp.tick()
    backends[0].on_error("bad codec")
    assert len(errors) == 1 and "bad codec" in errors[0]
    n = len(backends[0].calls)
    lp.tick()
    lp.tick()
    assert [c for c in backends[0].calls[n:] if c[0] in ("load", "play")] == []
    lp.update(snap("b.mp4"), "a")
    lp.tick()
    assert any(c[0] == "load" for c in backends[0].calls[n:])


def test_follow_pose_and_unfollow(env):
    lp, bridge, _be, views, _e = env
    lp.update(snap(), "a")
    lp.set_mode("video")
    lp.set_follow("a")
    assert ("follow", "a") in bridge.calls
    bridge.next_pose = {"yaw": 30.0, "pitch": -5.0, "roll": 1.0, "t": 1}
    lp._poll_pose()
    assert ("pose", 30.0, -5.0, 1.0) in views[0].log
    lp.set_follow(None)
    assert bridge.calls[-1] == ("unfollow",)
    lp.set_follow("a")
    lp.set_mode(None)
    assert bridge.calls[-1] == ("unfollow",)


def test_follow_ignored_in_audio_mode(env):
    lp, bridge, *_ = env
    lp.set_mode("audio")
    lp.set_follow("a")
    assert ("follow", "a") not in bridge.calls


def test_window_close_signals(env):
    lp, *_ = env
    lp.set_mode("video")
    seen = []
    lp.windowClosed.connect(lambda: seen.append(1))
    lp.window.close()
    assert seen == [1]


def test_no_frames_reports_decoder_problem(env):
    lp, bridge, backends, views, errors = env
    lp.set_mode("video")
    lp.update(snap(), "a")
    lp.tick()
    assert lp._no_frame_timer.isActive()
    lp._check_frames()
    assert errors and "no video frames" in errors[-1]


def test_frame_cancels_no_frame_watch(env):
    lp, bridge, backends, views, errors = env
    lp.set_mode("video")
    lp.update(snap(), "a")
    lp.tick()
    from PySide6.QtGui import QImage
    backends[0].on_frame(QImage(4, 4, QImage.Format_RGB32))
    assert not lp._no_frame_timer.isActive()
    lp._check_frames()
    assert not errors
    assert views[0].log[-1][0] == "frame"
