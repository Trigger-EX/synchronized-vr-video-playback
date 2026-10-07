import asyncio
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncvr import parsers, watchdogs
from syncvr.adbtool import Adb
from syncvr.controller import CommandError, Controller
from syncvr.fleetops import AdbFleet
from syncvr.library import Library
from syncvr.watchdogs import (Action, BlackScreen, Keepalive, Overheat, Popup, StayAwake, WatchdogError, Watchdogs,
                              pattern_hash, test_pattern as try_pattern)

HERE = Path(__file__).parent
FAKE_ADB = str(HERE / "fakeadb" / "adb")
FX = HERE / "fixtures" / "dumpsys"
PLAYER = "com.syncvr.player"
NOW = 1000.0


def fx(name):
    return (FX / f"{name}.txt").read_text()


def kinds(actions):
    return [a.kind for a in actions]


def entry(t=NOW - 10, sent=(), verified=None, **flags):
    return {"t": t, "flags": flags, "sent": list(sent), "suppressed": None, "verified": verified}


# ------------------------------------------------------------ decide(): pure tables

@pytest.mark.parametrize("obs, want", [
    ({"wakefulness": "Awake"}, []),
    ({"wakefulness": "Asleep"}, ["wake"]),
    ({"wakefulness": "Dozing"}, ["wake"]),
    ({"wakefulness": "Dreaming"}, ["wake"]),
    ({"wakefulness": "unknown"}, []),
    ({"wakefulness": None}, []),
    ({"wakefulness": "Asleep", "asleep": True}, []),  # put to sleep on purpose
    ({"unreachable": True}, []),
])
def test_stay_awake_decide(obs, want):
    assert kinds(StayAwake.decide([], dict(obs, now=NOW), {})) == want


def popup_obs(fixture, online=True, **extra):
    text = fx(fixture)
    return dict({"focus_title": parsers.focus_title(text), "focused_app": parsers.focused_app(text),
                 "online": online, "now": NOW}, **extra)


POPUP_CFG = dict(Popup.defaults)


@pytest.mark.parametrize("fixture, want", [
    ("window_anr", ["back"]),
    ("window_crash_dialog", ["back"]),
    ("window_other_dialog", []),  # a dialog, but not the player's: the evidence pattern does not match
    ("window", []),  # the player in front, no dialog
])
def test_popup_decide_dialogs(fixture, want):
    assert kinds(Popup.decide([], popup_obs(fixture), POPUP_CFG)) == want


def test_popup_evidence_is_configurable():
    obs = popup_obs("window_other_dialog")
    assert kinds(Popup.decide([], obs, dict(POPUP_CFG, evidence="com.other.app"))) == ["back"]
    assert Popup.decide([], obs, dict(POPUP_CFG, evidence="(")) == []  # invalid regex never matches


def test_popup_gives_up_after_three_failures_and_resumes_when_the_dialog_is_gone():
    def failed():
        return entry(sent=["back"], verified=False, dialog=True)
    obs = popup_obs("window_anr")
    assert kinds(Popup.decide([failed(), failed()], obs, POPUP_CFG)) == ["back"]
    assert Popup.decide([failed(), failed(), failed()], obs, POPUP_CFG) == []
    # a clean sample in between starts the count again
    assert kinds(Popup.decide([failed(), failed(), failed(), entry(dialog=False), failed()], obs, POPUP_CFG)) == ["back"]
    # a braked sample is not a failure
    braked = entry(dialog=True)
    assert kinds(Popup.decide([failed(), failed(), braked], obs, POPUP_CFG)) == ["back"]


@pytest.mark.parametrize("online, history, want", [
    (False, [], []),  # first sample: not yet
    (False, [entry(crash=True)], ["launch"]),  # second sample in a row
    (False, [entry(crash=True), entry(crash=True, t=NOW - 5, sent=["launch"])], []),  # launched 5 s ago
    (False, [entry(crash=True), entry(crash=True, t=NOW - 90, sent=["launch"])], ["launch"]),
    (True, [entry(crash=True)], []),  # the player is connected: not a crash
    (False, [entry(crash=False), entry(crash=True)], ["launch"]),
])
def test_popup_decide_crash(online, history, want):
    assert kinds(Popup.decide(history, popup_obs("window_home", online=online), POPUP_CFG)) == want


def test_popup_never_launches_when_the_app_is_in_front():
    assert Popup.decide([entry(crash=True)], popup_obs("window", online=False), POPUP_CFG) == []


