"""Watchdogs: unattended checks that fix a headset when it is clearly stuck.

One asyncio task per watchdog (started by ``SyncServer.start``). A cycle is:

    observe (adb on the shared executor) -> decide(history, obs, cfg) -> act (dedicated send executor)

``decide`` is a pure function of the headset's recent history, one observation and the config; it returns
``Action`` values and touches nothing. User regexes are matched inside ``observe`` (on the executor, never on the
event loop) and the result travels in the observation. The framework re-checks ``Controller.brake()`` immediately
before EACH send, once more on the event loop after waiting for a poll slot and a last time on the send worker
right before the adb command. When braked the decision is logged and nothing is sent. A watchdog that is enabled
but not armed is observe-only and never sends. ``armed`` is never persisted, so every restart is observe-only again.

Attempts are recorded BEFORE they run (history entry in a ``finally``, per-headset ``mem`` outside the bounded
history), so a send that raises still counts for cooldowns, back-off and the give-up counter.

No Qt. Uses the controller only through ``devices``, ``intentionally_asleep``, ``fleet``, ``brake()``,
``data_dir`` and ``log_event``; adb runs on executors and returns plain data.
"""

import asyncio
import csv
import hashlib
import logging
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import parsers
from .adbtool import DEFAULT_PACKAGE
from .fleetops import ADB_PORT, ListingFailed, resolve_serials

log = logging.getLogger(__name__)

SEND_KINDS = frozenset({"wake", "back", "launch", "refresh", "connect"})
DEFAULT_BUDGET = 4  # concurrent adb observations/sends across ALL watchdogs
HISTORY_LEN = 20  # observations kept per headset and watchdog
DECISIONS_KEPT = 50
DECISIONS_SHOWN = 10
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 3600.0
# Bounds for every numeric setting, so an edit can never make a rule act (or give up) absurdly often or never.
BOUNDS: Dict[str, Tuple[float, float]] = {
    "interval_s": (MIN_INTERVAL_S, MAX_INTERVAL_S), "max_failures": (1, 10), "verify_delay_s": (0.2, 30.0),
    "crash_samples": (1, 20), "launch_cooldown_s": (10.0, 3600.0), "cooldown_s": (10.0, 3600.0),
    "samples": (1, 60), "retry_s": (5.0, 3600.0)}
MAX_PATTERN_LEN = 200  # user regexes (overheat pattern, popup evidence)
MIN_PATTERN_LEN = 3
MAX_EXCLUDE_LEN = 200
MAX_EXCLUDE_TOKENS = 10
MIN_EXCLUDE_TOKEN = 3
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
MAX_LINE_LEN = 1000  # a line is cut here before any user regex sees it
MAX_LOG_LINES = 1000  # logcat -t N per cycle
MAX_WINDOW_LINES = 400
MATCH_BUDGET_S = 2.0  # wall time one observation may spend matching user regexes (checked between lines)
TEST_PATTERN_BUDGET_S = 5.0
OBSERVE_TIMEOUT_S = 90.0  # the event loop gives up on an observation that takes longer (worker may linger)
SEND_WORKERS = 2  # dedicated executor for watchdog sends
ACTIVE_MODES = ("playing", "paused")  # desired modes in which the player is meant to be running
MAX_BACKOFF_S = 900.0  # keepalive connect back-off ceiling
PLAUSIBLE_MIN_DEVICES = 5  # keepalive: more than this many addresses AND more than half missing = suspect listing
LOG_YEAR_S = 366 * 86400.0
BASELINE_SLACK_S = 2.0  # the device clock may be this far behind the cursor before logcat is re-baselined
PROBE_MAX_BYTES = 5 * 1024 * 1024
PROBE_KEEP_FILES = 14
PROBE_EVERY_S = 300.0  # at most one "unsampled" probe row per headset this often
WINDOW_CMD = "dumpsys window windows | grep -E 'Window #|mCurrentFocus' || true"
WINDOW_LINE = re.compile(r"Window #|mCurrentFocus")
DIALOG_RE = re.compile(r"Application Error|Application Not Responding")
LOG_TS = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}")
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,)")
_ALT_REPEAT = re.compile(r"\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)\s*[+*{]")
_BACKREF = re.compile(r"\\[1-9]|\(\?P=")
DEFAULT_EXCLUDES = (DEFAULT_PACKAGE, "SyncVR")
PROBE_DIR = "probe"
PATTERN_FILES = ("logcat.txt", "window.txt")  # what a diagnostic snapshot folder holds
MAX_PATTERN_FILE = 8 * 1024 * 1024
MAX_MATCHES_SHOWN = 50


class WatchdogError(Exception):
    """A request the watchdog framework refuses (the controller turns it into a CommandError)."""


@dataclass(frozen=True)
class Action:
    kind: str  # one of SEND_KINDS, or "log" (writes a record, sends nothing)
    reason: str = ""
    arg: Any = None


# ---------------------------------------------------------------- pure helpers

def pattern_hash(pattern: str, exclude: Sequence[str] = DEFAULT_EXCLUDES) -> str:
    """Identity of a confirmed rule: the pattern AND what is excluded from matching (package, extra tokens)."""
    text = (pattern or "") + "\0" + "\0".join(exclude)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def pattern_problem(pattern: str) -> Optional[str]:
    """Why a user regex is refused (a phrase that continues "the pattern ..."), or None when it is acceptable.

    The stdlib ``re`` cannot be interrupted, so catastrophic backtracking is prevented up front: a length cap,
    no backreferences, no repeated groups that contain a quantifier or an alternation, and no pattern that matches
    the empty string (which would match every line).
    """
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN_LEN:
        return f"is longer than {MAX_PATTERN_LEN} characters"
    try:
        compiled = re.compile(pattern)
    except re.error:
        return "is not a valid regular expression"
    if _BACKREF.search(pattern):
        return "uses a backreference, which is not allowed"
    if _NESTED_QUANT.search(pattern) or _ALT_REPEAT.search(pattern):
        return "has a repeated group that can backtrack catastrophically (nested quantifier or repeated alternation)"
    if compiled.search(""):
        return "matches the empty string, so it would match every line"
    return None


def _player_line(line: str, exclude: Sequence[str]) -> bool:
    return any(token and token in line for token in exclude)


def drop_player_lines(lines: Sequence[str], exclude: Sequence[str]) -> List[str]:
    return [ln for ln in lines if not _player_line(ln, exclude)]


def _regex(pattern: str):
    """The compiled pattern, or None when it is empty or refused by :func:`pattern_problem`."""
    if not pattern or pattern_problem(pattern) is not None:
        return None
    return re.compile(pattern)


def guarded_search(regex, lines: Sequence[str], budget_s: float = MATCH_BUDGET_S,
                   clock: Callable[[], float] = time.monotonic) -> Tuple[bool, bool]:
    """``(matched, timed_out)``: does ``regex`` match any line? Lines are cut to MAX_LINE_LEN, and the wall-time
    budget is checked between lines; running out of time counts as no match. Call it from a worker thread."""
    deadline = clock() + budget_s
    for line in lines:
        if clock() > deadline:
            return False, True
        if regex.search(line[:MAX_LINE_LEN]):
            return True, False
    return False, False


