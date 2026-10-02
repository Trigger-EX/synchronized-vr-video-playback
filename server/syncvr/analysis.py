"""Background analysis of library videos: ffprobe results and SHA-256 checksums.

Both are slow on big libraries, so they run on worker threads and never on the
event loop. Results are cached on disk by (file name, size, mtime), so a restart
does not redo them and an edited file is picked up again automatically.
"""

import hashlib
import json
import logging
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from .limits import GO_LIMITS, GoLimits, check_video, ffprobe_missing_issue
from .mp4 import has_moov_at_front
from .store import StateStore

log = logging.getLogger(__name__)

CACHE_FILE = "content_cache.json"
PROBE_VERSION = 1  # bump when the shape or meaning of the probe summary changes
MOV_EXTENSIONS = {".mp4", ".m4v", ".mov"}
FFPROBE_TIMEOUT_S = 60
HASH_CHUNK = 1024 * 1024


class ProbeError(Exception):
    pass


# ---------------------------------------------------------------- ffprobe

def _run_ffprobe(ffprobe: str, args: List[str]) -> dict:
    cmd = [ffprobe, "-v", "error", "-of", "json"] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFPROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ProbeError("timed out")
    except OSError as exc:
        raise ProbeError(str(exc))
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError(message[-1] if message else f"exit code {proc.returncode}")
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise ProbeError("unreadable ffprobe output")


def _number(value, cast=float) -> Optional[float]:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _frame_rate(stream: dict) -> Optional[float]:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key) or "").partition("/")
        n, d = _number(num), _number(den or 1)
        if n and d:
            return round(n / d, 3)
    return None


def _level_text(codec: str, level) -> Optional[str]:
    level = _number(level, int)
    if not level or level <= 0:
        return None
    if codec == "h264":
        return f"{level / 10:g}"
    if codec == "hevc":
        return f"{level / 30:g}"
    return None


def keyframe_stats(packets: List[dict], fps: Optional[float]) -> Optional[dict]:
    """Max and average distance between keyframes, from ffprobe packet entries
    (``pts_time`` and ``flags``). With a single keyframe in the sampled window the
    result is a lower bound."""
    times, keys = [], []
    for p in packets:
        t = _number(p.get("pts_time"))
        if t is None:
            t = _number(p.get("dts_time"))
        if t is None:
            continue
        times.append(t)
        if "K" in (p.get("flags") or ""):
            keys.append(t)
    if not keys:
        return None
    keys.sort()
    if len(keys) >= 2:
        gaps = [b - a for a, b in zip(keys, keys[1:])]
        return {"max": round(max(gaps), 3), "avg": round(sum(gaps) / len(gaps), 3), "lower_bound": False}
    span = max(times) - min(times) + (1.0 / fps if fps else 0.0)
    return {"max": round(span, 3), "avg": round(span, 3), "lower_bound": True}


def summarize_probe(info: dict, packets: Optional[List[dict]], faststart: Optional[bool]) -> dict:
    """Boil ffprobe's ``-show_format -show_streams`` JSON (plus sampled packets and the
    moov position) down to the few fields the checks and the dashboard use."""
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = {
        "error": None,
        "container": fmt.get("format_name"),
        "duration": _number(fmt.get("duration")),
        "bit_rate": _number(fmt.get("bit_rate"), int),
        "faststart": faststart,
        "video": None,
        "audio": None,
        "keyframes": None,
    }
    if v:
        codec = (v.get("codec_name") or "").lower()
        fps = _frame_rate(v)
        out["video"] = {
            "codec": codec,
            "profile": v.get("profile"),
            "level": _number(v.get("level"), int),
            "level_text": _level_text(codec, v.get("level")),
            "width": _number(v.get("width"), int),
            "height": _number(v.get("height"), int),
            "pix_fmt": v.get("pix_fmt"),
            "fps": fps,
            "bit_rate": _number(v.get("bit_rate"), int),
        }
        if packets:
            out["keyframes"] = keyframe_stats(packets, fps)
    if a:
        out["audio"] = {"codec": (a.get("codec_name") or "").lower(), "channels": _number(a.get("channels"), int)}
    return out


def probe_file(ffprobe: str, path: Path, sample_s: float = GO_LIMITS.keyframe_sample_s) -> dict:
    """Run ffprobe on one file and return the summary. Never raises: a file ffprobe
    cannot read yields ``{"error": "..."}``."""
    try:
        info = _run_ffprobe(ffprobe, ["-show_format", "-show_streams", str(path)])
        packets = None
        if any(s.get("codec_type") == "video" for s in info.get("streams") or []):
            try:
                data = _run_ffprobe(ffprobe, ["-select_streams", "v:0", "-read_intervals", f"%+{sample_s:g}",
                                              "-show_entries", "packet=pts_time,dts_time,flags", str(path)])
                packets = data.get("packets") or []
            except ProbeError as exc:
                log.warning("keyframe scan of %s failed: %s", path.name, exc)
        faststart = None
        if path.suffix.lower() in MOV_EXTENSIONS:
            try:
                faststart = has_moov_at_front(path)
            except OSError:
                pass
        return summarize_probe(info, packets, faststart)
    except ProbeError as exc:
        return {"error": str(exc)}


# --------------------------------------------------------------- checksums

