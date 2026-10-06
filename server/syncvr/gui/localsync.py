"""Decide what the operator's local player should do to follow a headset. Qt-free."""

import os
from typing import Optional

from ..protocol import is_safe_filename
from .format import position_of

SEEK_THRESHOLD_S = 0.2
SEEK_COOLDOWN_S = 1.5
_MODES = {"playing": "play", "paused": "pause"}


def plan(snapshot: Optional[dict], device_id: str, now: float, content_dir, player_pos: Optional[float]) -> dict:
    """Target for the local player. Keys: mode ('play'|'pause'|'stop'), target_pos, seek, and
    path (resolved file) or error. `now` is server time; `player_pos` the local position or None."""
    dev = next((d for d in (snapshot or {}).get("devices") or [] if d.get("id") == device_id), None)
    desired = (dev or {}).get("desired")
    mode = _MODES.get((desired or {}).get("mode"))
    if mode is None:
        return {"mode": "stop", "target_pos": None, "seek": False}
    target = position_of(desired, now)
    out = {"mode": mode, "target_pos": target, "seek": False}
    name = desired.get("video")
    if not is_safe_filename(name):
        out["error"] = "unsafe or missing file name: %r" % (name,)
        return out
    path = os.path.join(str(content_dir), name)
    if not os.path.isfile(path):
        out["error"] = "file not found: %s" % name
        return out
    out["path"] = path
    out["seek"] = player_pos is None or abs(player_pos - target) > SEEK_THRESHOLD_S
    return out


class LocalSync:
    """plan() plus a seek cooldown so a seek has time to land before the next one."""

    def __init__(self, threshold: float = SEEK_THRESHOLD_S, cooldown: float = SEEK_COOLDOWN_S):
        self.threshold, self.cooldown = threshold, cooldown
        self._last_seek: Optional[float] = None

    def reset(self) -> None:
        self._last_seek = None

    def step(self, snapshot, device_id: str, now: float, content_dir, player_pos: Optional[float]) -> dict:
        p = plan(snapshot, device_id, now, content_dir, player_pos)
        if p["mode"] == "stop":
            self.reset()
        if p["seek"]:
            drift = player_pos is not None and abs(player_pos - p["target_pos"]) <= self.threshold
            if drift or (self._last_seek is not None and 0 <= now - self._last_seek < self.cooldown
                         and player_pos is not None):
                p["seek"] = False
            else:
                self._last_seek = now
        return p
