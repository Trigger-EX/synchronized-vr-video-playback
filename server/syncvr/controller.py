"""Fleet state and the operator commands that drive it.

The controller keeps, for every headset it has ever seen, a *desired playback
state*: stopped, paused at a position, or playing from an anchor
``(media position, server time)``. Commands from the dashboard update the
desired state of the targeted headsets and push it to the ones that are
online. A headset that (re)connects later is sent its desired state and
rejoins the show in sync, because an anchor fully determines where playback
should be at any moment.
"""

import logging
import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import __version__
from .library import Library, Video
from .protocol import DEFAULT_SYNC_SETTINGS, server_clock, validate_settings

log = logging.getLogger(__name__)

# How long a download job may look idle on the headset before its slot is freed.
DOWNLOAD_IDLE_GRACE_S = 20.0


class CommandError(Exception):
    """A dashboard/API command that cannot be carried out as requested."""


@dataclass
class Device:
    device_id: str
    name: str = ""
    group: str = ""
    model: str = ""
    app_version: str = ""
    serial: str = ""
    ip: str = ""
    volume: float = 1.0
    online: bool = False
    last_seen: float = 0.0  # wall-clock, for display
    connected_at: float = 0.0  # wall-clock
    status: Dict[str, Any] = field(default_factory=dict)
    inventory: Dict[str, int] = field(default_factory=dict)
    desired: Optional[Dict[str, Any]] = None
    conn: Any = None

    @property
    def label(self) -> str:
        return self.name or self.serial or self.device_id

    def saved(self) -> dict:
        return {"name": self.name, "group": self.group, "volume": self.volume, "serial": self.serial,
                "model": self.model, "last_seen": self.last_seen}

    def to_json(self) -> dict:
        return {
            "id": self.device_id,
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "model": self.model,
            "app_version": self.app_version,
            "serial": self.serial,
            "ip": self.ip,
            "volume": self.volume,
            "online": self.online,
            "last_seen": self.last_seen,
            "connected_at": self.connected_at,
            "status": self.status,
            "inventory": self.inventory,
            "desired": self.desired,
        }


def position_at(desired: Optional[dict], t: float) -> float:
    """Media position a desired state implies at server time ``t``."""
    if not desired or desired.get("mode") not in ("playing", "paused"):
        return 0.0
    pos = float(desired.get("pos", 0.0))
    if desired["mode"] == "playing":
        pos += max(0.0, t - desired["at"])
        duration = desired.get("duration")
        if duration:
            pos = pos % duration if desired.get("loop") else min(pos, duration)
    return pos


