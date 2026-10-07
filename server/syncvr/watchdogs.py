"""Watchdogs: unattended checks that fix a headset when it is clearly stuck.

One asyncio task per watchdog (started by ``SyncServer.start``). A cycle is:

    observe (adb on the shared executor, filtered on the headset) -> decide(history, obs, cfg) -> act

``decide`` is a pure function of the headset's recent history, one observation and the config; it returns
``Action`` values and touches nothing. The framework then re-checks ``Controller.brake()`` immediately before
EACH send: when braked the decision is logged and nothing is sent. A watchdog that is enabled but not armed is
observe-only and never sends. ``armed`` is never persisted, so every restart is observe-only again.

No Qt. Uses the controller only through ``devices``, ``intentionally_asleep``, ``fleet``, ``brake()``,
``data_dir`` and ``log_event``, always from the event loop thread; adb runs on the executor and returns plain data.
"""

import asyncio
import csv
import hashlib
import logging
import re
import shlex
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import parsers
from .adbtool import DEFAULT_PACKAGE
from .fleetops import ADB_PORT

log = logging.getLogger(__name__)

SEND_KINDS = frozenset({"wake", "back", "launch", "refresh", "connect"})
DEFAULT_BUDGET = 4  # concurrent adb observations/sends across ALL watchdogs
HISTORY_LEN = 20  # observations kept per headset and watchdog
DECISIONS_KEPT = 50
DECISIONS_SHOWN = 10
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 3600.0
DIALOG_RE = re.compile(r"Application Error|Application Not Responding")
LOG_TS = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}")
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

def pattern_hash(pattern: str) -> str:
    return hashlib.sha256((pattern or "").encode("utf-8")).hexdigest()[:16]


def _player_line(line: str, exclude: Sequence[str]) -> bool:
    return any(token and token in line for token in exclude)


def drop_player_lines(lines: Sequence[str], exclude: Sequence[str]) -> List[str]:
    return [ln for ln in lines if not _player_line(ln, exclude)]


def _regex(pattern: str):
    try:
        return re.compile(pattern) if pattern else None
    except re.error:
        return None


def last_sent(history: Sequence[dict], kinds, now: float) -> Optional[float]:
    """Seconds since this headset was last sent one of ``kinds`` (None if never within the kept history)."""
    for entry in reversed(history):
        if any(k in kinds for k in entry.get("sent", ())):
            return now - entry["t"]
    return None


def _in_cooldown(history, kinds, now, seconds) -> bool:
    ago = last_sent(history, kinds, now)
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
            "gone. If the player is disconnected and the app is not in front (2 samples in a row), the player is "
            "launched (60 s cooldown) instead.")
    defaults = {"interval_s": 10.0, "package": DEFAULT_PACKAGE, "evidence": re.escape(DEFAULT_PACKAGE),
                "max_failures": 3, "verify_delay_s": 1.5, "crash_samples": 2, "launch_cooldown_s": 60.0}
    regex_keys = ("evidence",)
    CMD = 'dumpsys window windows | grep -E "mCurrentFocus|mFocusedApp" || true'

    def observe(self, wd, dev, serial, tele):
        if serial is None:
            return {"unreachable": True, "online": tele["online"]}
        out = wd.ctl.fleet.shell(serial, self.CMD)
        return {"focus_title": parsers.focus_title(out), "focused_app": parsers.focused_app(out),
                "online": tele["online"]}

    @staticmethod
    def analyze(obs, cfg):
        title = obs.get("focus_title") or ""
        app = obs.get("focused_app") or ""
        pkg = cfg.get("package") or DEFAULT_PACKAGE
        evidence = _regex(cfg.get("evidence") or "")
        dialog = bool(DIALOG_RE.search(title)) and evidence is not None and bool(evidence.search(title))
        in_front = parsers.package_of(app) == pkg or title.startswith(pkg)
        crash = (not obs.get("unreachable") and not dialog and not obs.get("online") and not in_front
                 and (obs.get("focus_title") is not None or obs.get("focused_app") is not None))
        return {"dialog": dialog, "in_front": in_front, "crash": crash}

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
            if failures >= int(cfg.get("max_failures", 3)):
                return []
            return [Action("back", f"dialog '{obs.get('focus_title')}' over the player (failed attempts: {failures})")]
        if flags["crash"]:
            need = max(1, int(cfg.get("crash_samples", 2)))
            if _trailing(history, "crash") + 1 >= need and \
                    not _in_cooldown(history, ("launch",), now, float(cfg.get("launch_cooldown_s", 60.0))):
                return [Action("launch", "player disconnected and the app is not in front")]
        return []

    def verify(self, wd, dev, serial, action, obs, cfg):
        if action.kind != "back":
            return None
        wd.sleeper(float(cfg.get("verify_delay_s", 1.5)))
        after = self.observe(wd, dev, serial, {"online": obs.get("online")})
        return not self.analyze(after, cfg)["dialog"]


