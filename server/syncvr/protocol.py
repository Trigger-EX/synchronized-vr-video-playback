"""Wire protocol shared by the server, the simulator and the headset app.

Headsets talk to the server over a single TCP connection carrying UTF-8 JSON
objects, one per line. Every object has a ``type`` field. See docs/PROTOCOL.md
for the full message reference.

All timestamps exchanged on the wire are in *server clock* seconds: the
server's ``time.monotonic()``. Headsets estimate their offset to that clock
with NTP-style ping/pong exchanges and schedule playback against it.
"""

import json
import re
import time

PROTOCOL_VERSION = 1
SERVICE_NAME = "syncvr"

DEFAULT_HTTP_PORT = 8080
DEFAULT_TCP_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766

MAX_LINE_BYTES = 1024 * 1024

PROJECTIONS = ("360", "180", "flat")
STEREO_MODES = ("mono", "tb", "sbs")

# Tuning for the headset-side sync engine. The server pushes these to every
# headset on connect and whenever they change, so the whole fleet can be
# retuned from the dashboard without rebuilding the headset app.
DEFAULT_SYNC_SETTINGS = {
    # How far in the future a synchronized start is scheduled. Must comfortably
    # exceed command delivery time plus the time a headset needs to load/seek.
    "play_lead_ms": 1500,
    # Same, for seeks while playing (headsets pause, seek, then restart together).
    "seek_lead_ms": 1500,
    # How far in the future a synchronized pause is scheduled.
    "pause_lead_ms": 300,
    # "rate": nudge playback speed to absorb small drift, hard-seek big drift.
    # "seek": only ever hard-seek (use if rate changes misbehave on a device).
    # "external": hand the target time to the video player's own clock sync, if
    #             it has one; experimental.
    "correction_mode": "rate",
    # Drift below this is ignored.
    "deadband_ms": 20,
    # Rate correction: rate = 1 - gain * drift_seconds, clamped to +/- max.
    "rate_gain": 0.8,
    "max_rate_adjust": 0.05,
    # Drift above this triggers a hard seek (in any mode).
    "hard_seek_ms": 300,
    # In "seek" mode, drift above this triggers a hard seek.
    "seek_mode_threshold_ms": 80,
    # Minimum time between two hard seeks.
    "seek_cooldown_ms": 3000,
    # Ignore drift measurements for this long after starting or seeking.
    "settle_ms": 750,
}

_SETTING_LIMITS = {
    "play_lead_ms": (200, 10000),
    "seek_lead_ms": (200, 10000),
    "pause_lead_ms": (0, 5000),
    "deadband_ms": (1, 1000),
    "rate_gain": (0.05, 5.0),
    "max_rate_adjust": (0.0, 0.25),
    "hard_seek_ms": (20, 10000),
    "seek_mode_threshold_ms": (10, 10000),
    "seek_cooldown_ms": (0, 60000),
    "settle_ms": (0, 10000),
}
CORRECTION_MODES = ("rate", "seek", "external")


def server_clock() -> float:
    """The clock every playback schedule is expressed in."""
    return time.monotonic() + _offset


_offset = 0.0
SAME_BOOT_TOLERANCE_S = 2.0


def set_clock_offset(offset: float) -> None:
    global _offset
    _offset = float(offset)


def clock_epoch() -> dict:
    """Snapshot to persist so a restarted server can continue the same server_clock timeline."""
    wall, mono = time.time(), time.monotonic()
    return {"offset": _offset, "wall_minus_mono": wall - mono, "saved_wall": wall,
            "server_now": mono + _offset}


def _num(d: dict, key: str):
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def rebase_clock(saved) -> None:
    """Restore the clock offset from a saved clock_epoch(); tolerates missing or garbage input."""
    if not isinstance(saved, dict):
        return
    offset, wmm = _num(saved, "offset"), _num(saved, "wall_minus_mono")
    if offset is not None and wmm is not None and abs((time.time() - time.monotonic()) - wmm) < SAME_BOOT_TOLERANCE_S:
        set_clock_offset(offset)
        return
    server_now, saved_wall = _num(saved, "server_now"), _num(saved, "saved_wall")
    if server_now is None or saved_wall is None:
        return
    set_clock_offset(server_now + (time.time() - saved_wall) - time.monotonic())


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict:
    msg = json.loads(line.decode("utf-8"))
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError("message must be a JSON object with a string 'type'")
    return msg


def validate_settings(changes: dict, base: dict = None) -> dict:
    """Return ``base`` updated with ``changes``, rejecting unknown or out-of-range values."""
    result = dict(base if base is not None else DEFAULT_SYNC_SETTINGS)
    for key, value in changes.items():
        if key not in DEFAULT_SYNC_SETTINGS:
            raise ValueError(f"unknown setting: {key}")
        if key == "correction_mode":
            if value not in CORRECTION_MODES:
                raise ValueError(f"correction_mode must be one of {CORRECTION_MODES}")
            result[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        lo, hi = _SETTING_LIMITS[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        result[key] = type(DEFAULT_SYNC_SETTINGS[key])(value)
    return result


_SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")


def is_safe_filename(name: str) -> bool:
    """Content names travel to headsets and become file names there."""
    return (
        isinstance(name, str)
        and bool(_SAFE_NAME.match(name))
        and name not in (".", "..")
        and not name.startswith(".")
        and not name.endswith(".part")
    )
