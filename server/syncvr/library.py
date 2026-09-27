"""The content library: video files in a folder, plus per-video playback metadata."""

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .mp4 import read_mp4_info
from .protocol import PROJECTIONS, STEREO_MODES, is_safe_filename

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
EDITABLE_FIELDS = ("title", "projection", "stereo", "rotation", "loop")


@dataclass
class Video:
    name: str
    size: int
    mtime: float
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    title: str = ""
    projection: str = "360"
    stereo: str = "mono"
    rotation: float = 0.0  # yaw offset in degrees applied on the headset
    loop: bool = False

    def meta(self) -> dict:
        return {k: getattr(self, k) for k in EDITABLE_FIELDS}

    def playback_fields(self) -> dict:
        """What a headset needs to know to render this video."""
        return {
            "video": self.name,
            "projection": self.projection,
            "stereo": self.stereo,
            "rotation": self.rotation,
            "duration": self.duration or 0.0,
        }


def guess_format(filename: str) -> Dict[str, str]:
    """Infer projection/stereo layout from common filename conventions,
    e.g. ``concert_360_TB.mp4``, ``trailer-180-sbs.mp4``, ``intro_flat.mp4``."""
    tokens = set(re.split(r"[\s_\-.\[\]()]+", Path(filename).stem.lower()))
    projection = "360"
    if "180" in tokens or "vr180" in tokens:
        projection = "180"
    elif tokens & {"flat", "2d", "screen", "rect"}:
        projection = "flat"
    stereo = "mono"
    if tokens & {"tb", "ou", "overunder", "topbottom", "3dv", "stereo"}:
        stereo = "tb"
    elif tokens & {"sbs", "lr", "sidebyside", "3dh"}:
        stereo = "sbs"
    return {"projection": projection, "stereo": stereo}


class Library:
    def __init__(self, root: Path, metadata: Optional[Dict[str, dict]] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata: Dict[str, dict] = metadata if metadata is not None else {}
        self.videos: Dict[str, Video] = {}

    def scan(self) -> bool:
        """Re-read the content folder. Returns True if anything changed."""
        found: Dict[str, Video] = {}
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if not is_safe_filename(path.name):
                log.warning("skipping %s: unsupported file name", path.name)
                continue
            st = path.stat()
            old = self.videos.get(path.name)
            if old and old.size == st.st_size and old.mtime == st.st_mtime:
                found[path.name] = old
                continue
            video = Video(name=path.name, size=st.st_size, mtime=st.st_mtime)
            try:
                info = read_mp4_info(path)
                video.duration, video.width, video.height = info.duration, info.width, info.height
            except (OSError, ValueError) as exc:
                log.warning("could not read metadata from %s: %s", path.name, exc)
            guessed = guess_format(path.name)
            video.projection, video.stereo = guessed["projection"], guessed["stereo"]
            video.title = path.stem
            for key, value in self.metadata.get(path.name, {}).items():
                if key in EDITABLE_FIELDS:
                    setattr(video, key, value)
            found[path.name] = video
        changed = found.keys() != self.videos.keys() or any(
            found[n] is not self.videos.get(n) for n in found
        )
        self.videos = found
        return changed

    def get(self, name: str) -> Optional[Video]:
        return self.videos.get(name)

    def path_of(self, name: str) -> Optional[Path]:
        return self.root / name if name in self.videos else None

    def update_meta(self, name: str, changes: dict) -> Video:
        video = self.videos.get(name)
        if video is None:
            raise KeyError(name)
        clean = {}
        for key, value in changes.items():
            if key not in EDITABLE_FIELDS:
                raise ValueError(f"cannot edit {key}")
            if key == "projection" and value not in PROJECTIONS:
                raise ValueError(f"projection must be one of {PROJECTIONS}")
            if key == "stereo" and value not in STEREO_MODES:
                raise ValueError(f"stereo must be one of {STEREO_MODES}")
            if key == "rotation":
                value = float(value) % 360.0
            if key == "loop":
                value = bool(value)
            if key == "title":
                value = str(value)[:200]
            clean[key] = value
        for key, value in clean.items():
            setattr(video, key, value)
        self.metadata[name] = video.meta()
        return video

    def to_json(self) -> List[dict]:
        out = []
        for v in self.videos.values():
            d = asdict(v)
            d.pop("mtime")
            out.append(d)
        return out