class Overheat(Watchdog):
    name = "overheat"
    feature = "watchdog.overheat"
    title = "Overheat"
    rule = ("Read only the logcat lines written since the last cycle (logcat -T), dropping the player's own lines. "
            "When the confirmed pattern matches one of them AND also matches a line of the window list "
            "(again without the player's lines), send BACK (keyevent 4) to dismiss the prompt (60 s cooldown). "
            "There is no built-in pattern: it can be armed only after the pattern was tested against a snapshot "
            "and confirmed.")
    defaults = {"interval_s": 15.0, "pattern": "", "pattern_confirmed": "", "package": DEFAULT_PACKAGE,
                "exclude": "", "cooldown_s": 60.0}
    regex_keys = ("pattern",)

    @staticmethod
    def excludes(cfg: dict) -> List[str]:
        extra = [t.strip() for t in str(cfg.get("exclude") or "").split(",") if t.strip()]
        return [cfg.get("package") or DEFAULT_PACKAGE, "SyncVR"] + extra

    def arm_blocker(self, cfg):
        pattern = cfg.get("pattern") or ""
        if not pattern:
            return "no pattern is set"
        if _regex(pattern) is None:
            return "the pattern is not a valid regular expression"
        if cfg.get("pattern_confirmed") != pattern_hash(pattern):
            return "the current pattern has not been tested against a snapshot and confirmed"
        return None

    @staticmethod
    def _filtered(command: str, exclude: Sequence[str]) -> str:
        flags = " ".join("-e " + shlex.quote(t) for t in exclude if t)
        return f"({command} | grep -v -F {flags}) || true" if flags else f"({command}) || true"

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
        out = fleet.shell(serial, self._filtered(f"logcat -d -T {shlex.quote(cursor or '1')}", exclude))
        lines, newest = [], cursor
        for line in out.splitlines():
            ts = LOG_TS.match(line)
            if not ts:
                continue
            if cursor is None or ts.group(0) > cursor:  # -T includes the line at the cursor itself
                lines.append(line)
            if newest is None or ts.group(0) > newest:
                newest = ts.group(0)
        obs = {"cursor": newest, "log_lines": [] if cursor is None else lines, "window_lines": [],
               "baseline": cursor is None}
        if any(pattern.search(ln) for ln in obs["log_lines"]):  # window list only when the log already matches
            win = fleet.shell(serial, self._filtered("dumpsys window windows | grep -E 'Window #|mCurrentFocus'",
                                                     exclude))
            obs["window_lines"] = win.splitlines()
        return obs

    @staticmethod
    def analyze(obs, cfg):
        pattern = _regex(cfg.get("pattern") or "")
        if pattern is None or obs.get("unreachable"):
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
        if _in_cooldown(history, ("back",), obs["now"], float(cfg.get("cooldown_s", 60.0))):
            return []
        return [Action("back", "overheat prompt matched in logcat and in the window list")]


class BlackScreen(Watchdog):
    name = "black_screen"
    feature = "watchdog.black_screen"
    title = "Black-screen probe"
    rule = ("Act only when ALL hold: display state is OFF, the player is the focused app, wakefulness is Awake, the "
            "headset was not put to sleep from this server, and its desired mode is playing. After 3 samples in a "
            "row it sends the recovery (wake, or a screen refresh) and then waits 120 s. A display that is ON with a "
            "black picture is only recorded in the probe CSV, never acted on.")
    defaults = {"interval_s": 30.0, "package": DEFAULT_PACKAGE, "samples": 3, "cooldown_s": 120.0,
                "recovery": "wake", "log_probe": True}

    def observe(self, wd, dev, serial, tele):
        base = {"desired": tele["desired"], "label": dev.label, "serial": serial or ""}
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
        return {"suspect": display == "OFF" and in_front and awake and playing and not obs.get("asleep"),
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
                    not _in_cooldown(history, ("wake", "refresh"), obs["now"], float(cfg.get("cooldown_s", 120.0))):
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
            "For each one that is missing, run `adb connect <ip>:5555`, then send WAKE. Retries a headset at most "
            "every 30 s. Never touches a headset that was put to sleep from this server.")
    defaults = {"interval_s": 30.0, "retry_s": 30.0}

    def observe(self, wd, dev, serial, tele):  # no adb call: the cycle's device listing already answers
        return {"ip": dev.ip, "reachable": serial is not None}

    @staticmethod
    def analyze(obs, cfg):
        return {"missing": bool(obs.get("ip")) and not obs.get("reachable")}

    @classmethod
    def decide(cls, history, obs, cfg):
        if obs.get("asleep") or not cls.analyze(obs, cfg)["missing"]:
            return []
        if _in_cooldown(history, ("connect",), obs["now"], float(cfg.get("retry_s", 30.0))):
            return []
        return [Action("connect", f"{obs['ip']}:{ADB_PORT} is not in adb devices"),
                Action("wake", "wake after reconnecting")]

    def execute(self, wd, serial, action, obs):
        address = f"{obs['ip']}:{ADB_PORT}"
        if action.kind == "connect":
            return wd.ctl.fleet.connect(address)
        return wd.ctl.fleet.wake_screen(address)


