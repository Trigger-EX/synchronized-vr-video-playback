"""What the Oculus Go can play, and the checks that compare a probed video against it.

All the numbers live in ``GoLimits`` so they are easy to refine after the first
hardware checkpoint. The defaults are deliberately conservative. ``check_video``
takes the plain dict produced by ``analysis.summarize_probe`` and returns a list
of issues; it never touches the disk or ffmpeg, so it is easy to unit-test.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

ERROR, WARN, INFO = "error", "warn", "info"
_LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass(frozen=True)
class GoLimits:
    # Largest frame sizes the hardware decoder takes, per codec. A video fits if
    # it fits inside any one of the listed (width, height) boxes.
    max_resolutions: Dict[str, Tuple[Tuple[int, int], ...]] = field(default_factory=lambda: {
        "h264": ((3840, 2160), (4096, 2048)),
        "hevc": ((4096, 2048),),
    })
    # Accepted profiles (ffprobe names, compared case-insensitively) and profiles that
    # decode but are a risk.
    ok_profiles: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: {
        "h264": ("baseline", "constrained baseline", "main", "high", "constrained high"),
        "hevc": ("main",),
    })
    warn_profiles: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: {
        "hevc": ("main 10",),
    })
    max_fps: float = 60.0
    max_keyframe_interval_s: float = 2.0
    audio_codecs: Tuple[str, ...] = ("aac", "opus", "mp3")
    # Seconds of packets sampled from the start of the video to measure keyframe spacing.
    keyframe_sample_s: float = 30.0


GO_LIMITS = GoLimits()

CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC"}


def issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def _seconds(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _box(size: Tuple[int, int]) -> str:
    return f"{size[0]}×{size[1]}"


def _fits(width: int, height: int, boxes: Sequence[Tuple[int, int]]) -> bool:
    return any(width <= w and height <= h for w, h in boxes)


def check_video(probe: Optional[dict], limits: GoLimits = GO_LIMITS) -> List[dict]:
    """Compare a probe summary with the Go's limits. Errors come first."""
    if not probe:
        return []
    if probe.get("error"):
        return [issue(ERROR, "probe_failed", f"ffprobe could not read this file: {probe['error']}")]
    video = probe.get("video")
    if not video:
        return [issue(ERROR, "no_video", "No video stream found")]
    out: List[dict] = []

    codec = (video.get("codec") or "").lower()
    name = CODEC_NAMES.get(codec, codec or "unknown")
    width, height = video.get("width") or 0, video.get("height") or 0
    profile = (video.get("profile") or "").strip()

    if codec not in limits.max_resolutions:
        out.append(issue(WARN, "codec", f"{name} video is untested on the Go; H.264 or HEVC is safest"))
    else:
        boxes = limits.max_resolutions[codec]
        if width and height and not _fits(width, height, boxes):
            allowed = " or ".join(_box(b) for b in boxes)
            out.append(issue(WARN, "resolution",
                             f"{width}×{height} is above the Go's {name} decoder limit ({allowed}); it may still play"))
        if profile:
            lowered = profile.lower()
            if lowered in limits.warn_profiles.get(codec, ()):
                out.append(issue(WARN, "profile", f"{name} {profile} may not decode smoothly on the Go"))
            elif lowered not in limits.ok_profiles.get(codec, ()):
                accepted = "/".join(p.title() for p in limits.ok_profiles.get(codec, ()) if "constrained" not in p)
                out.append(issue(ERROR, "profile",
                                 f"{name} {profile} is not supported by the Go's decoder (use {accepted})"))

    fps = video.get("fps")
    if fps and fps > limits.max_fps + 0.01:
        out.append(issue(ERROR, "fps", f"{fps:g} fps is above the Go's limit ({limits.max_fps:g} fps)"))

    kf = probe.get("keyframes")
    if kf and kf.get("max") and kf["max"] > limits.max_keyframe_interval_s:
        if kf.get("lower_bound"):
            message = f"No keyframe for at least {_seconds(kf['max'])} s: seeks will be slow"
        else:
            message = f"Keyframes every {_seconds(kf['max'])} s: seeks will be slow"
        out.append(issue(WARN, "keyframes", message))

    if probe.get("faststart") is False:
        out.append(issue(WARN, "moov_at_end",
                         "The moov atom is at the end of the file: playback starts slowly "
                         "(re-mux with -movflags +faststart)"))

    audio = probe.get("audio")
    if not audio:
        out.append(issue(INFO, "no_audio", "No audio track"))
    elif (audio.get("codec") or "").lower() not in limits.audio_codecs:
        accepted = ", ".join(c.upper() for c in limits.audio_codecs)
        out.append(issue(WARN, "audio_codec",
                         f"Audio codec {audio.get('codec') or 'unknown'} may not play on the Go ({accepted} are safe)"))

    out.sort(key=lambda i: _LEVEL_ORDER[i["level"]])
    return out


def ffprobe_missing_issue() -> dict:
    return issue(INFO, "ffprobe_missing", "ffprobe not found: install ffmpeg to check videos against the Go's limits")


def worst_level(issues: Sequence[dict]) -> Optional[str]:
    return min((i["level"] for i in issues), key=_LEVEL_ORDER.__getitem__, default=None)
