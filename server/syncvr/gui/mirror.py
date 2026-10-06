"""View a headset's screen with adb + scrcpy (the Go player cannot stream frames itself). No Qt in here."""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

ADB_PORT = 5555
CONNECT_TIMEOUT = 8.0
READY_MARK = "[server] INFO: Device:"
READY_TIMEOUT = 8.0
LAUNCH_GAP = 0.3
PORT_RANGE = "27183:27282"
WM_CLASS = "SyncVR-view"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

INSTALL_HELP = (
    "Viewing a headset needs adb and scrcpy, which SyncVR does not bundle.\n\n"
    "Linux:  sudo apt install adb scrcpy\n"
    "Windows:  download the scrcpy release zip (it includes adb), extract it next to SyncVR.exe "
    "or add it to PATH, or use File > Set scrcpy/adb folder...\n"
    "macOS:  brew install scrcpy android-platform-tools\n\n"
    "The headset must have developer mode on and listen over Wi-Fi: connect it by USB once and run "
    "`adb tcpip 5555`."
)


# Single left-eye region of the Go's side-by-side mirror (width:height:x:y), from the old Headjack panel.
EYE_CROP = "1224:1232:0:104"
# Keep the mirror light: small frames, capped rate and bitrate, no render buffering or mipmaps.
MAX_SIZE = 480
MAX_FPS = 30
VIDEO_BIT_RATE = "2M"