DEFINITIONS: Dict[str, Watchdog] = {w.name: w for w in (StayAwake(), Popup(), Overheat(), BlackScreen(), Keepalive())}


# ----------------------------------------------------------- pattern testing

def test_pattern(pattern: str, folder, exclude: Sequence[str] = (DEFAULT_PACKAGE, "SyncVR")) -> dict:
    """Run ``pattern`` over a snapshot folder's logcat and window files (the player's own lines dropped).

    Returns ``{"pattern", "hash", "error", "files", "matches": [{"file", "line_no", "line"}], "total"}``.
    Never raises for bad input; ``error`` says what is wrong.
    """
    result = {"pattern": pattern, "hash": pattern_hash(pattern), "error": None, "files": [], "matches": [],
              "total": 0}
    if not pattern:
        result["error"] = "the pattern is empty"
        return result
    regex = _regex(pattern)
    if regex is None:
        result["error"] = "the pattern is not a valid regular expression"
        return result
    folder = Path(folder)
    if not folder.is_dir():
        result["error"] = f"not a folder: {folder}"
        return result
    for name in PATTERN_FILES:
        path = folder / name
        try:
            if not path.is_file() or path.stat().st_size > MAX_PATTERN_FILE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result["files"].append(name)
        for number, line in enumerate(text.splitlines(), 1):
            if _player_line(line, exclude) or not regex.search(line):
                continue
            result["total"] += 1
            if len(result["matches"]) < MAX_MATCHES_SHOWN:
                result["matches"].append({"file": name, "line_no": number, "line": line.strip()[:300]})
    if not result["files"]:
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
        self.history: Dict[str, deque] = {}
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
        self._err_logged: set = set()

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
            try:
                st.cfg = self._validated_cfg(name, saved.get("cfg"), dict(st.cfg))
            except WatchdogError:
                pass

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
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
                    raise WatchdogError(f"{key} must be a non-negative number")
                value = type(want)(value)
            else:
                if not isinstance(value, str) or len(value) > 500:
                    raise WatchdogError(f"{key} must be text of at most 500 characters")
            if key in w.regex_keys and value and _regex(value) is None:
                raise WatchdogError(f"{key} is not a valid regular expression")
            cfg[key] = value
        interval = cfg.get("interval_s", 15.0)
        if not MIN_INTERVAL_S <= interval <= MAX_INTERVAL_S:
            raise WatchdogError(f"interval_s must be between {MIN_INTERVAL_S:g} and {MAX_INTERVAL_S:g}")
        if cfg.get("recovery", "wake") not in ("wake", "refresh"):
            raise WatchdogError("recovery must be 'wake' or 'refresh'")
        return cfg

    def tested(self, name: str) -> bool:
        return self.defs[name].feature in self.ctl.tested_features

    def set(self, name: str, body: dict, tested_pattern: bool = False) -> dict:
        """Apply ``{enabled, armed, cfg, testing}``. Everything is validated before anything changes.

        ``pattern_confirmed`` may only be set by the pattern test (``tested_pattern=True``), never by a plain request.
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
        enabled = body.get("enabled", st.enabled)
        armed = body.get("armed", st.armed)
        if not enabled and body.get("enabled") is False:
            armed = False
        if armed and body.get("armed") is not True and w.arm_blocker(cfg):
            armed = False  # an edit made the current arming invalid (e.g. a new pattern): fall back to observe-only
        if armed:
            if not st.armed:  # a new arming, not a no-op repeat
                if not self.tested(name) and body.get("testing") is not True:
                    raise WatchdogError(f"{w.title} is not marked as tested; arm it from the Testing flow "
                                        f"(or send testing: true)")
            blocker = w.arm_blocker(cfg)
            if blocker:
                raise WatchdogError(f"cannot arm {w.title}: {blocker}")
            enabled = True
        st.cfg = cfg
        st.enabled = enabled
        st.armed = bool(armed) and w.arm_blocker(cfg) is None
        if body.get("armed") is True and st.armed:
            self.ctl.log_event("warn", f"{w.title} watchdog ARMED")
        elif body.get("armed") is False or (not st.armed and armed):
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
                "cfg": dict(st.cfg), "pattern_hash": pattern_hash(st.cfg.get("pattern", "")),
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

    async def cycle(self, name: str) -> None:
        """One observe/decide/act pass over every headset not intentionally asleep."""
        w, st = self.defs[name], self.states[name]
        ctl = self.ctl
        st.counters["cycles"] += 1
        count: Counter = Counter()
        listed = await self._adb(ctl.fleet.listed) if w.needs_listing else set()
        devices = [d for d in list(ctl.devices.values()) if d.device_id not in ctl.intentionally_asleep]
        results = await asyncio.gather(*(self._device_cycle(w, st, d, listed, count) for d in devices),
                                       return_exceptions=True)
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
        key = (w.name, dev.device_id)
        if key not in self._err_logged:  # once per headset until it works again, not every cycle
            self._err_logged.add(key)
            self.ctl.log_event("warn", f"{w.title} watchdog: {dev.label}: {text}", dev)

    def _tele(self, st, dev, listed) -> dict:
        status = dev.status or {}
        return {"online": bool(dev.online), "worn": status.get("worn"),
                "desired": (dev.desired or {}).get("mode"), "cursor": st.cursors.get(dev.device_id),
                "cfg": dict(st.cfg), "listed": listed}

    async def _device_cycle(self, w: Watchdog, st: WState, dev, listed, count: Counter) -> None:
        ctl = self.ctl
        try:
            serial = ctl.fleet.adb_serial(dev, listed)
            tele = self._tele(st, dev, listed)
            obs = await self._adb(lambda: w.observe(self, dev, serial, tele))
            count["checked"] += 1
            st.counters["observed"] += 1
            if obs.get("unreachable"):
                count["unreachable"] += 1
                st.counters["unreachable"] += 1
            if obs.get("cursor"):
                st.cursors[dev.device_id] = obs["cursor"]
            cfg = dict(st.cfg)
            obs = dict(obs, now=self.clock(), asleep=dev.device_id in ctl.intentionally_asleep)
            history = list(st.history.setdefault(dev.device_id, deque(maxlen=HISTORY_LEN)))
            actions = w.decide(history, obs, cfg)
            entry = {"t": obs["now"], "flags": w.analyze(obs, cfg), "sent": [], "suppressed": None,
                     "verified": None}
            if any(a.kind in SEND_KINDS for a in actions):
                count["act"] += 1
            for action in actions:
                if not await self._do(w, st, dev, serial, action, obs, cfg, entry, count):
                    break
            st.history[dev.device_id].append(entry)
            self._err_logged.discard((w.name, dev.device_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            count["errors"] += 1
            self._record_error(w, st, dev, exc)

    def _decision(self, st, dev, action: Action, text: str, **extra) -> None:
        item = {"t": time.time(), "device": dev.device_id, "label": dev.label, "text": text,
                "actions": [action.kind], "sent": [], "suppressed": None, "error": None}
        item.update(extra)
        st.decisions.append(item)

    async def _do(self, w, st, dev, serial, action: Action, obs, cfg, entry, count) -> bool:
        """Carry out one action. False stops this headset's remaining actions."""
        ctl = self.ctl
        if action.kind not in SEND_KINDS:  # a log record: sends nothing, so no brake
            try:
                w.log_action(self, dev, action, obs)
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
            # Re-check immediately before the send: the brake or a Sleep command may have changed while
            # this cycle waited for a poll-budget slot.
            brake = ctl.brake()
            if brake or dev.device_id in ctl.intentionally_asleep or not st.armed:
                reason = brake or ("intentionally asleep" if dev.device_id in ctl.intentionally_asleep
                                   else "disarmed")
                entry["suppressed"] = reason
                count["suppressed"] += 1
                st.counters["suppressed"] += 1
                self._decision(st, dev, action, f"would {action.kind}: {action.reason} ({reason}, not sent)",
                               suppressed=reason)
                return False
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(ctl.fleet.executor, lambda: w.execute(self, serial, action, obs))
        entry["sent"].append(action.kind)
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
        data_dir = getattr(self.ctl, "data_dir", None)
        if not data_dir:
            return
        folder = Path(data_dir) / PROBE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"probe_{datetime.now().strftime('%Y%m%d')}.csv"
        fields = ["time", "device", "label", "display", "wakefulness", "in_front", "desired", "picture"]
        fresh = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if fresh:
                writer.writeheader()
            writer.writerow(dict(row, time=datetime.now().isoformat(timespec="seconds"), device=dev.device_id,
                                 label=dev.label))