def log_seconds(stamp: str) -> Optional[float]:
    """Seconds since Jan 1 of a logcat ``MM-DD HH:MM:SS.mmm`` stamp (no year; leap year assumed)."""
    try:
        when = datetime(2000, int(stamp[0:2]), int(stamp[3:5]), int(stamp[6:8]), int(stamp[9:11]),
                        int(stamp[12:14]))
        return (when - datetime(2000, 1, 1)).total_seconds() + int(stamp[15:18]) / 1000.0
    except (ValueError, IndexError):
        return None


def stamp_delta(new: str, old: str) -> Optional[float]:
    """``new - old`` in seconds on the circular year, so Dec 31 -> Jan 1 reads as a few seconds forward."""
    a, b = log_seconds(new), log_seconds(old)
    if a is None or b is None:
        return None
    delta = (a - b) % LOG_YEAR_S
    return delta - LOG_YEAR_S if delta > LOG_YEAR_S / 2 else delta


def new_log_lines(lines: Sequence[str], cursor: Optional[str]) -> Tuple[List[str], Optional[str], bool]:
    """``(new lines, new cursor, rebaselined)`` from the (unfiltered, chronological) tail of logcat.

    No cursor: baseline only (nothing is new). A cursor later than the newest line (the device clock stepped back,
    the buffer was cleared) re-baselines to the newest line instead of waiting for the clock to catch up.
    """
    stamped = [(ln, m.group(0)) for ln in lines for m in [LOG_TS.match(ln)] if m]
    if not stamped:
        return [], cursor, False
    newest = stamped[-1][1]
    if cursor is None:
        return [], newest, False
    ahead = stamp_delta(newest, cursor)
    if ahead is None or ahead < -BASELINE_SLACK_S:
        return [], newest, True
    return [ln for ln, ts in stamped if (stamp_delta(ts, cursor) or 0.0) > 0], newest, False


def last_sent(history: Sequence[dict], kinds, now: float, stamps: Optional[dict] = None) -> Optional[float]:
    """Seconds since this headset was last sent one of ``kinds``: the newest of the kept history and ``stamps``
    (``{kind: clock time}``, kept per headset outside the bounded history). None if never."""
    ago = None
    for entry in reversed(history):
        if any(k in kinds for k in entry.get("sent", ())):
            ago = now - entry["t"]
            break
    for kind in kinds:
        if stamps and kind in stamps:
            other = now - stamps[kind]
            ago = other if ago is None else min(ago, other)
    return ago


def _in_cooldown(history, kinds, now, seconds, stamps=None) -> bool:
    ago = last_sent(history, kinds, now, stamps)
    return ago is not None and ago < seconds


def _trailing(history: Sequence[dict], key: str) -> int:
    """How many of the most recent entries in a row have a truthy flag ``key``."""
    n = 0
    for entry in reversed(history):
        if not entry.get("flags", {}).get(key):
            break
        n += 1
    return n


# ------------------------------------------------------------------ watchdogs

class Watchdog:
    name = ""
    feature = ""
    title = ""
    rule = ""  # quoted verbatim in the arm dialog
    defaults: Dict[str, Any] = {}
    regex_keys: tuple = ()
    needs_listing = True  # wants the `adb devices` set (to resolve each headset's adb serial)
    log_every_s = 0.0  # minimum seconds between "log" actions per headset (0: every time)

    def arm_blocker(self, cfg: dict) -> Optional[str]:
        """Why this watchdog cannot be armed with ``cfg`` (None when it can)."""
        return None

    def observe(self, wd: "Watchdogs", dev, serial: Optional[str], tele: dict) -> dict:  # runs on the executor
        raise NotImplementedError

    @staticmethod
    def analyze(obs: dict, cfg: dict) -> dict:
        """Flags worth keeping in the history, derived purely from one observation."""
        return {}

    @staticmethod
    def decide(history: Sequence[dict], obs: dict, cfg: dict) -> List[Action]:
        raise NotImplementedError

    def cycle_guard(self, devices: Sequence, serials: Dict[str, Optional[str]]) -> Optional[str]:
        """A reason to skip the whole cycle (an implausible picture of the fleet), or None."""
        return None

    def after_entry(self, mem: dict, entry: dict) -> None:
        """Update the per-headset memory (kept outside the bounded history) from the finished history entry."""

    def execute(self, wd: "Watchdogs", serial: Optional[str], action: Action, obs: dict) -> str:  # executor
        fleet = wd.ctl.fleet
        if action.kind == "wake":
            return fleet.wake_screen(serial)
        if action.kind == "back":
            return fleet.back(serial)
        if action.kind == "launch":
            return fleet.launch(serial)
        if action.kind == "refresh":
            return fleet.screen_refresh(serial)
        raise RuntimeError(f"{self.name} cannot send {action.kind}")

    def verify(self, wd: "Watchdogs", dev, serial: Optional[str], action: Action, obs: dict,
               cfg: dict) -> Optional[bool]:  # executor
        return None

    def log_action(self, wd: "Watchdogs", dev, action: Action, obs: dict) -> None:
        """Write the record of a ``log`` action (default: nothing)."""

    def summarize(self, count: Counter) -> str:
        return f"{count['checked']} checked, {count['act']} needing action"


class StayAwake(Watchdog):
    name = "stay_awake"
    feature = "watchdog.stay_awake"
    title = "Stay-awake"
    rule = ("Every cycle, read each headset's wakefulness. If it is Asleep, Dozing or Dreaming and the headset was "
            "not put to sleep from this server, send WAKE (keyevent 224). Headsets that are worn (player "
            "telemetry) are not polled.")
    defaults = {"interval_s": 15.0}

    def observe(self, wd, dev, serial, tele):
        if tele["online"] and tele["worn"] is True:
            return {"wakefulness": "Awake", "source": "telemetry"}
        if serial is None:
            return {"unreachable": True}
        return {"wakefulness": wd.ctl.fleet.wakefulness(serial), "source": "adb"}

    @staticmethod
    def analyze(obs, cfg):
        return {"wakefulness": obs.get("wakefulness")}

    @staticmethod
    def decide(history, obs, cfg):
        if obs.get("unreachable") or obs.get("asleep"):
            return []
        state = obs.get("wakefulness")
        if state in ("Asleep", "Dozing", "Dreaming"):
            return [Action("wake", f"wakefulness is {state}")]
        return []  # Awake, or unparseable ("unknown"): never guess