OVERHEAT_CFG = dict(Overheat.defaults, pattern="OverheatPrompt|temperature critical")


def overheat_obs(log="logcat_overheat", window="window_overheat", **extra):
    return dict({"log_lines": fx(log).splitlines() if log else [], "window_lines": fx(window).splitlines() if window else [],
                 "now": NOW}, **extra)


def test_overheat_decide_needs_log_and_window():
    assert kinds(Overheat.decide([], overheat_obs(), OVERHEAT_CFG)) == ["back"]
    assert Overheat.decide([], overheat_obs(window=None), OVERHEAT_CFG) == []  # not in the window list
    assert Overheat.decide([], overheat_obs(log=None), OVERHEAT_CFG) == []  # nothing new in logcat
    assert Overheat.decide([], overheat_obs(window="window_home"), OVERHEAT_CFG) == []


def test_overheat_ignores_the_players_own_lines_in_both_sources():
    player_log = [ln for ln in fx("logcat_overheat").splitlines() if "SyncVR" in ln]
    assert player_log and Overheat.decide([], dict(overheat_obs(), log_lines=player_log), OVERHEAT_CFG) == []
    player_win = [ln for ln in fx("window_overheat").splitlines() if PLAYER in ln]
    assert any("OverheatBanner" in ln for ln in player_win)
    obs = dict(overheat_obs(), window_lines=player_win)
    assert Overheat.decide([], obs, dict(OVERHEAT_CFG, pattern="Overheat")) == []
    # the same text from the system is acted on
    assert kinds(Overheat.decide([], overheat_obs(), dict(OVERHEAT_CFG, pattern="Overheat"))) == ["back"]


def test_overheat_cooldown_and_no_pattern():
    assert Overheat.decide([entry(t=NOW - 30, sent=["back"])], overheat_obs(), OVERHEAT_CFG) == []
    assert kinds(Overheat.decide([entry(t=NOW - 90, sent=["back"])], overheat_obs(), OVERHEAT_CFG)) == ["back"]
    assert Overheat.decide([], overheat_obs(), dict(OVERHEAT_CFG, pattern="")) == []
    assert Overheat.decide([], dict(overheat_obs(), no_pattern=True), OVERHEAT_CFG) == []


BLACK_CFG = dict(BlackScreen.defaults)


def black_obs(display="OFF", wakefulness="Awake", app=f"{PLAYER}/.MainActivity", desired="playing", **extra):
    return dict({"display": display, "wakefulness": wakefulness, "focused_app": app, "desired": desired, "now": NOW},
                **extra)


SUSPECT = entry(suspect=True)


@pytest.mark.parametrize("obs, history, want", [
    (black_obs(), [SUSPECT, SUSPECT], ["wake"]),  # the third sample in a row
    (black_obs(), [SUSPECT], []),
    (black_obs(), [], []),
    (black_obs(), [SUSPECT, entry(suspect=False), SUSPECT], []),  # not consecutive
    (black_obs(), [SUSPECT, SUSPECT, entry(t=NOW - 60, sent=["wake"], suspect=True)], []),  # 120 s cooldown
    (black_obs(), [entry(t=NOW - 200, sent=["wake"]), SUSPECT, SUSPECT], ["wake"]),
    (black_obs(display="ON"), [SUSPECT, SUSPECT], []),  # display on: logged only
    (black_obs(wakefulness="Asleep"), [SUSPECT, SUSPECT], []),
    (black_obs(wakefulness="Dozing"), [SUSPECT, SUSPECT], []),
    (black_obs(app="com.oculus.vrshell/.MainActivity"), [SUSPECT, SUSPECT], []),  # app not in front
    (black_obs(app=None), [SUSPECT, SUSPECT], []),
    (black_obs(desired="paused"), [SUSPECT, SUSPECT], []),
    (black_obs(desired=None), [SUSPECT, SUSPECT], []),
    (black_obs(asleep=True), [SUSPECT, SUSPECT], []),  # intentionally asleep
    (black_obs(display=None), [SUSPECT, SUSPECT], []),  # unparseable: never guess
    (black_obs(skipped="not playing"), [SUSPECT, SUSPECT], []),
])
def test_black_screen_decide(obs, history, want):
    assert kinds([a for a in BlackScreen.decide(history, obs, BLACK_CFG) if a.kind != "log"]) == want


