"""Fleet state and the operator commands that drive it.

The controller keeps, for every headset it has ever seen, a *desired playback
state*: stopped, paused at a position, or playing from an anchor
``(media position, server time)``. Commands from the dashboard update the
desired state of the targeted headsets and push it to the ones that are
online. A headset that (re)connects later is sent its desired state and
rejoins the show in sync, because an anchor fully determines where playback
should be at any moment.
"""

import asyncio
import hashlib
import json
import logging
import statistics
import time
import uuid
from collections import Counter, OrderedDict, defaultdict, deque, namedtuple
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from pathlib import Path

from . import __version__, automation, features, watchdogs
from .fleetops import ADB_PORT, MIN_ASLEEP_S, AdbFleet, scan_candidates
from .library import Library, Video
from .protocol import DEFAULT_SYNC_SETTINGS, server_clock, validate_settings

log = logging.getLogger(__name__)

# How long a headset may go without reporting a download before its job stops counting
# toward the concurrent-download limit (the job itself is kept until the headset says it is done).
DOWNLOAD_IDLE_GRACE_S = 20.0

# Pose streaming: the headset stops by itself 15 s after the last pose_stream, so renew well before.
POSE_STREAM_HZ = 10
POSE_RENEW_S = 10.0

# Crash recovery: restored desired states older than this are dropped; after a restart the
# controller waits RECOVERY_WINDOW_S for headsets to report what they are playing.
STALE_DESIRED_S = 30 * 60.0
RECOVERY_WINDOW_S = 20.0
RECOVERY_STATUS_WAIT_S = 3.0
RECOVERY_GROUP_S = 0.25
ANCHOR_EQUAL_S = 1e-3
ANCHOR_SANITY_S = 2.0
PLAYBACK_ACTIONS = ("load", "play", "pause", "seek", "stop")
MAX_JOBS_KEPT = 20
PREVIEW_NAMES_SHOWN = 8
POWER_ACTIONS = frozenset({"sleep", "wake", "screen_refresh", "poweroff"})
ADDRESS_ACTIONS = frozenset({"connect", "purge"})  # act on saved ip:5555 addresses, not on `adb devices` serials
ADB_ACTIONS = POWER_ACTIONS | ADDRESS_ACTIONS | {"snapshot"}  # run through adb jobs; callers pre-fetch `adb devices` off the loop


Addr = namedtuple("Addr", "device_id")  # a job item keyed by adb address (no known headset behind it)

BW_DEFAULT_MB, BW_MAX_MB = 20, 200
BW_DEFAULT_S, BW_MIN_S, BW_MAX_S = 30, 5, 120
BW_MAX_PARALLEL = 8
BW_MIN_VIDEO_BYTES = 1024 * 1024
BW_GRACE_S = 5.0  # extra wait for the headset's reply before its entry fails
BW_TIMEOUT_MSG = "timed out (player may not support bandwidth_test)"


class CommandError(Exception):
    """A dashboard/API command that cannot be carried out as requested."""