class Popup(Watchdog):
    name = "popup"
    feature = "watchdog.popup"
    title = "Popup and crash"
    rule = ("BACK (keyevent 4) is sent only when the focused window's title matches 'Application Error' or "
            "'Application Not Responding' AND the evidence pattern matches the focus line (by default the player's "
            "package). It is verified after sending; after 3 failures for a headset it stops until the dialog is "
            "gone; failures and cooldowns are remembered per headset, not only in the recent history, and a BACK "
            "that raises counts as a failure. If the player is disconnected, the headset's desired mode is playing "
            "or paused and the app is not in front (2 samples in a row), the player is launched (60 s cooldown) "
            "instead.")
    defaults = {"interval_s": 10.0, "package": DEFAULT_PACKAGE, "evidence": re.escape(DEFAULT_PACKAGE),
                "max_failures": 3, "verify_delay_s": 1.5, "crash_samples": 2, "launch_cooldown_s": 60.0}
    regex_keys = ("evidence",)
    CMD = 'dumpsys window windows | grep -E "mCurrentFocus|mFocusedApp" || true'

    def observe(self, wd, dev, serial, tele):
        if serial is None:
            return {"unreachable": True, "online": tele["online"]}
        out = wd.ctl.fleet.shell(serial, self.CMD)
        title = parsers.focus_title(out)
        obs = {"focus_title": title, "focused_app": parsers.focused_app(out), "online": tele["online"],
               "desired": tele.get("desired")}
        evidence = _regex((tele.get("cfg") or {}).get("evidence") or "")
        dialog = False
        if title and evidence is not None and DIALOG_RE.search(title[:MAX_LINE_LEN]):
            dialog, timed_out = guarded_search(evidence, [title])  # the user regex runs here, on the executor
            if timed_out:
                obs["match_timeout"] = True
        obs["dialog"] = dialog
        return obs

    @staticmethod
    def analyze(obs, cfg):
        title = obs.get("focus_title") or ""
        app = obs.get("focused_app") or ""
        pkg = cfg.get("package") or DEFAULT_PACKAGE
        if "dialog" in obs:  # decided by observe() on the executor
            dialog = bool(obs["dialog"])
        else:  # a hand-built observation (tests, replays): match here
            evidence = _regex(cfg.get("evidence") or "")
            dialog = bool(DIALOG_RE.search(title)) and evidence is not None and bool(evidence.search(title))
        in_front = parsers.package_of(app) == pkg or title.startswith(pkg)
        crash = (not obs.get("unreachable") and not dialog and not obs.get("online") and not in_front
                 and obs.get("desired") in ACTIVE_MODES
                 and (obs.get("focus_title") is not None or obs.get("focused_app") is not None))
        return {"dialog": dialog, "in_front": in_front, "crash": crash}

    def after_entry(self, mem, entry):
        if not entry["flags"].get("dialog"):
            mem["failures"] = 0  # the dialog is gone: a new one starts a new count
        elif "back" in entry["sent"] and (entry["verified"] is False or "back" in entry["failed"]):
            mem["failures"] = mem.get("failures", 0) + 1

    @classmethod
    def decide(cls, history, obs, cfg):
        if obs.get("unreachable") or obs.get("asleep"):
            return []
        now = obs["now"]
        flags = cls.analyze(obs, cfg)
        if flags["dialog"]:
            failures = 0
            for entry in reversed(history):  # the current run of samples that showed a dialog
                if not entry.get("flags", {}).get("dialog"):
                    break
                if "back" in entry.get("sent", ()) and entry.get("verified") is False:
                    failures += 1
            failures = max(failures, int(obs.get("failures") or 0))  # the per-headset count outlives the deque
            if failures >= int(cfg.get("max_failures", 3)):
                return []
            return [Action("back", f"dialog '{obs.get('focus_title')}' over the player (failed attempts: {failures})")]
        if flags["crash"]:
            need = max(1, int(cfg.get("crash_samples", 2)))
            if _trailing(history, "crash") + 1 >= need and \
                    not _in_cooldown(history, ("launch",), now, float(cfg.get("launch_cooldown_s", 60.0)),
                                     obs.get("sent_at")):
                return [Action("launch", "player disconnected and the app is not in front")]
        return []

    def verify(self, wd, dev, serial, action, obs, cfg):
        if action.kind != "back":
            return None
        wd.sleeper(float(cfg.get("verify_delay_s", 1.5)))
        after = self.observe(wd, dev, serial, {"online": obs.get("online"), "desired": obs.get("desired"),
                                               "cfg": cfg})
        if after.get("unreachable") or (after.get("focus_title") is None and after.get("focused_app") is None):
            return False  # could not read the headset: never report success for what was not seen
        return not self.analyze(after, cfg)["dialog"]


