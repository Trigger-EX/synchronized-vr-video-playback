"""Deterministic tests of the reference sync engine on a virtual clock.

headset/Tests~/EngineTests.cs runs the same scenarios against the C# port.
"""

import pytest

from syncvr.sync_engine import ClockSync, SyncEngine

FRAME = 1.0 / 72.0


class FakePlayer:
    def __init__(self, start_latency=0.08, seek_duration=0.2, load_time=0.3, rate_error=0.0,
                 freezes_when_rate_changed=False, file_missing=False, length=600.0):
        self.now = 0.0
        self.start_latency = start_latency
        self.seek_duration = seek_duration
        self.load_time = load_time
        self.rate_error = rate_error
        self.freezes = freezes_when_rate_changed
        self.file_missing = file_missing
        self.video_length = length
        self.loaded_video = None
        self.error = None
        self.seeks = 0
        self._pos = self._pos_at = 0.0
        self._rate = 1.0
        self._playing = self._prepared = self._looping = False
        self._start_at = self._seek_done_at = self._load_done_at = None
        self._seek_target = 0.0

    def _advance(self):
        if self._playing and self._seek_done_at is None and self._prepared:
            speed = 0.0 if self.freezes and abs(self._rate - 1) > 1e-9 else self._rate * (1 + self.rate_error)
            self._pos += (self.now - self._pos_at) * speed
            if self._pos >= self.video_length:
                if self._looping:
                    self._pos %= self.video_length
                else:
                    self._pos = self.video_length
                    self._playing = False
        self._pos_at = self.now

    def tick(self, now):
        self.now = now
        self._advance()
        if self._load_done_at is not None and now >= self._load_done_at:
            self._prepared, self._load_done_at = True, None
        if self._seek_done_at is not None and now >= self._seek_done_at:
            self._pos, self._seek_done_at = self._seek_target, None
        if self._start_at is not None and now >= self._start_at:
            self._playing, self._start_at = True, None

    is_prepared = property(lambda self: self._prepared)
    is_seeking = property(lambda self: self._seek_done_at is not None)
    is_playing = property(lambda self: self._playing)
    length = property(lambda self: self.video_length if self._prepared else 0.0)
    can_set_rate = True

    @property
    def time(self):
        self._advance()
        return self._pos

    def load(self, msg):
        self.stop()
        if self.file_missing:
            self.error = "video not on this headset"
            return
        self.loaded_video = msg["video"]
        self._load_done_at = self.now + self.load_time

    def play(self):
        self._advance()
        if self._prepared:
            self._start_at = self.now + self.start_latency

    def pause(self):
        self._advance()
        self._playing = False
        self._start_at = None

    def stop(self):
        self._prepared = self._playing = False
        self._start_at = self._seek_done_at = self._load_done_at = None
        self._pos = 0.0
        self.loaded_video = self.error = None

    def seek(self, t):
        self._advance()
        self.seeks += 1
        self._seek_target = t
        self._seek_done_at = self.now + self.seek_duration

    def set_rate(self, r):
        self._advance()
        self._rate = r

    def set_loop(self, loop):
        self._looping = loop

    def set_external_time(self, t):
        pass

    def clear_external_time(self):
        pass


def cmd(pos, at, loop=False, duration=600.0):
    return {"video": "v.mp4", "pos": pos, "at": at, "loop": loop, "duration": duration,
            "projection": "360", "stereo": "mono", "rotation": 0.0}


def run(engine, player, start, end):
    t = start
    while t < end:
        player.tick(t)
        engine.update(t)
        t += FRAME
    player.tick(t)
    return t


def error(engine, player, now):
    return player.time - engine.expected_position(now)


def test_scheduled_start():
    p = FakePlayer()
    e = SyncEngine(p)
    e.on_play(cmd(10, 1.5))
    t = run(e, p, 0, 8)
    assert e.state == "playing"
    assert abs(error(e, p, t)) < 0.02  # residual below the 20 ms deadband is left alone
    assert e.start_latency == pytest.approx(0.08, abs=0.03)


def test_late_join():
    p = FakePlayer(seek_duration=0.4)
    e = SyncEngine(p)
    e.on_play(cmd(0, -95))
    t = run(e, p, 0, 8)
    assert e.state == "playing" and abs(error(e, p, t)) < 0.01
    assert e.seek_time >= 0.39


def test_rate_correction_holds_fast_decoder():
    p = FakePlayer(rate_error=0.004)
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5))
    t = run(e, p, 0, 120)
    assert abs(error(e, p, t)) < 0.025 and p.seeks <= 2


def test_seek_mode():
    p = FakePlayer(rate_error=0.004)
    e = SyncEngine(p)
    e.settings["correction_mode"] = "seek"
    e.on_play(cmd(0, 1.5))
    t = run(e, p, 0, 120)
    assert abs(error(e, p, t)) < 0.09 and e.rate == 1.0


def test_pause_and_resume():
    p = FakePlayer()
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5))
    t = run(e, p, 0, 6)
    e.on_pause(cmd(7.25, t + 0.3))
    t = run(e, p, t, t + 2)
    assert e.state == "paused" and p.time == pytest.approx(7.25, abs=1e-9)
    e.on_play(cmd(7.25, t + 1.5))
    t = run(e, p, t, t + 6)
    assert e.state == "playing" and abs(error(e, p, t)) < 0.01


def test_loop():
    p = FakePlayer(length=10, rate_error=0.002)
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5, loop=True, duration=10))
    t = run(e, p, 0, 34.3)
    err = (error(e, p, t) + 5) % 10 - 5
    assert e.state == "playing" and abs(err) < 0.03


def test_end_without_loop():
    p = FakePlayer(length=5)
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5, duration=5))
    run(e, p, 0, 9)
    assert e.state == "ended"


def test_stall_falls_back_to_seek():
    p = FakePlayer(rate_error=0.004, freezes_when_rate_changed=True)
    events = []
    e = SyncEngine(p, lambda level, msg: events.append(msg))
    e.on_play(cmd(0, 1.5))
    t = run(e, p, 0, 60)
    assert e.forced_mode == "seek" and len(events) == 1
    assert abs(error(e, p, t)) < 0.1


def test_missing_file_then_retry():
    p = FakePlayer(file_missing=True)
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5))
    run(e, p, 0, 1)
    assert e.state == "error"
    p.file_missing = False
    e.on_play(cmd(0, 3))
    t = run(e, p, 1, 8)
    assert e.state == "playing" and abs(error(e, p, t)) < 0.02


def test_resync_after_suspend():
    p = FakePlayer()
    e = SyncEngine(p)
    e.on_play(cmd(0, 1.5))
    t = run(e, p, 0, 5)
    p.pause()  # the OS paused the decoder while the app was suspended
    t = run(e, p, t, t + 10)
    e.resync()
    t = run(e, p, t, t + 5)
    assert e.state == "playing" and abs(error(e, p, t)) < 0.01


def test_clock_sync_prefers_fastest_round_trip():
    c = ClockSync()
    c.add(t0=10.0, ts=1010.2, t1=10.3)  # slow, asymmetric
    c.add(t0=20.0, ts=1020.005, t1=20.01)  # fast round trip wins
    assert c.offset == pytest.approx(1020.005 - 20.005)
    assert c.rtt == pytest.approx(0.01)
