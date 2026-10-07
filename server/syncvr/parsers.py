"""Pure parsers over ``dumpsys`` text from a Go, shared by diagnostics and (later) watchdogs.

Every function takes the raw text and returns a value or None. They never raise: missing, empty,
non-string or garbled input is simply "unknown" (None).
"""

import re
from typing import Optional

THERMAL_NAMES = {0: "none", 1: "light", 2: "moderate", 3: "severe", 4: "critical", 5: "emergency", 6: "shutdown"}

_WAKEFULNESS = re.compile(r"mWakefulness=(\w+)")
_DISPLAY_STATE = (re.compile(r"Display State=(\w+)"), re.compile(r"mScreenState=(\w+)"),
                  re.compile(r"\bstate (ON|OFF|DOZE\w*|UNKNOWN|VR|ON_SUSPEND)\b"))
_FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^\s}]+)")
_FOCUS_TITLE = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^}]*)\}")
_FOCUSED_APP = re.compile(r"mFocusedApp=\S*ActivityRecord\{\S+ \S+ ([^\s}]+)")
_THERMAL = re.compile(r"Thermal Status:\s*(-?\d+)")
_TEMP = re.compile(r"^\s*temperature:\s*(-?\d+)\s*$", re.MULTILINE)


def _text(text) -> str:
    return text if isinstance(text, str) else ""


def wakefulness(text) -> Optional[str]:
    """``Awake``, ``Asleep``, ``Dozing`` or ``Dreaming`` from ``dumpsys power``."""
    match = _WAKEFULNESS.search(_text(text))
    return match.group(1) if match else None


def display_state(text) -> Optional[str]:
    """``ON``, ``OFF``, ``DOZE`` ... from ``dumpsys display``."""
    body = _text(text)
    for pattern in _DISPLAY_STATE:
        match = pattern.search(body)
        if match:
            return match.group(1).upper()
    return None


def focused_window(text) -> Optional[str]:
    """Component of ``mCurrentFocus`` (``pkg/Activity``) from ``dumpsys window windows``; None when no focus."""
    match = _FOCUS.search(_text(text))
    return match.group(1) if match else None


def focus_title(text) -> Optional[str]:
    """Whole title of ``mCurrentFocus`` (``pkg/Activity``, or ``Application Error: pkg`` for a dialog)."""
    match = _FOCUS_TITLE.search(_text(text))
    return match.group(1).strip() or None if match else None


def focused_app(text) -> Optional[str]:
    """Component of ``mFocusedApp`` (``pkg/.Activity``); None when null or absent."""
    match = _FOCUSED_APP.search(_text(text))
    return match.group(1) if match else None


def package_of(component) -> Optional[str]:
    """``com.x/.Main`` -> ``com.x``."""
    if not isinstance(component, str) or not component:
        return None
    return component.split("/", 1)[0] or None


def thermal_status(text) -> Optional[int]:
    """Android thermal status 0 (none) .. 6 (shutdown) from ``dumpsys thermalservice``."""
    match = _THERMAL.search(_text(text))
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 6 else None


def battery_temp_c(text) -> Optional[float]:
    """Battery temperature in degrees C (``dumpsys battery`` reports tenths)."""
    match = _TEMP.search(_text(text))
    if not match:
        return None
    celsius = int(match.group(1)) / 10.0
    return celsius if -40.0 <= celsius <= 150.0 else None