@dataclass
class Device:
    device_id: str
    name: str = ""
    group: str = ""
    model: str = ""
    app_version: str = ""
    player: str = ""  # which app the headset runs: "native", "sim", ... ("" = unknown)
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
    pose: Optional[Dict[str, float]] = None  # latest {yaw,pitch,roll,t}; deliberately not in to_json
    bandwidth: Optional[Dict[str, Any]] = None  # last bandwidth test result; shown, never persisted

    @property
    def label(self) -> str:
        return self.name or self.serial or self.device_id

    def saved(self) -> dict:
        return {"name": self.name, "group": self.group, "volume": self.volume, "serial": self.serial,
                "ip": self.ip, "model": self.model, "last_seen": self.last_seen, "desired": self.desired}

    def to_json(self) -> dict:
        return {
            "id": self.device_id,
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "model": self.model,
            "app_version": self.app_version,
            "player": self.player,
            "serial": self.serial,
            "ip": self.ip,
            "volume": self.volume,
            "online": self.online,
            "last_seen": self.last_seen,
            "connected_at": self.connected_at,
            "status": self.status,
            "inventory": self.inventory,
            "desired": self.desired,
            "bandwidth": self.bandwidth,
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
        # Jobs whose headset stopped reporting progress: they no longer hold a download slot,
        # but are kept (and persisted) until the headset reports the end of the job. A headset
        # may be hashing existing files, or still be busy with a job from before a server restart.
        self.idle: Dict[str, dict] = {}

    def request(self, device_id: str, names: List[str], delete_others: bool) -> None:
        self.queue[device_id] = {"files": list(names), "delete_others": delete_others}
        self.idle.pop(device_id, None)
        self.controller.dirty = True
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
            job["id"] = uuid.uuid4().hex[:8]
            job["started"] = job["last_progress"] = time.monotonic()
            job["bytes"] = sum(v.size for v in missing)
            self.active[device_id] = job
            self.controller.send(dev, {
                "type": "sync_content",
                "files": [self._file_entry(dev, v) for v in videos],
                "delete_others": job["delete_others"],
                "job": job["id"],
            })
            self.controller.log_event("info", f"sending {len(missing)} file(s) to {dev.label}", dev)
        self.controller.changed()

    def _file_entry(self, dev: "Device", video: Video) -> dict:
        entry = {"name": video.name, "size": video.size, "url": dev.conn.content_url(video.name)}
        sha256 = self.controller.library.sha256_of(video.name)
        if sha256:  # omitted until the background checksum has finished
            entry["sha256"] = sha256
        return entry

    def finished(self, device_id: str, msg: dict) -> None:
        self.controller.dirty = True
        current = self.active.get(device_id) or self.idle.get(device_id)
        if current is not None and msg.get("job") not in (None, current["id"]):
            return  # the end of an earlier job (e.g. one started before a server restart)
        self.idle.pop(device_id, None)
        self.active.pop(device_id, None)
        if current is not None:
            dev = self.controller.devices.get(device_id)
            failed = msg.get("failed") or []
            if failed:
                self.controller.log_event("error", f"download failed on {dev.label}: {', '.join(failed)}", dev)
            else:
                self.controller.log_event("info", f"content up to date on {dev.label}", dev)
        self.pump()

    def check_idle(self, device_id: str, status: dict) -> None:
        job = self.active.get(device_id)
        if not job:
            return
        now = time.monotonic()
        if status.get("download"):
            job["last_progress"] = now
        elif now - job["last_progress"] > DOWNLOAD_IDLE_GRACE_S:
            # Free the slot but keep the job: only the headset can say it is finished.
            del self.active[device_id]
            self.idle[device_id] = job
            self.pump()

    def disconnected(self, device_id: str) -> None:
        self.controller.dirty = True
        job = self.active.pop(device_id, None) or self.idle.pop(device_id, None)
        if job is not None:
            # Retry first when the headset comes back.
            self.queue[device_id] = job
            self.queue.move_to_end(device_id, last=False)
        self.pump()

    def cancel(self, device_ids: Iterable[str]) -> None:
        self.controller.dirty = True
        for device_id in device_ids:
            self.queue.pop(device_id, None)
            if (self.active.pop(device_id, None) or self.idle.pop(device_id, None)) is not None:
                dev = self.controller.devices.get(device_id)
                if dev and dev.online:
                    self.controller.send(dev, {"type": "cancel_downloads"})
        self.pump()

    def pending_json(self) -> dict:
        """Jobs to resume after a server restart (active ones first), without progress."""
        jobs = dict(self.active)
        for device_id, job in self.idle.items():
            jobs.setdefault(device_id, job)
        for device_id, job in self.queue.items():
            jobs.setdefault(device_id, job)
        return {i: {"files": job["files"], "delete_others": job["delete_others"]} for i, job in jobs.items()}

    def to_json(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "queued": list(self.queue),
            "active": list(self.active),
            "idle": list(self.idle),
        }


class Controller:
    def __init__(self, library: Library, server_name: str = "SyncVR", max_downloads: int = 4,
                 fleet: Optional[AdbFleet] = None):
        self.library = library
        self.fleet = fleet or AdbFleet()
        self.tested_features: set = set()
        self.show_mode = False  # manual only; persisted; brakes automation (see automation.py)
        self.data_dir: Optional[Path] = None  # where the CLI push lock lives; set by the app
        # adb actions (see start_job): action -> feature that must be tested, and the actions that need
        # a confirmation token from preview(). Filled in by the action implementations.
        self.gated_actions: Dict[str, str] = {}
        self.confirm_actions: set = set()
        self.action_labels: Dict[str, str] = {}
        self.preview_warnings: Dict[str, Callable[[List[Device], dict], List[str]]] = {}
        self._job_failed_cbs: Dict[str, Callable[[str], None]] = {}
        # headsets an operator put to sleep on purpose; stay-awake style watchdogs must leave them alone
        self.intentionally_asleep: set = set()
        self.watchdogs = watchdogs.Watchdogs(self)
        self.gated_actions["poweroff"] = "power.poweroff"
        self.confirm_actions.update(POWER_ACTIONS)
        self.adb_actions = set(ADB_ACTIONS)
        self.gated_actions["snapshot"] = "debug.snapshot"  # read-only: gated but needs no confirm token
        self.action_labels.update({"sleep": "Sleep", "wake": "Wake", "screen_refresh": "Screen refresh",
                                   "poweroff": "Power off", "snapshot": "Snapshot"})
        self.preview_warnings["screen_refresh"] = self._refresh_warnings
        # actions whose token covers something other than `adb devices` serials: (targets) -> [(item, address)]
        self.token_pairs: Dict[str, Callable[[List[Device]], List[tuple]]] = {
            "connect": self._address_pairs, "purge": self._address_pairs}
        self.gated_actions["purge"] = "adb.purge"
        self.confirm_actions.add("purge")
        self.gated_actions["bandwidth_test"] = "debug.bandwidth"
        self.action_labels.update({"connect": "Connect", "purge": "Purge adb connections of",
                                   "bandwidth_test": "Bandwidth test"})
        self.preview_warnings["purge"] = self._purge_warnings
        self._bw_pending: Dict[str, str] = {}  # device id -> job id of the test it is running
        self._bw_timers: Dict[str, Any] = {}
        self._bw_runs: Dict[str, dict] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None  # where job results are marshalled to
        self.jobs: "OrderedDict[str, dict]" = OrderedDict()
        self.server_name = server_name
        self.settings = dict(DEFAULT_SYNC_SETTINGS)
        self.devices: Dict[str, Device] = {}
        self.distributor = Distributor(self, max_downloads)
        self.events: deque = deque(maxlen=300)
        self._listeners: List[Callable[[], None]] = []
        self.dirty = False
        self.server_info: Dict[str, Any] = {}
        self.following: Optional[str] = None
        self._renew_handle = None
        # crash recovery (see _reconcile)
        self.recovering_until = 0.0
        self._rec_active = False
        self._held: Dict[str, float] = {}  # online headsets waiting for their first synced status
        self._members: set = set()  # headsets reconciled into the recovered show
        self._cancelled: set = set()  # headsets an operator command took over during recovery
        self._status_t: Dict[str, float] = {}
        self._raw: Dict[str, dict] = {}  # what each headset reported, before group snapping
        self._rec_result: Optional[dict] = None
        self._rec_timer = None

    # ------------------------------------------------------------------ state

    def load(self, data: dict) -> None:
        self.settings = validate_settings(data.get("settings", {}), DEFAULT_SYNC_SETTINGS)
        try:
            fresh = time.time() - float(data.get("saved_wall", 0.0)) <= STALE_DESIRED_S
        except (TypeError, ValueError):
            fresh = False
        for device_id, saved in data.get("devices", {}).items():
            dev = Device(device_id=device_id)
            for key in ("name", "group", "serial", "model", "ip"):
                setattr(dev, key, str(saved.get(key, "")))
            dev.volume = float(saved.get("volume", 1.0))
            dev.last_seen = float(saved.get("last_seen", 0.0))
            if fresh and isinstance(saved.get("desired"), dict) and saved["desired"].get("mode"):
                dev.desired = dict(saved["desired"])
            self.devices[device_id] = dev
        self.library.metadata.update(data.get("videos", {}))
        saved_features = data.get("tested_features", [])
        self.tested_features = features.normalize(saved_features if isinstance(saved_features, list) else [])
        self.show_mode = data.get("show_mode") is True
        asleep = data.get("intentionally_asleep")
        self.intentionally_asleep = {i for i in asleep if isinstance(i, str) and i in self.devices} \
            if isinstance(asleep, list) else set()
        self.watchdogs.load(data.get("watchdogs"))  # enabled + cfg only; never armed
        if "max_downloads" in data:
            self.distributor.max_concurrent = max(0, int(data["max_downloads"]))
        for device_id, job in data.get("downloads", {}).items():
            if device_id in self.devices:
                self.distributor.queue[device_id] = {"files": [str(n) for n in job.get("files", [])],
                                                     "delete_others": bool(job.get("delete_others", False))}
        if data.get("devices"):  # a restart of a populated server: headsets may be mid show
            self.recovering_until = server_clock() + RECOVERY_WINDOW_S
            self._rec_active = True
            self._rec_result = None

    def any_active(self) -> bool:
        """True while any headset is meant to be playing or paused (drives the periodic save)."""
        return any(d.desired and d.desired.get("mode") in ("playing", "paused") for d in self.devices.values())

    def dump(self) -> dict:
        return {
            "saved_wall": time.time(),
            "settings": self.settings,
            "max_downloads": self.distributor.max_concurrent,
            "devices": {d.device_id: d.saved() for d in self.devices.values()},
            "videos": self.library.metadata,
            "downloads": self.distributor.pending_json(),
            "tested_features": sorted(self.tested_features),
            "show_mode": self.show_mode,
            "intentionally_asleep": sorted(i for i in self.intentionally_asleep if i in self.devices),
            "watchdogs": self.watchdogs.dump(),
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
        self._recovery_poll()
        return {
            "server": dict(self.server_info, name=self.server_name, version=__version__, time=server_clock(),
                           recovery=self._recovery_json()),
            "settings": self.settings,
            "devices": [d.to_json() for d in self.devices.values()],
            "library": self.library.to_json(),
            "downloads": self.distributor.to_json(),
            "events": list(self.events)[-150:],
            "features": features.describe(self.tested_features),
            "jobs": [dict(j, results=list(j["results"])) for j in self.jobs.values()],
            "asleep": sorted(i for i in self.intentionally_asleep if i in self.devices),
            "brake": self.brake_json(),
            "watchdogs": self.watchdogs.to_json(),
        }

    # ----------------------------------------------------------------- brakes

    def brake(self) -> Optional[str]:
        """Why automation must not act right now, or None. Manual commands ignore this."""
        return automation.brake_reason(self.show_mode, bool(self.distributor.active), self.data_dir)

    def brake_json(self) -> dict:
        reason = self.brake()
        return {"active": reason is not None, "reason": reason, "show_mode": self.show_mode}

    def set_show_mode(self, enabled) -> None:
        if not isinstance(enabled, bool):
            raise CommandError("enabled must be true or false")
        if enabled != self.show_mode:
            self.show_mode = enabled
            self.log_event("info", "Show Mode on: automation paused" if enabled else "Show Mode off")
        self.changed(persist=True)

    # -------------------------------------------------------------- watchdogs

    def set_watchdog(self, name: str, body: dict, tested_pattern: bool = False) -> dict:
        try:
            result = self.watchdogs.set(name, body, tested_pattern=tested_pattern)
        except watchdogs.WatchdogError as exc:
            raise CommandError(str(exc))
        self.changed(persist=True)
        return result

    def _snapshot_folder(self, folder) -> Path:
        """A diagnostic snapshot folder, which must live inside <data_dir>/snapshots."""
        if not self.data_dir:
            raise CommandError("no data directory is configured")
        if not isinstance(folder, str) or not folder:
            raise CommandError("folder must be the path of a snapshot folder")
        root = (Path(self.data_dir) / "snapshots").resolve()
        path = Path(folder)
        path = (path if path.is_absolute() else root / path).resolve()
        if path != root and root not in path.parents:
            raise CommandError(f"the folder must be inside {root}")
        return path

    def watchdog_test_pattern(self, name: str, body: dict) -> dict:
        """Test the overheat pattern against a snapshot folder; with ``confirm: true`` also confirm it.

        Confirming needs a valid pattern with at least one match, and sets ``pattern_confirmed`` to its hash.
        """
        if name != "overheat":
            raise CommandError(f"{name} has no pattern to test")
        if not isinstance(body, dict):
            raise CommandError("request body must be a JSON object")
        st = self.watchdogs.states["overheat"]
        pattern = body.get("pattern", st.cfg.get("pattern", ""))
        if not isinstance(pattern, str):
            raise CommandError("pattern must be text")
        result = self.watchdogs.confirm_pattern(pattern, self._snapshot_folder(body.get("folder")))
        result["confirmed"] = False
        if body.get("confirm") is True:
            if result["error"]:
                raise CommandError(f"cannot confirm: {result['error']}")
            by_file = result.get("by_file", {})
            missing = [f for f in watchdogs.PATTERN_FILES if not by_file.get(f)]
            if missing:  # the live rule needs the prompt in the logcat AND in the window list
                raise CommandError("cannot confirm: the pattern must match in both logcat.txt and window.txt; "
                                   f"no match in {', '.join(missing)}")
            self.set_watchdog("overheat", {"cfg": {"pattern": pattern, "pattern_confirmed": result["hash"]}},
                              tested_pattern=True)
            result["confirmed"] = True
            self.log_event("info", f"overheat pattern confirmed ({result['total']} matches in the snapshot)")
        return result

    # ------------------------------------------------------------------- jobs

    def open_job(self, action: str, ids: Iterable[str]) -> dict:
        """Register a running job over ``ids`` (its total) and pin the event loop results are marshalled to."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self.loop
        if loop is None:
            raise CommandError("the server is not running")
        self.loop = loop
        job_id = uuid.uuid4().hex[:8]
        job = {"id": job_id, "action": action, "state": "running", "total": len(list(ids)),
               "done": 0, "failed": 0, "started": time.time(), "results": []}
        self.jobs[job_id] = job
        while len(self.jobs) > MAX_JOBS_KEPT:
            old = self.jobs.popitem(last=False)[0]
            self._job_failed_cbs.pop(old, None)
            self._bw_runs.pop(old, None)
        return job

    def start_job(self, action: str, items: List[tuple], fn: Callable[[str], Any],
                  on_failed: Optional[Callable[[str], None]] = None,
                  notes: Optional[Dict[str, str]] = None) -> dict:
        """Run ``fn(adb_serial)`` for each ``(item, serial)`` on the shared executor.

        ``item`` is a device or anything with a ``device_id`` (an :data:`Addr` for jobs keyed by address). An item
        whose serial is None is reported as unreachable without running ``fn``, or with ``notes[device_id]``.
        ``on_failed(device_id)`` runs on the loop thread for each headset that failed or was unreachable.

        Returns ``{"job": id}`` at once. Each result is marshalled back to the event loop thread with
        call_soon_threadsafe, where it becomes one OK/FAILED log line; workers never touch controller state.
        """
        job = self.open_job(action, [dev.device_id for dev, _ in items])
        if on_failed is not None:
            self._job_failed_cbs[job["id"]] = on_failed
        for dev, serial in items:
            self._submit(job["id"], dev.device_id, serial, fn, (notes or {}).get(dev.device_id))
        self.changed()
        return {"job": job["id"]}

    def _submit(self, job_id: str, dev_id: str, serial: Optional[str], fn: Callable[[str], Any],
                note: Optional[str] = None) -> None:
        loop = self.loop

        def work() -> None:
            if serial is None:
                ok, message = False, note or "unreachable (not reachable by adb)"
            else:
                ok, message = self._run_one(fn, serial)
            try:
                loop.call_soon_threadsafe(self._job_result, job_id, dev_id, ok, message)
            except RuntimeError:
                pass  # loop closed: the server is shutting down

        self.fleet.submit(work)

    @staticmethod
    def _run_one(fn: Callable[[str], Any], serial: str) -> tuple:
        try:
            return True, str(fn(serial) or "")
        except Exception as exc:  # a failing headset must not take the job down
            return False, str(exc) or type(exc).__name__

    def _job_result(self, job_id: str, dev_id: str, ok: bool, message: str, mbps: Optional[float] = None) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        dev = self.devices.get(dev_id)
        label = dev.label if dev else dev_id
        job["done"] += 1
        job["failed"] += not ok
        entry = {"device": dev_id, "ok": ok, "message": message}
        if mbps is not None:
            entry["mbps"] = mbps
        job["results"].append(entry)
        callback = self._job_failed_cbs.get(job_id)
        if not ok and callback is not None:
            callback(dev_id)
        self.log_event("info" if ok else "error",
                       f"{job['action']}: {label} {'OK' if ok else 'FAILED'}" + (f" ({message})" if message else ""),
                       dev)
        self._settle_job(job)

    def _settle_job(self, job: dict) -> None:
        """Close the job once every result is in (and a scan has finished probing)."""
        if job["state"] != "running" or job["done"] < job["total"] or job.get("probing"):
            return
        job["state"] = "failed" if job["failed"] and job["failed"] == job["total"] else "done"
        self._job_failed_cbs.pop(job["id"], None)
        if job["action"] == "bandwidth_test":
            self._bw_summary(job)

    def _bw_summary(self, job: dict) -> None:
        rates = [(r["mbps"], r["device"]) for r in job["results"] if r["ok"] and r.get("mbps")]
        failed = job["failed"]
        if not rates:
            job["summary"] = {"n": 0, "median": None, "min": None, "min_label": None, "failed": failed}
            self.log_event("error", f"bandwidth_test: no results, {failed} failed")
            return
        low, low_id = min(rates)
        dev = self.devices.get(low_id)
        label = dev.label if dev else low_id
        median = round(statistics.median(r for r, _ in rates), 2)
        job["summary"] = {"n": len(rates), "median": median, "min": low, "min_label": label, "failed": failed}
        self.log_event("info", f"bandwidth_test: {len(rates)} OK, median {median:g} Mbps, "
                               f"min {low:g} Mbps ({label}), {failed} failed")

    def reachable(self, targets: List[Device], listed=None) -> List[tuple]:
        """``(device, adb serial)`` for each target; the serial is None when adb cannot reach it."""
        listed = self.fleet.listed() if listed is None else listed
        return [(d, self.fleet.adb_serial(d, listed)) for d in targets]

    # ----------------------------------------------------- features, preview

    def set_feature_tested(self, key: str, tested: bool) -> None:
        try:
            self.tested_features = (features.mark_tested if tested else features.unmark_tested)(
                self.tested_features, key)
        except KeyError as exc:
            raise CommandError(exc.args[0])
        self.changed(persist=True)

    @staticmethod
    def _dry_run(params: dict) -> bool:
        return bool(params.get("dry_run", True))

    def _pairs_for(self, action: str, targets: List[Device], listed=None) -> List[tuple]:
        """The ``(item, serial)`` pairs a confirmation token covers: saved addresses or `adb devices` serials."""
        pairs_fn = self.token_pairs.get(action)
        return pairs_fn(targets) if pairs_fn is not None else self.reachable(targets, listed)

    @staticmethod
    def _address_plan(targets: List[Device]) -> List[tuple]:
        """``(item, address or None, note)`` per target: one entry per distinct saved ``ip:5555``."""
        plan, seen = [], {}
        for dev in targets:
            if not dev.ip:
                plan.append((dev, None, "no saved address"))
                continue
            address = f"{dev.ip}:{ADB_PORT}"
            if address in seen:
                plan.append((dev, None, f"same address as {seen[address].label}"))
                continue
            seen[address] = dev
            plan.append((Addr(address), address, None))
        return plan

    def _address_pairs(self, targets: List[Device]) -> List[tuple]:
        return [(item, address) for item, address, _ in self._address_plan(targets)]

    def _purge_warnings(self, targets: List[Device], params: dict) -> List[str]:
        warnings = ["Purge drops each headset's adb connection and reconnects it; adb jobs are refused meanwhile."]
        if automation.push_in_progress(self.data_dir):
            warnings.append("A push is in progress: a live purge will be refused until it finishes.")
        return warnings

    def _token(self, action: str, pairs: List[tuple], dry_run: bool) -> str:
        serials = sorted(serial or f"unreachable:{d.device_id}" for d, serial in pairs)
        blob = json.dumps([action, serials, dry_run], separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def preview(self, params: dict, listed=None) -> dict:
        """What a command would hit, in words, plus the token that confirms it.

        ``listed`` is the set from ``adb devices``; callers on the event loop fetch it on the executor
        first so no adb process runs here.
        """
        action = str(params.get("action", ""))
        if getattr(self, f"_act_{action}", None) is None:
            raise CommandError(f"unknown action: {action}")
        targets = self.resolve_targets(params.get("targets"))
        if not targets:
            raise CommandError("no headsets match the selected targets")
        pairs = self._pairs_for(action, targets, listed)
        labels = [d.label for d in targets]
        verb = self.action_labels.get(action) or action.replace("_", " ").capitalize()
        if len(targets) == len(self.devices):
            scope = f"{verb} EVERY headset ({len(targets)})"
        else:
            names = ", ".join(labels[:PREVIEW_NAMES_SHOWN])
            if len(labels) > PREVIEW_NAMES_SHOWN:
                names += f" and {len(labels) - PREVIEW_NAMES_SHOWN} more"
            scope = f"{verb} {len(targets)} headset{'s' if len(targets) != 1 else ''}: {names}"
        warnings = []
        if action in ("poweroff", "purge") and self._dry_run(params):
            warnings.append("Dry run: nothing will be sent to the headsets.")
        down = [d.label for d, serial in pairs if serial is None]
        if down:
            what = "without a usable saved address" if action in ADDRESS_ACTIONS else "not reachable by adb"
            warnings.append(f"{len(down)} {what}: {', '.join(down[:PREVIEW_NAMES_SHOWN])}")
        if self.show_mode:
            warnings.append(automation.SHOW_MODE_WARNING)
        extra = self.preview_warnings.get(action)
        if extra is not None:
            warnings.extend(extra(targets, params))
        result = {"scope_text": scope, "labels": labels, "warnings": warnings,
                  "every": len(targets) == len(self.devices), "show_mode": self.show_mode,
                  "token": self._token(action, pairs, self._dry_run(params)),
                  "needs_confirm": action in self.confirm_actions}
        if action == "purge":  # the dialog's dry-run tick picks the matching token without a new preview
            result["dry_run_choice"] = True
            result["tokens"] = {"dry_run": self._token(action, pairs, True), "live": self._token(action, pairs, False)}
        return result

    @staticmethod
    def _is_playing(dev: Device) -> bool:
        return bool((dev.desired or {}).get("mode") == "playing" or (dev.status or {}).get("state") == "playing")

    def _refresh_warnings(self, targets: List[Device], params: dict) -> List[str]:
        playing = [d.label for d in targets if self._is_playing(d)]
        if not playing:
            return []
        names = ", ".join(playing[:PREVIEW_NAMES_SHOWN])
        return [f"Screen refresh interrupts playback (visible hitch) on {len(playing)} playing "
                f"headset{'s' if len(playing) != 1 else ''}: {names}"]

    def _guard(self, action: str, targets: List[Device], params: dict, listed=None) -> None:
        """Refuse gated actions that are untested, and confirm-required ones without a matching token."""
        feature = self.gated_actions.get(action)
        if feature and feature not in self.tested_features and params.get("testing") is not True:
            label = features.REGISTRY.get(feature, ("", feature))[1]
            raise CommandError(f"{label} is not marked as tested; use the Testing section "
                               f"(or send testing: true) to run it")
        if action in self.confirm_actions:
            token = self._token(action, self._pairs_for(action, targets, listed), self._dry_run(params))
            if params.get("confirm") != token:
                raise CommandError("confirmation missing or out of date (headsets changed since the "
                                   "preview); request a new preview and confirm again")

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
        # Older apps do not send this field.
        dev.player = str(hello.get("player") or "")
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
        self._recovery_poll()
        if self._rec_active and device_id not in self._cancelled:
            self._held[device_id] = server_clock()  # hold desired state until it reports what it is playing
            self._arm_timer()
        else:
            self._send_desired(dev)
        if self.following == device_id:
            self.send(dev, {"type": "pose_stream", "hz": POSE_STREAM_HZ})
        self.log_event("info", f"{dev.label} connected from {dev.ip}", dev)
        self.distributor.pump()
        return dev

    def headset_disconnected(self, dev: Device, conn) -> None:
        if dev.conn is not conn:
            return  # superseded by a newer connection
        dev.conn = None
        dev.online = False
        dev.pose = None
        self._held.pop(dev.device_id, None)
        self.dirty = True  # remember when it was last seen
        self.distributor.disconnected(dev.device_id)
        if dev.device_id in self._bw_pending:
            self._bw_fail(dev.device_id, "headset disconnected")
        self.log_event("warn", f"{dev.label} disconnected", dev)

    def headset_message(self, dev: Device, msg: dict) -> None:
        dev.last_seen = time.time()
        kind = msg["type"]
        if kind == "status":
            msg.pop("type")
            dev.status = msg
            self._status_t[dev.device_id] = server_clock()
            if dev.device_id in self._held and msg.get("clock_synced"):
                self._reconcile(dev)
            self._recovery_poll()
            self.distributor.check_idle(dev.device_id, msg)
            self.changed()
        elif kind == "inventory":
            files = msg.get("files") or []
            dev.inventory = {str(f["name"]): int(f.get("size", 0)) for f in files if isinstance(f, dict) and "name" in f}
            self.changed()
        elif kind == "pose":
            try:
                dev.pose = {"yaw": float(msg["yaw"]), "pitch": float(msg["pitch"]),
                            "roll": float(msg.get("roll", 0.0)), "t": time.monotonic()}
            except (KeyError, TypeError, ValueError):
                pass  # no changed(): poses arrive ~10 Hz and must not wake the dashboard
        elif kind == "downloads_finished":
            if not msg.get("cancelled"):  # a cancelled job's slot was already freed
                self.distributor.finished(dev.device_id, msg)
        elif kind == "event":
            level = msg.get("level", "info")
            self.log_event(level if level in ("info", "warn", "error") else "info",
                           f"{dev.label}: {msg.get('message', '')}", dev)
        elif kind == "bandwidth_result":
            self._bw_result(dev, msg)
        else:
            log.debug("ignoring %s from %s", kind, dev.label)

    # ------------------------------------------------------------------- pose

    def follow(self, device_id: str) -> None:
        """Ask one headset to stream its view direction; renewed every POSE_RENEW_S until unfollow()."""
        if device_id != self.following:
            self._stop_stream()
        self.following = device_id
        self._renew()

    def unfollow(self) -> None:
        self._stop_stream()
        self.following = None

    def _stop_stream(self) -> None:
        if self._renew_handle is not None:
            self._renew_handle.cancel()
            self._renew_handle = None
        dev = self.devices.get(self.following) if self.following else None
        if dev is not None:
            self.send(dev, {"type": "pose_stream", "hz": 0})
            dev.pose = None

    def _renew(self) -> None:
        if self._renew_handle is not None:
            self._renew_handle.cancel()
            self._renew_handle = None
        dev = self.devices.get(self.following) if self.following else None
        if dev is None:
            return
        self.send(dev, {"type": "pose_stream", "hz": POSE_STREAM_HZ})
        try:
            self._renew_handle = asyncio.get_running_loop().call_later(POSE_RENEW_S, self._renew)
        except RuntimeError:
            pass  # no event loop (unit tests): caller renews by hand

    def pose_of(self, device_id: str) -> Optional[dict]:
        dev = self.devices.get(device_id)
        return dict(dev.pose) if dev is not None and dev.pose else None

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

    def execute(self, action: str, params: dict, listed=None) -> dict:
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            raise CommandError(f"unknown action: {action}")
        targets = self.resolve_targets(params.get("targets"))
        if not targets:
            raise CommandError("no headsets match the selected targets")
        self._guard(action, targets, params, listed)
        if action in PLAYBACK_ACTIONS:
            self._cancel_recovery(targets)
        online = sum(1 for d in targets if d.online)
        result = handler(targets, params if listed is None else dict(params, _listed=listed)) or {}
        self.changed()
        result.setdefault("targets", len(targets))
        result["online"] = online
        if not online and action in ("load", "play", "pause", "seek", "stop"):
            result["warning"] = "No targeted headset is online; it will get this when it connects."
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

    # ------------------------------------------------------------ power (adb jobs)

    def _power_job(self, action: str, targets, params, fn, on_failed=None) -> dict:
        return self.start_job(action, self.reachable(targets, params.get("_listed")), fn, on_failed)

    def _act_sleep(self, targets, params):
        ids = [d.device_id for d in targets]
        self.intentionally_asleep.update(ids)  # before the first adb call, so no watchdog races the sleep
        self.dirty = True  # persisted: a restart must not wake what was put to sleep on purpose
        return self._power_job("sleep", targets, params, self.fleet.sleep_screen, on_failed=self._not_asleep)

    def _not_asleep(self, device_id: str) -> None:
        self.intentionally_asleep.discard(device_id)
        self.dirty = True

    def _act_wake(self, targets, params):
        self.intentionally_asleep.difference_update(d.device_id for d in targets)
        self.dirty = True
        return self._power_job("wake", targets, params, self.fleet.wake_screen)

    def _act_screen_refresh(self, targets, params):
        self.intentionally_asleep.difference_update(d.device_id for d in targets)
        self.dirty = True
        min_asleep = max(0.0, min(30.0, float(params.get("min_asleep_s", MIN_ASLEEP_S))))
        return self._power_job("screen_refresh", targets, params,
                               lambda serial: self.fleet.screen_refresh(serial, min_asleep_s=min_asleep))

    def _act_poweroff(self, targets, params):
        dry = self._dry_run(params)
        spec = params.get("targets")
        every = spec in (None, "", "all") or len(targets) == len(self.devices)
        if not dry and every and params.get("confirm_every") is not True:
            raise CommandError("refusing to power off EVERY headset without the EVERY-headset confirmation; "
                               "select specific headsets or confirm the 'EVERY headset' preview")
        if dry:
            self.log_event("info", f"poweroff dry run: would power off {len(targets)} headset"
                                   f"{'s' if len(targets) != 1 else ''}: {', '.join(d.label for d in targets)}")
        return self._power_job("poweroff", targets, params, lambda serial: self.fleet.poweroff(serial, dry_run=dry))

    def _act_snapshot(self, targets, params):
        if self.data_dir is None:
            raise CommandError("no data folder configured; snapshots cannot be saved")
        shot = params.get("screenshot", False)
        if not isinstance(shot, bool):
            raise CommandError("screenshot must be true or false")
        items = self.reachable(targets, params.get("_listed"))
        labels = {serial: d.label for d, serial in items if serial}
        data_dir = self.data_dir
        return self.start_job("snapshot", items,
                              lambda serial: self.fleet.snapshot(serial, labels.get(serial, serial), data_dir, shot))

    def _act_connect(self, targets, params):
        plan = self._address_plan(targets)
        notes = {item.device_id: note for item, _, note in plan if note}
        return self.start_job("connect", [(item, address) for item, address, _ in plan], self.fleet.connect,
                              notes=notes)

    def _act_purge(self, targets, params):
        dry = self._dry_run(params)
        if not dry:
            if automation.push_in_progress(self.data_dir):
                raise CommandError("refusing to purge while a push is in progress (the CLI push lock is held)")
            busy = [j["action"] for j in self.jobs.values()
                    if j["state"] == "running" and (j["action"] in self.adb_actions or j["action"] == "scan")]
            if busy:
                raise CommandError(f"refusing to purge while an adb job is running ({busy[0]}); wait for it to finish")
        plan = self._address_plan(targets)
        self.log_event("info", f"purge {'dry run: would reconnect' if dry else 'reconnecting'} "
                               f"{sum(1 for _, a, _ in plan if a)} adb address(es)")
        notes = {item.device_id: note for item, _, note in plan if note}
        return self.start_job("purge", [(item, address) for item, address, _ in plan],
                              lambda address: self.fleet.reconnect(address, dry_run=dry), notes=notes)

    def scan(self, cidr, listed=None) -> dict:
        """Probe a small private subnet for adb on 5555 and connect the open hosts that are known headsets."""
        try:
            ips = scan_candidates(cidr)
        except ValueError as exc:
            raise CommandError(str(exc))
        if any(j["action"] == "scan" and j["state"] == "running" for j in self.jobs.values()):
            raise CommandError("a scan is already running")
        job = self.open_job("scan", [])
        job.update(probing=len(ips), probed=len(ips), open=0, cidr=str(cidr).strip())
        loop, listed = self.loop, set(listed or ())

        def probe(ip: str) -> None:
            try:
                found = self.fleet.probe(ip)
            except Exception:
                found = False
            try:
                loop.call_soon_threadsafe(self._scan_probed, job["id"], ip, found, listed)
            except RuntimeError:
                pass  # loop closed

        for ip in ips:
            self.fleet.submit(probe, ip)
        self.changed()
        return {"job": job["id"], "addresses": len(ips)}

    def _scan_probed(self, job_id: str, ip: str, found: bool, listed) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job["probing"] -= 1
        address = f"{ip}:{ADB_PORT}"
        if found:
            job["open"] += 1
            known = any(d.ip == ip for d in self.devices.values())
            if not known:  # not ours: never touch it
                self.log_event("info", f"scan: {address} is open but not a known SyncVR headset; not connecting")
            else:
                job["total"] += 1
                if address in listed:
                    self._job_result(job_id, address, True, "already connected")
                else:
                    self._submit(job_id, address, address, self.fleet.connect)
        if job["probing"] <= 0:
            job.pop("probing", None)
            self.log_event("info", f"scan {job['cidr']}: {job['probed']} probed, {job['open']} open, "
                                   f"{job['total']} known headset(s)")
            self._settle_job(job)
        self.changed()

    # ------------------------------------------------------------- bandwidth

    def _bw_queued(self, dev_id: str) -> bool:
        return any(dev_id in run["queue"] for run in self._bw_runs.values())

    @staticmethod
    def _bw_number(params: dict, key: str, default, low, high) -> float:
        raw = params.get(key)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise CommandError(f"{key} must be a number")
        if value != value:
            raise CommandError(f"{key} must be a number")
        return max(low, min(high, value))

    def _bw_refusal(self, dev: Device) -> Optional[str]:
        """Why one headset must not take a test right now (checked again when it is sent)."""
        if not dev.online:
            return "offline"
        reason = self.brake()
        if reason:
            return f"not run: {'Show Mode is on' if reason == automation.REASON_SHOW_MODE else reason}"
        if self._is_playing(dev):
            return "not run: headset is playing"
        return None

    def _act_bandwidth_test(self, targets, params):
        reason = self.brake()
        if reason:
            raise CommandError("bandwidth test refused: " +
                               ("Show Mode is on" if reason == automation.REASON_SHOW_MODE else reason))
        playing = [d.label for d in targets if self._is_playing(d)]
        if playing:
            raise CommandError(f"bandwidth test refused: playing now: {', '.join(playing[:PREVIEW_NAMES_SHOWN])}")
        busy = [d.label for d in targets if d.device_id in self._bw_pending or self._bw_queued(d.device_id)]
        if busy:
            raise CommandError(f"bandwidth test already running on: {', '.join(busy[:PREVIEW_NAMES_SHOWN])}")
        if not any(d.online for d in targets):
            raise CommandError("no targeted headset is online")
        mb = int(self._bw_number(params, "mb", BW_DEFAULT_MB, 1, BW_MAX_MB))
        seconds = int(self._bw_number(params, "timeout_s", BW_DEFAULT_S, BW_MIN_S, BW_MAX_S))
        parallel = int(self._bw_number(params, "parallel", 1, 1, BW_MAX_PARALLEL))
        name = params.get("video")
        if name:
            video = self._video(name)
        elif self.library.videos:
            video = max(self.library.videos.values(), key=lambda v: v.size)
        else:
            raise CommandError("the library has no video to download for the test")
        if video.size < BW_MIN_VIDEO_BYTES:
            raise CommandError(f"{video.name} is smaller than 1 MB; pick a larger video")
        job = self.open_job("bandwidth_test", [d.device_id for d in targets])
        run = {"queue": deque(), "running": set(), "parallel": parallel, "video": video.name,
               "bytes": min(mb * 1024 * 1024, video.size), "seconds": seconds}
        self._bw_runs[job["id"]] = run
        for dev in targets:
            if dev.online:
                run["queue"].append(dev.device_id)
            else:
                self._job_result(job["id"], dev.device_id, False, "offline")
        self._bw_pump(job["id"])
        return {"job": job["id"]}

    def _bw_pump(self, job_id: str) -> None:
        run = self._bw_runs.get(job_id)
        if run is None:
            return
        while run["queue"] and len(run["running"]) < run["parallel"]:
            dev_id = run["queue"].popleft()
            dev = self.devices.get(dev_id)
            refusal = self._bw_refusal(dev) if dev is not None else "unknown headset"
            if refusal:
                self._job_result(job_id, dev_id, False, refusal)
                continue
            try:
                url = dev.conn.content_url(run["video"])
                dev.conn.send({"type": "bandwidth_test", "job": job_id, "url": url,
                               "bytes": run["bytes"], "seconds": run["seconds"]})
            except Exception as exc:
                self._job_result(job_id, dev_id, False, f"could not send: {exc}")
                continue
            self._bw_pending[dev_id] = job_id
            run["running"].add(dev_id)
            self._bw_timers[dev_id] = self.loop.call_later(run["seconds"] + BW_GRACE_S, self._bw_timeout,
                                                           dev_id, job_id)
        job = self.jobs.get(job_id)
        if job is None or job["state"] != "running":
            self._bw_runs.pop(job_id, None)
        self.changed()

    def _bw_clear(self, dev_id: str, job_id: str) -> None:
        self._bw_pending.pop(dev_id, None)
        timer = self._bw_timers.pop(dev_id, None)
        if timer is not None:
            timer.cancel()
        run = self._bw_runs.get(job_id)
        if run is not None:
            run["running"].discard(dev_id)

    def _bw_fail(self, dev_id: str, message: str) -> None:
        job_id = self._bw_pending.get(dev_id)
        if job_id is None:
            return
        self._bw_clear(dev_id, job_id)
        dev = self.devices.get(dev_id)
        if dev is not None:
            dev.bandwidth = {"ok": False, "mbps": None, "bytes": 0, "seconds": 0.0, "error": message,
                             "t": time.time()}
        self._job_result(job_id, dev_id, False, message)
        self._bw_pump(job_id)

    def _bw_timeout(self, dev_id: str, job_id: str) -> None:
        if self._bw_pending.get(dev_id) == job_id:
            self._bw_fail(dev_id, BW_TIMEOUT_MSG)

    def _bw_result(self, dev: Device, msg: dict) -> None:
        job_id = self._bw_pending.get(dev.device_id)
        if job_id is None or str(msg.get("job")) != job_id:
            log.debug("ignoring bandwidth_result for unknown job from %s", dev.label)
            return
        try:
            mbps = float(msg.get("mbps"))
            size, secs = int(msg.get("bytes") or 0), float(msg.get("seconds") or 0.0)
        except (TypeError, ValueError):
            mbps, size, secs = 0.0, 0, 0.0
        ok = msg.get("ok") is True and mbps == mbps and 0 < mbps < float("inf")
        error = "" if ok else str(msg.get("error") or "test failed")
        self._bw_clear(dev.device_id, job_id)
        dev.bandwidth = {"ok": ok, "mbps": round(mbps, 2) if ok else None, "bytes": size, "seconds": secs,
                         "error": error, "t": time.time()}
        text = f"{mbps:.1f} Mbps ({size / 1048576:.1f} MB in {secs:.1f} s)" if ok else error
        self._job_result(job_id, dev.device_id, ok, text, round(mbps, 2) if ok else None)
        self._bw_pump(job_id)

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

    # --------------------------------------------------------- crash recovery
    #
    # After a restart the server does not know what the headsets are doing, but they
    # do: they keep playing on their own anchor. So the first synced status of each
    # headset is adopted as its desired state (instead of re-cueing it), headsets that
    # are within RECOVERY_GROUP_S of each other are snapped to one anchor, and only
    # headsets whose state differs from the adopted one are sent anything.

    def _recovery_poll(self) -> None:
        if not self._rec_active:
            return
        now = server_clock()
        for device_id, t in list(self._held.items()):
            dev = self.devices.get(device_id)
            if dev is None or not dev.online:
                self._held.pop(device_id, None)
            elif now - t >= RECOVERY_STATUS_WAIT_S:
                self._reconcile(dev)
        if now >= self.recovering_until:
            self._finish_recovery()

    def recovery_tick(self) -> None:
        self._recovery_poll()

    def _arm_timer(self) -> None:
        if self._rec_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (unit tests): the caller ticks by hand

        def fire():
            self._rec_timer = None
            self._recovery_poll()
            if self._rec_active:
                self._arm_timer()
        self._rec_timer = loop.call_later(RECOVERY_STATUS_WAIT_S / 2, fire)

    def _cancel_recovery(self, targets: List[Device]) -> None:
        if not self._rec_active:
            return
        for dev in targets:
            self._cancelled.add(dev.device_id)
            self._held.pop(dev.device_id, None)
            self._members.discard(dev.device_id)
            self._raw.pop(dev.device_id, None)
        self.dirty = True
        if all(i in self._cancelled for i in self.devices):
            self._rec_active = False
            self._held.clear()

    @staticmethod
    def _same_anchor(a: dict, b: dict) -> bool:
        return (abs(float(a["pos"]) - float(b["pos"])) <= ANCHOR_EQUAL_S
                and abs(float(a["at"]) - float(b["at"])) <= ANCHOR_EQUAL_S
                and bool(a.get("loop", False)) == bool(b.get("loop", False)))

    def _adopt_playing(self, video: Video, st: dict, t: float) -> dict:
        anchor = st.get("anchor")
        position = st.get("position")
        try:
            pos, at, loop = float(anchor["pos"]), float(anchor["at"]), bool(anchor.get("loop", False))
        except (TypeError, KeyError, ValueError):
            anchor = None
        if anchor is not None:
            implied = position_at(self._desired_for(video, "playing", pos, at, loop), t)
            if st.get("state") == "playing" and position is not None and abs(implied - float(position)) > ANCHOR_SANITY_S:
                log.warning("anchor of %s implies %.2fs but it reports %.2fs; rebuilding the anchor",
                            video.name, implied, float(position))
                anchor = None
        if anchor is None:
            ref = st.get("expected")
            if ref is None:
                ref = position
            pos, at, loop = float(ref or 0.0), t, video.loop
        d = self._desired_for(video, "playing", pos, at, loop)
        d["pos"], d["at"] = pos, at  # exact copy, no rounding
        return d

    def _reports(self, dev: Device, d: dict) -> bool:
        """Does the headset already report the state ``d``?"""
        st = dev.status
        if st.get("video") != d["video"]:
            return False
        if d["mode"] == "paused":
            return st.get("state") == "paused"
        if st.get("state") not in ("playing", "syncing"):
            return False
        a = st.get("anchor")
        if isinstance(a, dict) and "pos" in a and "at" in a:
            try:
                return self._same_anchor(a, d)
            except (TypeError, ValueError):
                return False
        ref = st.get("expected")
        if ref is None:
            ref = st.get("position")
        t = self._status_t.get(dev.device_id, server_clock())
        return ref is not None and abs(position_at(d, t) - float(ref)) <= 0.01

    def _consensus(self) -> Optional[dict]:
        """What most recovered headsets are doing: the biggest playing cohort, else the biggest paused one."""
        for mode in ("playing", "paused"):
            cohorts: Counter = Counter()
            example: Dict[Any, dict] = {}
            for i in self._members:
                d = self.devices[i].desired if i in self.devices else None
                if d and d.get("mode") == mode:
                    key = (d["video"], d["pos"], d.get("at"), d.get("loop", False))
                    cohorts[key] += 1
                    example.setdefault(key, d)
            if cohorts:
                return example[cohorts.most_common(1)[0][0]]
        return None

    def _regroup(self, dev: Device, now: float) -> List[Device]:
        """Snap the playing headsets within RECOVERY_GROUP_S of ``dev`` to their median anchor."""
        mine = dev.desired
        cands = []
        for i in self._members:
            other = self.devices.get(i)
            d = self._raw.get(i)
            if other and d and d.get("mode") == "playing" and d["video"] == mine["video"]:
                cands.append((position_at(d, now), i, other))
        cands.sort(key=lambda g: (g[0], g[1]))
        k = next(n for n, g in enumerate(cands) if g[2] is dev)
        lo = hi = k  # grow the group while neighbours are within RECOVERY_GROUP_S of each other
        while lo > 0 and cands[lo][0] - cands[lo - 1][0] <= RECOVERY_GROUP_S:
            lo -= 1
        while hi < len(cands) - 1 and cands[hi + 1][0] - cands[hi][0] <= RECOVERY_GROUP_S:
            hi += 1
        group = cands[lo:hi + 1]
        if len(group) < 2:
            return []
        anchor = self._raw[group[(len(group) - 1) // 2][1]]
        changed = []
        for _, _, other in group:
            if not self._same_anchor(other.desired, anchor):
                other.desired = dict(anchor)
                changed.append(other)
        return changed

    def _reconcile(self, dev: Device) -> None:
        self._held.pop(dev.device_id, None)
        if not self._rec_active:
            self._send_desired(dev)
            return
        st = dev.status
        t = self._status_t.get(dev.device_id, server_clock())
        now = server_clock()
        state = st.get("state")
        video = self.library.get(st.get("video")) if st.get("video") else None
        saved = dev.desired
        adopted: Optional[dict] = None
        if video is not None and state in ("playing", "syncing"):
            adopted = self._adopt_playing(video, st, t)
        elif video is not None and state in ("paused", "loading", "ended"):
            if state != "paused" and saved and saved.get("mode") in ("playing", "paused") \
                    and saved.get("video") == video.name:
                adopted = dict(saved)
            else:
                pos = st.get("position")
                adopted = self._desired_for(video, "paused", self._clamp(video, pos or 0.0))
        if adopted is not None:
            dev.desired = adopted
            self._raw[dev.device_id] = adopted
            self._members.add(dev.device_id)
            touched = self._regroup(dev, now) if adopted["mode"] == "playing" else []
            for other in [dev] + [o for o in touched if o is not dev]:
                if not self._reports(other, other.desired):
                    self._send_desired(other)
        else:  # idle: join the show, else fall back to the saved state
            cons = self._consensus()
            if cons is not None:
                dev.desired = dict(cons)
            if dev.desired and dev.desired.get("mode") in ("playing", "paused"):
                self._members.add(dev.device_id)
            self._send_desired(dev)
        self.dirty = True

    def _finish_recovery(self) -> None:
        for device_id in list(self._held):
            dev = self.devices.get(device_id)
            if dev is not None:
                self._reconcile(dev)
        self._held.clear()
        cons = self._consensus()
        for dev in self.devices.values():
            if (not dev.online and dev.device_id not in self._members and dev.device_id not in self._cancelled
                    and cons is not None and dev.desired and dev.desired.get("mode") in ("playing", "paused")):
                dev.desired = dict(cons)
        self._rec_active = False
        self.dirty = True
        if self._members and cons is not None:
            pos = position_at(cons, server_clock())
            self._rec_result = {"rejoined": len(self._members), "position": round(pos, 2), "video": cons["video"]}
            self.log_event("info", f"Recovered show: {len(self._members)} headsets rejoined at "
                                   f"{int(pos) // 60:02d}:{int(pos) % 60:02d} ({cons['video']})")
        else:
            self._rec_result = {"rejoined": len(self._members), "position": None, "video": None}

    def _recovery_json(self) -> dict:
        if self._rec_active:
            cons = self._consensus()
            return {"active": True, "rejoined": len(self._members),
                    "position": round(position_at(cons, server_clock()), 2) if cons else None,
                    "video": cons["video"] if cons else None}
        r = self._rec_result or {"rejoined": 0, "position": None, "video": None}
        return dict(r, active=False)

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
        if {"projection", "stereo", "rotation"} & changes.keys():
            self._push_view(video)
        self.changed(persist=True)
        return video

    def _push_view(self, video: Video) -> None:
        """Tell headsets that have this video loaded how to display it now."""
        view = {k: v for k, v in video.playback_fields().items()
                if k in ("projection", "stereo", "rotation")}
        for dev in self.devices.values():
            d = dev.desired
            if d and d.get("mode") in ("playing", "paused") and d.get("video") == video.name:
                d.update(view)
                if dev.online:
                    self.send(dev, dict(view, type="view", video=video.name))

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
