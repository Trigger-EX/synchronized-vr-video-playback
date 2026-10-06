"""Lower-resolution "laptop copy" of a video for the operator's local playback. Qt-free.

The proxy keeps only the left eye (mono) at the pixel density the Go can show: GO_PPD px/deg
over the projection's angular span. It never upscales."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .projection import GO_PPD, norm_projection, norm_stereo

CRF = 21
FLAT_MAX_W = 1920
CACHE_DIR = ".laptop"
SPAN_DEG = {"360": 360.0, "180": 180.0}


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def proxy_plan(width, height, projection, stereo) -> Optional[tuple]:
    """(out_w, out_h, crop_filter) for the laptop copy, or None when the original should be used
    (unknown size, or mono and already at/below the target size). crop_filter is '' for mono."""
    if not width or not height or width < 2 or height < 2:
        return None
    proj, st = norm_projection(projection), norm_stereo(stereo)
    ew, eh = int(width), int(height)
    if st == "sbs":
        ew //= 2
    elif st == "tb":
        eh //= 2
    ew, eh = _even(ew), _even(eh)
    target_w = SPAN_DEG.get(proj) and round(SPAN_DEG[proj] * GO_PPD) or FLAT_MAX_W
    if target_w >= ew:
        if st == "mono":
            return None
        out_w, out_h = ew, eh  # no upscale: keep the eye's own size
    else:
        out_w = _even(target_w)
        out_h = _even(round(out_w * eh / ew))
    crop = "" if st == "mono" else "crop=%d:%d:0:0" % (ew, eh)
    return out_w, out_h, crop


def ffmpeg_args(src, dst, plan, fps, audio_codec: Optional[str] = None, ffmpeg: str = "ffmpeg") -> list:
    out_w, out_h, crop = plan
    gop = max(1, int(round(fps or 30)))
    vf = ",".join(f for f in (crop, "scale=%d:%d" % (out_w, out_h)) if f)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf, "-c:v", "libx264", "-crf", str(CRF), "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-g", str(gop), "-movflags", "+faststart"]
    args += ["-c:a", "copy"] if (audio_codec or "").lower() == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    return args + ["-f", "mp4", "-progress", "pipe:1", "-nostats", str(dst)]


def cache_path(content_dir, name: str, size, mtime, plan) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(name)[0])
    return Path(content_dir) / CACHE_DIR / ("%s-%s-%d-crf%d-%dx%d.mp4" % (
        stem, int(size or 0), int(mtime or 0), CRF, plan[0], plan[1]))


def stale_proxies(content_dir, name: str) -> list:
    """Cached proxies of `name` (any size/mtime), e.g. to delete when the original is removed."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(name)[0])
    d = Path(content_dir) / CACHE_DIR
    rx = re.compile(r"^%s-\d+-\d+-crf\d+-\d+x\d+\.mp4(\.part)?$" % re.escape(stem))
    return [p for p in d.iterdir() if rx.match(p.name)] if d.is_dir() else []


def parse_progress(line: str, duration: Optional[float]) -> Optional[float]:
    """Percent (0-100) from an ffmpeg -progress line, or None."""
    m = re.match(r"out_time_(?:us|ms)=(\d+)", line.strip())
    if not m or not duration or duration <= 0:
        return None
    return max(0.0, min(100.0, int(m.group(1)) / 1e6 / duration * 100.0))


class ProxyJob:
    """Runs ffmpeg in a thread. on_progress(pct) and on_done(dst_or_None, error) are called from
    that thread. Output goes to dst + '.part' and is renamed on success."""

    def __init__(self, args, dst, duration, on_progress: Callable, on_done: Callable, popen=subprocess.Popen):
        self.args, self.dst, self.duration = list(args), Path(dst), duration
        self._progress, self._done, self._popen = on_progress, on_done, popen
        self._proc = None
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def part(self) -> Path:
        return self.dst.with_name(self.dst.name + ".part")

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def _run(self) -> None:
        err = None
        try:
            self.dst.parent.mkdir(parents=True, exist_ok=True)
            args = self.args[:-1] + [str(self.part)]
            self._proc = self._popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in self._proc.stdout:
                pct = parse_progress(line, self.duration)
                if pct is not None and not self._cancelled:
                    self._progress(pct)
            rc = self._proc.wait()
            if self._cancelled:
                err = "cancelled"
            elif rc != 0:
                err = "ffmpeg exited with code %s" % rc
            else:
                os.replace(self.part, self.dst)
        except FileNotFoundError:
            err = "ffmpeg not found"
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
        if err:
            try:
                self.part.unlink()
            except OSError:
                pass
        if not self._cancelled:
            self._done(None if err else str(self.dst), err)


def default_job_factory(args, dst, duration, on_progress, on_done):
    return ProxyJob(args, dst, duration, on_progress, on_done)
