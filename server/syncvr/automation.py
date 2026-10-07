"""Brakes for automation: reasons a watchdog must not send anything to the headsets.

Brakes block AUTOMATION only. Manual commands always run; their confirmation just says Show Mode is on.
No Qt and no aiohttp, so the CLI (`adbtool push`) can use the push lock too.
"""

import contextlib
import os
import sys
from pathlib import Path
from typing import Iterator, Optional, Union

PUSH_LOCK_FILE = "push.lock"
REASON_SHOW_MODE = "show_mode"
REASON_SYNC = "sync in progress"
REASON_PUSH = "push in progress"
SHOW_MODE_WARNING = "Show Mode is on: automation is paused, but this command will still run."


def pid_alive(pid: int) -> bool:
    """True if a process with this id exists. Never signals the process."""
    if pid <= 0 or pid > 2 ** 31 - 1:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, OverflowError):
        return False
    return True


def push_lock_path(data_dir: Union[str, Path]) -> Path:
    return Path(data_dir) / PUSH_LOCK_FILE


def push_in_progress(data_dir: Optional[Union[str, Path]]) -> bool:
    """A live CLI push holds the lock. A missing, unreadable, malformed or dead-PID file never counts."""
    if not data_dir:
        return False
    try:
        pid = int(push_lock_path(data_dir).read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return False
    return pid_alive(pid)


@contextlib.contextmanager
def push_lock(data_dir: Union[str, Path]) -> Iterator[None]:
    """Hold the push lock (a pid file in the data dir) for the duration, removed on any exit.

    Best effort: if the file cannot be written the push still runs, it just does not brake automation.
    """
    path = push_lock_path(data_dir)
    mine = str(os.getpid())
    written = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mine + "\n", encoding="utf-8")
        written = True
    except OSError:
        pass
    try:
        yield
    finally:
        if written:
            try:
                if path.read_text(encoding="utf-8").strip() == mine:  # not another push's file
                    path.unlink()
            except OSError:
                pass


def brake_reason(show_mode: bool, sync_active: bool, data_dir: Optional[Union[str, Path]]) -> Optional[str]:
    """First matching reason, in priority order, or None when automation may act."""
    if show_mode:
        return REASON_SHOW_MODE
    if sync_active:
        return REASON_SYNC
    if push_in_progress(data_dir):
        return REASON_PUSH
    return None
