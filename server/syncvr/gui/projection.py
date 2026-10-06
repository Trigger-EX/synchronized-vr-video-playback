"""Pure projection math for the laptop view (no Qt, no GL).

Naming follows library.py / ViewSpec.kt: projection '360' | '180' | 'flat',
stereo 'mono' | 'tb' | 'sbs'; unknown values fall back to 360 / mono.
Texture coords are normalized with the origin at the top-left of the frame.
World axes: x right, y up, z forward (yaw 0 / pitch 0 looks at +z, the centre
of the equirect image). Positive yaw looks right, positive pitch looks up.
"""
from __future__ import annotations

import math

Rect = tuple[float, float, float, float]  # (u0, v0, u1, v1)
Mat3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vec3 = tuple[float, float, float]

FOV_MIN, FOV_MAX, FOV_DEFAULT = 30.0, 110.0, 80.0


def norm_projection(projection: str | None) -> str:
    p = (projection or "").strip().lower()
    return p if p in ("180", "flat") else "360"


def norm_stereo(stereo: str | None) -> str:
    s = (stereo or "").strip().lower()
    return s if s in ("tb", "sbs") else "mono"


def clamp_fov(fov: float) -> float:
    return max(FOV_MIN, min(FOV_MAX, float(fov)))


def left_eye_rect(stereo: str | None) -> Rect:
    """Crop of the left eye: tb -> top half, sbs -> left half, mono -> whole frame."""
    s = norm_stereo(stereo)
    if s == "tb":
        return (0.0, 0.0, 1.0, 0.5)
    if s == "sbs":
        return (0.0, 0.0, 0.5, 1.0)
    return (0.0, 0.0, 1.0, 1.0)


def eye_aspect(frame_w: int, frame_h: int, stereo: str | None) -> float:
    """Width/height of a single eye image for a frame of the given size."""
    s = norm_stereo(stereo)
    w, h = float(max(frame_w, 1)), float(max(frame_h, 1))
    if s == "tb":
        h /= 2
    elif s == "sbs":
        w /= 2
    return w / h


def _mul(a: Mat3, b: Mat3) -> Mat3:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def view_matrix(yaw: float, pitch: float, roll: float = 0.0, rotation: float = 0.0) -> Mat3:
    """Rotation taking camera-space rays (x right, y up, z forward) to world space.

    Angles in degrees. `rotation` is the per-video yaw offset (Video.rotation):
    the content is turned by +rotation, i.e. the view yaws by -rotation.
    """
    y = math.radians(yaw - rotation)
    p = math.radians(pitch)
    r = math.radians(roll)
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    ry: Mat3 = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    rp: Mat3 = ((1.0, 0.0, 0.0), (0.0, cp, sp), (0.0, -sp, cp))  # +pitch looks up
    rr: Mat3 = ((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0))
    return _mul(ry, _mul(rp, rr))


def apply(m: Mat3, v: Vec3) -> Vec3:
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


def ray_dir(x_ndc: float, y_ndc: float, fov: float, aspect: float) -> Vec3:
    """Camera-space unit ray for NDC (-1..1, y up); `fov` is the vertical fov in degrees."""
    t = math.tan(math.radians(fov) / 2)
    x, y, z = x_ndc * t * aspect, y_ndc * t, 1.0
    n = math.sqrt(x * x + y * y + z * z)
    return (x / n, y / n, 1.0 / n)


def sample_uv(direction: Vec3, projection: str | None) -> tuple[float, float] | None:
    """Equirect (u, v) in the eye image for a world direction, v=0 at the top.

    360 spans longitude -180..180; 180 spans -90..90 (front half) and returns
    None outside it. 'flat' has no sphere mapping, use flat_rect instead.
    """
    x, y, z = direction
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    lon = math.atan2(x, z)
    lat = math.asin(max(-1.0, min(1.0, y / n)))
    if norm_projection(projection) == "180":
        if abs(lon) > math.pi / 2:
            return None
        u = lon / math.pi + 0.5
    else:
        u = lon / (2 * math.pi) + 0.5
    return (u, 0.5 - lat / math.pi)


def flat_rect(eye_aspect_ratio: float, widget_w: float, widget_h: float) -> tuple[float, float, float, float]:
    """Letterboxed target rect (x, y, w, h) in widget pixels, centred, keeping aspect."""
    ww, wh = max(float(widget_w), 1.0), max(float(widget_h), 1.0)
    a = eye_aspect_ratio if eye_aspect_ratio > 0 else 16 / 9
    w, h = (ww, ww / a) if ww / wh <= a else (wh * a, wh)
    return ((ww - w) / 2, (wh - h) / 2, w, h)
