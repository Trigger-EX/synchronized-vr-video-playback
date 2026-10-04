"""Clickable PC launcher: runs the server in a background thread with a small Tk control window.

Falls back to console mode when tkinter or a display is missing. Importing this module needs neither
tkinter nor aiohttp; both are loaded lazily.
"""

import asyncio
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

from .protocol import DEFAULT_DISCOVERY_PORT, DEFAULT_HTTP_PORT, DEFAULT_TCP_PORT

log = logging.getLogger(__name__)

# Relative folders resolve against server/, so a double-click works from any working directory.
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_NAME = "syncvr.log"
REFRESH_MS = 1000


# ---------------------------------------------------------------- pure helpers

def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True if something already accepts connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dashboard_url(snapshot: Optional[dict], http_port: int, lan: bool = True) -> str:
    host = "localhost"
    if lan and snapshot:
        addrs = (snapshot.get("server") or {}).get("addresses") or []
        if addrs:
            host = addrs[0]
    return "http://%s:%d/" % (host, http_port)


def status_lines(snapshot: Optional[dict], url: str) -> List[str]:
    """Text shown in the control window (and printed in console mode)."""
    if not snapshot:
        return ["Starting...", "Dashboard: " + url]
    devices = snapshot.get("devices") or []
    online = sum(1 for d in devices if d.get("online"))
    videos = len(snapshot.get("library") or [])
    dl = snapshot.get("downloads") or {}
    lines = ["Headsets: %d online of %d known" % (online, len(devices)),
             "Videos in content folder: %d" % videos]
    busy = len(dl.get("active") or []) + len(dl.get("queued") or [])
    if busy:
        lines.append("Downloads: %d active or queued" % busy)
    lines.append("Dashboard: " + url)
    return lines


def load_overrides(data_dir: Path) -> dict:
    """Optional data/launcher.json: content, data, http_port, tcp_port, name, password, open_browser."""
    try:
        with open(Path(data_dir) / "launcher.json", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


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
    data_dir = Path(args.data) if args.data else BASE_DIR / "data"
    ov = load_overrides(data_dir)
    content = args.content or ov.get("content") or str(BASE_DIR / "content")
    if args.data is None and ov.get("data"):
        data_dir = Path(ov["data"])
    cfg = ServerConfig(
        content_dir=Path(content), data_dir=data_dir,
        http_port=args.http_port if args.http_port is not None else int(ov.get("http_port", DEFAULT_HTTP_PORT)),
        tcp_port=int(ov.get("tcp_port", DEFAULT_TCP_PORT)),
        discovery_port=DEFAULT_DISCOVERY_PORT,
        discovery=not args.no_discovery, name=ov.get("name", "SyncVR"),
        password=ov.get("password", ""), public_host=ov.get("public_host", ""))
    cfg.content_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg, bool(ov.get("open_browser", True))


def _offer_install(message: str, tk_ok: bool) -> bool:
    """aiohttp is missing: offer to pip install it. Returns True if it is importable afterwards."""
    install = False
    if tk_ok:
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            install = messagebox.askyesno("SyncVR", message + "\n\nInstall it now?", icon="question")
            root.destroy()
        except Exception:
            tk_ok = False
    if not tk_ok:
        print(message)
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
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def tk_available() -> bool:
    """True when tkinter imports and a window can actually be created."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:  # ImportError, TclError (no DISPLAY), ...
        return False


def run_console(thread: ServerThread, url: str, log_path: Path) -> None:
    print("SyncVR is running. Dashboard: %s\nLog: %s\nPress Ctrl+C to stop." % (url, log_path))
    try:
        while thread.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def run_window(thread: ServerThread, config, url_local: str, log_path: Path) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("SyncVR")
    root.resizable(False, False)
    status = tk.StringVar(value="Starting...")
    tk.Label(root, text="SyncVR server is running", font=("TkDefaultFont", 13, "bold")).pack(padx=20, pady=(14, 4))
    tk.Label(root, textvariable=status, justify="left", anchor="w").pack(padx=20, pady=4, fill="x")
    box = tk.Frame(root)
    box.pack(padx=20, pady=(8, 14), fill="x")
    state = {"snap": None, "closing": False}

    def current_url() -> str:
        return dashboard_url(state["snap"], thread.http_port)

    buttons = [("Open dashboard", lambda: open_path(url_local)),
               ("Open content folder", lambda: open_path(Path(config.content_dir).resolve())),
               ("Open log", lambda: open_path(log_path)),
               ("Stop & quit", lambda: quit_now())]
    for text, cmd in buttons:
        tk.Button(box, text=text, command=cmd, width=26).pack(pady=2, fill="x")

    def refresh():
        if state["closing"]:
            return
        if not thread.running:
            status.set("The server stopped. See the log for details.")
            return
        # snapshot() blocks briefly on the server loop; keep it off the Tk thread when slow.
        state["snap"] = thread.snapshot(timeout=0.5) or state["snap"]
        status.set("\n".join(status_lines(state["snap"], current_url())))
        root.after(REFRESH_MS, refresh)

    def quit_now():
        if state["closing"]:
            return
        state["closing"] = True
        status.set("Stopping...")
        root.update_idletasks()
        thread.stop(timeout=5.0)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", quit_now)
    refresh()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if not state["closing"]:
            thread.stop(timeout=5.0)


def main(args) -> int:
    """Entry point for ``python -m syncvr gui``."""
    tk_ok = tk_available() if not args.console else False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        if not _offer_install("SyncVR needs the 'aiohttp' package, which is not installed.", tk_ok):
            return 1
    config, open_browser = _make_config(args)
    log_path = setup_logging(config.data_dir, verbose=args.verbose)
    local_url = dashboard_url(None, config.http_port, lan=False)

    if config.http_port and port_in_use(config.http_port):
        log.info("port %d already in use; opening the running dashboard instead", config.http_port)
        open_path(local_url)
        return 0

    thread = ServerThread(config)
    try:
        thread.start()
    except Exception as exc:
        log.error("could not start the server: %s", exc)
        msg = "SyncVR could not start: %s\n\nSee %s" % (exc, log_path)
        if tk_ok:
            try:
                import tkinter
                from tkinter import messagebox
                root = tkinter.Tk()
                root.withdraw()
                messagebox.showerror("SyncVR", msg)
                root.destroy()
            except Exception:
                print(msg)
        else:
            print(msg)
        return 1

    local_url = dashboard_url(None, config.http_port, lan=False)
    if open_browser and not args.no_browser:
        open_path(local_url)
    try:
        if tk_ok:
            run_window(thread, config, local_url, log_path)
        else:
            run_console(thread, local_url, log_path)
    finally:
        thread.stop(timeout=5.0)
    return 0