class Overheat(Watchdog):
    name = "overheat"
    feature = "watchdog.overheat"
    title = "Overheat"
    rule = ("Read the last 1000 logcat lines each cycle, keep only those newer than the last cycle (the player's own "
            "lines dropped), and match the confirmed pattern on the server's worker threads with a time budget. "
            "When the pattern matches one of the new lines AND a line of the window list (again without the "
            "player's lines), send BACK (keyevent 4) to dismiss the prompt (60 s cooldown, kept per headset) and "
            "check afterwards that the prompt left the window list. There is no built-in pattern: it can be armed "
            "only after the pattern was tested against a snapshot, matched in BOTH its logcat and its window list, "
            "and was confirmed in this server run (a restart asks for the confirmation again).")
    defaults = {"interval_s": 15.0, "pattern": "", "pattern_confirmed": "", "package": DEFAULT_PACKAGE,
                "exclude": "", "cooldown_s": 60.0, "verify_delay_s": 1.5}
    regex_keys = ("pattern",)

    @staticmethod
    def excludes(cfg: dict) -> List[str]:
        extra = [t.strip() for t in str(cfg.get("exclude") or "").split(",") if t.strip()]
        return [cfg.get("package") or DEFAULT_PACKAGE, "SyncVR"] + extra

    @staticmethod
    def rule_hash(cfg: dict) -> str:
        """The hash ``pattern_confirmed`` must equal: the pattern plus the package/exclude tokens it is judged with."""
        return pattern_hash(cfg.get("pattern") or "", Overheat.excludes(cfg))

    def arm_blocker(self, cfg):
        pattern = cfg.get("pattern") or ""
        if not pattern:
            return "no pattern is set"
        problem = pattern_problem(pattern)
        if problem:
            return f"the pattern {problem}"
        if cfg.get("pattern_confirmed") != self.rule_hash(cfg):
            return "the current pattern has not been tested against a snapshot and confirmed"
        return None

    @staticmethod
    def _window(fleet, serial, pattern, exclude) -> Tuple[List[str], bool, bool]:
        out = fleet.shell(serial, WINDOW_CMD)
        lines = [ln[:MAX_LINE_LEN] for ln in out.splitlines()[:MAX_WINDOW_LINES] if WINDOW_LINE.search(ln)]
        lines = drop_player_lines(lines, exclude)
        matched, timed_out = guarded_search(pattern, lines)
        return lines, matched, timed_out

    def observe(self, wd, dev, serial, tele):
        cfg = tele["cfg"]
        pattern = _regex(cfg.get("pattern") or "")
        if pattern is None:
            return {"no_pattern": True}
        if serial is None:
            return {"unreachable": True}
        fleet = wd.ctl.fleet
        exclude = self.excludes(cfg)
        cursor = tele.get("cursor")
        out = fleet.shell(serial, f"logcat -d -t {MAX_LOG_LINES}")  # unfiltered: the newest stamp is the truth
        raw = [ln[:MAX_LINE_LEN] for ln in out.splitlines()[-MAX_LOG_LINES:]]
        fresh, newest, rebased = new_log_lines(raw, cursor)
        lines = drop_player_lines(fresh, exclude)
        in_log, timed_out = guarded_search(pattern, lines)  # on the executor, never the event loop
        obs = {"cursor": newest, "log_lines": lines, "window_lines": [], "baseline": cursor is None,
               "rebaselined": rebased, "in_log": in_log, "in_window": False}
        if in_log:  # window list only when the log already matches
            win, in_window, win_timeout = self._window(fleet, serial, pattern, exclude)
            obs.update(window_lines=win, in_window=in_window)
            timed_out = timed_out or win_timeout
        if timed_out:
            obs["match_timeout"] = True
        return obs

    @staticmethod
    def analyze(obs, cfg):
        if obs.get("unreachable"):
            return {"overheat": False}
        if "in_log" in obs and "in_window" in obs:  # decided by observe() on the executor
            in_log, in_win = bool(obs["in_log"]), bool(obs["in_window"])
        else:  # a hand-built observation (tests, replays): match here
            pattern = _regex(cfg.get("pattern") or "")
            if pattern is None:
                return {"overheat": False}
            exclude = Overheat.excludes(cfg)
            in_log = any(pattern.search(ln) for ln in drop_player_lines(obs.get("log_lines") or [], exclude))
            in_win = any(pattern.search(ln) for ln in drop_player_lines(obs.get("window_lines") or [], exclude))
        return {"overheat": in_log and in_win, "in_log": in_log, "in_window": in_win}

    @classmethod
    def decide(cls, history, obs, cfg):
        if obs.get("unreachable") or obs.get("asleep") or obs.get("no_pattern"):
            return []
        if not cls.analyze(obs, cfg)["overheat"]:
            return []
        if _in_cooldown(history, ("back",), obs["now"], float(cfg.get("cooldown_s", 60.0)), obs.get("sent_at")):
            return []
        return [Action("back", "overheat prompt matched in logcat and in the window list")]

    def verify(self, wd, dev, serial, action, obs, cfg):
        if action.kind != "back":
            return None
        wd.sleeper(float(cfg.get("verify_delay_s", 1.5)))
        pattern = _regex(cfg.get("pattern") or "")
        if pattern is None or serial is None:
            return False
        _lines, matched, timed_out = self._window(wd.ctl.fleet, serial, pattern, self.excludes(cfg))
        return not matched and not timed_out  # an unreadable headset raises above: never "verified"


class BlackScreen(Watchdog):
    name = "black_screen"
    feature = "watchdog.black_screen"
    title = "Black-screen probe"
    rule = ("Act only when ALL hold: display state is OFF, the player is the focused app, wakefulness is Awake, the "
            "headset was not put to sleep from this server, its desired mode is playing, and the player does not "
            "report it as not worn (the proximity sensor may switch the display off while Awake). After 3 samples "
            "in a row it sends the recovery (wake, or a screen refresh) and then waits 120 s; the wait counts from "
            "the attempt, also when the attempt fails. A display that is ON with a black picture is only recorded "
            "in the probe CSV (at most one row per headset every 5 minutes), never acted on.")
    defaults = {"interval_s": 30.0, "package": DEFAULT_PACKAGE, "samples": 3, "cooldown_s": 120.0,
                "recovery": "wake", "log_probe": True}
    log_every_s = PROBE_EVERY_S

    def observe(self, wd, dev, serial, tele):
        base = {"desired": tele["desired"], "label": dev.label, "serial": serial or "", "worn": tele.get("worn")}
        if tele["desired"] != "playing":
            return dict(base, skipped="not playing")  # telemetry answers: nothing to probe
        if serial is None:
            return dict(base, unreachable=True)
        fleet = wd.ctl.fleet
        wake = fleet.wakefulness(serial)
        display = parsers.display_state(fleet.shell(serial, 'dumpsys display | grep -E "Display State|mScreenState" '
                                                            '|| true'))
        focus = parsers.focused_app(fleet.shell(serial, "dumpsys window windows | grep mFocusedApp || true"))
        return dict(base, wakefulness=wake, display=display, focused_app=focus)

    @staticmethod
    def analyze(obs, cfg):
        if obs.get("unreachable") or obs.get("skipped"):
            return {"suspect": False, "on_playing": False}
        pkg = cfg.get("package") or DEFAULT_PACKAGE
        in_front = parsers.package_of(obs.get("focused_app")) == pkg
        awake = obs.get("wakefulness") == "Awake"
        playing = obs.get("desired") == "playing"
        display = obs.get("display")
        not_worn = obs.get("worn") is False  # display OFF is expected then (proximity), so never a black screen
        return {"suspect": display == "OFF" and in_front and awake and playing and not obs.get("asleep")
                and not not_worn,
                "on_playing": display == "ON" and in_front and awake and playing,
                "display": display, "in_front": in_front}

    @classmethod
    def decide(cls, history, obs, cfg):
        if obs.get("unreachable") or obs.get("skipped") or obs.get("asleep"):
            return []
        flags = cls.analyze(obs, cfg)
        actions = []
        if flags["on_playing"] and cfg.get("log_probe", True):
            actions.append(Action("log", "display ON while playing", arg={
                "display": obs.get("display"), "wakefulness": obs.get("wakefulness"),
                "in_front": flags["in_front"], "desired": obs.get("desired"), "picture": "unsampled"}))
        if flags["suspect"]:
            need = max(1, int(cfg.get("samples", 3)))
            if _trailing(history, "suspect") + 1 >= need and \
                    not _in_cooldown(history, ("wake", "refresh"), obs["now"], float(cfg.get("cooldown_s", 120.0)),
                                     obs.get("sent_at")):
                kind = "refresh" if cfg.get("recovery") == "refresh" else "wake"
                actions.append(Action(kind, f"display OFF, app in front, awake and playing for {need} samples"))
        return actions

    def log_action(self, wd, dev, action: Action, obs: dict) -> None:
        wd.write_probe_row(dev, action.arg)


