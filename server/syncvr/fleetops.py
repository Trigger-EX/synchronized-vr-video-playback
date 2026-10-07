"""Fleet-wide adb operations for the server and GUI (no Qt, no asyncio).

``AdbFleet`` wraps :class:`adbtool.Adb` and owns the one thread pool every adb job shares, so
that dozens of headsets never mean dozens of simultaneous adb processes.
"""

import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, Optional, Sequence, Set, Tuple

from . import diagnostics
from .adbtool import Adb

MAX_WORKERS = 10
ADB_PORT = 5555

KEYEVENT_SLEEP = 223  # KEYCODE_SLEEP: screen off, never toggles
KEYEVENT_WAKEUP = 224  # KEYCODE_WAKEUP: screen on, never toggles
WAKEFULNESS_CMD = "dumpsys power | grep mWakefulness"
POLL_TIMEOUT_S = 3.0
POLL_INTERVAL_S = 0.2
MIN_ASLEEP_S = 1.0
_WAKEFULNESS = re.compile(r"mWakefulness=(\w+)")

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def shared_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="syncvr-adb")
        return _executor


def adb_serial(dev, listed: Iterable[str]) -> Optional[str]:
    """The adb serial to address ``dev`` by, or None when adb cannot reach it.

    Prefers the Wi-Fi address (``ip:5555``), then the USB serial; either must appear in ``listed``
    (the output of ``adb devices``).
    """
    listed = set(listed)
    ip = getattr(dev, "ip", "")
    if ip and f"{ip}:{ADB_PORT}" in listed:
        return f"{ip}:{ADB_PORT}"
    serial = getattr(dev, "serial", "")
    if serial and serial in listed:
        return serial
    return None


def resolve_serials(devices: Sequence, listed: Iterable[str]) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """Unambiguous adb serial per device id (None when unreachable) plus a note for every refused address.

    For unattended automation, where acting on the wrong headset is worse than skipping one. A device is addressed
    by the serial it reported itself, then by its remembered ``ip:5555``, and only when that value is in ``listed``
    and is claimed by exactly one known device. An IP that two or more devices claim (a stale persisted address,
    NAT) is never used: those devices are unreachable unless their own serial resolves. ``notes[device_id]`` says
    why an address was refused.
    """
    listed = set(listed)
    devices = list(devices)
    ips: Dict[str, int] = {}
    serials: Dict[str, int] = {}
    for d in devices:
        if getattr(d, "ip", ""):
            ips[d.ip] = ips.get(d.ip, 0) + 1
        if getattr(d, "serial", ""):
            serials[d.serial] = serials.get(d.serial, 0) + 1
    result: Dict[str, Optional[str]] = {}
    notes: Dict[str, str] = {}
    for d in devices:
        ip, serial = getattr(d, "ip", ""), getattr(d, "serial", "")
        chosen = None
        if serial and serials.get(serial) == 1 and serial in listed:
            chosen = serial
        elif ip and ips.get(ip) == 1 and f"{ip}:{ADB_PORT}" in listed:
            chosen = f"{ip}:{ADB_PORT}"
        if ip and ips.get(ip, 0) > 1:
            notes[d.device_id] = f"address {ip} is claimed by {ips[ip]} headsets"
        elif serial and serials.get(serial, 0) > 1:
            notes[d.device_id] = f"serial {serial} is claimed by {serials[serial]} headsets"
        if chosen is None and d.device_id in notes:
            result[d.device_id] = None
        else:
            result[d.device_id] = chosen
    return result, notes


class ListingFailed(RuntimeError):
    """``adb devices`` itself failed (as opposed to reporting no devices)."""


