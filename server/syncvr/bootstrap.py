"""Stdlib-only first-run bootstrap: creates server/.venv, installs dependencies, runs ``syncvr gui``.

Usage: ``python -m syncvr.bootstrap gui [args]`` (run from the server folder or with it on sys.path).
Set SYNCVR_NO_VENV=1 to skip the venv and use the current interpreter.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

SERVER_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = SERVER_DIR / ".venv"
STAMP_NAME = ".syncvr-deps"
MEDIA_STAMP_NAME = ".syncvr-media"
REQUIREMENTS = ["aiohttp>=3.8", "PySide6-Essentials>=6.5"]  # core
MEDIA_REQUIREMENTS = ["PySide6-Addons>=6.5"]  # optional, installed later from the GUI (install_media)


def venv_python(venv: Path = VENV_DIR, gui_exe: Optional[bool] = None) -> Path:
    """Interpreter inside the venv. On Windows pythonw.exe when we were started by pythonw (no console)."""
    venv = Path(venv)
    if sys.platform.startswith("win"):
        if gui_exe is None:
            gui_exe = os.path.basename(sys.executable).lower().startswith("pythonw")
        return venv / "Scripts" / ("pythonw.exe" if gui_exe else "python.exe")
    return venv / "bin" / "python"


def media_installed(venv: Path = VENV_DIR) -> bool:
    return (Path(venv) / MEDIA_STAMP_NAME).exists()


def _requirements(venv: Path = VENV_DIR) -> List[str]:
    """Core requirements, plus the media add-on once it was installed into this venv."""
    return REQUIREMENTS + (MEDIA_REQUIREMENTS if media_installed(venv) else [])


def _stamp_text(venv: Path = VENV_DIR) -> str:
    return "\n".join(_requirements(venv)) + "\n"


def needs_install(venv: Path = VENV_DIR) -> bool:
    """True when the venv is missing or its stamp does not match REQUIREMENTS."""
    venv = Path(venv)
    if not venv_python(venv, gui_exe=False).exists():
        return True
    try:
        return (venv / STAMP_NAME).read_text(encoding="utf-8") != _stamp_text(venv)
    except OSError:
        return True


def _create_venv(venv: Path) -> bool:
    import venv as venv_mod
    try:
        venv_mod.create(str(venv), with_pip=True, clear=venv.exists())
        return True
    except Exception as exc:  # ensurepip missing, read-only folder, ...
        print("Could not create the virtual environment: %s" % exc)
        print("On Debian/Ubuntu/Mint run: sudo apt install python3 python3-venv")
        shutil.rmtree(str(venv), ignore_errors=True)
        return False


def _pip_install(py: Path, requirements: Optional[List[str]] = None, quiet: bool = False) -> bool:
    cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check"] + (requirements or REQUIREMENTS)
    kwargs = {}
    if quiet:  # called from the GUI: no console window, output captured
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # visible progress under pythonw
        print("Installing SyncVR dependencies (first run, ~100 MB). No Python? tools/get-desktop.sh|.ps1 installs the ready-made desktop app instead.")
    try:
        return subprocess.call(cmd, **kwargs) == 0
    except OSError as exc:
        print("pip failed to start: %s" % exc)
        return False


def install_media(venv: Path = VENV_DIR, installer=None) -> bool:
    """Install the optional media add-on (PySide6-Addons) into the venv with its own pip, then record it
    so needs_install() keeps it in the requirement set. ``installer(py, requirements)`` is injectable for tests."""
    venv = Path(venv)
    py = venv_python(venv, gui_exe=False)
    if not py.exists():
        return False
    ok = (installer or (lambda p, reqs: _pip_install(p, reqs, quiet=True)))(py, REQUIREMENTS + MEDIA_REQUIREMENTS)
    if not ok:
        return False
    try:
        (venv / MEDIA_STAMP_NAME).write_text("\n".join(MEDIA_REQUIREMENTS) + "\n", encoding="utf-8")
        (venv / STAMP_NAME).write_text(_stamp_text(venv), encoding="utf-8")
    except OSError:
        return False
    return True


def running_from_venv(venv: Path = VENV_DIR) -> bool:
    """True when this process is the source-run GUI inside server/.venv (never in a frozen bundle)."""
    if getattr(sys, "frozen", False):
        return False
    try:
        return Path(sys.prefix).resolve() == Path(venv).resolve()
    except OSError:
        return False


def ensure_venv(venv: Path = VENV_DIR) -> Optional[Path]:
    """Return the venv interpreter ready to use, or None to fall back to the current Python."""
    venv = Path(venv)
    if needs_install(venv):
        broken = venv.exists() and not venv_python(venv, gui_exe=False).exists()
        if not venv_python(venv, gui_exe=False).exists() or broken:
            if not _create_venv(venv):
                return None
        if not _pip_install(venv_python(venv, gui_exe=False), _requirements(venv)):
            print("Dependency install failed; will retry next start.")
            return None
        try:
            (venv / STAMP_NAME).write_text(_stamp_text(venv), encoding="utf-8")  # only after pip succeeded
        except OSError:
            pass
    return venv_python(venv)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    py = None
    if not os.environ.get("SYNCVR_NO_VENV"):
        py = ensure_venv()
    if py is None:
        py = Path(sys.executable)
        print("SyncVR: using %s without the private environment; the control window needs PySide6, "
              "so you will probably get the console mode only." % py)
    return subprocess.call([str(py), "-m", "syncvr"] + argv, cwd=str(SERVER_DIR))


if __name__ == "__main__":
    sys.exit(main())
