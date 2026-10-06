"""Headset-side synchronization logic (reference implementation).

The Kotlin engine in player-android/core is a port of this module; the
simulator runs this one. Keep them in step when changing either.

Model
-----
The server sends *anchors*: "video V is at media position P at server time T".
The headset converts server time to its own clock using ``ClockSync``; from
then on the video should be at ``P + (now - T)``.

Starting (and every hard re-sync) is done by *cueing*: pick a moment C far
enough ahead that a seek will have finished by then (C = T for a scheduled
start, or now + margin for a late join), pause, seek to where the video must
be at C, and call play() just before C - early by the player's start-up
latency. Both the seek duration and the start-up latency are measured on
every cue, so each headset calibrates itself.

While playing, the engine compares the player's position with the anchor
every frame. Small drift is absorbed by nudging playback speed by a few
percent ("rate" mode); drift beyond a threshold triggers a new cue.
"""

import math
from collections import deque
from typing import Callable, Optional

from .protocol import DEFAULT_SYNC_SETTINGS

DRIFT_SMOOTHING = 0.1
LEARN_SAMPLES = 6
LEARN_GAIN = 0.7
# Once rate correction kicks in, keep going until drift is below this fraction of the deadband.
RATE_RELEASE = 0.2
# Playing can start this late relative to its cue before we cue again instead.
LATE_START_TOLERANCE_S = 0.05
# Rate mode watchdog: if the video does not advance for this long while the
# rate is not 1.0, assume the player cannot change speed and fall back to seeks.
# Must stay well below the time drift needs to reach hard_seek_ms, or a re-cue
# (which resets the speed) would hide the stall.
STALL_TIMEOUT_S = 0.25


class ClockSync:
    """NTP-style offset estimate between the local clock and the server clock.

    Each ping records local send time t0, server time ts and local receive
    time t1. The sample with the smallest round trip is the least disturbed by
    queuing delay, so its offset ``ts - (t0 + t1) / 2`` is used.
    """

    def __init__(self, window: int = 20):
        self.samples = deque(maxlen=window)
        self.offset: Optional[float] = None
        self.rtt: Optional[float] = None

    @property
    def synced(self) -> bool:
        return self.offset is not None

    def add(self, t0: float, ts: float, t1: float) -> None:
        rtt = t1 - t0
        if rtt < 0:
            return
        self.samples.append((rtt, ts - (t0 + t1) / 2.0))
        self.rtt, self.offset = min(self.samples)

    def reset(self) -> None:
        self.samples.clear()
        self.offset = self.rtt = None

    def server_time(self, local: float) -> float:
        return local + (self.offset or 0.0)