def _exe_name(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" and not name.endswith(".exe") else name


def find_tool(name: str, extra_dirs: Iterable = (), frozen: Optional[bool] = None, exe_dir=None, meipass=None,
              which: Callable = shutil.which) -> Optional[str]:
    """Look in the configured folders, next to the executable and in its `tools` dir (frozen: also the bundle), then PATH."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    dirs = [Path(d) for d in extra_dirs if d]
    if frozen:
        exe = Path(exe_dir) if exe_dir else Path(sys.executable).resolve().parent
        bundle = meipass if meipass is not None else getattr(sys, "_MEIPASS", None)
        for base in ([exe] + ([Path(bundle)] if bundle else [])):
            dirs += [base, base / "tools"]
    exe_name = _exe_name(name)
    for d in dirs:
        cand = d / exe_name
        if cand.is_file():
            return str(cand)
    return which(name)


def stderr_tail(text: str, lines: int = 8) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def session_is_wayland(env=None, platform: str = "") -> bool:
    env = os.environ if env is None else env
    return (platform or sys.platform).startswith("linux") and (
        env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or env.get("QT_QPA_PLATFORM", "").startswith("wayland")
        or bool(env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY")))


def embed_supported(env=None, platform: str = "", qt_platform: str = "") -> bool:
    """scrcpy's window can be docked when both it and Qt are X11 clients (Linux, also via XWayland) or on Windows.
    Qt on native Wayland, offscreen, a Wayland session without XWayland, and macOS get a separate window."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform == "win32":
        return True
    if not platform.startswith("linux"):
        return False
    wayland = session_is_wayland(env, platform)
    if (qt_platform or ("wayland" if wayland else "xcb")) != "xcb":
        return False
    return bool(env.get("DISPLAY")) or not wayland


def find_native_window(title: str) -> Optional[int]:
    """Native window id of the scrcpy window with this exact title, or None (not there yet / no way to look)."""
    try:
        if sys.platform == "win32":
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            return int(hwnd) or None
        if shutil.which("xdotool"):
            out = subprocess.run(["xdotool", "search", "--name", "^%s$" % title], capture_output=True, text=True,
                                 timeout=2).stdout.split()
            return int(out[0]) if out else None
        if shutil.which("xwininfo"):
            out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                if '"%s"' % title in line:
                    return int(line.split()[0], 16)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


class _Entry:
    def __init__(self, proc, name: str, started: float):
        self.proc, self.name, self.started = proc, name, started
        self.lines = []
        self.ready = threading.Event()
        self.notified = False
        if getattr(proc, "stdout", None) is not None:
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                self.lines.append(text.rstrip())
                if READY_MARK in text:
                    self.ready.set()
        except (OSError, ValueError):
            pass

    def tail(self) -> str:
        return stderr_tail("\n".join(self.lines))


class MirrorManager:
    """One scrcpy process per headset. `on_error(title, message)` reports problems, `on_ready(device_id)` fires once
    scrcpy has connected to the device. Call `poll()` periodically."""

    def __init__(self, on_error: Optional[Callable] = None, tools_dir: str = "", on_ready: Optional[Callable] = None,
                 clock: Callable = time.monotonic, sleep: Callable = time.sleep):
        self.on_error = on_error or (lambda title, message: None)
        self.on_ready = on_ready or (lambda device_id: None)
        self.tools_dir = tools_dir or ""
        self.full_feed = False  # False: one eye only (the cropped region, as in the old Headjack panel)
        self._clock, self._sleep = clock, sleep
        self._last_launch = None
        self._procs: Dict[str, _Entry] = {}

    def tool(self, name: str) -> Optional[str]:
        return find_tool(name, [self.tools_dir])

    def is_running(self, device_id: str) -> bool:
        entry = self._procs.get(device_id)
        return bool(entry) and entry.proc.poll() is None

    def start(self, device_id: str, ip: str, name: str = "", embed: bool = False) -> bool:
        """Launch scrcpy. Returns False when nothing new was started (already open, or an error was reported)."""
        if self.is_running(device_id):
            return False  # its window is already open
        self._reap(device_id)
        if not ip:
            self.on_error("View headset", "This headset has not reported an IP address yet.")
            return False
        adb, scrcpy = self.tool("adb"), self.tool("scrcpy")
        if not adb or not scrcpy:
            missing = " and ".join(n for n, p in (("adb", adb), ("scrcpy", scrcpy)) if not p)
            self.on_error("View headset", "%s not found.\n\n%s" % (missing, INSTALL_HELP))
            return False
        serial = "%s:%d" % (ip, ADB_PORT)
        try:
            proc = subprocess.run([adb, "connect", serial], capture_output=True, text=True,
                                  timeout=CONNECT_TIMEOUT, creationflags=NO_WINDOW)
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            self.on_error("View headset", "adb connect %s timed out.\n\nIs the headset awake and on the same network? "
                          "Run `adb tcpip 5555` once over USB." % serial)
            return False
        except OSError as e:
            self.on_error("View headset", "Could not run adb: %s" % e)
            return False
        if proc.returncode != 0 or "connected to" not in out.lower() or "cannot" in out.lower() \
                or "failed" in out.lower():
            self.on_error("View headset", "adb could not connect to %s.\n\n%s\n\nThe headset needs developer mode "
                          "and `adb tcpip 5555` run once over USB so it listens over Wi-Fi."
                          % (serial, stderr_tail(out) or "(no output)"))
            return False
        cmd = [scrcpy, "-s", serial, "--port", PORT_RANGE, "--window-title", self.title(name or device_id),
               "--max-size", str(MAX_SIZE), "--max-fps", str(MAX_FPS),
               "--video-bit-rate", VIDEO_BIT_RATE, "--no-audio", "--no-mipmaps", "--video-buffer=0", "--stay-awake"]
        if not self.full_feed:
            cmd += ["--crop", EYE_CROP]
        if embed:
            cmd.append("--window-borderless")
        env = dict(os.environ, ADB=adb, SDL_VIDEO_X11_WMCLASS=WM_CLASS, SDL_VIDEO_WAYLAND_WMCLASS=WM_CLASS)
        if embed and sys.platform.startswith("linux"):
            env["SDL_VIDEODRIVER"] = "x11"  # a native Wayland window cannot be reparented into Qt's X11 dock
        if self._last_launch is not None:
            self._sleep(max(0.0, LAUNCH_GAP - (self._clock() - self._last_launch)))
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                 creationflags=NO_WINDOW)
        except OSError as e:
            self.on_error("View headset", "Could not run scrcpy: %s" % e)
            return False
        self._last_launch = self._clock()
        self._procs[device_id] = _Entry(p, name or device_id, self._last_launch)
        return True

    @staticmethod
    def title(name: str) -> str:
        return "SyncVR - %s" % name

    def _reap(self, device_id: str, report: bool = False) -> None:
        entry = self._procs.pop(device_id, None)
        if entry and report and entry.proc.returncode:
            self.on_error("View headset", "scrcpy for %s stopped (exit %s).\n\n%s"
                          % (entry.name, entry.proc.returncode, entry.tail() or "(no output)"))

    def poll(self) -> None:
        """Forget windows the user closed, report scrcpy runs that failed or never became ready, announce ready ones."""
        for device_id in list(self._procs):
            entry = self._procs[device_id]
            if entry.proc.poll() is not None:
                self._reap(device_id, report=True)
            elif entry.ready.is_set():
                if not entry.notified:
                    entry.notified = True
                    self.on_ready(device_id)
            elif self._clock() - entry.started > READY_TIMEOUT:
                self.stop(device_id)
                self.on_error("View headset", "scrcpy did not connect to %s within %d s.\n\n%s"
                              % (entry.name, READY_TIMEOUT, entry.tail() or "(no output)"))

    def stop(self, device_id: str) -> None:
        entry = self._procs.get(device_id)
        if entry and entry.proc.poll() is None:
            entry.proc.terminate()
            try:
                entry.proc.wait(2)
            except subprocess.TimeoutExpired:
                entry.proc.kill()
        self._procs.pop(device_id, None)

    def stop_all(self) -> None:
        for device_id in list(self._procs):
            self.stop(device_id)