def sha256_file(path: Path, should_stop: Callable[[], bool] = lambda: False) -> Optional[str]:
    """Hex SHA-256 of a file, or None if ``should_stop`` asked to give up part-way."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if should_stop():
                return None
    return h.hexdigest()


# ---------------------------------------------------------------- analyzer

class ContentAnalyzer:
    """Probe and checksum results for the files in the content folder."""

    def __init__(self, cache_path: Optional[Path] = None, ffprobe: Optional[str] = "auto",
                 limits: GoLimits = GO_LIMITS, probe_workers: int = 2):
        self.limits = limits
        self.ffprobe = shutil.which("ffprobe") if ffprobe == "auto" else ffprobe
        self.store = StateStore(cache_path) if cache_path else None
        self.on_update: Optional[Callable[[], None]] = None  # called (from a worker thread) after new results
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._files: Dict[str, dict] = {}
        self._pending: Set[tuple] = set()
        self._futures: List[Future] = []
        self._closed = False
        self._probe_pool = ThreadPoolExecutor(max_workers=probe_workers, thread_name_prefix="syncvr-probe")
        self._hash_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="syncvr-hash")
        if self.store:
            files = self.store.load().get("files")
            if isinstance(files, dict):
                self._files = {k: v for k, v in files.items() if isinstance(v, dict)}

    @property
    def available(self) -> bool:
        return self.ffprobe is not None

    # ------------------------------------------------------------ lookups

    def _entry(self, name: str, size: int, mtime: float) -> dict:
        e = self._files.get(name)
        if e and e.get("size") == size and e.get("mtime") == mtime:
            return e
        return {}

    def probe_of(self, name: str, size: int, mtime: float) -> Optional[dict]:
        with self._lock:
            e = self._entry(name, size, mtime)
            return e.get("probe") if e.get("probe_version") == PROBE_VERSION else None

    def sha256_of(self, name: str, size: int, mtime: float) -> Optional[str]:
        with self._lock:
            return self._entry(name, size, mtime).get("sha256")

    def issues_of(self, name: str, size: int, mtime: float) -> List[dict]:
        if not self.available:
            return [ffprobe_missing_issue()]
        return check_video(self.probe_of(name, size, mtime), self.limits)

    def analysis_state(self, name: str, size: int, mtime: float) -> str:
        if not self.available:
            return "unavailable"
        return "done" if self.probe_of(name, size, mtime) is not None else "pending"

    # ------------------------------------------------------------ scheduling

    def request(self, name: str, path: Path, size: int, mtime: float) -> None:
        """Make sure this file version gets probed and hashed. Cheap to call repeatedly."""
        if self._closed:
            return
        with self._lock:
            have = self._entry(name, size, mtime)
            need_probe = self.available and have.get("probe_version") != PROBE_VERSION
            need_hash = not have.get("sha256")
            jobs = []
            for kind, needed in (("probe", need_probe), ("sha256", need_hash)):
                key = (kind, name, size, mtime)
                if needed and key not in self._pending:
                    self._pending.add(key)
                    jobs.append(key)
        for key in jobs:
            pool, fn = (self._probe_pool, self._do_probe) if key[0] == "probe" else (self._hash_pool, self._do_hash)
            self._futures.append(pool.submit(fn, key, path))
        self._futures = [f for f in self._futures if not f.done()]

    def prune(self, names) -> None:
        """Forget files that left the content folder."""
        keep = set(names)
        with self._lock:
            stale = [n for n in self._files if n not in keep]
            for n in stale:
                del self._files[n]
        if stale:
            self._save()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until all queued work is finished (used by tests). True if it all finished."""
        while True:
            futures = [f for f in self._futures if not f.done()]
            if not futures:
                return True
            done, not_done = wait_futures(futures, timeout=timeout)
            if not_done:
                return False

    def close(self) -> None:
        self._closed = True
        self._probe_pool.shutdown(wait=False)
        self._hash_pool.shutdown(wait=False)

    # ---------------------------------------------------------- worker side

    def _store(self, key: tuple, **fields) -> None:
        _, name, size, mtime = key
        with self._lock:
            e = self._files.get(name)
            if not e or e.get("size") != size or e.get("mtime") != mtime:
                e = self._files[name] = {"size": size, "mtime": mtime}
            e.update(fields)
        self._save()
        callback = self.on_update
        if callback:
            try:
                callback()
            except Exception:
                log.exception("analysis update callback failed")

    def _unchanged(self, key: tuple, path: Path) -> bool:
        _, _, size, mtime = key
        try:
            st = path.stat()
        except OSError:
            return False
        return st.st_size == size and st.st_mtime == mtime

    def _do_probe(self, key: tuple, path: Path) -> None:
        try:
            if self._closed:
                return
            probe = probe_file(self.ffprobe, path, self.limits.keyframe_sample_s)
            if self._unchanged(key, path):
                self._store(key, probe=probe, probe_version=PROBE_VERSION)
        except Exception:
            log.exception("probing %s failed", path.name)
        finally:
            with self._lock:
                self._pending.discard(key)

    def _do_hash(self, key: tuple, path: Path) -> None:
        try:
            if self._closed:
                return
            digest = sha256_file(path, lambda: self._closed)
            if digest and self._unchanged(key, path):
                self._store(key, sha256=digest)
        except OSError as exc:
            log.warning("could not checksum %s: %s", path.name, exc)
        except Exception:
            log.exception("checksum of %s failed", path.name)
        finally:
            with self._lock:
                self._pending.discard(key)

    def _save(self) -> None:
        if not self.store:
            return
        with self._lock:
            data = {"version": 1, "files": json.loads(json.dumps(self._files))}
        with self._save_lock:
            try:
                self.store.save(data)
            except OSError as exc:
                log.error("could not save %s: %s", self.store.path, exc)