class Distributor:
    """Pushes content to headsets, limiting how many download at once so the
    Wi-Fi network is not saturated by dozens of simultaneous multi-GB transfers."""

    def __init__(self, controller: "Controller", max_concurrent: int = 4):
        self.controller = controller
        self.max_concurrent = max_concurrent
        self.queue: "OrderedDict[str, dict]" = OrderedDict()
        self.active: Dict[str, dict] = {}

    def request(self, device_id: str, names: List[str], delete_others: bool) -> None:
        self.queue[device_id] = {"files": list(names), "delete_others": delete_others}
        self.pump()

    def pump(self) -> None:
        for device_id in list(self.queue):
            if self.max_concurrent and len(self.active) >= self.max_concurrent:
                break
            dev = self.controller.devices.get(device_id)
            if dev is None:
                del self.queue[device_id]
                continue
            if not dev.online or device_id in self.active:
                continue
            job = self.queue.pop(device_id)
            videos = [v for v in (self.controller.library.get(n) for n in job["files"]) if v]
            missing = [v for v in videos if dev.inventory.get(v.name) != v.size]
            wanted = {v.name for v in videos}
            extras = job["delete_others"] and any(n not in wanted for n in dev.inventory)
            if not missing and not extras:
                continue
            job["started"] = time.monotonic()
            job["bytes"] = sum(v.size for v in missing)
            self.active[device_id] = job
            self.controller.send(dev, {
                "type": "sync_content",
                "files": [{"name": v.name, "size": v.size, "url": dev.conn.content_url(v.name)} for v in videos],
                "delete_others": job["delete_others"],
            })
            self.controller.log_event("info", f"sending {len(missing)} file(s) to {dev.label}", dev)
        self.controller.changed()

    def finished(self, device_id: str, msg: dict) -> None:
        if self.active.pop(device_id, None) is not None:
            dev = self.controller.devices.get(device_id)
            failed = msg.get("failed") or []
            if failed:
                self.controller.log_event("error", f"download failed on {dev.label}: {', '.join(failed)}", dev)
            else:
                self.controller.log_event("info", f"content up to date on {dev.label}", dev)
        self.pump()

    def check_idle(self, device_id: str, status: dict) -> None:
        job = self.active.get(device_id)
        if job and not status.get("download") and time.monotonic() - job["started"] > DOWNLOAD_IDLE_GRACE_S:
            self.finished(device_id, {})

    def disconnected(self, device_id: str) -> None:
        job = self.active.pop(device_id, None)
        if job is not None:
            # Retry first when the headset comes back.
            self.queue[device_id] = job
            self.queue.move_to_end(device_id, last=False)
        self.pump()

    def cancel(self, device_ids: Iterable[str]) -> None:
        for device_id in device_ids:
            self.queue.pop(device_id, None)
            if self.active.pop(device_id, None) is not None:
                dev = self.controller.devices.get(device_id)
                if dev and dev.online:
                    self.controller.send(dev, {"type": "cancel_downloads"})
        self.pump()

    def to_json(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "queued": list(self.queue),
            "active": list(self.active),
        }


