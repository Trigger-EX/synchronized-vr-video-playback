"""Clickable PC launcher: runs the server in a background thread with a small Qt (PySide6) control window.

Falls back to console mode when PySide6 or a display is missing. Importing this module needs neither
PySide6 nor aiohttp; both are loaded lazily.
"""

import asyncio
import concurrent.futures
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from . import paths
from .protocol import DEFAULT_DISCOVERY_PORT, DEFAULT_HTTP_PORT, DEFAULT_TCP_PORT

log = logging.getLogger(__name__)

# Relative folders resolve against server/, so a double-click works from any working directory.
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_NAME = "syncvr.log"


# ---------------------------------------------------------------- pure helpers

def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True if something already accepts connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def operator_address(snapshot: Optional[dict], http_port: int) -> str:
    """ip:port to type into the Android operator app."""
    addrs = ((snapshot or {}).get("server") or {}).get("addresses") or []
    return "%s:%d" % (addrs[0] if addrs else "localhost", http_port)


def status_lines(snapshot: Optional[dict], address: str) -> List[str]:
    """Text shown in the control window (and printed in console mode)."""
    if not snapshot:
        return ["Starting...", "Operator app address: " + address]
    devices = snapshot.get("devices") or []
    online = sum(1 for d in devices if d.get("online"))
    videos = len(snapshot.get("library") or [])
    dl = snapshot.get("downloads") or {}
    lines = ["Headsets: %d online of %d known" % (online, len(devices)),
             "Videos in content folder: %d" % videos]
    busy = len(dl.get("active") or []) + len(dl.get("queued") or [])
    if busy:
        lines.append("Downloads: %d active or queued" % busy)
    lines.append("Operator app address: " + address)
    return lines