def test_black_screen_recovery_can_be_a_refresh_and_display_on_is_logged_only():
    assert kinds(BlackScreen.decide([SUSPECT, SUSPECT], black_obs(), dict(BLACK_CFG, recovery="refresh"))) == ["refresh"]
    acts = BlackScreen.decide([], black_obs(display="ON"), BLACK_CFG)
    assert kinds(acts) == ["log"] and acts[0].arg["picture"] == "unsampled"
    assert BlackScreen.decide([], black_obs(display="ON"), dict(BLACK_CFG, log_probe=False)) == []


@pytest.mark.parametrize("obs, history, want", [
    ({"ip": "10.9.9.9", "reachable": False}, [], ["connect", "wake"]),
    ({"ip": "10.9.9.9", "reachable": True}, [], []),
    ({"ip": "", "reachable": False}, [], []),  # no remembered address
    ({"ip": "10.9.9.9", "reachable": False}, [entry(t=NOW - 10, sent=["connect"])], []),  # retry every 30 s
    ({"ip": "10.9.9.9", "reachable": False}, [entry(t=NOW - 40, sent=["connect"])], ["connect", "wake"]),
    ({"ip": "10.9.9.9", "reachable": False, "asleep": True}, [], []),
])
def test_keepalive_decide(obs, history, want):
    assert kinds(Keepalive.decide(history, dict(obs, now=NOW), dict(Keepalive.defaults))) == want


# --------------------------------------------------------------- the framework

class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


@pytest.fixture
def adb_log(tmp_path, monkeypatch):
    log = tmp_path / "adb.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    return log


def adb_lines(log):
    return log.read_text().splitlines() if log.exists() else []


def sent(log, text):
    return [ln for ln in adb_lines(log) if text in ln]


def make_ctl(tmp_path, ips=(("192.168.1.20", "S1"),)):
    (tmp_path / "content").mkdir(exist_ok=True)
    lib = Library(tmp_path / "content")
    lib.scan()
    c = Controller(lib, fleet=AdbFleet(Adb(FAKE_ADB)))
    c.data_dir = tmp_path
    for i, (ip, serial) in enumerate(ips):
        d = c.headset_connected({"device_id": f"hs{i}", "serial": serial, "model": "Go"}, SimpleNamespace(
            remote_ip=ip, http_port=8080, send=lambda m: None, close=lambda: None))
        d.name = f"Go {i}"
    c.clock = Clock()
    c.watchdogs = Watchdogs(c, clock=c.clock)
    c.watchdogs.sleeper = lambda s: None
    return c


@pytest.fixture
def ctl(tmp_path, adb_log):
    return make_ctl(tmp_path)


def arm(ctl, name, cfg=None, **body):
    ctl.watchdogs.set(name, dict({"enabled": True, "armed": True, "testing": True, "cfg": cfg or {}}, **body))


def run(ctl, name, times=1, step=30.0):
    async def go():
        for _ in range(times):
            await ctl.watchdogs.cycle(name)
            ctl.clock.t += step
    asyncio.run(go())


def last_decision(ctl, name):
    return ctl.watchdogs.states[name].decisions[-1]