class Controller:
    def __init__(self, library: Library, server_name: str = "SyncVR", max_downloads: int = 4):
        self.library = library
        self.server_name = server_name
        self.settings = dict(DEFAULT_SYNC_SETTINGS)
        self.devices: Dict[str, Device] = {}
        self.distributor = Distributor(self, max_downloads)
        self.events: deque = deque(maxlen=300)
        self._listeners: List[Callable[[], None]] = []
        self.dirty = False
        self.server_info: Dict[str, Any] = {}

    # ------------------------------------------------------------------ state

    def load(self, data: dict) -> None:
        self.settings = validate_settings(data.get("settings", {}), DEFAULT_SYNC_SETTINGS)
        for device_id, saved in data.get("devices", {}).items():
            dev = Device(device_id=device_id)
            for key in ("name", "group", "serial", "model"):
                setattr(dev, key, str(saved.get(key, "")))
            dev.volume = float(saved.get("volume", 1.0))
            dev.last_seen = float(saved.get("last_seen", 0.0))
            self.devices[device_id] = dev
        self.library.metadata.update(data.get("videos", {}))
        if "max_downloads" in data:
            self.distributor.max_concurrent = max(0, int(data["max_downloads"]))

    def dump(self) -> dict:
        return {
            "settings": self.settings,
            "max_downloads": self.distributor.max_concurrent,
            "devices": {d.device_id: d.saved() for d in self.devices.values()},
            "videos": self.library.metadata,
        }

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def changed(self, persist: bool = False) -> None:
        if persist:
            self.dirty = True
        for fn in self._listeners:
            fn()

    def log_event(self, level: str, message: str, dev: Optional[Device] = None) -> None:
        entry = {"t": time.time(), "level": level, "message": message}
        if dev is not None:
            entry["device"] = dev.device_id
        self.events.append(entry)
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(message)
        self.changed()

    def snapshot(self) -> dict:
        return {
            "server": dict(self.server_info, name=self.server_name, version=__version__, time=server_clock()),
            "settings": self.settings,
            "devices": [d.to_json() for d in self.devices.values()],
            "library": self.library.to_json(),
            "downloads": self.distributor.to_json(),
            "events": list(self.events)[-150:],
        }

    # ------------------------------------------------------ headset lifecycle

    def send(self, dev: Device, msg: dict) -> None:
        if dev.online and dev.conn is not None:
            dev.conn.send(msg)

    def headset_connected(self, hello: dict, conn) -> Device:
        device_id = str(hello["device_id"])
        dev = self.devices.get(device_id)
        if dev is None:
            dev = Device(device_id=device_id)
            self.devices[device_id] = dev
            self.dirty = True
        if dev.conn is not None and dev.conn is not conn:
            dev.conn.close()
        dev.conn = conn
        dev.online = True
        dev.ip = conn.remote_ip
        dev.model = str(hello.get("model", dev.model))
        dev.app_version = str(hello.get("app_version", ""))
        dev.serial = str(hello.get("serial", dev.serial))
        dev.connected_at = dev.last_seen = time.time()
        dev.status = {}
        self.send(dev, {
            "type": "welcome",
            "server_name": self.server_name,
            "device_name": dev.label,
            "group": dev.group,
            "http_port": conn.http_port,
            "server_time": server_clock(),
            "settings": self.settings,
        })
        self.send(dev, {"type": "volume", "value": dev.volume})
        self._send_desired(dev)
        self.log_event("info", f"{dev.label} connected from {dev.ip}", dev)
        self.distributor.pump()
        return dev

    def headset_disconnected(self, dev: Device, conn) -> None:
        if dev.conn is not conn:
            return  # superseded by a newer connection
        dev.conn = None
        dev.online = False
        self.dirty = True  # remember when it was last seen
        self.distributor.disconnected(dev.device_id)
        self.log_event("warn", f"{dev.label} disconnected", dev)

    def headset_message(self, dev: Device, msg: dict) -> None:
        dev.last_seen = time.time()
        kind = msg["type"]
        if kind == "status":
            msg.pop("type")
            dev.status = msg
            self.distributor.check_idle(dev.device_id, msg)
            self.changed()
        elif kind == "inventory":
            files = msg.get("files") or []
            dev.inventory = {str(f["name"]): int(f.get("size", 0)) for f in files if isinstance(f, dict) and "name" in f}
            self.changed()
        elif kind == "downloads_finished":
            if not msg.get("cancelled"):  # a cancelled job's slot was already freed
                self.distributor.finished(dev.device_id, msg)
        elif kind == "event":
            level = msg.get("level", "info")
            self.log_event(level if level in ("info", "warn", "error") else "info",
                           f"{dev.label}: {msg.get('message', '')}", dev)
        else:
            log.debug("ignoring %s from %s", kind, dev.label)

    # --------------------------------------------------------------- commands

    def resolve_targets(self, spec, online_only: bool = False) -> List[Device]:
        if spec in (None, "all"):
            devices = list(self.devices.values())
        elif spec == "online":
            devices = [d for d in self.devices.values() if d.online]
        elif isinstance(spec, dict) and "group" in spec:
            devices = [d for d in self.devices.values() if d.group == spec["group"]]
        elif isinstance(spec, list):
            unknown = [i for i in spec if i not in self.devices]
            if unknown:
                raise CommandError(f"unknown headset(s): {', '.join(map(str, unknown))}")
            devices = [self.devices[i] for i in spec]
        else:
            raise CommandError("targets must be 'all', 'online', a list of headset ids or {'group': name}")
        if online_only:
            devices = [d for d in devices if d.online]
        return devices

    def execute(self, action: str, params: dict) -> dict:
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            raise CommandError(f"unknown action: {action}")
        targets = self.resolve_targets(params.get("targets"))
        result = handler(targets, params) or {}
        self.changed()
        result.setdefault("targets", len(targets))
        return result

    def _video(self, name) -> Video:
        video = self.library.get(name)
        if video is None:
            raise CommandError(f"no such video: {name}")
        return video

    def _send_desired(self, dev: Device, at: Optional[float] = None) -> None:
        d = dev.desired
        if d is None:
            return
        mode = d["mode"]
        if mode == "stopped":
            self.send(dev, {"type": "stop"})
            return
        msg = {k: d[k] for k in ("video", "projection", "stereo", "rotation", "duration", "pos")}
        if mode == "playing":
            msg.update(type="play", at=d["at"], loop=d.get("loop", False))
        else:
            msg.update(type="pause", at=at if at is not None else server_clock())
        self.send(dev, msg)

    def _desired_for(self, video: Video, mode: str, pos: float, at: Optional[float] = None,
                     loop: bool = False) -> dict:
        d = dict(video.playback_fields(), mode=mode, pos=round(float(pos), 4))
        if mode == "playing":
            d.update(at=at, loop=loop)
        return d

    def _clamp(self, video: Video, pos: float) -> float:
        pos = max(0.0, float(pos))
        if video.duration:
            pos = min(pos, max(0.0, video.duration - 0.1))
        return pos

    def _act_load(self, targets, params):
        video = self._video(params.get("video"))
        pos = self._clamp(video, params.get("pos", 0.0))
        for dev in targets:
            dev.desired = self._desired_for(video, "paused", pos)
            self._send_desired(dev)

    def _act_play(self, targets, params):
        now = server_clock()
        lead = self.settings["play_lead_ms"] / 1000.0
        # Headsets with nothing loaded join whatever most of the fleet is playing.
        running = Counter(d.desired["video"] for d in self.devices.values()
                          if d.online and d.desired and d.desired["mode"] == "playing")
        fallback = running.most_common(1)[0][0] if running else None
        by_video: Dict[str, List[Device]] = defaultdict(list)
        for dev in targets:
            name = params.get("video") or (dev.desired or {}).get("video") or fallback
            if name:
                by_video[name].append(dev)
        if not by_video:
            raise CommandError("choose a video to play")
        for name, devs in by_video.items():
            video = self._video(name)
            loop = bool(params.get("loop", video.loop))
            if params.get("pos") is not None:
                pos, at = self._clamp(video, params["pos"]), now + lead
            else:
                pos, at, loop = self._join_point(devs, video, now + lead, loop)
            desired = self._desired_for(video, "playing", pos, at, loop)
            for dev in devs:
                dev.desired = dict(desired)
                self._send_desired(dev)
        return {"at": now + lead}

    def _join_point(self, devs: List[Device], video: Video, start_at: float, loop: bool):
        """Where a play command without an explicit position should start:
        join a cohort already playing this video (preferring one among the
        targets), else resume where the targets were paused, else start over."""
        def cohorts(pool):
            return Counter(
                (d.desired["pos"], d.desired["at"], d.desired.get("loop", False))
                for d in pool
                if d.desired and d.desired["mode"] == "playing" and d.desired["video"] == video.name
            )
        playing = cohorts(devs) or cohorts(d for d in self.devices.values() if d.online)
        if playing:
            return playing.most_common(1)[0][0]
        paused = Counter(
            d.desired["pos"] for d in devs
            if d.desired and d.desired["mode"] == "paused" and d.desired["video"] == video.name
        )
        if paused:
            return paused.most_common(1)[0][0], start_at, loop
        return 0.0, start_at, loop

    def _act_pause(self, targets, params):
        at = server_clock() + self.settings["pause_lead_ms"] / 1000.0
        for dev in targets:
            d = dev.desired
            if d and d["mode"] == "playing":
                dev.desired = dict(d, mode="paused", pos=round(position_at(d, at), 4))
                dev.desired.pop("at", None)
                dev.desired.pop("loop", None)
                self._send_desired(dev, at=at)

    def _act_seek(self, targets, params):
        if params.get("pos") is None and params.get("delta") is None:
            raise CommandError("seek needs 'pos' or 'delta'")
        now = server_clock()
        at = now + self.settings["seek_lead_ms"] / 1000.0
        for dev in targets:
            d = dev.desired
            if not d or d["mode"] not in ("playing", "paused"):
                continue
            video = self.library.get(d["video"])
            if video is None:
                continue
            if params.get("pos") is not None:
                new = float(params["pos"])
            else:
                new = position_at(d, now) + float(params["delta"])
            new = self._clamp(video, new)
            if d["mode"] == "playing":
                dev.desired = self._desired_for(video, "playing", new, at, d.get("loop", False))
            else:
                dev.desired = self._desired_for(video, "paused", new)
            self._send_desired(dev)

    def _act_stop(self, targets, params):
        for dev in targets:
            dev.desired = {"mode": "stopped"}
            self._send_desired(dev)

    def _act_resync(self, targets, params):
        for dev in targets:
            self._send_desired(dev)

    def _act_volume(self, targets, params):
        try:
            value = min(1.0, max(0.0, float(params["value"])))
        except (KeyError, TypeError, ValueError):
            raise CommandError("volume needs a 'value' between 0 and 1")
        for dev in targets:
            dev.volume = value
            self.send(dev, {"type": "volume", "value": value})
        self.dirty = True

    def _act_recenter(self, targets, params):
        for dev in targets:
            self.send(dev, {"type": "recenter"})

    def _act_message(self, targets, params):
        text = str(params.get("text", ""))[:500]
        seconds = min(600.0, max(0.0, float(params.get("seconds", 5))))
        for dev in targets:
            self.send(dev, {"type": "message", "text": text, "seconds": seconds})

    def _act_identify(self, targets, params):
        for dev in targets:
            self.send(dev, {"type": "identify", "name": dev.label, "seconds": 8})

    def _act_sync_content(self, targets, params):
        names = params.get("videos", "all")
        if names == "all":
            names = list(self.library.videos)
        for name in names:
            self._video(name)
        for dev in targets:
            self.distributor.request(dev.device_id, names, bool(params.get("delete_others", False)))
        return {"queued": len(targets)}

    def _act_delete_content(self, targets, params):
        names = params.get("videos") or []
        if not isinstance(names, list) or not names:
            raise CommandError("delete_content needs a list of 'videos'")
        for dev in targets:
            self.send(dev, {"type": "delete_content", "names": names})

    def _act_cancel_downloads(self, targets, params):
        self.distributor.cancel(d.device_id for d in targets)

    # ------------------------------------------------------- admin endpoints

    def update_device(self, device_id: str, changes: dict) -> Device:
        dev = self.devices.get(device_id)
        if dev is None:
            raise CommandError(f"unknown headset: {device_id}")
        for key in ("name", "group"):
            if key in changes:
                setattr(dev, key, str(changes[key]).strip()[:64])
        self.send(dev, {"type": "device_info", "device_name": dev.label, "group": dev.group})
        self.changed(persist=True)
        return dev

    def forget_device(self, device_id: str) -> None:
        dev = self.devices.get(device_id)
        if dev is None:
            raise CommandError(f"unknown headset: {device_id}")
        if dev.online:
            raise CommandError("cannot forget a connected headset")
        del self.devices[device_id]
        self.distributor.cancel([device_id])
        self.changed(persist=True)

    def update_settings(self, changes: dict) -> dict:
        try:
            self.settings = validate_settings(changes, self.settings)
        except ValueError as exc:
            raise CommandError(str(exc))
        for dev in self.devices.values():
            self.send(dev, {"type": "settings", "settings": self.settings})
        self.changed(persist=True)
        return self.settings

    def update_video(self, name: str, changes: dict) -> Video:
        try:
            video = self.library.update_meta(name, changes)
        except KeyError:
            raise CommandError(f"no such video: {name}")
        except ValueError as exc:
            raise CommandError(str(exc))
        self.changed(persist=True)
        return video

    def set_max_downloads(self, value: int) -> None:
        self.distributor.max_concurrent = max(0, int(value))
        self.dirty = True
        self.distributor.pump()

    def rescan(self) -> bool:
        changed = self.library.scan()
        if changed:
            self.log_event("info", f"library: {len(self.library.videos)} video(s)")
        self.changed()
        return changed