class SyncEngine:
    """Drives a player object so it tracks the server's anchors.

    The player must provide: ``loaded_video`` (str or None), ``is_prepared``,
    ``is_seeking``, ``is_playing``, ``error``, ``time``, ``length``,
    ``can_set_rate``, ``load(video_msg)``, ``play()``, ``pause()``, ``stop()``,
    ``seek(t)``, ``set_rate(r)``, ``set_loop(b)``, ``set_external_time(t)`` and
    ``clear_external_time()``.
    """

    def __init__(self, player, emit_event: Callable[[str, str], None] = lambda level, msg: None):
        self.player = player
        self.emit_event = emit_event
        self.settings = dict(DEFAULT_SYNC_SETTINGS)
        self.state = "idle"  # idle|loading|ready|playing|paused|ended|error
        self.anchor: Optional[dict] = None
        self.pause_req: Optional[dict] = None
        self.video_msg: Optional[dict] = None
        self.pending_start = False
        self.cue_at: Optional[float] = None
        self.seek_started: Optional[float] = None
        self.settle_until = 0.0
        self.last_seek = -math.inf
        self.drift: Optional[float] = None
        self.rate = 1.0
        self.seek_time = 0.3  # learned: how long a seek takes
        self.start_latency = 0.1  # learned: delay between play() and frames moving
        self.learning = False
        self.learn_samples = []
        self.forced_mode: Optional[str] = None
        self.last_progress_pos: Optional[float] = None
        self.last_progress_t = 0.0

    # ----------------------------------------------------------- commands

    def _ensure_loaded(self, msg: dict) -> None:
        if self.player.loaded_video != msg["video"]:
            self.player.load(msg)
            self.state = "loading"
            self.drift = None
        self.video_msg = msg

    def on_play(self, msg: dict) -> None:
        same = (self.anchor is not None and self.anchor["video"] == msg["video"]
                and abs(self.anchor["pos"] - msg["pos"]) < 1e-3 and abs(self.anchor["at"] - msg["at"]) < 1e-3)
        self._ensure_loaded(msg)
        self.anchor = msg
        self.pause_req = None
        if same and self.state == "playing":
            return  # resync of the anchor we already follow
        self.pending_start = True
        self.cue_at = None

    def on_pause(self, msg: dict) -> None:
        self._ensure_loaded(msg)
        self.pause_req = msg
        if self.state != "playing":
            self.anchor = None
            self.pending_start = False

    def on_stop(self) -> None:
        self.player.stop()
        self.anchor = self.pause_req = self.video_msg = None
        self.pending_start = False
        self.seek_started = None
        self.state = "idle"
        self._set_rate(1.0)

    def on_settings(self, settings: dict) -> None:
        self.settings.update(settings)
        self.forced_mode = None

    def resync(self) -> None:
        """Re-cue the current anchor, e.g. after the app was suspended."""
        if self.anchor is not None and self.state in ("playing", "ready"):
            self.pending_start = True
            self.cue_at = None

    # ------------------------------------------------------------- update

    def expected_position(self, now: float) -> Optional[float]:
        if self.anchor is None:
            return None
        pos = self.anchor["pos"] + (now - self.anchor["at"])
        length = self._length()
        if length and self.anchor.get("loop"):
            pos %= length
        return pos

    def _length(self) -> float:
        return self.player.length or (self.anchor or {}).get("duration") or 0.0

    def _set_rate(self, rate: float) -> None:
        if abs(rate - self.rate) > 1e-4:
            self.rate = rate
            self.player.set_rate(rate)

    def _settle(self, now: float, learn: bool) -> None:
        self.settle_until = now + self.settings["settle_ms"] / 1000.0
        self.drift = None
        self.learning = learn
        self.learn_samples = []
        self.last_progress_pos = None

    def _seek(self, now: float, pos: float) -> None:
        self.player.seek(pos)
        self.seek_started = now

    def _track_seek(self, now: float) -> None:
        if self.seek_started is not None and not self.player.is_seeking:
            took = now - self.seek_started
            self.seek_started = None
            # Rise immediately, decay slowly: better to cue a little too early.
            self.seek_time = took if took > self.seek_time else 0.9 * self.seek_time + 0.1 * took

    def cue_margin(self) -> float:
        return min(5.0, 1.5 * self.seek_time + 0.15)

    def update(self, now: float) -> None:
        """Call once per rendered frame with the current *server* time."""
        self._track_seek(now)
        if getattr(self.player, "error", None):
            self.state = "error"
            return
        if self.state == "loading":
            if not self.player.is_prepared:
                return
            self.state = "paused"
            if self.anchor is None and self.pause_req is None:
                self.pause_req = dict(self.video_msg or {}, at=now, pos=0.0)

        if self.pause_req is not None and now >= self.pause_req["at"]:
            req, self.pause_req = self.pause_req, None
            self._set_rate(1.0)
            self.player.pause()
            self._seek(now, req["pos"])
            self.anchor = None
            self.pending_start = False
            self.state = "paused"
            return

        if self.anchor is None:
            return

        if self.pending_start:
            self._start(now)
            return

        if self.state == "playing":
            self._correct(now)

    def _start(self, now: float) -> None:
        if self.cue_at is None:
            at = self.anchor["at"]
            if at - now < self.cue_margin():
                at = now + self.cue_margin()  # late: pick a reachable point further on
            self.cue_at = at
            self._set_rate(1.0)
            self.player.set_loop(bool(self.anchor.get("loop")))
            self.player.pause()
            self._seek(now, self._wrap(self.expected_position(at)))
            if self.state != "playing":
                self.state = "ready"
        if self.player.is_seeking or now < self.cue_at - self.start_latency:
            return
        if now > self.cue_at + LATE_START_TOLERANCE_S:
            self.cue_at = None  # the seek took too long; cue again further ahead
            return
        self.player.play()
        self.pending_start = False
        self.cue_at = None
        self.state = "playing"
        self._settle(now, learn=True)

    def _wrap(self, pos: float) -> float:
        length = self._length()
        if length and self.anchor and self.anchor.get("loop"):
            return pos % length
        return max(0.0, min(pos, length - 0.05)) if length else max(0.0, pos)

    def _correct(self, now: float) -> None:
        if self.player.is_seeking or now < self.settle_until:
            return
        expected = self.expected_position(now)
        length = self._length()
        if length and not self.anchor.get("loop") and expected >= length - 0.25:
            if expected >= length:
                self.state = "ended"
            return
        actual = self.player.time
        if self._stalled(now, actual):
            return
        raw = actual - expected
        if length and self.anchor.get("loop"):
            raw = (raw + length / 2.0) % length - length / 2.0
        self.drift = raw if self.drift is None else self.drift + DRIFT_SMOOTHING * (raw - self.drift)

        if self.learning:
            # Right after a cued start, drift is exactly how wrong our
            # start-latency estimate was (negative = started late).
            self.learn_samples.append(raw)
            if len(self.learn_samples) >= LEARN_SAMPLES:
                err = sorted(self.learn_samples)[len(self.learn_samples) // 2]
                self.start_latency = min(1.0, max(0.0, self.start_latency - LEARN_GAIN * err))
                self.learning = False

        s = self.settings
        mode = self.forced_mode or s["correction_mode"]
        if mode == "rate" and not self.player.can_set_rate:
            mode = "seek"
        drift = self.drift
        threshold = s["seek_mode_threshold_ms"] if mode == "seek" else s["hard_seek_ms"]
        if abs(drift) > threshold / 1000.0:
            if now - self.last_seek >= s["seek_cooldown_ms"] / 1000.0:
                self.last_seek = now
                self.pending_start = True
                self.cue_at = None
            return
        if mode == "rate":
            deadband = s["deadband_ms"] / 1000.0
            if abs(drift) > deadband:
                adjust = max(-s["max_rate_adjust"], min(s["max_rate_adjust"], s["rate_gain"] * drift))
                self._set_rate(1.0 - adjust)
            elif abs(drift) < deadband * RATE_RELEASE:
                self._set_rate(1.0)
        if mode == "external":
            self.player.set_external_time(expected)
        else:
            self.player.clear_external_time()

    def _stalled(self, now: float, actual: float) -> bool:
        """Detect players that freeze when their speed is changed."""
        if self.last_progress_pos is None or abs(actual - self.last_progress_pos) > 1e-6:
            self.last_progress_pos = actual
            self.last_progress_t = now
            return False
        if self.rate != 1.0 and now - self.last_progress_t > STALL_TIMEOUT_S:
            self.forced_mode = "seek"
            self._set_rate(1.0)
            self.emit_event("warn", "video stalled while changing speed; using seek-only correction")
            self._settle(now, False)
            return True
        return False

    def status(self, now: float) -> dict:
        expected = self.expected_position(now)
        state = self.state
        if state == "playing" and self.pending_start:
            state = "syncing"
        st = {
            "state": state,
            "video": (self.video_msg or {}).get("video"),
            "position": round(self.player.time, 3) if self.player.loaded_video else None,
            "expected": round(expected, 3) if expected is not None and self.state == "playing" else None,
            "duration": round(self._length(), 3) or None,
            "drift_ms": round(self.drift * 1000.0, 1) if self.drift is not None else None,
            "rate": round(self.rate, 4),
            "mode": self.forced_mode or self.settings["correction_mode"],
            "seek_time_ms": round(self.seek_time * 1000.0),
            "start_latency_ms": round(self.start_latency * 1000.0),
        }
        if self.anchor is not None:
            st["anchor"] = {"pos": round(self.anchor["pos"], 3), "at": round(self.anchor["at"], 3),
                            "loop": bool(self.anchor.get("loop"))}
        return st