class AdbFleet:
    def __init__(self, adb: Optional[Adb] = None, executor: Optional[ThreadPoolExecutor] = None):
        self.adb = adb or Adb()
        self._executor = executor

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor or shared_executor()

    def listed_checked(self) -> Set[str]:
        """Serials `adb devices` reports as ready; raises :class:`ListingFailed` when adb is missing or fails."""
        try:
            return set(self.adb.devices())
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise ListingFailed(str(exc) or type(exc).__name__) from exc

    def listed(self) -> Set[str]:
        """Like :meth:`listed_checked` but empty on failure (interactive callers; unattended code must not use it)."""
        try:
            return self.listed_checked()
        except ListingFailed:
            return set()

    def adb_serial(self, dev, listed: Optional[Iterable[str]] = None) -> Optional[str]:
        return adb_serial(dev, self.listed() if listed is None else listed)

    def run(self, serial: str, *args, **kw) -> str:
        return self.adb.run(serial, *args, **kw)

    def shell(self, serial: str, command: str, **kw) -> str:
        return self.adb.shell(serial, command, **kw)

    def submit(self, fn, *args):
        return self.executor.submit(fn, *args)

    # ------------------------------------------------------------- power

    clock = staticmethod(time.monotonic)
    sleeper = staticmethod(time.sleep)

    def sleep_screen(self, serial: str) -> str:
        self.shell(serial, f"input keyevent {KEYEVENT_SLEEP}")
        return "sleep sent"

    def wake_screen(self, serial: str) -> str:
        self.shell(serial, f"input keyevent {KEYEVENT_WAKEUP}")
        return "wake sent"

    def launch(self, serial: str) -> str:
        """Start the player (same ``monkey`` launch as ``syncvr adb launch``)."""
        self.shell(serial, f"monkey -p {self.adb.package} -c android.intent.category.LAUNCHER 1 >/dev/null "
                           f"&& echo launched")
        return "launch sent"

    def back(self, serial: str) -> str:
        self.shell(serial, "input keyevent 4")  # KEYCODE_BACK
        return "back sent"

    def connect(self, address: str) -> str:
        """``adb connect``; adb exits 0 even when it fails, so the message decides."""
        out = self.adb.run(None, "connect", address)
        if "connected to" not in out:  # also matches "already connected to"
            raise RuntimeError(out or f"could not connect to {address}")
        return out

    def wakefulness(self, serial: str) -> str:
        """``Awake``, ``Asleep``, ``Dozing``, ``Dreaming`` or ``unknown`` (unparseable output)."""
        match = _WAKEFULNESS.search(self.shell(serial, WAKEFULNESS_CMD))
        return match.group(1) if match else "unknown"

    def wait_wakefulness(self, serial: str, want: str, timeout_s: float = POLL_TIMEOUT_S,
                         interval_s: float = POLL_INTERVAL_S) -> bool:
        deadline = self.clock() + timeout_s
        while True:
            if self.wakefulness(serial) == want:
                return True
            if self.clock() >= deadline:
                return False
            self.sleeper(interval_s)

    def screen_refresh(self, serial: str, min_asleep_s: float = MIN_ASLEEP_S,
                       timeout_s: float = POLL_TIMEOUT_S) -> str:
        """One sleep, confirm Asleep, hold ``min_asleep_s``, one wake, confirm Awake."""
        self.shell(serial, f"input keyevent {KEYEVENT_SLEEP}")
        if not self.wait_wakefulness(serial, "Asleep", timeout_s):
            raise RuntimeError(f"screen did not go to sleep within {timeout_s:g} s")
        self.sleeper(max(0.0, float(min_asleep_s)))
        self.shell(serial, f"input keyevent {KEYEVENT_WAKEUP}")
        if not self.wait_wakefulness(serial, "Awake", timeout_s):
            raise RuntimeError(f"screen did not wake within {timeout_s:g} s")
        return "refreshed"

    def poweroff(self, serial: str, dry_run: bool = True) -> str:
        if dry_run:
            return "would power off (dry run, nothing sent)"
        self.shell(serial, "reboot -p")
        return "power off sent"

    # ------------------------------------------------------- diagnostics

    def snapshot(self, serial: str, label: str, data_dir, screenshot: bool = False) -> str:
        """Capture one headset into ``<data_dir>/snapshots/``; returns a result line with the folder path."""
        result = diagnostics.capture_device(self, serial, label, data_dir, screenshot)
        if len(result.failed) >= result.total:
            raise RuntimeError(f"every command failed; see {result.folder}")
        note = f" ({len(result.failed)} of {result.total} commands failed: {', '.join(result.failed)})" \
            if result.failed else ""
        return f"saved to {result.folder}{note}"