class Keepalive(Watchdog):
    name = "keepalive"
    feature = "watchdog.keepalive"
    title = "Keepalive"
    rule = ("Every cycle, compare `adb devices` with the headsets whose Wi-Fi address (IP) the server remembers. "
            "For each one that is missing, run `adb connect <ip>:5555`, then send WAKE. A headset is retried after "
            "30 s, then 60 s, 120 s ... (doubling, at most 15 minutes) while connects keep failing; a connect that "
            "raises counts as an attempt. Never touches a headset that was put to sleep from this server, an "
            "address that two headsets claim, or any cycle where `adb devices` failed or where more than half of "
            "more than 5 headsets look missing (that points at adb, not the headsets).")
    defaults = {"interval_s": 30.0, "retry_s": 30.0}

    def observe(self, wd, dev, serial, tele):  # no adb call: the cycle's device listing already answers
        return {"ip": "" if tele.get("ambiguous") else dev.ip, "reachable": serial is not None}

    @staticmethod
    def analyze(obs, cfg):
        return {"missing": bool(obs.get("ip")) and not obs.get("reachable")}

    @staticmethod
    def retry_delay(cfg: dict, fails: int) -> float:
        return min(MAX_BACKOFF_S, float(cfg.get("retry_s", 30.0)) * (2 ** min(max(0, fails), 10)))

    @classmethod
    def decide(cls, history, obs, cfg):
        if obs.get("asleep") or not cls.analyze(obs, cfg)["missing"]:
            return []
        delay = cls.retry_delay(cfg, int(obs.get("connect_fails") or 0))
        if _in_cooldown(history, ("connect",), obs["now"], delay, obs.get("sent_at")):
            return []
        return [Action("connect", f"{obs['ip']}:{ADB_PORT} is not in adb devices"),
                Action("wake", "wake after reconnecting")]

    def cycle_guard(self, devices, serials):
        known = [d for d in devices if d.ip]
        missing = [d for d in known if serials.get(d.device_id) is None]
        if len(known) > PLAUSIBLE_MIN_DEVICES and len(missing) * 2 > len(known):
            return (f"{len(missing)} of {len(known)} headsets are missing from adb devices: that looks like an adb "
                    f"problem, not that many dropped connections")
        return None

    def after_entry(self, mem, entry):
        if not entry["flags"].get("missing"):
            mem["connect_fails"] = 0
        elif "connect" in entry["sent"]:  # still missing after an attempt (or the attempt raised): back off
            mem["connect_fails"] = mem.get("connect_fails", 0) + 1

    def execute(self, wd, serial, action, obs):
        address = f"{obs['ip']}:{ADB_PORT}"
        if action.kind == "connect":
            return wd.ctl.fleet.connect(address)
        return wd.ctl.fleet.wake_screen(address)


DEFINITIONS: Dict[str, Watchdog] = {w.name: w for w in (StayAwake(), Popup(), Overheat(), BlackScreen(), Keepalive())}


# ----------------------------------------------------------- pattern testing

def test_pattern(pattern: str, folder, exclude: Sequence[str] = DEFAULT_EXCLUDES,
                 budget_s: float = TEST_PATTERN_BUDGET_S, clock: Callable[[], float] = time.monotonic) -> dict:
    """Run ``pattern`` over a snapshot folder's logcat and window files (the player's own lines dropped).

    The same line selection as the live rule: logcat lines carry a timestamp, window lines are the
    ``Window #`` / ``mCurrentFocus`` ones. Returns ``{"pattern", "hash", "error", "files", "by_file": {file: matches},
    "matches": [{"file", "line_no", "line"}], "total", "timed_out"}``. Never raises for bad input; ``error`` says
    what is wrong. Matching stops when ``budget_s`` is used up (``timed_out``).
    """
    exclude = list(exclude)
    result = {"pattern": pattern, "hash": pattern_hash(pattern, exclude), "error": None, "files": [],
              "by_file": {}, "matches": [], "total": 0, "timed_out": False}
    if not pattern:
        result["error"] = "the pattern is empty"
        return result
    problem = pattern_problem(pattern)
    if problem:
        result["error"] = f"the pattern {problem}"
        return result
    regex = re.compile(pattern)
    folder = Path(folder)
    if not folder.is_dir():
        result["error"] = f"not a folder: {folder}"
        return result
    deadline = clock() + budget_s
    keep = {"logcat.txt": LOG_TS, "window.txt": WINDOW_LINE}
    for name in PATTERN_FILES:
        path = folder / name
        try:
            if not path.is_file() or path.stat().st_size > MAX_PATTERN_FILE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result["files"].append(name)
        result["by_file"][name] = 0
        for number, line in enumerate(text.splitlines(), 1):
            if clock() > deadline:
                result["timed_out"] = True
                break
            line = line[:MAX_LINE_LEN]
            if not keep[name].search(line) or _player_line(line, exclude) or not regex.search(line):
                continue
            result["total"] += 1
            result["by_file"][name] += 1
            if len(result["matches"]) < MAX_MATCHES_SHOWN:
                result["matches"].append({"file": name, "line_no": number, "line": line.strip()[:300]})
    if result["timed_out"]:
        result["error"] = f"matching took longer than {budget_s:g} s and was stopped: simplify the pattern"
    elif not result["files"]:
        result["error"] = "the folder has neither logcat.txt nor window.txt (is it a snapshot folder?)"
    return result


# ------------------------------------------------------------------ framework

class WState:
    def __init__(self, defaults: dict):
        self.enabled = False
        self.armed = False
        self.cfg = dict(defaults)
        self.last_cycle: Optional[float] = None  # wall clock
        self.last_summary = ""
        self.counters: Counter = Counter()
        self.decisions: deque = deque(maxlen=DECISIONS_KEPT)
        self.history: Dict[str, deque] = {}  # bounded: observations only, never the source of cooldowns or give-up
        self.mem: Dict[str, dict] = {}  # per headset: sent_at {kind: clock}, failures, connect_fails, log_t
        self.cursors: Dict[str, str] = {}
        self.task = None


