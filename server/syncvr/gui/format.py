"""Display helpers shared by the Qt widgets; ported from the web dashboard. Qt-free."""

import math
from typing import List, Optional

DRIFT_GOOD_MS = 25
DRIFT_MEH_MS = 80
_UNITS = ("B", "KB", "MB", "GB", "TB")
_ISSUE_LEVELS = (("error", ("error", "errors")), ("warn", ("warning", "warnings")), ("info", ("note", "notes")))


def fmt_time(sec) -> str:
    if sec is None or not isinstance(sec, (int, float)) or not math.isfinite(sec):
        return "–"
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d" % (h, m) if h else "%d" % m) + ":%02d" % s


def fmt_bytes(n) -> str:
    if n is None:
        return "–"
    i = 0
    while n >= 1024 and i < len(_UNITS) - 1:
        n /= 1024
        i += 1
    return ("%.1f %s" % (n, _UNITS[i])) if i else "%d %s" % (n, _UNITS[0])


def drift_class(ms) -> str:
    ms = abs(ms)
    return "good" if ms < DRIFT_GOOD_MS else "meh" if ms < DRIFT_MEH_MS else "poor"


def position_of(desired: Optional[dict], t: float) -> Optional[float]:
    """Playback position of a desired-state dict at server time t; None unless playing or paused."""
    if not desired or desired.get("mode") not in ("playing", "paused"):
        return None
    pos = desired["pos"]
    if desired["mode"] == "playing":
        pos += max(0.0, t - desired["at"])
        dur = desired.get("duration")
        if dur:
            pos = pos % dur if desired.get("loop") else min(pos, dur)
    return pos


def state_name(dev: dict) -> str:
    return (dev.get("status") or {}).get("state") or "connecting" if dev.get("online") else "offline"


def battery_class(pct: int) -> str:
    return "poor" if pct < 20 else "meh" if pct < 40 else ""


def rtt_class(ms) -> str:
    return "meh" if ms > 50 else ""


def wifi_class(dbm) -> str:
    return "poor" if dbm < -75 else "meh" if dbm < -65 else ""


def temp_class(c) -> str:
    return "poor" if c > 42 else ""


def content_summary(dev: dict, library: List[dict]) -> str:
    if not library:
        return ""
    inv = dev.get("inventory") or {}
    have = sum(1 for v in library if inv.get(v["name"]) == v["size"])
    return "%d/%d videos" % (have, len(library))


def device_info(dev: dict, library: List[dict]) -> List[tuple]:
    """(text, severity) pairs for the info line of an online headset card."""
    st = dev.get("status") or {}
    info = []
    if st.get("battery") is not None and st["battery"] >= 0:
        pct = int(round(st["battery"] * 100))
        info.append(("battery %d%%%s" % (pct, " (charging)" if st.get("charging") else ""), battery_class(pct)))
    if st.get("temp_c") is not None and st["temp_c"] > 0:
        info.append(("%.0f°C" % st["temp_c"], temp_class(st["temp_c"])))
    if st.get("worn") is not None:
        info.append(("on head" if st["worn"] else "not worn", ""))
    if st.get("rtt_ms") is not None:
        info.append(("rtt %.0f ms" % st["rtt_ms"], rtt_class(st["rtt_ms"])))
    if st.get("wifi_rssi"):
        info.append(("wifi %d dBm" % st["wifi_rssi"], wifi_class(st["wifi_rssi"])))
    if st.get("storage_free") is not None and 0 <= st["storage_free"] < 2 * 1024 ** 3:
        info.append(("%s free" % fmt_bytes(st["storage_free"]), "meh"))
    summary = content_summary(dev, library)
    if summary:
        info.append((summary, ""))
    return info


def fleet_stats(devices: List[dict]) -> dict:
    """Online/playing counts and worst absolute drift (None if nobody reports drift)."""
    online = [d for d in devices if d.get("online")]
    playing = [d for d in online if (d.get("status") or {}).get("state") == "playing"]
    drifts = [abs(d["status"]["drift_ms"]) for d in playing if d["status"].get("drift_ms") is not None]
    return {"total": len(devices), "online": len(online), "playing": len(playing),
            "worst_drift": max(drifts) if drifts else None}


def sort_devices(devices: List[dict]) -> List[dict]:
    def key(d):
        parts = [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in _split_digits(d.get("label") or "")]
        return (not d.get("online"), parts)
    return sorted(devices, key=key)


def _split_digits(text: str) -> List[str]:
    out, cur = [], ""
    for ch in text:
        if cur and ch.isdigit() != cur[-1].isdigit():
            out.append(cur)
            cur = ""
        cur += ch
    return out + [cur] if cur else out


def groups_of(devices: List[dict]) -> List[str]:
    return sorted({d["group"] for d in devices if d.get("group")})


def probe_summary(video: dict) -> str:
    p = video.get("probe")
    if not p or not p.get("video"):
        return "%d×%d" % (video["width"], video["height"]) if video.get("width") else ""
    x = p["video"]
    bits = ["%s×%s" % (x.get("width"), x.get("height"))]
    bits.append(" ".join(b for b in (x.get("codec"), x.get("profile"),
                                     "L" + x["level_text"] if x.get("level_text") else "") if b))
    if x.get("fps"):
        bits.append("%g fps" % round(x["fps"], 2))
    if p.get("bit_rate"):
        bits.append("%.1f Mb/s" % (p["bit_rate"] / 1e6))
    kf = p.get("keyframes")
    if kf and not kf.get("lower_bound"):
        bits.append("GOP %g s" % round(kf["max"], 1))
    return " · ".join(bits)


def issues_summary(video: dict) -> tuple:
    """(text, severity) for the library 'checks' column; severity is ok/info/warn/error/pending or '' if not analysed."""
    if not video.get("analysis"):
        return "", ""
    issues = video.get("issues") or []
    if video["analysis"] == "pending":
        return "checking…", "pending"
    if not issues:
        return "OK", "ok"
    for level, (one, many) in _ISSUE_LEVELS:
        n = sum(1 for i in issues if i.get("level") == level)
        if n:
            return "%d %s" % (n, one if n == 1 else many), level
    return "OK", "ok"
