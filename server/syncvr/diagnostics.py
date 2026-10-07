"""Read-only per-headset diagnostic snapshot: dumpsys services, logcat, getprop and an optional screenshot.

Everything is captured over adb and written on the server, one file per command, into
``<data_dir>/snapshots/<label>_<serial>_<YYYYmmdd-HHMMSS-ffffff>/``. Nothing is written on the headset
(the screenshot uses ``exec-out``, so the PNG streams over adb). A failing or timed-out command writes
an ``.error.txt`` note and the rest still run. No Qt, no asyncio.
"""

import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from . import parsers

SNAPSHOT_DIR = "snapshots"
MAX_CONCURRENT = 4  # fleet-wide, across jobs
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

DUMPSYS_TIMEOUT_S = 15.0
LOGCAT_TIMEOUT_S = 30.0
GETPROP_TIMEOUT_S = 10.0
SCREENCAP_TIMEOUT_S = 30.0


class Step(NamedTuple):
    name: str  # file stem
    command: str  # run with `adb shell`
    timeout_s: float


STEPS = (
    Step("power", "dumpsys power", DUMPSYS_TIMEOUT_S),
    Step("display", "dumpsys display", DUMPSYS_TIMEOUT_S),
    Step("window", "dumpsys window windows", DUMPSYS_TIMEOUT_S),
    Step("activity", "dumpsys activity activities", DUMPSYS_TIMEOUT_S),
    Step("audio", "dumpsys audio", DUMPSYS_TIMEOUT_S),
    Step("surfaceflinger", "dumpsys SurfaceFlinger", DUMPSYS_TIMEOUT_S),
    Step("surfaceflinger_list", "dumpsys SurfaceFlinger --list", DUMPSYS_TIMEOUT_S),
    Step("thermal", "dumpsys thermalservice", DUMPSYS_TIMEOUT_S),
    Step("battery", "dumpsys battery", DUMPSYS_TIMEOUT_S),
    Step("logcat", "logcat -d -t 2000", LOGCAT_TIMEOUT_S),
    Step("getprop", "getprop", GETPROP_TIMEOUT_S),
)
SCREENSHOT_FILE = "screenshot.png"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str, fallback: str = "x") -> str:
    """Filename-safe form of a label or serial (``192.168.1.20:5555`` -> ``192.168.1.20_5555``)."""
    cleaned = _UNSAFE.sub("_", str(text or "")).strip("._")[:48]
    return cleaned or fallback


def snapshots_dir(data_dir) -> Path:
    return Path(data_dir) / SNAPSHOT_DIR


def make_snapshot_dir(data_dir, label: str, serial: str, now: Optional[datetime] = None) -> Path:
    """Create a fresh, unique folder. A name collision gets ``-2``, ``-3`` ... and never reuses a folder."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    base = f"{safe_name(label, 'headset')}_{safe_name(serial, 'serial')}_{stamp}"
    root = snapshots_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        path = root / (base if n == 1 else f"{base}-{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1


class Result(NamedTuple):
    folder: Path
    total: int
    failed: List[str]  # names of commands that failed or timed out


def _error_note(folder: Path, name: str, message: str) -> None:
    (folder / f"{name}.error.txt").write_text(f"{name} failed: {message}\n", encoding="utf-8")


def _screencap(adb_path: str, serial: str, timeout_s: float) -> bytes:
    proc = subprocess.run([adb_path, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True,
                          timeout=timeout_s, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"adb exited with {proc.returncode}")
    if not proc.stdout:
        raise RuntimeError("screencap returned no data")
    return proc.stdout


def _summary(folder: Path, label: str, serial: str, texts: dict, failed: List[str], total: int) -> str:
    focus = parsers.focused_window(texts.get("window"))
    app = parsers.focused_app(texts.get("window"))
    thermal = parsers.thermal_status(texts.get("thermal"))
    temp = parsers.battery_temp_c(texts.get("battery"))

    def show(value):
        return "unknown" if value is None else str(value)
    lines = [f"Headset: {label}", f"Serial: {serial}", f"Folder: {folder.name}",
             f"Wakefulness: {show(parsers.wakefulness(texts.get('power')))}",
             f"Display state: {show(parsers.display_state(texts.get('display')))}",
             f"Focused window: {show(focus)}",
             f"Focused app: {show(app)}",
             "Thermal status: " + ("unknown" if thermal is None else f"{thermal} ({parsers.THERMAL_NAMES[thermal]})"),
             "Battery temperature: " + ("unknown" if temp is None else f"{temp:.1f} C"),
             f"Commands: {total - len(failed)} of {total} succeeded"]
    if failed:
        lines.append("Failed: " + ", ".join(failed) + " (see *.error.txt)")
    return "\n".join(lines) + "\n"


def capture_device(fleet, serial: str, label: str, data_dir, screenshot: bool = False,
                   now: Optional[datetime] = None,
                   step_hook: Optional[Callable[[str], None]] = None) -> Result:
    """Capture one headset. Blocks while four other captures are running (fleet-wide cap)."""
    with _slots:
        folder = make_snapshot_dir(data_dir, label, serial, now)
        failed: List[str] = []
        texts: dict = {}
        steps = list(STEPS)
        total = len(steps) + (1 if screenshot else 0)
        for step in steps:
            if step_hook:
                step_hook(step.name)
            try:
                out = fleet.shell(serial, step.command, timeout=step.timeout_s)
                texts[step.name] = out
                (folder / f"{step.name}.txt").write_text(out + "\n", encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                failed.append(step.name)
                _error_note(folder, step.name, f"timed out after {step.timeout_s:g} s")
            except Exception as exc:  # one bad command never aborts the rest
                failed.append(step.name)
                _error_note(folder, step.name, str(exc) or type(exc).__name__)
        if screenshot:
            try:
                (folder / SCREENSHOT_FILE).write_bytes(_screencap(fleet.adb.adb, serial, SCREENCAP_TIMEOUT_S))
            except subprocess.TimeoutExpired:
                failed.append("screenshot")
                _error_note(folder, "screenshot", f"timed out after {SCREENCAP_TIMEOUT_S:g} s")
            except Exception as exc:
                failed.append("screenshot")
                _error_note(folder, "screenshot", str(exc) or type(exc).__name__)
        try:
            (folder / "SUMMARY.txt").write_text(_summary(folder, label, serial, texts, failed, total),
                                                encoding="utf-8")
        except Exception as exc:  # parsers never raise; this is only a disk problem
            failed.append("summary")
            _error_note(folder, "summary", str(exc))
        return Result(folder, total, failed)