class Watchdogs:
    def __init__(self, controller, clock: Callable[[], float] = time.monotonic, budget: int = DEFAULT_BUDGET):
        self.ctl = controller
        self.clock = clock
        self.sleeper: Callable[[float], None] = time.sleep  # used on the executor (verify delay)
        self.budget_size = max(1, int(budget))
        self.defs = DEFINITIONS
        self.states = {name: WState(w.defaults) for name, w in DEFINITIONS.items()}
        self._budget: Optional[asyncio.Semaphore] = None
        self._err_logged: set = set()  # (watchdog, device_id, kind): logged once until it clears
        self._send_executor: Optional[ThreadPoolExecutor] = None
        self._probe_lock = threading.Lock()

    def _executor(self) -> ThreadPoolExecutor:
        """The small executor only sends use, so a send never queues behind observations in the shared one."""
        if self._send_executor is None:
            self._send_executor = ThreadPoolExecutor(max_workers=SEND_WORKERS, thread_name_prefix="syncvr-wd-send")
        return self._send_executor

    # ------------------------------------------------------------ config

    def dump(self) -> dict:
        """Only ``enabled`` and ``cfg`` persist. ``armed`` never does."""
        return {name: {"enabled": st.enabled, "cfg": dict(st.cfg)} for name, st in self.states.items()}

    def load(self, data) -> None:
        if not isinstance(data, dict):
            return
        for name, saved in data.items():
            st = self.states.get(name)
            if st is None or not isinstance(saved, dict):
                continue
            st.enabled = saved.get("enabled") is True
            st.armed = False
            cfg = saved.get("cfg")
            if isinstance(cfg, dict) and "pattern_confirmed" in cfg:
                cfg = dict(cfg, pattern_confirmed="")  # a confirmation never survives a restart
            try:
                st.cfg = self._validated_cfg(name, cfg, dict(st.cfg))
            except WatchdogError:
                pass
            if "pattern_confirmed" in st.cfg:
                st.cfg["pattern_confirmed"] = ""

    def _validated_cfg(self, name: str, changes, base: dict) -> dict:
        w = self.defs[name]
        if changes is None:
            return base
        if not isinstance(changes, dict):
            raise WatchdogError("cfg must be an object")
        cfg = dict(base)
        for key, value in changes.items():
            if key not in w.defaults:
                raise WatchdogError(f"unknown {name} setting: {key}")
            want = w.defaults[key]
            if isinstance(want, bool):
                if not isinstance(value, bool):
                    raise WatchdogError(f"{key} must be true or false")
            elif isinstance(want, (int, float)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                    raise WatchdogError(f"{key} must be a number")
                lo, hi = BOUNDS.get(key, (0, 86400))
                if not lo <= value <= hi:
                    raise WatchdogError(f"{key} must be between {lo:g} and {hi:g}")
                value = type(want)(value)
            else:
                if not isinstance(value, str) or len(value) > 500:
                    raise WatchdogError(f"{key} must be text of at most 500 characters")
                self._check_text(w, key, value)
            cfg[key] = value
        if cfg.get("recovery", "wake") not in ("wake", "refresh"):
            raise WatchdogError("recovery must be 'wake' or 'refresh'")
        return cfg

    @staticmethod
    def _check_text(w: Watchdog, key: str, value: str) -> None:
        if key == "package":
            if not PACKAGE_RE.match(value):
                raise WatchdogError("package must be an Android package name such as com.example.player")
        elif key == "exclude":
            tokens = [t.strip() for t in value.split(",") if t.strip()]
            if len(value) > MAX_EXCLUDE_LEN or len(tokens) > MAX_EXCLUDE_TOKENS \
                    or any(len(t) < MIN_EXCLUDE_TOKEN for t in tokens):
                raise WatchdogError(f"exclude must be at most {MAX_EXCLUDE_TOKENS} comma-separated words of "
                                    f"{MIN_EXCLUDE_TOKEN}+ characters ({MAX_EXCLUDE_LEN} in all)")
        elif key == "pattern_confirmed":
            if value and not re.fullmatch(r"[0-9a-f]{16}", value):
                raise WatchdogError("pattern_confirmed is not a confirmation hash")
        elif key in w.regex_keys and value:
            problem = pattern_problem(value)
            if problem:
                raise WatchdogError(f"{key} {problem}")
            if len(value) < MIN_PATTERN_LEN:
                raise WatchdogError(f"{key} must be at least {MIN_PATTERN_LEN} characters")

    def tested(self, name: str) -> bool:
        return self.defs[name].feature in self.ctl.tested_features

    def set(self, name: str, body: dict, tested_pattern: bool = False) -> dict:
        """Apply ``{enabled, armed, cfg, testing}``. Everything is validated before anything changes.

        ``pattern_confirmed`` may only be set by the pattern test (``tested_pattern=True``), never by a plain request.
        ANY change of a setting disarms (observe-only again); sending ``armed: true`` in the same request counts as a
        fresh arming and goes through the same gating.
        """
        if name not in self.defs:
            raise WatchdogError(f"unknown watchdog: {name}")
        if not isinstance(body, dict):
            raise WatchdogError("request body must be a JSON object")
        st, w = self.states[name], self.defs[name]
        for key in ("enabled", "armed"):
            if key in body and not isinstance(body[key], bool):
                raise WatchdogError(f"{key} must be true or false")
        if isinstance(body.get("cfg"), dict) and "pattern_confirmed" in body["cfg"] and not tested_pattern \
                and body["cfg"]["pattern_confirmed"] != st.cfg.get("pattern_confirmed"):
            raise WatchdogError("pattern_confirmed is set only by testing the pattern against a snapshot")
        cfg = self._validated_cfg(name, body.get("cfg"), dict(st.cfg))
        changed = any(cfg.get(k) != st.cfg.get(k) for k in cfg if not (tested_pattern and k == "pattern_confirmed"))
        was_armed = st.armed and not changed
        enabled = body.get("enabled", st.enabled)
        armed = body.get("armed", was_armed)
        if body.get("enabled") is False:
            armed = False
        if armed and body.get("armed") is not True and w.arm_blocker(cfg):
            armed = False
        if armed:
            if not was_armed and not self.tested(name) and body.get("testing") is not True:
                raise WatchdogError(f"{w.title} is not marked as tested; arm it from the Testing flow "
                                    f"(or send testing: true)")
            blocker = w.arm_blocker(cfg)
            if blocker:
                raise WatchdogError(f"cannot arm {w.title}: {blocker}")
            enabled = True
        prev_armed = st.armed
        if enabled and not st.enabled:  # a fresh start: no stale cursor or give-up count from before
            st.cursors.clear()
            for mem in st.mem.values():
                mem["failures"] = 0
                mem["connect_fails"] = 0
        st.cfg = cfg
        st.enabled = enabled
        st.armed = bool(armed) and w.arm_blocker(cfg) is None
        if body.get("armed") is True and st.armed:
            self.ctl.log_event("warn", f"{w.title} watchdog ARMED")
        elif body.get("armed") is False or (prev_armed and not st.armed):
            self.ctl.log_event("info", f"{w.title} watchdog is observe-only")
        return self.to_json_one(name)

    def disarm_all(self) -> None:
        for st in self.states.values():
            st.armed = False

    def confirm_pattern(self, pattern: str, folder, exclude=None) -> dict:
        """Test ``pattern`` against a snapshot folder. Used by the controller for both test and confirm."""
        st = self.states["overheat"]
        return test_pattern(pattern, folder, Overheat.excludes(dict(st.cfg)) if exclude is None else exclude)

    # ------------------------------------------------------------ state out

    def to_json_one(self, name: str) -> dict:
        st, w = self.states[name], self.defs[name]
        return {"name": name, "title": w.title, "feature": w.feature, "rule": w.rule,
                "enabled": st.enabled, "armed": st.armed,
                "mode": "armed" if st.armed else "observe" if st.enabled else "off",
                "tested": self.tested(name), "arm_blocker": w.arm_blocker(st.cfg),
                "cfg": dict(st.cfg),
                "pattern_hash": Overheat.rule_hash(st.cfg) if name == "overheat"
                else pattern_hash(st.cfg.get("pattern", "")),
                "last_cycle": st.last_cycle, "last_summary": st.last_summary,
                "counters": dict(st.counters), "decisions": list(st.decisions)[-DECISIONS_SHOWN:]}

    def to_json(self) -> list:
        return [self.to_json_one(name) for name in self.defs]

    # ------------------------------------------------------------ running

    def start(self, loop=None) -> None:
        loop = loop or asyncio.get_running_loop()
        self._budget = asyncio.Semaphore(self.budget_size)
        for name, st in self.states.items():
            if st.task is None or st.task.done():
                st.task = loop.create_task(self._loop(name), name=f"watchdog-{name}")

    async def stop(self) -> None:
        tasks = [st.task for st in self.states.values() if st.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for st in self.states.values():
            st.task = None
        if self._send_executor is not None:
            self._send_executor.shutdown(wait=False)
            self._send_executor = None

    async def _loop(self, name: str) -> None:
        st = self.states[name]
        while True:
            if st.enabled:
                try:
                    await self.cycle(name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a bad cycle never ends the loop
                    self._failed_cycle(name, exc)
            await asyncio.sleep(max(0.01, float(st.cfg.get("interval_s", 15.0))))

    def _failed_cycle(self, name: str, exc: Exception) -> None:
        st = self.states[name]
        st.counters["errors"] += 1
        st.last_summary = f"cycle failed: {exc}"
        st.decisions.append({"t": time.time(), "device": None, "label": None, "text": f"cycle error: {exc}",
                             "actions": [], "sent": [], "suppressed": None, "error": str(exc) or type(exc).__name__})
        log.exception("watchdog %s: cycle failed", name)
        self.ctl.changed()

    def _sem(self) -> asyncio.Semaphore:
        if self._budget is None:
            self._budget = asyncio.Semaphore(self.budget_size)
        return self._budget

    async def _adb(self, fn: Callable[[], Any]):
        """Run blocking adb work on the shared executor, holding one slot of the global poll budget."""
        async with self._sem():
            return await asyncio.get_running_loop().run_in_executor(self.ctl.fleet.executor, fn)

    def _log_once(self, key: tuple, level: str, text: str, dev=None) -> None:
        if key not in self._err_logged:
            self._err_logged.add(key)
            self.ctl.log_event(level, text, dev) if dev is not None else self.ctl.log_event(level, text)

    def _skip_cycle(self, w, st, counter: str, summary: str) -> None:
        st.counters[counter] += 1
        st.last_cycle = time.time()
        st.last_summary = f"skipped: {summary}"
        self.ctl.changed()

    async def cycle(self, name: str) -> None:
        """One observe/decide/act pass over every headset not intentionally asleep."""
        w, st = self.defs[name], self.states[name]
        ctl = self.ctl
        st.counters["cycles"] += 1
        count: Counter = Counter()
        listed: set = set()
        if w.needs_listing:
            try:
                listed = await self._adb(ctl.fleet.listed_checked)
            except ListingFailed as exc:  # not "no headsets": never act on a failed listing
                self._log_once((w.name, "*", "listing"), "warn",
                               f"{w.title} watchdog: `adb devices` failed ({exc}); skipping cycles until it works")
                self._skip_cycle(w, st, "listing_failed", f"`adb devices` failed ({exc})")
                return
            self._err_logged.discard((w.name, "*", "listing"))
        everyone = list(ctl.devices.values())
        known = {d.device_id for d in everyone}
        for gone in [k for k in st.history if k not in known]:
            st.history.pop(gone, None)
        for gone in [k for k in st.cursors if k not in known]:
            st.cursors.pop(gone, None)
        for gone in [k for k in st.mem if k not in known]:
            st.mem.pop(gone, None)
        for key in [k for k in self._err_logged if k[0] == w.name and k[1] != "*" and k[1] not in known]:
            self._err_logged.discard(key)
        serials, notes = resolve_serials(everyone, listed)
        for dev_id in {k[1] for k in self._err_logged if k[0] == w.name and k[2] == "ambiguous"} - set(notes):
            self._err_logged.discard((w.name, dev_id, "ambiguous"))
        for dev_id, note in notes.items():
            label = ctl.devices[dev_id].label if dev_id in ctl.devices else dev_id
            self._log_once((w.name, dev_id, "ambiguous"), "warn",
                           f"{w.title} watchdog: {label}: {note}; not acting on it", ctl.devices.get(dev_id))
        devices = [d for d in everyone if d.device_id not in ctl.intentionally_asleep]
        reason = w.cycle_guard(devices, serials)
        if reason:
            self._log_once((w.name, "*", "guard"), "warn", f"{w.title} watchdog: skipping cycles: {reason}")
            self._skip_cycle(w, st, "implausible", reason)
            return
        self._err_logged.discard((w.name, "*", "guard"))
        results = await asyncio.gather(
            *(self._device_cycle(w, st, d, serials.get(d.device_id),
                                 d.device_id in notes and serials.get(d.device_id) is None, listed, count)
              for d in devices), return_exceptions=True)
        for dev, res in zip(devices, results):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, Exception):  # _device_cycle catches its own; this is a framework bug at worst
                self._record_error(w, st, dev, res)
                count["errors"] += 1
        st.last_cycle = time.time()
        extra = f", {count['errors']} errors" if count["errors"] else ""
        extra += f", {count['suppressed']} braked ({ctl.brake()})" if count["suppressed"] else ""
        st.last_summary = w.summarize(count) + extra
        ctl.changed()

    def _record_error(self, w, st, dev, exc) -> None:
        text = str(exc) or type(exc).__name__
        st.counters["errors"] += 1
        st.decisions.append({"t": time.time(), "device": dev.device_id, "label": dev.label,
                             "text": f"error: {text}", "actions": [], "sent": [], "suppressed": None,
                             "error": text})
        # once per headset until it works again, not every cycle
        self._log_once((w.name, dev.device_id, "error"), "warn", f"{w.title} watchdog: {dev.label}: {text}", dev)

    def _tele(self, st, dev, listed, ambiguous: bool = False) -> dict:
        status = dev.status or {}
        return {"online": bool(dev.online), "worn": status.get("worn"),
                "desired": (dev.desired or {}).get("mode"), "cursor": st.cursors.get(dev.device_id),
                "cfg": dict(st.cfg), "listed": listed, "ambiguous": ambiguous}

    async def _device_cycle(self, w: Watchdog, st: WState, dev, serial, ambiguous, listed, count: Counter) -> None:
        ctl = self.ctl
        mem = st.mem.setdefault(dev.device_id, {})
        history = st.history.setdefault(dev.device_id, deque(maxlen=HISTORY_LEN))
        entry = None
        try:
            tele = self._tele(st, dev, listed, ambiguous)
            try:
                obs = await asyncio.wait_for(self._adb(lambda: w.observe(self, dev, serial, tele)),
                                             OBSERVE_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise WatchdogError(f"observation took longer than {OBSERVE_TIMEOUT_S:g} s")
            count["checked"] += 1
            st.counters["observed"] += 1
            if obs.get("unreachable"):
                count["unreachable"] += 1
                st.counters["unreachable"] += 1
                st.cursors.pop(dev.device_id, None)  # the device clock/log may differ when it is back
            elif obs.get("cursor"):
                st.cursors[dev.device_id] = obs["cursor"]
            if obs.get("match_timeout"):
                st.counters["match_timeout"] += 1
                self._log_once((w.name, dev.device_id, "match_timeout"), "warn",
                               f"{w.title} watchdog: {dev.label}: pattern matching ran out of time; treated as no "
                               f"match (simplify the pattern)", dev)
            else:
                self._err_logged.discard((w.name, dev.device_id, "match_timeout"))
            cfg = dict(st.cfg)
            obs = dict(obs, now=self.clock(), asleep=dev.device_id in ctl.intentionally_asleep,
                       sent_at=dict(mem.get("sent_at", {})), failures=int(mem.get("failures", 0)),
                       connect_fails=int(mem.get("connect_fails", 0)))
            actions = w.decide(list(history), obs, cfg)
            entry = {"t": obs["now"], "flags": w.analyze(obs, cfg), "sent": [], "failed": [], "suppressed": None,
                     "verified": None}
            if any(a.kind in SEND_KINDS for a in actions):
                count["act"] += 1
            for action in actions:
                if not await self._do(w, st, dev, serial, action, obs, cfg, entry, count, mem):
                    break
            if not entry["failed"]:
                self._err_logged.discard((w.name, dev.device_id, "error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            count["errors"] += 1
            self._record_error(w, st, dev, exc)
        finally:
            if entry is not None:  # always: an attempt that raised still counts
                history.append(entry)
                try:
                    w.after_entry(mem, entry)
                except Exception:
                    log.exception("watchdog %s: after_entry failed for %s", w.name, dev.label)

    def _decision(self, st, dev, action: Action, text: str, **extra) -> None:
        item = {"t": time.time(), "device": dev.device_id, "label": dev.label, "text": text,
                "actions": [action.kind], "sent": [], "suppressed": None, "error": None}
        item.update(extra)
        st.decisions.append(item)

    def _block_reason(self, st, dev) -> Optional[str]:
        """Why a send must not go out right now (None: it may). Safe to call from any thread."""
        brake = self.ctl.brake()
        if brake:
            return brake
        if dev.device_id in self.ctl.intentionally_asleep:
            return "intentionally asleep"
        if not st.armed:
            return "disarmed"
        return None

    def _send_now(self, w, st, dev, serial, action, obs) -> Optional[str]:
        """Runs on a send worker: the last check, right before the adb command. Returns a block reason or None."""
        reason = self._block_reason(st, dev)
        if reason:
            return reason
        w.execute(self, serial, action, obs)
        return None

    async def _do(self, w, st, dev, serial, action: Action, obs, cfg, entry, count, mem) -> bool:
        """Carry out one action. False stops this headset's remaining actions."""
        ctl = self.ctl
        loop = asyncio.get_running_loop()
        if action.kind not in SEND_KINDS:  # a log record: sends nothing, so no brake
            now = self.clock()
            last = mem.get("log_t")
            if w.log_every_s and last is not None and now - last < w.log_every_s:
                return True
            mem["log_t"] = now
            try:
                await self._adb(lambda: w.log_action(self, dev, action, obs))  # file I/O off the loop thread
            except Exception as exc:
                log.warning("watchdog %s: could not log for %s: %s", w.name, dev.label, exc)
            return True
        brake = ctl.brake()
        if brake:
            entry["suppressed"] = brake
            count["suppressed"] += 1
            st.counters["suppressed"] += 1
            self._decision(st, dev, action, f"would {action.kind}: {action.reason} (braked: {brake}, not sent)",
                           suppressed=brake)
            return False
        if not st.armed:
            st.counters["observed_only"] += 1
            self._decision(st, dev, action, f"would {action.kind}: {action.reason} (observe-only, not sent)")
            return False
        async with self._sem():
            reason = self._block_reason(st, dev)  # the world may have changed while waiting for a slot
            blocked = reason is not None
            if not blocked:
                # Record the attempt BEFORE it runs: a send that raises or never returns still counts.
                stamps = mem.setdefault("sent_at", {})
                previous = stamps.get(action.kind)
                stamps[action.kind] = self.clock()
                entry["sent"].append(action.kind)
                try:
                    reason = await loop.run_in_executor(self._executor(),
                                                        lambda: self._send_now(w, st, dev, serial, action, obs))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    entry["failed"].append(action.kind)
                    entry["verified"] = False
                    count["errors"] += 1
                    count["send_failed"] += 1
                    st.counters["send_failed"] += 1
                    self._record_error(w, st, dev, exc)
                    self._decision(st, dev, action, f"sent {action.kind}: {action.reason}, FAILED: {exc}",
                                   sent=[action.kind], verified=False, error=str(exc) or type(exc).__name__)
                    return False
                if reason:  # the worker's last check said no: nothing went out, so undo the record
                    entry["sent"].remove(action.kind)
                    if previous is None:
                        stamps.pop(action.kind, None)
                    else:
                        stamps[action.kind] = previous
                    blocked = True
            if blocked:
                entry["suppressed"] = reason
                count["suppressed"] += 1
                st.counters["suppressed"] += 1
                self._decision(st, dev, action, f"would {action.kind}: {action.reason} ({reason}, not sent)",
                               suppressed=reason)
                return False
        count["sent"] += 1
        st.counters["sent"] += 1
        verified = None
        try:
            verified = await self._adb(lambda: w.verify(self, dev, serial, action, obs, cfg))
        except Exception as exc:
            log.warning("watchdog %s: verify failed for %s: %s", w.name, dev.label, exc)
            verified = False
        if verified is not None:
            entry["verified"] = verified
        note = "" if verified is None else (", verified" if verified else ", NOT verified")
        self._decision(st, dev, action, f"sent {action.kind}: {action.reason}{note}", sent=[action.kind],
                       verified=verified)
        ctl.log_event("info", f"{w.title} watchdog: sent {action.kind} to {dev.label} ({action.reason}){note}", dev)
        return True

    # ------------------------------------------------------------ probe CSV

    def write_probe_row(self, dev, row: dict) -> None:
        """Append to today's probe CSV (a worker thread). A file over PROBE_MAX_BYTES is set aside under a
        timestamped name and only the newest PROBE_KEEP_FILES files are kept."""
        data_dir = getattr(self.ctl, "data_dir", None)
        if not data_dir:
            return
        fields = ["time", "device", "label", "display", "wakefulness", "in_front", "desired", "picture"]
        with self._probe_lock:
            folder = Path(data_dir) / PROBE_DIR
            folder.mkdir(parents=True, exist_ok=True)
            day = datetime.now().strftime("%Y%m%d")
            path = folder / f"probe_{day}.csv"
            if path.exists() and path.stat().st_size >= PROBE_MAX_BYTES:
                path.rename(folder / f"probe_{day}_{datetime.now().strftime('%H%M%S%f')}.csv")
            fresh = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                if fresh:
                    writer.writeheader()
                writer.writerow(dict(row, time=datetime.now().isoformat(timespec="seconds"),
                                     device=dev.device_id, label=dev.label))
            files = sorted(folder.glob("probe_*.csv"), key=lambda p: p.stat().st_mtime)
            for old in files[:-PROBE_KEEP_FILES]:
                try:
                    old.unlink()
                except OSError:
                    pass
