"""The content library: video files in a folder, plus per-video playback metadata."""

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .analysis import ContentAnalyzer
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
    # Where projection/stereo came from: operator, filename, resolution or default.
    format_source: str = "default"

    def meta(self) -> dict:
        return {k: getattr(self, k) for k in EDITABLE_FIELDS}

    def detect_format(self, stored: Optional[dict] = None) -> None:
        """Set projection/stereo and format_source.
        Precedence per field: operator value > filename > resolution > default."""
        stored = stored or {}
        by_name = guess_format(self.name)
        by_res = guess_from_resolution(self.width, self.height)
        have_res = bool(self.width and self.height)
        sources = set()
        for key in ("projection", "stereo"):
            if key in stored:
                setattr(self, key, stored[key])
                sources.add("operator")
            elif key in by_name:
                setattr(self, key, by_name[key])
                sources.add("filename")
            else:
                setattr(self, key, by_res[key])
                sources.add("resolution" if have_res else "default")
        for src in ("operator", "filename", "resolution"):
            if src in sources:
                self.format_source = src
                return
        self.format_source = "default"

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
    e.g. ``concert_360_TB.mp4``, ``trailer-180-sbs.mp4``, ``intro_flat.mp4``.
    Only fields the name actually mentions are returned."""
    tokens = set(re.split(r"[\s_\-.\[\]()]+", Path(filename).stem.lower()))
    out: Dict[str, str] = {}
    if "180" in tokens or "vr180" in tokens:
        out["projection"] = "180"
    elif "360" in tokens:
        out["projection"] = "360"
    elif tokens & {"flat", "2d", "screen", "rect"}:
        out["projection"] = "flat"
    if tokens & {"tb", "ou", "overunder", "topbottom", "3dv", "stereo"}:
        out["stereo"] = "tb"
    elif tokens & {"sbs", "lr", "sidebyside", "3dh"}:
        out["stereo"] = "sbs"
    elif tokens & {"mono", "2d"}:
        out["stereo"] = "mono"
    return out


def guess_from_resolution(width: Optional[int], height: Optional[int]) -> Dict[str, str]:
    """Infer layout from the frame aspect ratio. Falls back to 360 mono."""
    if not width or not height:
        return {"projection": "360", "stereo": "mono"}
    ratio = width / height
    if abs(ratio - 2.0) < 0.1:
        return {"projection": "360", "stereo": "mono"}
    if abs(ratio - 1.0) < 0.1:
        return {"projection": "360", "stereo": "tb"}
    if abs(ratio - 4.0) < 0.2:
        return {"projection": "360", "stereo": "sbs"}
    if 1.6 <= ratio <= 1.9:
        return {"projection": "flat", "stereo": "mono"}
    if abs(ratio - 3.55) < 0.15:
        return {"projection": "flat", "stereo": "sbs"}
    return {"projection": "360", "stereo": "mono"}


class Library:
    def __init__(self, root: Path, metadata: Optional[Dict[str, dict]] = None,
                 analyzer: Optional[ContentAnalyzer] = None):
        self.root = Path(root)
        self.analyzer = analyzer
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
            video.title = path.stem
            stored = {k: v for k, v in self.metadata.get(path.name, {}).items()
                      if k in EDITABLE_FIELDS}
            for key, value in stored.items():
                if key not in ("projection", "stereo"):
                    setattr(video, key, value)
            video.detect_format(stored)
            found[path.name] = video
        changed = found.keys() != self.videos.keys() or any(
            found[n] is not self.videos.get(n) for n in found
        )
        self.videos = found
        if self.analyzer:
            self.analyzer.prune(found)
            for v in found.values():
                self.analyzer.request(v.name, self.root / v.name, v.size, v.mtime)
        return changed

    def sha256_of(self, name: str) -> Optional[str]:
        """SHA-256 of a library file, or None while it has not been computed yet."""
        v = self.videos.get(name)
        if v is None or self.analyzer is None:
            return None
        return self.analyzer.sha256_of(v.name, v.size, v.mtime)

    def get(self, name: str) -> Optional[Video]:
        return self.videos.get(name)

    def path_of(self, name: str) -> Optional[Path]:
        return self.root / name if name in self.videos else None

    def update_meta(self, name: str, changes: dict) -> Video:
        video = self.videos.get(name)
        if video is None:
            raise KeyError(name)
        clean = {}
        auto = set()
        for key, value in changes.items():
            if key not in EDITABLE_FIELDS:
                raise ValueError(f"cannot edit {key}")
            if key in ("projection", "stereo") and value == "auto":
                auto.add(key)
                continue
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
        stored = self.metadata.setdefault(name, {})
        for key, value in clean.items():
            if getattr(video, key) != value or key in stored:
                stored[key] = value
        for key in auto:
            stored.pop(key, None)
        for key, value in clean.items():
            setattr(video, key, value)
        video.detect_format({k: v for k, v in stored.items() if k in ("projection", "stereo")})
        if not stored:
            self.metadata.pop(name, None)
        return video

    def to_json(self) -> List[dict]:
        out = []
        for v in self.videos.values():
            d = asdict(v)
            d.pop("mtime")
            if self.analyzer:
                a = self.analyzer
                d["probe"] = a.probe_of(v.name, v.size, v.mtime)
                d["issues"] = a.issues_of(v.name, v.size, v.mtime)
                d["analysis"] = a.analysis_state(v.name, v.size, v.mtime)
                d["sha256"] = a.sha256_of(v.name, v.size, v.mtime)
            out.append(d)
        return out