def test_observe_only_by_default_logs_but_sends_nothing(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    ctl.watchdogs.set("stay_awake", {"enabled": True})
    run(ctl, "stay_awake")
    assert not sent(adb_log, "keyevent")
    d = last_decision(ctl, "stay_awake")
    assert "observe-only" in d["text"] and d["sent"] == []
    assert ctl.watchdogs.states["stay_awake"].counters["observed_only"] == 1


def test_armed_stay_awake_wakes_only_a_sleeping_headset(ctl, adb_log, monkeypatch):
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake")
    assert not sent(adb_log, "keyevent")  # Awake: nothing to do
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    run(ctl, "stay_awake")
    assert len(sent(adb_log, "-s 192.168.1.20:5555 shell input keyevent 224")) == 1
    assert last_decision(ctl, "stay_awake")["sent"] == ["wake"]


def test_worn_headset_is_not_polled(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    ctl.devices["hs0"].status = {"worn": True}
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake")
    assert not sent(adb_log, "dumpsys power") and not sent(adb_log, "keyevent")


def test_intentionally_asleep_headsets_are_skipped(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    ctl.intentionally_asleep.add("hs0")
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake")
    assert not sent(adb_log, "-s 192.168.1.20:5555")
    assert ctl.watchdogs.states["stay_awake"].counters["observed"] == 0


def test_brake_suppresses_the_send_but_the_cycle_is_logged(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    arm(ctl, "stay_awake")
    ctl.set_show_mode(True)
    run(ctl, "stay_awake")
    assert not sent(adb_log, "keyevent")
    st = ctl.watchdogs.states["stay_awake"]
    d = last_decision(ctl, "stay_awake")
    assert d["suppressed"] == "show_mode" and "braked" in d["text"] and d["sent"] == []
    assert st.counters["suppressed"] == 1 and st.counters["cycles"] == 1 and st.last_cycle is not None
    assert "braked" in st.last_summary
    ctl.set_show_mode(False)
    run(ctl, "stay_awake")
    assert len(sent(adb_log, "keyevent 224")) == 1


def test_brake_is_rechecked_immediately_before_each_send(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    arm(ctl, "stay_awake")
    answers = iter([None, "show_mode", "show_mode"])  # clear when decided, braked by the time it would send
    ctl.brake = lambda: next(answers, "show_mode")
    run(ctl, "stay_awake")
    assert not sent(adb_log, "keyevent")
    assert last_decision(ctl, "stay_awake")["suppressed"] == "show_mode"


def test_armed_is_never_persisted(ctl, tmp_path):
    arm(ctl, "stay_awake", {"interval_s": 20})
    ctl.watchdogs.set("black_screen", {"enabled": True})
    assert ctl.watchdogs.states["stay_awake"].armed
    data = ctl.dump()
    assert "armed" not in str(data["watchdogs"])
    assert data["watchdogs"]["stay_awake"] == {"enabled": True, "cfg": dict(ctl.watchdogs.states["stay_awake"].cfg)}
    fresh = make_ctl(tmp_path)
    fresh.load(data)
    st = fresh.watchdogs.states["stay_awake"]
    assert st.enabled is True and st.armed is False and st.cfg["interval_s"] == 20.0
    assert fresh.watchdogs.states["black_screen"].enabled is True
    assert fresh.snapshot()["watchdogs"][0]["mode"] == "observe"


def test_load_ignores_garbage(ctl):
    ctl.watchdogs.load({"stay_awake": {"enabled": True, "cfg": {"interval_s": "x"}}, "nope": {}, "popup": 3})
    ctl.watchdogs.load("junk")
    assert ctl.watchdogs.states["stay_awake"].cfg["interval_s"] == 15.0


def test_an_error_in_a_cycle_does_not_end_the_loop(ctl, monkeypatch):
    calls = {"n": 0}
    real = ctl.fleet.listed

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("adb exploded")
        return real()
    monkeypatch.setattr(ctl.fleet, "listed", flaky)
    ctl.watchdogs.set("stay_awake", {"enabled": True})
    ctl.watchdogs.states["stay_awake"].cfg["interval_s"] = 0.02

    async def go():
        ctl.watchdogs.start(asyncio.get_running_loop())
        await asyncio.sleep(0.5)
        await ctl.watchdogs.stop()
    asyncio.run(go())
    st = ctl.watchdogs.states["stay_awake"]
    assert st.counters["errors"] == 2 and st.counters["cycles"] >= 4
    assert st.last_cycle is not None and "cycle failed" not in st.last_summary
    assert st.task is None


def test_a_failing_observation_is_recorded_per_headset_and_others_continue(tmp_path, adb_log, monkeypatch):
    ctl = make_ctl(tmp_path, ips=(("192.168.1.20", "S1"), ("", "USB123")))
    monkeypatch.setenv("FAKE_ADB_FAIL", "192.168.1.20:5555")
    monkeypatch.setenv("FAKE_ADB_WAKEFULNESS", "Asleep")
    arm(ctl, "stay_awake")
    run(ctl, "stay_awake")
    st = ctl.watchdogs.states["stay_awake"]
    assert st.counters["errors"] == 1 and st.counters["observed"] == 1
    assert len(sent(adb_log, "-s USB123 shell input keyevent 224")) == 1
    assert any(e["level"] == "warn" and "Go 0" in e["message"] for e in ctl.events)


def test_poll_budget_limits_concurrent_adb_across_watchdogs(tmp_path, adb_log, monkeypatch):
    ips = tuple((f"192.168.1.{20 + i}", f"S{i}") for i in range(6))
    ctl = make_ctl(tmp_path, ips=ips)
    ctl.watchdogs = Watchdogs(ctl, clock=ctl.clock, budget=2)
    lock, state = threading.Lock(), {"now": 0, "max": 0}

    def slow(result):
        def fn(*a, **kw):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return result
        return fn
    monkeypatch.setattr(ctl.fleet, "wakefulness", slow("Awake"))
    monkeypatch.setattr(ctl.fleet, "shell", slow(""))
    for name in ("stay_awake", "popup"):
        ctl.watchdogs.set(name, {"enabled": True})

    async def go():
        await asyncio.gather(ctl.watchdogs.cycle("stay_awake"), ctl.watchdogs.cycle("popup"))
    asyncio.run(go())
    assert state["max"] == 2
    assert ctl.watchdogs.states["stay_awake"].counters["observed"] == 6
    assert ctl.watchdogs.states["popup"].counters["observed"] == 6


# ------------------------------------------------------------------ popup

def test_popup_sends_back_verifies_and_gives_up_after_three_failures(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_anr")
    arm(ctl, "popup")
    run(ctl, "popup", times=5, step=10)
    assert len(sent(adb_log, "-s 192.168.1.20:5555 shell input keyevent 4")) == 3
    st = ctl.watchdogs.states["popup"]
    assert st.counters["sent"] == 3
    verified = [d["verified"] for d in st.decisions if d.get("sent")]
    assert verified == [False, False, False]  # the fixture dialog never goes away


def test_popup_stops_after_the_dialog_is_dismissed(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_anr")
    arm(ctl, "popup")
    # a fake window that clears once BACK was sent
    real = ctl.fleet.back

    def back(serial):
        monkeypatch.setenv("FAKE_ADB_WINDOW", "window")
        return real(serial)
    monkeypatch.setattr(ctl.fleet, "back", back)
    run(ctl, "popup", times=3)
    assert len(sent(adb_log, "keyevent 4")) == 1
    assert [d["verified"] for d in ctl.watchdogs.states["popup"].decisions if d.get("sent")] == [True]


def test_popup_ignores_a_foreign_dialog(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_other_dialog")
    arm(ctl, "popup")
    run(ctl, "popup", times=2)
    assert not sent(adb_log, "keyevent")


def test_crash_launches_the_player_once_per_cooldown(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_home")
    ctl.devices["hs0"].online = False
    arm(ctl, "popup")
    run(ctl, "popup", times=1, step=10)
    assert not sent(adb_log, "monkey")
    run(ctl, "popup", times=1, step=10)
    assert len(sent(adb_log, "monkey -p com.syncvr.player")) == 1
    run(ctl, "popup", times=1, step=10)
    assert len(sent(adb_log, "monkey")) == 1  # 60 s cooldown
    assert not sent(adb_log, "keyevent")  # never HOME or BACK for a crash
    ctl.clock.t += 100
    run(ctl, "popup")
    assert len(sent(adb_log, "monkey")) == 2


def test_connected_player_in_the_background_is_not_launched(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_home")
    arm(ctl, "popup")
    run(ctl, "popup", times=4)
    assert not sent(adb_log, "monkey")


# --------------------------------------------------------------- overheat

def snapshot_folder(tmp_path, window="window_overheat", logcat="logcat_overheat", name="snap1"):
    folder = tmp_path / "snapshots" / name
    folder.mkdir(parents=True)
    shutil.copy(FX / f"{logcat}.txt", folder / "logcat.txt")
    shutil.copy(FX / f"{window}.txt", folder / "window.txt")
    return folder


PATTERN = "OverheatPrompt|temperature critical"


def test_overheat_cannot_be_armed_without_a_confirmed_pattern(ctl):
    wd = ctl.watchdogs
    with pytest.raises(WatchdogError, match="no pattern"):
        wd.set("overheat", {"enabled": True, "armed": True, "testing": True})
    with pytest.raises(WatchdogError, match="not been tested"):
        wd.set("overheat", {"armed": True, "testing": True, "cfg": {"pattern": PATTERN}})
    assert not wd.states["overheat"].armed and wd.states["overheat"].cfg["pattern"] == ""  # nothing half applied
    with pytest.raises(WatchdogError, match="valid regular"):
        wd.set("overheat", {"cfg": {"pattern": "("}})
    # a hash typed in by hand does not count
    with pytest.raises(WatchdogError, match="only by testing"):
        wd.set("overheat", {"cfg": {"pattern": PATTERN, "pattern_confirmed": pattern_hash(PATTERN)}})
    with pytest.raises(CommandError):
        ctl.set_watchdog("overheat", {"armed": True, "testing": True})
    assert wd.to_json_one("overheat")["arm_blocker"] == "no pattern is set"


def test_test_pattern_shows_matches_without_the_players_lines(tmp_path):
    folder = snapshot_folder(tmp_path)
    res = try_pattern(PATTERN, folder)
    assert res["error"] is None and res["files"] == ["logcat.txt", "window.txt"] and res["hash"] == pattern_hash(PATTERN)
    assert res["total"] == len(res["matches"]) >= 3
    assert not any("SyncVR" in m["line"] or PLAYER in m["line"] for m in res["matches"])
    assert {m["file"] for m in res["matches"]} == {"logcat.txt", "window.txt"}
    assert all(m["line_no"] >= 1 for m in res["matches"])
    assert try_pattern("NoSuchThing", folder)["total"] == 0
    assert "empty" in try_pattern("", folder)["error"]
    assert "regular" in try_pattern("(", folder)["error"]
    assert "not a folder" in try_pattern(PATTERN, tmp_path / "nope")["error"]
    assert "snapshot folder" in try_pattern(PATTERN, tmp_path)["error"]


def test_confirming_a_pattern_unlocks_arming_and_editing_it_locks_again(ctl, tmp_path):
    folder = snapshot_folder(tmp_path)
    res = ctl.watchdog_test_pattern("overheat", {"folder": str(folder), "pattern": PATTERN})
    assert res["total"] >= 3 and res["confirmed"] is False
    assert ctl.watchdogs.states["overheat"].cfg["pattern"] == ""  # testing alone changes nothing
    res = ctl.watchdog_test_pattern("overheat", {"folder": folder.name, "pattern": PATTERN, "confirm": True})
    assert res["confirmed"] is True
    st = ctl.watchdogs.states["overheat"]
    assert st.cfg["pattern"] == PATTERN and st.cfg["pattern_confirmed"] == pattern_hash(PATTERN)
    ctl.set_watchdog("overheat", {"armed": True, "testing": True})
    assert st.armed and st.enabled
    ctl.set_watchdog("overheat", {"cfg": {"pattern": "Overheat"}})  # a different pattern: disarmed again
    assert not st.armed and st.enabled
    with pytest.raises(CommandError, match="not been tested"):
        ctl.set_watchdog("overheat", {"armed": True, "testing": True})
    assert any("confirmed" in e["message"] for e in ctl.events)


def test_confirm_refuses_bad_patterns_and_outside_folders(ctl, tmp_path):
    folder = snapshot_folder(tmp_path)
    for pattern in ("NoSuchThing", "(", ""):
        with pytest.raises(CommandError, match="cannot confirm"):
            ctl.watchdog_test_pattern("overheat", {"folder": str(folder), "pattern": pattern, "confirm": True})
    assert ctl.watchdogs.states["overheat"].cfg["pattern_confirmed"] == ""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for bad in (str(outside), "../elsewhere", "/etc", None, ""):
        with pytest.raises(CommandError):
            ctl.watchdog_test_pattern("overheat", {"folder": bad, "pattern": PATTERN})
    with pytest.raises(CommandError, match="no pattern"):
        ctl.watchdog_test_pattern("popup", {"folder": str(folder)})


def confirmed_overheat(ctl, tmp_path, pattern=PATTERN):
    folder = snapshot_folder(tmp_path)
    ctl.watchdog_test_pattern("overheat", {"folder": str(folder), "pattern": pattern, "confirm": True})
    ctl.set_watchdog("overheat", {"armed": True, "testing": True})


def test_overheat_cycle_reads_new_log_lines_only(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_overheat")
    confirmed_overheat(ctl, tmp_path)
    run(ctl, "overheat")  # first cycle only sets the cursor: old lines are not new
    assert not sent(adb_log, "keyevent")
    st = ctl.watchdogs.states["overheat"]
    assert st.cursors["hs0"] == "10-07 12:00:03.400"
    assert any("logcat -d -T" in ln and "SyncVR" in ln for ln in adb_lines(adb_log))  # filtered on the headset
    st.cursors["hs0"] = "10-07 12:00:00.000"  # pretend the prompt lines were written since the last cycle
    run(ctl, "overheat")
    assert len(sent(adb_log, "-s 192.168.1.20:5555 shell input keyevent 4")) == 1
    run(ctl, "overheat")  # nothing new now
    assert len(sent(adb_log, "keyevent 4")) == 1


def test_overheat_without_the_prompt_in_the_window_list_sends_nothing(ctl, tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_LOGCAT", str(FX / "logcat_overheat.txt"))
    monkeypatch.setenv("FAKE_ADB_WINDOW", "window_home")
    confirmed_overheat(ctl, tmp_path)
    ctl.watchdogs.states["overheat"].cursors["hs0"] = "10-07 12:00:00.000"
    run(ctl, "overheat")
    assert not sent(adb_log, "keyevent")


# ------------------------------------------------------------ black screen

def test_black_screen_acts_on_the_third_sample_then_cools_down(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_DISPLAY", "display_off")
    ctl.devices["hs0"].desired = {"mode": "playing"}
    arm(ctl, "black_screen")
    run(ctl, "black_screen", times=2)
    assert not sent(adb_log, "keyevent")
    run(ctl, "black_screen", times=1)
    assert len(sent(adb_log, "-s 192.168.1.20:5555 shell input keyevent 224")) == 1
    run(ctl, "black_screen", times=3, step=30)  # still black, but 120 s cooldown (3 x 30 s)
    assert len(sent(adb_log, "keyevent 224")) == 1
    run(ctl, "black_screen", times=1)
    assert len(sent(adb_log, "keyevent 224")) == 2


def test_black_screen_does_not_act_unless_playing_or_when_asleep_on_purpose(ctl, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_DISPLAY", "display_off")
    arm(ctl, "black_screen")
    run(ctl, "black_screen", times=5)  # desired mode not playing
    assert not adb_lines(adb_log) or not sent(adb_log, "dumpsys display")  # telemetry answered: no probe at all
    ctl.devices["hs0"].desired = {"mode": "playing"}
    ctl.intentionally_asleep.add("hs0")
    run(ctl, "black_screen", times=5)
    assert not sent(adb_log, "keyevent")


def test_display_on_while_playing_is_only_written_to_the_probe_csv(ctl, tmp_path, adb_log):
    ctl.devices["hs0"].desired = {"mode": "playing"}
    arm(ctl, "black_screen")
    run(ctl, "black_screen", times=4)
    assert not sent(adb_log, "keyevent")
    files = list((tmp_path / "probe").glob("*.csv"))
    assert len(files) == 1
    rows = files[0].read_text().splitlines()
    assert rows[0].startswith("time,device,label,display") and len(rows) == 5
    assert "hs0" in rows[1] and "ON" in rows[1] and "unsampled" in rows[1]


def test_probe_csv_is_written_even_when_observe_only_or_braked(ctl, tmp_path):
    ctl.devices["hs0"].desired = {"mode": "playing"}
    ctl.watchdogs.set("black_screen", {"enabled": True})
    ctl.set_show_mode(True)
    run(ctl, "black_screen", times=2)
    assert len((next((tmp_path / "probe").glob("*.csv")).read_text().splitlines())) == 3


# --------------------------------------------------------------- keepalive

def test_keepalive_connects_a_missing_headset_then_wakes_it(tmp_path, adb_log):
    ctl = make_ctl(tmp_path, ips=(("192.168.1.20", "S1"), ("10.9.9.9", "S3")))
    arm(ctl, "keepalive")
    run(ctl, "keepalive", step=0)
    lines = adb_lines(adb_log)
    assert "connect 10.9.9.9:5555" in lines
    assert not sent(adb_log, "connect 192.168.1.20")  # it is in adb devices
    assert lines.index("connect 10.9.9.9:5555") < lines.index("-s 10.9.9.9:5555 shell input keyevent 224")
    run(ctl, "keepalive", times=1, step=10)  # inside the 30 s retry window
    assert len(sent(adb_log, "connect 10.9.9.9")) == 1


def test_keepalive_failed_connect_skips_the_wake(tmp_path, adb_log, monkeypatch):
    monkeypatch.setenv("FAKE_ADB_CONNECT_FAIL", "10.9.9.9:5555")
    ctl = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"),))
    arm(ctl, "keepalive")
    run(ctl, "keepalive")
    assert sent(adb_log, "connect 10.9.9.9") and not sent(adb_log, "keyevent")
    assert ctl.watchdogs.states["keepalive"].counters["errors"] == 1
    assert any("failed to connect" in e["message"] for e in ctl.events)


def test_keepalive_is_blocked_by_brakes_and_observe_only(tmp_path, adb_log):
    ctl = make_ctl(tmp_path, ips=(("10.9.9.9", "S3"),))
    ctl.watchdogs.set("keepalive", {"enabled": True})
    run(ctl, "keepalive")
    assert not sent(adb_log, "connect")
    assert "would connect" in ctl.watchdogs.states["keepalive"].decisions[0]["text"]
    arm(ctl, "keepalive")
    ctl.set_show_mode(True)
    run(ctl, "keepalive")
    assert not sent(adb_log, "connect") and not sent(adb_log, "keyevent")
    assert ctl.watchdogs.states["keepalive"].decisions[-1]["suppressed"] == "show_mode"


# ------------------------------------------------------- gating and the web API

def test_arming_is_gated_by_the_feature_unless_testing(ctl):
    wd = ctl.watchdogs
    with pytest.raises(WatchdogError, match="not marked as tested"):
        wd.set("stay_awake", {"enabled": True, "armed": True})
    assert not wd.states["stay_awake"].armed and not wd.states["stay_awake"].enabled
    ctl.set_feature_tested("watchdog.stay_awake", True)
    wd.set("stay_awake", {"enabled": True, "armed": True})
    assert wd.states["stay_awake"].armed
    wd.set("stay_awake", {"enabled": False})  # disabling disarms
    assert not wd.states["stay_awake"].armed
    with pytest.raises(WatchdogError, match="true or false"):
        wd.set("stay_awake", {"armed": "yes"})


def test_set_validates_before_changing_anything(ctl):
    wd = ctl.watchdogs
    for bad in ({"cfg": {"nope": 1}}, {"cfg": {"interval_s": 0}}, {"cfg": {"interval_s": "5"}},
                {"cfg": {"interval_s": True}}, {"cfg": [1]}, {"cfg": {"interval_s": 99999}}):
        with pytest.raises(WatchdogError):
            wd.set("stay_awake", dict(bad, enabled=True))
    with pytest.raises(WatchdogError, match="unknown watchdog"):
        wd.set("nope", {})
    with pytest.raises(WatchdogError, match="recovery"):
        wd.set("black_screen", {"cfg": {"recovery": "reboot"}})
    assert not wd.states["stay_awake"].enabled and wd.states["stay_awake"].cfg["interval_s"] == 15.0


def test_snapshot_exposes_the_watchdogs(ctl):
    arm(ctl, "stay_awake")
    rows = {w["name"]: w for w in ctl.snapshot()["watchdogs"]}
    assert list(rows) == ["stay_awake", "popup", "overheat", "black_screen", "keepalive"]
    sa = rows["stay_awake"]
    assert sa["mode"] == "armed" and sa["armed"] and sa["enabled"] and sa["rule"] and sa["feature"] == "watchdog.stay_awake"
    assert rows["popup"]["mode"] == "off" and rows["popup"]["tested"] is False
    assert rows["overheat"]["arm_blocker"]


def test_web_route(tmp_path, adb_log):
    from aiohttp.test_utils import TestClient, TestServer
    from syncvr.web import WebApp
    c = make_ctl(tmp_path)
    folder = snapshot_folder(tmp_path)

    async def go():
        async with TestClient(TestServer(WebApp(c).app)) as client:
            r = await client.post("/api/watchdogs/stay_awake", json={"enabled": True})
            assert r.status == 200 and (await r.json())["watchdog"]["mode"] == "observe"
            r = await client.post("/api/watchdogs/stay_awake", json={"armed": True})
            assert r.status == 400 and "tested" in (await r.json())["error"]
            r = await client.post("/api/watchdogs/stay_awake", json={"armed": True, "testing": True})
            assert r.status == 200 and (await r.json())["watchdog"]["armed"] is True
            state = await (await client.get("/api/state")).json()
            assert state["watchdogs"][0]["armed"] is True
            assert (await client.post("/api/watchdogs/nope", json={})).status == 400
            assert (await client.post("/api/watchdogs/popup", data="x")).status == 400
            r = await client.post("/api/watchdogs/overheat", json={"armed": True, "testing": True})
            assert r.status == 400 and "pattern" in (await r.json())["error"]
            r = await client.post("/api/watchdogs/overheat/test_pattern",
                                  json={"folder": str(folder), "pattern": PATTERN, "confirm": True})
            body = await r.json()
            assert r.status == 200 and body["confirmed"] and body["total"] >= 3
            r = await client.post("/api/watchdogs/overheat", json={"armed": True, "testing": True})
            assert r.status == 200
            r = await client.post("/api/watchdogs/overheat/test_pattern", json={"folder": "/etc", "pattern": "x"})
            assert r.status == 400
    asyncio.run(go())
    assert c.dirty


def test_start_and_stop_manage_one_task_per_watchdog(ctl):
    async def go():
        ctl.watchdogs.start(asyncio.get_running_loop())
        tasks = [st.task for st in ctl.watchdogs.states.values()]
        assert len(tasks) == 5 and all(t is not None and not t.done() for t in tasks)
        await ctl.watchdogs.stop()
        assert all(t.done() for t in tasks)
    asyncio.run(go())
