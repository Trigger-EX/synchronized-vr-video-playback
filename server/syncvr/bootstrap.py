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
REQUIREMENTS = ["aiohttp>=3.8", "PySide6-Essentials>=6.5"]


def venv_python(venv: Path = VENV_DIR, gui_exe: Optional[bool] = None) -> Path:
    """Interpreter inside the venv. On Windows pythonw.exe when we were started by pythonw (no console)."""
    venv = Path(venv)
    if sys.platform.startswith("win"):
        if gui_exe is None:
            gui_exe = os.path.basename(sys.executable).lower().startswith("pythonw")
        return venv / "Scripts" / ("pythonw.exe" if gui_exe else "python.exe")
    return venv / "bin" / "python"


def _stamp_text() -> str:
    return "\n".join(REQUIREMENTS) + "\n"


def needs_install(venv: Path = VENV_DIR) -> bool:
    """True when the venv is missing or its stamp does not match REQUIREMENTS."""
    venv = Path(venv)
    if not venv_python(venv, gui_exe=False).exists():
        return True
    try:
        return (venv / STAMP_NAME).read_text(encoding="utf-8") != _stamp_text()
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


def _pip_install(py: Path) -> bool:
    cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check"] + REQUIREMENTS
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # visible progress under pythonw
    print("Installing SyncVR dependencies (first run, ~100 MB)...")
    try:
        return subprocess.call(cmd, **kwargs) == 0
    except OSError as exc:
        print("pip failed to start: %s" % exc)
        return False


def ensure_venv(venv: Path = VENV_DIR) -> Optional[Path]:
    """Return the venv interpreter ready to use, or None to fall back to the current Python."""
    venv = Path(venv)
    if needs_install(venv):
        broken = venv.exists() and not venv_python(venv, gui_exe=False).exists()
        if not venv_python(venv, gui_exe=False).exists() or broken:
            if not _create_venv(venv):
                return None
        if not _pip_install(venv_python(venv, gui_exe=False)):
            print("Dependency install failed; will retry next start.")
            return None
        try:
            (venv / STAMP_NAME).write_text(_stamp_text(), encoding="utf-8")  # only after pip succeeded
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
    return subprocess.call([str(py), "-m", "syncvr"] + argv, cwd=str(SERVER_DIR))


if __name__ == "__main__":
    sys.exit(main())
