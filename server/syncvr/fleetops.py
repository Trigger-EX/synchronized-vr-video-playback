"""Fleet-wide adb operations for the server and GUI (no Qt, no asyncio).

``AdbFleet`` wraps :class:`adbtool.Adb` and owns the one thread pool every adb job shares, so
that dozens of headsets never mean dozens of simultaneous adb processes.
"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional, Set

from .adbtool import Adb

MAX_WORKERS = 10
ADB_PORT = 5555

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


class AdbFleet:
    def __init__(self, adb: Optional[Adb] = None, executor: Optional[ThreadPoolExecutor] = None):
        self.adb = adb or Adb()
        self._executor = executor

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor or shared_executor()

    def listed(self) -> Set[str]:
        """Serials `adb devices` reports as ready; empty when adb is missing or fails."""
        try:
            return set(self.adb.devices())
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return set()

    def adb_serial(self, dev, listed: Optional[Iterable[str]] = None) -> Optional[str]:
        return adb_serial(dev, self.listed() if listed is None else listed)

    def run(self, serial: str, *args, **kw) -> str:
        return self.adb.run(serial, *args, **kw)

    def shell(self, serial: str, command: str, **kw) -> str:
        return self.adb.shell(serial, command, **kw)

    def submit(self, fn, *args):
        return self.executor.submit(fn, *args)
