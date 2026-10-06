"""Qt-free helpers that keep local video playback cheap: HW-decode environment defaults, a frame-rate
gate and conversion statistics. Must be usable before QtMultimedia is imported."""
from __future__ import annotations

import os
import sys
from typing import MutableMapping, Optional

HWDEC_ENV = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"  # honoured by Qt >= 6.5 (FFmpeg backend)
HWDEC_SWITCH = "SYNCVR_LOCAL_HWDEC"  # =0 forces software decoding
TARGET_FPS = 20.0
STATS_EVERY_S = 10.0

_HWDEC_DEFAULTS = (("linux", "vaapi,cuda"), ("win32", "d3d11va,cuda"), ("darwin", "videotoolbox"))


def hwdec_default(platform: Optional[str] = None) -> str:
    platform = sys.platform if platform is None else platform
    for prefix, types in _HWDEC_DEFAULTS:
        if platform.startswith(prefix):
            return types
    return ""


def configure_hwdec(environ: Optional[MutableMapping] = None, platform: Optional[str] = None) -> str:
    """Set the Qt FFmpeg HW-decode device list (never overriding the user's own value); returns the
    effective value ('' = Qt's own default). SYNCVR_LOCAL_HWDEC=0 selects software decoding ('none' is
    not a device type, so Qt ends up with an empty list)."""
    env = os.environ if environ is None else environ
    if env.get(HWDEC_SWITCH, "").strip().lower() in ("0", "off", "false", "no"):
        env.setdefault(HWDEC_ENV, "none")
    else:
        default = hwdec_default(platform)
        if default:
            env.setdefault(HWDEC_ENV, default)
    return env.get(HWDEC_ENV, "")


class FrameGate:
    """Admits at most `fps` frames per second (monotonic clock supplied by the caller)."""

    def __init__(self, fps: float = TARGET_FPS):
        self.interval = 1.0 / fps if fps > 0 else 0.0
        self._last: Optional[float] = None

    def wait(self, now: float) -> float:
        """0.0 and the frame is admitted, otherwise seconds until the next slot (nothing is recorded)."""
        remaining = 0.0 if self._last is None else self._last + self.interval - now
        if remaining <= 1e-9:
            self._last = now
            return 0.0
        return remaining

    def mark(self, now: float) -> None:
        self._last = now

    def reset(self) -> None:
        self._last = None


class FrameStats:
    """Counts received/converted/skipped frames and toImage time; report() every STATS_EVERY_S."""

    def __init__(self, every: float = STATS_EVERY_S):
        self.every = every
        self._t0: Optional[float] = None
        self.received = self.converted = self.skipped = 0
        self.convert_s = 0.0

    def add(self, now: float, converted: bool, seconds: float = 0.0) -> Optional[str]:
        if self._t0 is None:
            self._t0 = now
        self.received += 1
        if converted:
            self.converted += 1
            self.convert_s += seconds
        else:
            self.skipped += 1
        elapsed = now - self._t0
        if elapsed < self.every:
            return None
        text = "frames: %.1f/s received, %.1f/s converted, %d skipped, mean toImage %.1f ms" % (
            self.received / elapsed, self.converted / elapsed, self.skipped,
            1000.0 * self.convert_s / self.converted if self.converted else 0.0)
        self._t0, self.received, self.converted, self.skipped, self.convert_s = now, 0, 0, 0, 0.0
        return text