def load_overrides(data_dir: Path) -> dict:
    """Optional data/launcher.json: content, data, http_port, tcp_port, name, password."""
    try:
        with open(Path(data_dir) / "launcher.json", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def save_override(data_dir: Path, key: str, value) -> None:
    """Persist one setting in launcher.json, keeping the others."""
    data_dir = Path(data_dir)
    data = load_overrides(data_dir)
    data[key] = value
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "launcher.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, data_dir / "launcher.json")


def setup_logging(data_dir: Path, verbose: bool = False) -> Path:
    """Rotating log file under data/logs plus console output when a console exists."""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_NAME
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    for h in list(root.handlers):
        if getattr(h, "_syncvr", False):
            root.removeHandler(h)
    handlers = [logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        h._syncvr = True
        root.addHandler(h)
    return path


def open_path(target) -> None:
    """Open a file, folder or URL with the desktop's default handler."""
    target = str(target)
    try:
        if target.startswith("http"):
            webbrowser.open(target)
        elif sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # no handler installed, etc.
        log.warning("could not open %s: %s", target, exc)


# ---------------------------------------------------------------- supervisor

BACKOFF_S = (1.0, 2.0, 5.0)
MAX_RESTARTS = 5
RESTART_WINDOW_S = 600.0
PORT_WAIT_S = 10.0


def supervisor_enabled(args) -> bool:
    """The parent/child split applies to the window and console runs, not to the child itself or the self-test."""
    return not (getattr(args, "supervised", False) or getattr(args, "self_test", False)
                or os.environ.get("SYNCVR_NO_SUPERVISOR"))


def child_command(argv: List[str], recovered: bool = False) -> List[str]:
    """Command line that re-runs this app as the supervised child. argv is the full ``gui`` argument list."""
    extra = ["--supervised"] + (["--recovered"] if recovered else [])
    rest = [a for a in argv[1:] if a not in ("--supervised", "--recovered")] if argv[:1] == ["gui"] else \
        [a for a in argv if a not in ("--supervised", "--recovered")]
    if getattr(sys, "frozen", False):  # PyInstaller entry always implies "gui"
        return [sys.executable] + rest + extra
    return [sys.executable, "-m", "syncvr", "gui"] + rest + extra


def supervise(argv: List[str], port: int = 0, spawn=None, sleep=time.sleep, clock=time.monotonic,
              port_busy=port_in_use, say=None) -> int:
    """Run the app as a child process and restart it after a crash (non-zero exit or signal).

    Exit code 0 ends supervision. At most MAX_RESTARTS restarts per RESTART_WINDOW_S, then give up (return 1).
    Restarted children get --recovered. spawn(cmd) returns an object with wait() -> returncode.
    """
    say = say or (lambda m: (log.warning(m), print(m, file=sys.stderr) if sys.stderr is not None else None))
    spawn = spawn or (lambda cmd: subprocess.Popen(cmd))
    restarts: List[float] = []
    recovered = False
    while True:
        proc = spawn(child_command(argv, recovered))
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            try:
                proc.wait()  # the child got the same Ctrl+C
            except Exception:
                pass
            return 0
        if code == 0:
            return 0
        now = clock()
        restarts = [t for t in restarts if now - t < RESTART_WINDOW_S]
        if len(restarts) >= MAX_RESTARTS:
            say("SyncVR keeps crashing (%d restarts in %d minutes, last exit code %s); giving up. "
                "See the log in the data folder." % (len(restarts), RESTART_WINDOW_S // 60, code))
            return 1
        delay = BACKOFF_S[min(len(restarts), len(BACKOFF_S) - 1)]
        restarts.append(now)
        say("SyncVR exited unexpectedly (%s); restarting in %g s (restart %d of %d allowed per %d min)."
            % ("signal %d" % -code if code < 0 else "code %s" % code, delay, len(restarts), MAX_RESTARTS,
               RESTART_WINDOW_S // 60))
        sleep(delay)
        waited = 0.0
        while port and port_busy(port) and waited < PORT_WAIT_S:  # old process may still hold the port
            sleep(0.5)
            waited += 0.5
        recovered = True


def _run_supervisor(args) -> int:
    argv = list(getattr(args, "argv", None) or ["gui"] + sys.argv[1:])
    data_dir = Path(args.data) if getattr(args, "data", None) else paths.data_dir()
    port = args.http_port if getattr(args, "http_port", None) is not None else \
        int(load_overrides(data_dir).get("http_port", DEFAULT_HTTP_PORT))
    try:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(log_dir / "supervisor.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    except OSError:
        pass
    return supervise(argv, port=port)


# ---------------------------------------------------------------- server thread

class ServerThread:
    """Runs SyncServer on its own event loop. All controller access happens on that loop."""

    def __init__(self, config):
        self.config = config
        self.server = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop: Optional[asyncio.Event] = None

    @property
    def http_port(self) -> int:
        return self.config.http_port

    def start(self, timeout: float = 15.0) -> None:
        self._thread = threading.Thread(target=self._run, name="syncvr-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("server did not start within %.0f s" % timeout)
        if self.error is not None:
            raise self.error

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # reported to start() or logged
            if self.error is None:
                self.error = exc
            log.exception("server thread failed")
        finally:
            self._ready.set()

    async def _main(self) -> None:
        from .app import SyncServer  # lazy: needs aiohttp
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = SyncServer(self.config)
        try:
            await server.start()
        except BaseException as exc:
            self.error = exc
            try:
                await server.stop()
            except Exception:
                pass
            return
        self.server = server
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await server.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self, timeout: float = 2.0) -> Optional[dict]:
        """Controller snapshot taken on the server loop; None if not running or on timeout."""
        if not self.running or self.server is None or self.loop is None:
            return None

        async def take():
            return self.server.controller.snapshot()
        try:
            return asyncio.run_coroutine_threadsafe(take(), self.loop).result(timeout)
        except Exception:
            return None

    def call(self, fn, *args) -> "concurrent.futures.Future":
        """Run fn(controller, *args) on the server loop; the Future carries its result or exception."""
        if not self.running or self.server is None or self.loop is None:
            failed = concurrent.futures.Future()
            failed.set_exception(RuntimeError("server is not running"))
            return failed

        async def run():
            return fn(self.server.controller, *args)
        try:
            return asyncio.run_coroutine_threadsafe(run(), self.loop)
        except RuntimeError:  # loop closed
            failed = concurrent.futures.Future()
            failed.set_exception(RuntimeError("server is not running"))
            return failed

    def snapshot_async(self) -> Optional["concurrent.futures.Future"]:
        """Future for a controller snapshot taken on the server loop; None if not running."""
        if not self.running or self.server is None or self.loop is None:
            return None

        async def take():
            return self.server.controller.snapshot()
        try:
            return asyncio.run_coroutine_threadsafe(take(), self.loop)
        except RuntimeError:  # loop closed
            return None

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop the server and join the thread; True if it ended in time."""
        if self.loop is not None and self._stop is not None and self.running:
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:  # loop already closed
                pass
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.running


# ---------------------------------------------------------------- UI

def _make_config(args):
    from .app import ServerConfig  # lazy: needs aiohttp
    data_dir = Path(args.data) if args.data else paths.data_dir()
    ov = load_overrides(data_dir)
    content = args.content or ov.get("content") or str(paths.content_dir(data_dir))
    if args.data is None and ov.get("data"):
        data_dir = Path(ov["data"])
    cfg = ServerConfig(
        content_dir=Path(content), data_dir=data_dir,
        http_port=args.http_port if args.http_port is not None else int(ov.get("http_port", DEFAULT_HTTP_PORT)),
        tcp_port=0 if args.self_test else int(ov.get("tcp_port", DEFAULT_TCP_PORT)),
        discovery_port=DEFAULT_DISCOVERY_PORT,
        discovery=not (args.no_discovery or args.self_test), name=ov.get("name", "SyncVR"),
        password=ov.get("password", ""), public_host=ov.get("public_host", ""))
    cfg.content_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _offer_install(message: str) -> bool:
    """aiohttp is missing: offer (console only) to pip install it. True if importable afterwards."""
    print(message)
    print("Normally start-syncvr.sh / Start SyncVR.pyw set this up in server/.venv for you.")
    try:
        install = input("Install now? [y/N] ").strip().lower().startswith("y")
    except (EOFError, OSError):
        install = False
    if not install:
        return False
    cmd = [sys.executable, "-m", "pip", "install", "--user", "aiohttp"]
    if subprocess.call(cmd) != 0:
        cmd.insert(5, "--break-system-packages")  # PEP 668 distributions
        if subprocess.call(cmd) != 0:
            return False
    try:
        import importlib
        importlib.invalidate_caches()
        __import__("aiohttp")  # availability probe
        return True
    except ImportError:
        return False


_QT_PROBE = "from PySide6.QtWidgets import QApplication; QApplication([])"


def _qt_note(reason: str) -> None:
    """Say why the window is not available; shown before the console fallback starts."""
    msg = "SyncVR: no control window, falling back to console mode: " + reason
    log.warning(msg)
    if sys.stderr is not None:
        print(msg, file=sys.stderr)


def qt_available(console: bool = False) -> bool:
    """True when PySide6 imports and a Qt window can actually be created."""
    if console:
        return False
    try:
        __import__("PySide6")  # import, not find_spec: a broken install must count as missing
    except ImportError as exc:
        _qt_note("PySide6 is not installed in %s (%s). Delete server/.venv and start again to reinstall."
                 % (sys.executable, exc))
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        if os.environ.get("QT_QPA_PLATFORM", "") not in ("offscreen", "minimal"):
            _qt_note("no display found (DISPLAY/WAYLAND_DISPLAY unset)")
            return False
    # A missing platform plugin (xcb) aborts the whole process, so probe in a child process.
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run([sys.executable, "-c", _QT_PROBE], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, timeout=15, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        _qt_note("the Qt probe failed to run (%s)" % exc)
        return False
    if res.returncode != 0:
        detail = (res.stderr or b"").decode("utf-8", "replace").strip()[-600:]
        _qt_note("Qt could not start. On Linux try: sudo apt install libxcb-cursor0 libxcb-xinerama0\n%s" % detail)
        return False
    return True


def run_console(thread: ServerThread, address: str, log_path: Path) -> None:
    print("SyncVR is running. Operator app address: %s\nLog: %s\nPress Ctrl+C to stop." % (address, log_path))
    try:
        while thread.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _already_running(qt_ok: bool, msg: str) -> None:
    if qt_ok:
        try:
            from .gui.app import show_error
            show_error("SyncVR", msg)
            return
        except Exception:
            pass
    print(msg)


def _self_test(args) -> int:
    """Start the server on port 0 with offscreen Qt, take one snapshot through the bridge, exit 0 if it works."""
    import tempfile
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from .gui.app import self_test
    except ImportError as exc:
        print("self-test failed: PySide6 is not available: %s" % exc)
        return 1
    with tempfile.TemporaryDirectory(prefix="syncvr-selftest-") as tmp:
        args.data, args.content, args.http_port = str(Path(tmp) / "data"), str(Path(tmp) / "content"), 0
        config = _make_config(args)
        thread = ServerThread(config)
        try:
            thread.start()
            ok = self_test(thread, config, Path(tmp) / "self-test.log")
        except Exception as exc:
            print("self-test failed: %s" % exc)
            return 1
        finally:
            thread.stop(5.0)
    print("self-test ok" if ok else "self-test failed: no snapshot received")
    return 0 if ok else 1


def main(args) -> int:
    """Entry point for ``python -m syncvr gui``."""
    if getattr(args, "self_test", False):
        return _self_test(args)
    if supervisor_enabled(args):
        return _run_supervisor(args)
    qt_ok = qt_available(args.console)
    try:
        __import__("aiohttp")  # availability probe
    except ImportError:
        if not _offer_install("SyncVR needs the 'aiohttp' package, which is not installed."):
            return 1
    config = _make_config(args)
    log_path = setup_logging(config.data_dir, verbose=args.verbose)

    if config.http_port and port_in_use(config.http_port):
        log.info("port %d already in use; not starting a second one", config.http_port)
        msg = ("SyncVR is already running on port %d (an earlier server or window?). Stop the old one "
               "(close its window, or: pkill -f syncvr) and start again." % config.http_port)
        _already_running(qt_ok, msg)
        return 0

    lock = None
    if qt_ok:
        try:
            from .gui.app import acquire_instance_lock
            lock = acquire_instance_lock(config.data_dir)
        except ImportError:
            lock = None
        else:
            if lock is None:
                log.info("another SyncVR window holds the lock in %s", config.data_dir)
                _already_running(qt_ok, "SyncVR is already running (its window may be hidden or minimised).")
                return 0

    thread = ServerThread(config)
    try:
        thread.start()
    except Exception as exc:
        log.error("could not start the server: %s", exc)
        msg = "SyncVR could not start: %s\n\nSee %s" % (exc, log_path)
        shown = False
        if qt_ok:
            try:
                from .gui.app import show_error
                show_error("SyncVR", msg)
                shown = True
            except Exception:
                pass
        if not shown:
            print(msg)
        return 1

    try:
        if qt_ok:
            from .gui.app import run_window
            run_window(thread, config, log_path, recovered=getattr(args, "recovered", False))
        else:
            run_console(thread, operator_address(thread.snapshot(), config.http_port), log_path)
    finally:
        thread.stop(timeout=5.0)
        if lock is not None:
            lock.unlock()
    return 0
