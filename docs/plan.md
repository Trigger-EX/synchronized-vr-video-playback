# Plan: PySide6 launcher GUI (default on all OSes)

PySide6 is an optional extra `gui = ["PySide6-Essentials>=6.5"]`; aiohttp stays the only core dependency. "Click a file and it opens" comes from a stdlib-only bootstrap that creates `server/.venv`, pip-installs aiohttp + PySide6-Essentials on first run, then runs `python -m syncvr gui`. Rejected: PySide6 as a required dep (100 MB on every headless/CI install; PEP 668 blocks `pip --user` on Mint); keeping tk as a second fallback.

## Steps
1. `server/pyproject.toml`: add the `gui` extra.
2. New `server/syncvr/bootstrap.py` (stdlib only, `python -m syncvr.bootstrap gui [args]`): `REQUIREMENTS`, `venv_python(venv)` (Windows `Scripts/pythonw.exe` if parent is pythonw else `Scripts/python.exe`; else `bin/python`), `needs_install` (stamp file `.venv/.syncvr-deps` vs REQUIREMENTS; never write the stamp if pip fails), `ensure_venv()` (venv.create with pip, pip install, stamp; Windows: pip in `CREATE_NEW_CONSOLE`; rebuild a broken venv; missing ensurepip -> hint `sudo apt install python3-venv` and fall back to system Python), `SYNCVR_NO_VENV=1` skips, `main()` ends with `subprocess.call([venv_py, "-m", "syncvr", *argv], cwd=server)` (no os.execv).
3. `start-syncvr.sh`: if install needed and no tty, re-exec inside `x-terminal-emulator -e` / `gnome-terminal --`; then `exec python3 -m syncvr.bootstrap gui "$@"`; apt hint `python3 python3-venv`.
4. `Start SyncVR.pyw`: add `server` to `sys.path`, `sys.exit(bootstrap.main(["gui"]))`.
5. New executable `Start SyncVR.command` (macOS): `cd "$(dirname "$0")" && exec ./start-syncvr.sh`. `Start SyncVR.desktop` unchanged.
6. `server/syncvr/launcher.py`: remove tkinter. `qt_available(console)`: False if console; False on ImportError of PySide6 (import, not find_spec); Linux without DISPLAY/WAYLAND_DISPLAY -> False; subprocess probe (timeout 15, CREATE_NO_WINDOW on Windows) `from PySide6.QtWidgets import QApplication; QApplication([])` because a missing xcb plugin aborts the process. Add `ServerThread.snapshot_async()` (Future from run_coroutine_threadsafe, None if not running; snapshot still only on the loop thread). `_offer_install` console-only plus hint. Start errors: QMessageBox.critical if Qt, else print. On Qt failure log `sudo apt install libxcb-cursor0`.
7. New `server/syncvr/qt_ui.py`, imported lazily from `launcher.main`: `ControlWindow(QWidget)` with title, status label, buttons Open dashboard / Open content folder / Open log / Stop & quit; 1000 ms QTimer calls `snapshot_async()` only if none pending; done-callback emits `Signal(object)` (queued to GUI thread); close/Stop shows "Stopping...", processEvents, `thread.stop(5)`; `run_window()` reuses/creates QApplication, SIGINT handler -> app.quit() plus a 200 ms no-op timer, `app.exec()`.
8. Tests: `test_launcher.py` replace tk test with `test_import_without_pyside6` (`sys.modules["PySide6"]=None`, `qt_available(False) is False`) and a `snapshot_async` case; new `test_bootstrap.py` (venv_python per platform via monkeypatched sys.platform, stamp logic, REQUIREMENTS vs pyproject via tomllib); new `test_qt_ui.py` (`importorskip("PySide6")`, offscreen, fake thread with completed future, status text, Stop calls fake.stop).
9. CI server job: apt `libegl1 libxkbcommon0`, `pip install -e ".[test,gui]" pyflakes`, env `QT_QPA_PLATFORM: offscreen`.
10. README launcher section (first run downloads ~100 MB into `server/.venv`; Mint `python3-venv`, `libxcb-cursor0`; macOS `.command` right-click > Open first time) and note in this file.

## Constraints
aiohttp 3.8 compatible; no signal handlers off the main thread; tests pass with no PySide6/display; Python >= 3.9 for PySide6.

## Cannot verify here
Mint/Nemo double-click with terminal pop-up, Windows pythonw + pip console, macOS Gatekeeper, real X11/Wayland rendering, libxcb-cursor0 on stock Mint, first-run download time.
