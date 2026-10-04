"""Where the launcher keeps its data and content.

* From source: ``server/data`` and ``server/content`` (the developer layout).
* Frozen (PyInstaller): a per-user data folder, content in ``<data>/content``.
* Frozen with a file named ``portable`` next to the executable: ``data`` and ``content`` beside it.

Pure functions of their arguments (no Qt, no aiohttp) so they are easy to test.
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

APP_DIR_NAME = "SyncVR"
PORTABLE_MARKER = "portable"
SOURCE_DIR = Path(__file__).resolve().parent.parent  # server/


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    return Path(sys.executable).resolve().parent


def user_data_dir(platform: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                  home: Optional[Path] = None) -> Path:
    """Per-user application data folder for this OS."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP_DIR_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base and os.path.isabs(base) else home / ".local" / "share") / APP_DIR_NAME


def is_portable(exe_dir: Path) -> bool:
    return (Path(exe_dir) / PORTABLE_MARKER).exists()


def data_dir(frozen: Optional[bool] = None, exe_dir: Optional[Path] = None, platform: Optional[str] = None,
             env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> Path:
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return SOURCE_DIR / "data"
    exe_dir = executable_dir() if exe_dir is None else Path(exe_dir)
    if is_portable(exe_dir):
        return exe_dir / "data"
    return user_data_dir(platform, env, home)


def content_dir(data: Path, frozen: Optional[bool] = None, exe_dir: Optional[Path] = None) -> Path:
    """Default content folder for a given data folder."""
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return SOURCE_DIR / "content"
    exe_dir = executable_dir() if exe_dir is None else Path(exe_dir)
    if is_portable(exe_dir):
        return exe_dir / "content"
    return Path(data) / "content"
